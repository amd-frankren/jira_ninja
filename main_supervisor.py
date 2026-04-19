#!/usr/bin/env python3
"""
Process supervisor for main.py.

Features:
- Start main.py as a child process
- Monitor process state continuously
- Auto-restart when child exits unexpectedly
- Graceful shutdown on Ctrl+C / SIGTERM
- Optional restart delay and max restart limit
- Periodic SCET reconciliation (default every 8 hours):
  - Fetch Jira SCET total count + all SCET keys
  - Fetch SharePoint folder SCET file count + SCET keys (non-recursive)
  - Compare and write missing SCET list (SharePoint missing vs Jira)

Usage:
    python main_supervisor.py
    python main_supervisor.py --check-interval 2 --restart-delay 3
    python main_supervisor.py --max-restarts 10
    python main_supervisor.py --disable-main-py-start
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib import error, parse, request

from jira_export_external_scet import export_issue_to_file
from json_minify import minify_one_file
from sharepoint_upload_files import (
    graph_request,
    get_access_token,
    resolve_cached_site_drive_ids,
)

# Default args passed to main.py (can be adjusted here directly)
# DEFAULT_MAIN_ARGS: List[str] = [
#     "--interval", "60",
#     "--since-minutes", "0",
#     "--disable-sharepoint-upload",
#     "--debug-skip-target-scp-check",
#     "--debug-treat-updated-as-created",
# ]

DEFAULT_MAIN_ARGS: List[str] = [
    "--interval", "60",
    "--since-minutes", "60",
    "--disable-sharepoint-upload",
]

# Reconcile constants
JIRA_PROJECT_KEY = "SCET"
JIRA_SEARCH_API = "/rest/api/2/search"
ENV_EXTERNAL_JIRA_URL = "EXTERNAL_JIRA_URL"
ENV_EXTERNAL_JIRA_TOKEN = "EXTERNAL_JIRA_TOKEN"
RECONCILE_REMOTE_FOLDER = "SCETS/SCET_export_minified"

LOG_DIR = Path("log")
JIRA_SCET_COUNT_FILE = LOG_DIR / "jira_scet_ticket_count.txt"
JIRA_SCET_LIST_FILE = LOG_DIR / "jira_scet_ticket_numbers.txt"
SHAREPOINT_SCET_COUNT_FILE = LOG_DIR / "sharepoint_scet_file_count.txt"
SHAREPOINT_SCET_LIST_FILE = LOG_DIR / "sharepoint_scet_numbers.txt"
MISSING_SCET_LIST_FILE = LOG_DIR / "sharepoint_missing_scet_numbers.txt"
SHAREPOINT_DELTA_STATE_FILE = LOG_DIR / "sharepoint_scet_delta_state.json"
MISSING_SCET_SYNC_LOG_FILE = LOG_DIR / "sharepoint_missing_scet_upload_results.txt"
MISSING_SCET_EXPORT_DIR = Path("SCET_export_runtime")


def _extract_scet_number(text: str) -> Optional[str]:
    m = re.search(r"\bSCET-\d+\b", text.upper())
    return m.group(0) if m else None


def _scet_sort_key(scet: str) -> Tuple[int, str]:
    m = re.search(r"(\d+)$", scet)
    if m:
        return int(m.group(1)), scet
    return (10**18, scet)


def _write_list_file(path: Path, values: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(values) + ("\n" if values else "")
    path.write_text(content, encoding="utf-8")


def _write_count_file(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{count}\n", encoding="utf-8")


def _load_sharepoint_delta_state(path: Path, remote_folder: str) -> Dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "remote_folder": remote_folder,
            "delta_link": None,
            "files_by_id": {},
        }

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "version": 1,
            "remote_folder": remote_folder,
            "delta_link": None,
            "files_by_id": {},
        }

    if not isinstance(raw, dict):
        return {
            "version": 1,
            "remote_folder": remote_folder,
            "delta_link": None,
            "files_by_id": {},
        }

    if str(raw.get("remote_folder", "")).strip().strip("/") != remote_folder:
        return {
            "version": 1,
            "remote_folder": remote_folder,
            "delta_link": None,
            "files_by_id": {},
        }

    files_by_id_raw = raw.get("files_by_id", {})
    files_by_id: Dict[str, str] = {}
    if isinstance(files_by_id_raw, dict):
        for k, v in files_by_id_raw.items():
            file_id = str(k).strip()
            file_name = str(v).strip()
            if file_id and file_name:
                files_by_id[file_id] = file_name

    delta_link = raw.get("delta_link")
    if not isinstance(delta_link, str) or not delta_link.strip():
        delta_link = None

    return {
        "version": 1,
        "remote_folder": remote_folder,
        "delta_link": delta_link,
        "files_by_id": files_by_id,
    }


def _save_sharepoint_delta_state(path: Path, remote_folder: str, delta_link: str, files_by_id: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "remote_folder": remote_folder,
        "delta_link": delta_link,
        "files_by_id": files_by_id,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_sharepoint_delta_items(
    items: List[Any],
    files_by_id: Dict[str, str],
    expected_parent_path: str,
) -> Tuple[int, int]:
    added_or_updated = 0
    removed = 0
    expected = expected_parent_path.rstrip("/").lower()

    for item in items:
        if not isinstance(item, dict):
            continue

        item_id = str(item.get("id", "")).strip()
        if not item_id:
            continue

        deleted = "deleted" in item
        parent = item.get("parentReference", {})
        parent_path = ""
        if isinstance(parent, dict):
            parent_path = str(parent.get("path", "")).strip()
        is_direct_child = parent_path.rstrip("/").lower() == expected

        is_file = "file" in item
        name = str(item.get("name", "")).strip()

        if deleted:
            if item_id in files_by_id:
                del files_by_id[item_id]
                removed += 1
            continue

        if is_file and is_direct_child and name:
            old_name = files_by_id.get(item_id)
            files_by_id[item_id] = name
            if old_name != name:
                added_or_updated += 1
            continue

        if item_id in files_by_id:
            del files_by_id[item_id]
            removed += 1

    return added_or_updated, removed


def _jira_get_json(base_url: str, token: str, params: Dict[str, Any]) -> Dict[str, Any]:
    query = parse.urlencode(params)
    url = f"{base_url.rstrip('/')}{JIRA_SEARCH_API}?{query}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    req = request.Request(url=url, method="GET", headers=headers)
    try:
        with request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except error.HTTPError as http_err:
        detail = http_err.read().decode("utf-8", errors="ignore")
        raise RuntimeError(
            f"Jira API error {http_err.code} {http_err.reason} at {url}\n{detail}"
        ) from http_err
    except error.URLError as url_err:
        raise RuntimeError(f"Network error when calling Jira API at {url}: {url_err}") from url_err
    except json.JSONDecodeError as json_err:
        raise RuntimeError(f"Invalid JSON response from Jira API at {url}") from json_err


def _fetch_all_jira_scet_numbers(
    log_fn,
    should_stop: Optional[Callable[[], bool]] = None,
) -> List[str]:
    base_url = os.getenv(ENV_EXTERNAL_JIRA_URL, "").strip()
    if not base_url:
        raise RuntimeError(f"Missing required env var: {ENV_EXTERNAL_JIRA_URL}")

    token = os.getenv(ENV_EXTERNAL_JIRA_TOKEN, "").strip()
    if not token:
        raise RuntimeError(f"Missing required env var: {ENV_EXTERNAL_JIRA_TOKEN}")

    max_results = 100
    start_at = 0
    page = 0
    total: Optional[int] = None
    scet_numbers: Set[str] = set()

    while True:
        if should_stop and should_stop():
            log_fn("[reconcile][jira] interrupted by stop signal, abort paging.")
            raise RuntimeError("Reconcile interrupted by stop signal")
        page += 1
        params = {
            "jql": f"project = {JIRA_PROJECT_KEY} ORDER BY key ASC",
            "fields": "key",
            "maxResults": str(max_results),
            "startAt": str(start_at),
        }

        data = _jira_get_json(base_url=base_url, token=token, params=params)
        issues = data.get("issues", [])
        if not isinstance(issues, list):
            raise RuntimeError("Unexpected Jira response: issues is not a list")

        if total is None:
            try:
                total = int(data.get("total", 0))
            except Exception:
                total = 0
            log_fn(f"[reconcile][jira] total tickets reported by API: {total}")

        for issue in issues:
            if not isinstance(issue, dict):
                continue
            key = str(issue.get("key", "")).strip().upper()
            scet = _extract_scet_number(key)
            if scet:
                scet_numbers.add(scet)

        fetched = start_at + len(issues)
        pct = (fetched / total * 100.0) if total and total > 0 else 100.0
        log_fn(
            f"[reconcile][jira] page={page}, startAt={start_at}, "
            f"fetched_this_page={len(issues)}, fetched_total={fetched}/{total or 0} ({pct:.2f}%)"
        )

        if len(issues) < max_results:
            break

        start_at += max_results

    result = sorted(scet_numbers, key=_scet_sort_key)
    log_fn(f"[reconcile][jira] final unique SCET count: {len(result)}")
    return result


def _upload_local_file_to_sharepoint_reconcile_folder(
    local_file: Path,
    token: str,
    drive_id: str,
) -> Dict[str, Any]:
    if not local_file.exists() or not local_file.is_file():
        raise RuntimeError(f"Local file does not exist or is not a file: {local_file}")

    remote_folder = RECONCILE_REMOTE_FOLDER.strip().strip("/")
    remote_item_path = f"{remote_folder}/{local_file.name}" if remote_folder else local_file.name
    encoded_path = parse.quote(remote_item_path, safe="/")
    endpoint = f"/drives/{drive_id}/root:/{encoded_path}:/content"

    body = local_file.read_bytes()
    _, data = graph_request(
        "PUT",
        endpoint,
        token,
        body=body,
        content_type="application/octet-stream",
    )
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected upload response from SharePoint Graph API.")
    return data


def _sync_missing_scet_to_sharepoint(
    missing_on_sharepoint: List[str],
    log_fn,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    if not missing_on_sharepoint:
        log_fn("[reconcile][sync] no missing SCET on SharePoint, skip sync.")
        return

    token = get_access_token()
    _site_id, drive_id = resolve_cached_site_drive_ids(token, force_refresh=False)

    MISSING_SCET_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    success_count = 0
    failed_count = 0
    lines: List[str] = []
    run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"=== missing SCET sync run @ {run_ts} ===")
    lines.append(f"missing_count={len(missing_on_sharepoint)}")

    for idx, scet_key in enumerate(missing_on_sharepoint, start=1):
        if should_stop and should_stop():
            raise RuntimeError("Missing SCET sync interrupted by stop signal")

        step_begin = time.perf_counter()
        issue_key = scet_key.strip().upper()
        if not issue_key:
            continue

        try:
            exported_file = export_issue_to_file(
                issue_key=issue_key,
                output_dir=str(MISSING_SCET_EXPORT_DIR),
            )

            src_size, minified_size = minify_one_file(exported_file, exported_file)
            upload_result = _upload_local_file_to_sharepoint_reconcile_folder(
                local_file=Path(exported_file),
                token=token,
                drive_id=drive_id,
            )
            elapsed = time.perf_counter() - step_begin

            item_id = str(upload_result.get("id", "")).strip()
            web_url = str(upload_result.get("webUrl", "")).strip()
            success_count += 1

            log_fn(
                f"[reconcile][sync] ({idx}/{len(missing_on_sharepoint)}) uploaded {issue_key} "
                f"ok | minify={src_size}->{minified_size} bytes | elapsed={elapsed:.2f}s"
            )
            lines.append(
                f"[SUCCESS] issue={issue_key} file={exported_file} "
                f"minify={src_size}->{minified_size} "
                f"item_id={item_id or '<none>'} url={web_url or '<none>'}"
            )
        except Exception as exc:
            failed_count += 1
            log_fn(
                f"[reconcile][sync][error] ({idx}/{len(missing_on_sharepoint)}) "
                f"issue={issue_key} failed: {exc}"
            )
            lines.append(f"[FAILED] issue={issue_key} reason={exc}")

    lines.append(f"result: success={success_count}, failed={failed_count}")
    lines.append("")

    MISSING_SCET_SYNC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with MISSING_SCET_SYNC_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    log_fn(
        f"[reconcile][sync] missing SCET sync done: success={success_count}, failed={failed_count}, "
        f"log={MISSING_SCET_SYNC_LOG_FILE}"
    )


def _fetch_sharepoint_scet_numbers_non_recursive(
    log_fn,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Tuple[List[str], int]:
    token = get_access_token()
    _site_id, drive_id = resolve_cached_site_drive_ids(token, force_refresh=False)

    normalized_folder = RECONCILE_REMOTE_FOLDER.strip().strip("/")
    if normalized_folder:
        encoded_folder = parse.quote(normalized_folder, safe="/")
        full_sync_endpoint = (
            f"/drives/{drive_id}/root:/{encoded_folder}:/delta"
            f"?$select=id,name,file,folder,parentReference,deleted"
        )
        expected_parent_path = f"/drives/{drive_id}/root:/{normalized_folder}"
    else:
        full_sync_endpoint = (
            f"/drives/{drive_id}/root/delta"
            f"?$select=id,name,file,folder,parentReference,deleted"
        )
        expected_parent_path = f"/drives/{drive_id}/root:"

    state = _load_sharepoint_delta_state(
        SHAREPOINT_DELTA_STATE_FILE,
        remote_folder=normalized_folder,
    )
    files_by_id: Dict[str, str] = dict(state.get("files_by_id", {}))
    cached_delta_link = state.get("delta_link")

    def _run_delta_once(start_endpoint: str) -> Tuple[str, int, int, int]:
        page = 0
        added_or_updated_total = 0
        removed_total = 0
        scanned_items_total = 0
        next_endpoint: Optional[str] = start_endpoint
        final_delta_link: Optional[str] = None

        while next_endpoint:
            if should_stop and should_stop():
                log_fn("[reconcile][sharepoint] interrupted by stop signal, abort paging.")
                raise RuntimeError("Reconcile interrupted by stop signal")

            page += 1
            _, data = graph_request("GET", next_endpoint, token)
            if not isinstance(data, dict):
                raise RuntimeError("Unexpected SharePoint response when calling delta API.")

            items = data.get("value", [])
            if not isinstance(items, list):
                raise RuntimeError("Unexpected SharePoint response: value is not a list.")

            scanned_items_total += len(items)
            added_or_updated, removed = _apply_sharepoint_delta_items(
                items=items,
                files_by_id=files_by_id,
                expected_parent_path=expected_parent_path,
            )
            added_or_updated_total += added_or_updated
            removed_total += removed

            log_fn(
                f"[reconcile][sharepoint][delta] page={page}, "
                f"delta_items={len(items)}, added_or_updated={added_or_updated}, removed={removed}, "
                f"tracked_files={len(files_by_id)}"
            )

            next_link = data.get("@odata.nextLink")
            delta_link = data.get("@odata.deltaLink")

            if isinstance(next_link, str) and next_link.strip():
                next_endpoint = next_link.strip()
                log_fn("[reconcile][sharepoint][delta] next page detected, continue fetching...")
            else:
                next_endpoint = None

            if isinstance(delta_link, str) and delta_link.strip():
                final_delta_link = delta_link.strip()

        if not final_delta_link:
            raise RuntimeError("SharePoint delta API did not return @odata.deltaLink")

        return final_delta_link, page, scanned_items_total, (added_or_updated_total + removed_total)

    using_incremental = isinstance(cached_delta_link, str) and bool(cached_delta_link.strip())

    if using_incremental:
        log_fn("[reconcile][sharepoint][delta] using cached deltaLink (incremental sync).")
        try:
            delta_link, pages, scanned_items, change_count = _run_delta_once(cached_delta_link.strip())
        except Exception as exc:
            log_fn(
                f"[reconcile][sharepoint][delta][warn] incremental sync failed, "
                f"fallback to full delta sync: {exc}"
            )
            files_by_id = {}
            delta_link, pages, scanned_items, change_count = _run_delta_once(full_sync_endpoint)
    else:
        log_fn("[reconcile][sharepoint][delta] no cached deltaLink, running initial full delta sync.")
        files_by_id = {}
        delta_link, pages, scanned_items, change_count = _run_delta_once(full_sync_endpoint)

    _save_sharepoint_delta_state(
        SHAREPOINT_DELTA_STATE_FILE,
        remote_folder=normalized_folder,
        delta_link=delta_link,
        files_by_id=files_by_id,
    )

    names = sorted(files_by_id.values())
    scet_numbers: Set[str] = set()
    for name in names:
        scet = _extract_scet_number(name)
        if scet:
            scet_numbers.add(scet)

    result = sorted(scet_numbers, key=_scet_sort_key)
    log_fn(
        f"[reconcile][sharepoint] delta sync done: pages={pages}, scanned_delta_items={scanned_items}, "
        f"applied_changes={change_count}, tracked_files={len(files_by_id)}, "
        f"unique_scet={len(result)}, state_file={SHAREPOINT_DELTA_STATE_FILE}"
    )
    return result, len(files_by_id)


class MainSupervisor:
    def __init__(
        self,
        target_script: Path,
        check_interval: float = 30.0,
        restart_delay: float = 3.0,
        max_restarts: int = 0,
        script_args: Optional[List[str]] = None,
        reconcile_interval_hours: float = 8.0,
        start_main_py: bool = True,
    ) -> None:
        self.target_script = target_script
        self.check_interval = check_interval
        self.restart_delay = restart_delay
        self.max_restarts = max_restarts  # 0 means unlimited
        extra_args = script_args or []
        self.script_args = [*DEFAULT_MAIN_ARGS, *extra_args]
        self.restart_count = 0
        self.proc: Optional[subprocess.Popen] = None
        self._running = True
        self._signal_count = 0
        self.start_main_py = start_main_py

        self.reconcile_interval_seconds = max(reconcile_interval_hours * 3600.0, 60.0)
        self._next_reconcile_at = time.time()  # run once immediately, then every interval

    def _log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def _start_child(self) -> None:
        cmd = [sys.executable, str(self.target_script), *self.script_args]
        self.proc = subprocess.Popen(cmd)
        self._log(f"[info] started main.py, pid={self.proc.pid}")

    def _stop_child(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is not None:
            return

        self._log(f"[info] stopping child pid={self.proc.pid}")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
            self._log("[info] child terminated gracefully")
        except subprocess.TimeoutExpired:
            self._log("[warn] child did not exit in time, killing...")
            self.proc.kill()
            self.proc.wait()
            self._log("[info] child killed")

    def _handle_signal(self, signum, _frame) -> None:
        self._signal_count += 1

        if self._signal_count == 1:
            self._log(f"[info] received signal={signum}, shutting down supervisor...")
            self._running = False
            self._stop_child()
            return

        # If user presses Ctrl+C again while blocked in long network calls,
        # force-exit immediately to avoid appearing "stuck".
        self._log(
            f"[warn] received signal={signum} again (count={self._signal_count}), "
            "force exiting now."
        )
        os._exit(130)

    def _run_scet_reconcile(self) -> None:
        self._log("[reconcile] start SCET Jira vs SharePoint reconciliation")

        jira_scet_numbers = _fetch_all_jira_scet_numbers(
            self._log,
            should_stop=lambda: not self._running,
        )
        _write_count_file(JIRA_SCET_COUNT_FILE, len(jira_scet_numbers))
        _write_list_file(JIRA_SCET_LIST_FILE, jira_scet_numbers)
        self._log(
            f"[reconcile] Jira outputs written: {JIRA_SCET_COUNT_FILE}, {JIRA_SCET_LIST_FILE}"
        )

        sp_scet_numbers, sp_file_count = _fetch_sharepoint_scet_numbers_non_recursive(
            self._log,
            should_stop=lambda: not self._running,
        )
        _write_count_file(SHAREPOINT_SCET_COUNT_FILE, sp_file_count)
        _write_list_file(SHAREPOINT_SCET_LIST_FILE, sp_scet_numbers)
        self._log(
            f"[reconcile] SharePoint outputs written: "
            f"{SHAREPOINT_SCET_COUNT_FILE}, {SHAREPOINT_SCET_LIST_FILE}"
        )

        jira_set = set(jira_scet_numbers)
        sp_set = set(sp_scet_numbers)
        missing_on_sharepoint = sorted(jira_set - sp_set, key=_scet_sort_key)

        _write_list_file(MISSING_SCET_LIST_FILE, missing_on_sharepoint)
        self._log(
            f"[reconcile] compare done: jira={len(jira_set)}, sharepoint={len(sp_set)}, "
            f"missing_on_sharepoint={len(missing_on_sharepoint)}"
        )
        self._log(f"[reconcile] missing list written: {MISSING_SCET_LIST_FILE}")

        _sync_missing_scet_to_sharepoint(
            missing_on_sharepoint=missing_on_sharepoint,
            log_fn=self._log,
            should_stop=lambda: not self._running,
        )
        self._log("[reconcile] finished")

    def _run_scet_reconcile_safe(self) -> None:
        try:
            self._run_scet_reconcile()
        except Exception as exc:
            self._log(f"[reconcile][error] {exc}")

    def run(self) -> int:
        if not self.target_script.exists():
            self._log(f"[error] target script not found: {self.target_script}")
            return 1

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self._log("[info] supervisor started")
        self._log(
            f"[info] SCET reconcile enabled, interval={self.reconcile_interval_seconds / 3600.0:.2f}h"
        )
        if self.start_main_py:
            self._start_child()
        else:
            self._log("[info] main.py start disabled by --disable-main-py-start")

        while self._running:
            time.sleep(self.check_interval)

            now_ts = time.time()
            if now_ts >= self._next_reconcile_at:
                self._run_scet_reconcile_safe()
                self._next_reconcile_at = now_ts + self.reconcile_interval_seconds

            if not self.proc:
                continue

            ret = self.proc.poll()
            if ret is None:
                continue  # child still running

            self._log(f"[warn] child exited with code={ret}")

            if not self._running:
                break

            if self.max_restarts > 0 and self.restart_count >= self.max_restarts:
                self._log(
                    f"[error] reached max restarts ({self.max_restarts}), supervisor exiting."
                )
                return 2

            self.restart_count += 1
            self._log(
                f"[info] restarting child in {self.restart_delay:.1f}s "
                f"(restart #{self.restart_count})"
            )
            time.sleep(self.restart_delay)
            self._start_child()

        self._log("[info] supervisor stopped")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start and supervise main.py; auto-restart on crash."
    )
    parser.add_argument(
        "--script",
        default="main.py",
        help="Target script to supervise (default: main.py)",
    )
    parser.add_argument(
        "--check-interval",
        type=float,
        default=2.0,
        help="Health check interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=3.0,
        help="Delay before restarting crashed process in seconds (default: 3.0)",
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=0,
        help="Max restart count; 0 means unlimited (default: 0)",
    )
    parser.add_argument(
        "--reconcile-interval-hours",
        type=float,
        default=8.0,
        help="SCET Jira/SharePoint reconcile interval in hours (default: 8.0)",
    )
    parser.add_argument(
        "--disable-main-py-start",
        action="store_true",
        help="Do not start main.py child process; run supervisor reconcile loop only.",
    )
    parser.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        help="Extra arguments passed to target script. Use '--' before extra args.",
    )
    args = parser.parse_args()

    if args.check_interval <= 0:
        parser.error("--check-interval must be > 0")
    if args.restart_delay < 0:
        parser.error("--restart-delay must be >= 0")
    if args.max_restarts < 0:
        parser.error("--max-restarts must be >= 0")
    if args.reconcile_interval_hours <= 0:
        parser.error("--reconcile-interval-hours must be > 0")

    return args


def main() -> int:
    args = parse_args()

    child_args = args.script_args
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]

    supervisor = MainSupervisor(
        target_script=Path(args.script).resolve(),
        check_interval=args.check_interval,
        restart_delay=args.restart_delay,
        max_restarts=args.max_restarts,
        script_args=child_args,
        reconcile_interval_hours=args.reconcile_interval_hours,
        start_main_py=not args.disable_main_py_start,
    )
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
