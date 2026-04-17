#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Download Internal Jira PLAT tickets and save as JSON.

Supports two modes:
1) single: download one ticket
    python jira_export_internal_plat.py --mode single --browse-url [PLAT URL]
2) bulk:   download the entire PLAT project (download all by default)


Use 10 parallel workers and skip online PLAT list fetching:
python jira_export_internal_plat.py --mode bulk --skip-list-fetch --workers 10


"""

import argparse
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib import error, parse, request


ENV_INTERNAL_JIRA_TOKEN = "INTERNAL_JIRA_TOKEN"
ENV_PLAT_PROJECT_ISSUES_URL = "PLAT_PROJECT_ISSUES_URL"

DEFAULT_TIMEOUT = 30
DEFAULT_WORKERS = 6
DEFAULT_LIST_PAGE_SIZE = 100
DEFAULT_SUCCESS_RECORD_FILE = "downloaded_success_keys.txt"
# 0 means download all
DEFAULT_MAX_ISSUES = 0


def parse_browse_url(browse_url: str) -> Dict[str, str]:
    m = re.match(r"^(https?://[^/]+)/browse/([A-Za-z][A-Za-z0-9_]*-\d+)$", browse_url.strip())
    if not m:
        raise ValueError(f"Failed to parse browse URL: {browse_url}")
    return {"base_url": m.group(1), "issue_key": m.group(2)}


def parse_project_issues_url(project_url: str) -> Dict[str, str]:
    m = re.match(r"^(https?://[^/]+)/projects/([A-Za-z][A-Za-z0-9_]*)/issues(?:/[^?]+)?(?:\?.*)?$", project_url.strip())
    if not m:
        raise ValueError(f"Failed to parse project issues URL: {project_url}")
    return {"base_url": m.group(1), "project_key": m.group(2)}


def build_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "jira-export-internal-plat/2.2",
    }


def http_get_text(url: str, token: str, timeout: int) -> str:
    req = request.Request(url=url, method="GET", headers=build_headers(token))
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} request failed: {url}\nResponse: {body}") from e
    except error.URLError as e:
        raise RuntimeError(f"Network request failed: {url}\nReason: {e.reason}") from e


def http_get_json(url: str, token: str, timeout: int) -> Any:
    text = http_get_text(url, token, timeout)
    return json.loads(text) if text else {}


def fetch_paginated(
    url: str,
    token: str,
    item_keys: List[str],
    timeout: int,
    page_size: int = 100,
) -> Dict[str, Any]:
    start_at = 0
    all_items: List[Any] = []
    total: Optional[int] = None
    pages = 0

    while True:
        parsed = parse.urlparse(url)
        query = dict(parse.parse_qsl(parsed.query, keep_blank_values=True))
        query["startAt"] = str(start_at)
        query["maxResults"] = str(page_size)

        paged_url = parse.urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parse.urlencode(query),
            parsed.fragment,
        ))

        data = http_get_json(paged_url, token, timeout)
        pages += 1

        items = None
        for key in item_keys:
            if isinstance(data, dict) and key in data and isinstance(data[key], list):
                items = data[key]
                break

        if items is None:
            if pages == 1:
                return {"items": [], "total": 0, "raw": data}
            break

        all_items.extend(items)

        if isinstance(data, dict):
            total = data.get("total", total)
            max_results = data.get("maxResults", len(items))
        else:
            max_results = len(items)

        if len(items) == 0:
            break

        start_at += max_results if isinstance(max_results, int) and max_results > 0 else len(items)

        if total is not None and len(all_items) >= total:
            break

    return {
        "items": all_items,
        "total": total if total is not None else len(all_items),
        "count": len(all_items),
    }


def fetch_issue_data(base_url: str, issue_key: str, token: str, timeout: int) -> Dict[str, Any]:
    api_v2 = f"{base_url.rstrip('/')}/rest/api/2"
    api_v3 = f"{base_url.rstrip('/')}/rest/api/3"

    expand = "names,schema,operations,editmeta,changelog,renderedFields,versionedRepresentations"
    issue_url = f"{api_v2}/issue/{issue_key}?expand={parse.quote(expand, safe=',')}"
    issue = http_get_json(issue_url, token, timeout)

    result: Dict[str, Any] = {
        "source": {
            "baseUrl": base_url,
            "issueKey": issue_key,
            "issueUrl": f"{base_url.rstrip('/')}/browse/{issue_key}",
            "exportedAtUtc": datetime.now(timezone.utc).isoformat(),
        },
        "issue": issue,
        "extras": {},
        "warnings": [],
    }

    try:
        comments = fetch_paginated(
            f"{api_v2}/issue/{issue_key}/comment",
            token,
            item_keys=["comments", "values"],
            timeout=timeout,
        )
        result["extras"]["comments"] = comments
    except Exception as e:
        result["warnings"].append(f"Failed to fetch comments: {e}")

    try:
        worklogs = fetch_paginated(
            f"{api_v2}/issue/{issue_key}/worklog",
            token,
            item_keys=["worklogs", "values"],
            timeout=timeout,
        )
        result["extras"]["worklogs"] = worklogs
    except Exception as e:
        result["warnings"].append(f"Failed to fetch worklogs: {e}")

    changelog_fetched = False
    for base in (api_v2, api_v3):
        try:
            changelog = fetch_paginated(
                f"{base}/issue/{issue_key}/changelog",
                token,
                item_keys=["histories", "values"],
                timeout=timeout,
            )
            result["extras"]["changelog"] = changelog
            changelog_fetched = True
            break
        except Exception:
            continue

    if not changelog_fetched:
        result["warnings"].append(
            "Changelog pagination endpoint unavailable; kept issue.expand.changelog data when available."
        )

    return result


def save_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_issue_keys_from_file(path: str, project_key: str) -> List[str]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"List file does not exist: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    issue_keys = data.get("issueKeys", []) if isinstance(data, dict) else []
    if not isinstance(issue_keys, list):
        raise ValueError(f"Invalid list file format (issueKeys is not an array): {path}")

    filtered: List[str] = []
    for key in issue_keys:
        if isinstance(key, str) and re.fullmatch(rf"{re.escape(project_key)}-\d+", key):
            filtered.append(key)

    if not filtered:
        raise ValueError(f"No valid {project_key}-xxx key found in list file: {path}")

    return sorted(set(filtered), key=lambda x: int(x.split("-")[1]))


def load_success_issue_keys(path: str, project_key: str) -> List[str]:
    if not os.path.exists(path):
        return []

    keys: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            key = line.strip()
            if key and re.fullmatch(rf"{re.escape(project_key)}-\d+", key):
                keys.append(key)

    return sorted(set(keys), key=lambda x: int(x.split("-")[1]))


def fetch_issue_keys_by_project_api(
    base_url: str,
    project_key: str,
    token: str,
    timeout: int,
    page_size: int = DEFAULT_LIST_PAGE_SIZE,
    checkpoint_path: str = "",
) -> List[str]:
    api_v2 = f"{base_url.rstrip('/')}/rest/api/2/search"
    start_at = 0
    all_keys: List[str] = []
    page_no = 0

    while True:
        page_no += 1
        jql = f"project = {project_key} ORDER BY key ASC"
        params = {
            "jql": jql,
            "fields": "key",
            "startAt": str(start_at),
            "maxResults": str(page_size),
            "validateQuery": "false",
        }
        url = f"{api_v2}?{parse.urlencode(params)}"
        print(f"[LIST] Request #{page_no}: startAt={start_at}, maxResults={page_size}")
        data = http_get_json(url, token, timeout)

        issues = data.get("issues", []) if isinstance(data, dict) else []
        print(f"[LIST] Response #{page_no}: {len(issues)} issues")

        if not issues:
            if checkpoint_path:
                keys_snapshot = sorted(set(all_keys), key=lambda x: int(x.split("-")[1]))
                save_json(
                    checkpoint_path,
                    {
                        "source": {
                            "baseUrl": base_url,
                            "projectKey": project_key,
                            "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                        },
                        "progress": {
                            "pageNo": page_no,
                            "startAt": start_at,
                            "note": "This response is empty; list fetching is complete or no more data.",
                        },
                        "count": len(keys_snapshot),
                        "issueKeys": keys_snapshot,
                    },
                )
            break

        for issue in issues:
            key = issue.get("key")
            if isinstance(key, str) and re.fullmatch(rf"{re.escape(project_key)}-\d+", key):
                all_keys.append(key)

        total = data.get("total")
        max_results = data.get("maxResults", len(issues))
        start_at += max_results if isinstance(max_results, int) and max_results > 0 else len(issues)

        if checkpoint_path:
            keys_snapshot = sorted(set(all_keys), key=lambda x: int(x.split("-")[1]))
            save_json(
                checkpoint_path,
                {
                    "source": {
                        "baseUrl": base_url,
                        "projectKey": project_key,
                        "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                    },
                    "progress": {
                        "pageNo": page_no,
                        "startAt": start_at,
                        "totalFromApi": total,
                    },
                    "count": len(keys_snapshot),
                    "issueKeys": keys_snapshot,
                },
            )
            print(f"[LIST] List file updated: {checkpoint_path} (current: {len(keys_snapshot)})")

        if isinstance(total, int) and len(all_keys) >= total:
            break

    return sorted(set(all_keys), key=lambda x: int(x.split("-")[1]))


def fetch_issue_keys_by_html(project_url: str, token: str, project_key: str, timeout: int) -> List[str]:
    html = http_get_text(project_url, token, timeout)
    keys = re.findall(rf"{re.escape(project_key)}-\d+", html)
    return sorted(set(keys), key=lambda x: int(x.split("-")[1]))


def export_single_issue(browse_url: str, token: str, output: str, timeout: int) -> str:
    parsed = parse_browse_url(browse_url)
    base_url = parsed["base_url"]
    issue_key = parsed["issue_key"]
    output_path = output or f"{issue_key}.json"

    print(f"Start downloading single issue {issue_key} (timeout={timeout}s)...")
    data = fetch_issue_data(base_url, issue_key, token, timeout)
    save_json(output_path, data)
    return output_path


def export_bulk_by_project(
    project_url: str,
    token: str,
    output_dir: str,
    timeout: int,
    max_issues: int = DEFAULT_MAX_ISSUES,
    workers: int = DEFAULT_WORKERS,
    list_page_size: int = DEFAULT_LIST_PAGE_SIZE,
    skip_list_fetch: bool = False,
    list_file: str = "",
    success_record_file: str = "",
) -> Dict[str, Any]:
    parsed = parse_project_issues_url(project_url)
    base_url = parsed["base_url"]
    project_key = parsed["project_key"]

    warnings: List[str] = []
    os.makedirs(output_dir, exist_ok=True)
    list_path = list_file.strip() or os.path.join(output_dir, "plat_list.json")
    success_path = success_record_file.strip() or os.path.join(output_dir, DEFAULT_SUCCESS_RECORD_FILE)

    keys: List[str] = []

    if skip_list_fetch:
        print(f"[LIST] Skip online list fetching enabled; reading local file directly: {list_path}")
        keys = load_issue_keys_from_file(list_path, project_key)
        print(f"[LIST] Loaded {len(keys)} {project_key} keys from local file")
    else:
        print(f"Start fetching {project_key} issue list (timeout={timeout}s)...")
        print(f"List checkpoint file: {list_path}")
        print(f"List page size: {list_page_size}")

        try:
            keys = fetch_issue_keys_by_project_api(
                base_url,
                project_key,
                token,
                timeout=timeout,
                page_size=list_page_size,
                checkpoint_path=list_path,
            )
        except Exception as e:
            warnings.append(f"Failed to fetch issue list via Search API; fallback to HTML extraction: {e}")
            keys = []

        if not keys:
            print("[LIST] Search API returned no data; switching to HTML extraction...")
            keys = fetch_issue_keys_by_html(project_url, token, project_key, timeout=timeout)
            warnings.append("Issue list source is HTML extraction; it may be incomplete.")

        save_json(
            list_path,
            {
                "source": {
                    "projectUrl": project_url,
                    "baseUrl": base_url,
                    "projectKey": project_key,
                    "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
                },
                "count": len(keys),
                "issueKeys": keys,
                "warnings": warnings,
            },
        )
        print(f"Step 1 completed: wrote {list_path}, total {len(keys)} items")

    all_download_keys = keys[:max_issues] if max_issues > 0 else keys
    success_already_set = set(load_success_issue_keys(success_path, project_key))
    download_keys = [k for k in all_download_keys if k not in success_already_set]

    skipped_already_success = len(all_download_keys) - len(download_keys)
    if skipped_already_success > 0:
        print(
            f"[SUCCESS-RECORD] Skipped {skipped_already_success} already-successful issues (from {success_path})"
        )

    total = len(download_keys)
    success_count = 0
    failed: List[Dict[str, str]] = []
    success_lock = threading.Lock()

    def _download_one(issue_key: str) -> Dict[str, str]:
        try:
            data = fetch_issue_data(base_url, issue_key, token, timeout)
            out_file = os.path.join(output_dir, f"{issue_key}.json")
            save_json(out_file, data)

            with success_lock:
                if issue_key not in success_already_set:
                    with open(success_path, "a", encoding="utf-8") as sf:
                        sf.write(issue_key + "\n")
                    success_already_set.add(issue_key)

            return {"issueKey": issue_key, "ok": "1", "error": ""}
        except Exception as e:
            return {"issueKey": issue_key, "ok": "0", "error": str(e)}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {executor.submit(_download_one, k): k for k in download_keys}
        for idx, future in enumerate(as_completed(future_map), start=1):
            item = future.result()
            key = item["issueKey"]
            if item["ok"] == "1":
                success_count += 1
                print(f"[{idx}/{total}] OK {key}")
            else:
                failed.append({"issueKey": key, "error": item["error"]})
                print(f"[{idx}/{total}] FAIL {key}: {item['error']}", file=sys.stderr)

    summary: Dict[str, Any] = {
        "source": {
            "projectUrl": project_url,
            "baseUrl": base_url,
            "projectKey": project_key,
            "exportedAtUtc": datetime.now(timezone.utc).isoformat(),
            "skipListFetch": skip_list_fetch,
            "listFile": list_path,
            "successRecordFile": success_path,
        },
        "stats": {
            "listCount": len(keys),
            "downloadPlanned": len(download_keys),
            "skippedAlreadySuccess": skipped_already_success,
            "successCount": success_count,
            "failedCount": len(failed),
        },
        "issueKeysAll": keys,
        "issueKeysDownloaded": download_keys,
        "failed": failed,
        "warnings": warnings,
    }

    index_path = os.path.join(output_dir, "index.json")
    save_json(index_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Internal Jira PLAT tickets and save as JSON")
    parser.add_argument("--mode", choices=["single", "bulk"], default="bulk", help="single=one, bulk=batch")
    parser.add_argument("--browse-url", default="", help="single mode: Jira ticket browse URL (required)")
    parser.add_argument("--output", default="", help="single mode output file (default: {issue_key}.json)")
    parser.add_argument("--output-dir", default="PLAT_export", help="bulk mode output directory")
    parser.add_argument("--max-issues", type=int, default=DEFAULT_MAX_ISSUES, help="bulk mode max export count, default 0 (all)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="bulk mode concurrent worker count")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="network request timeout seconds (default 20)")
    parser.add_argument("--list-page-size", type=int, default=DEFAULT_LIST_PAGE_SIZE, help="list page size (default 100)")
    parser.add_argument("--skip-list-fetch", action="store_true", help="skip online list fetching and read local list file directly")
    parser.add_argument("--list-file", default="", help="local list file path (default: {output_dir}/plat_list.json)")
    parser.add_argument(
        "--success-record-file",
        default="",
        help=f"success record file (default: {{output_dir}}/{DEFAULT_SUCCESS_RECORD_FILE})",
    )
    args = parser.parse_args()

    try:
        token = os.getenv(ENV_INTERNAL_JIRA_TOKEN, "").strip()
        if not token:
            raise RuntimeError(f"Missing environment variable: {ENV_INTERNAL_JIRA_TOKEN}")

        if args.mode == "single":
            if not args.browse_url.strip():
                raise RuntimeError("single mode requires --browse-url")
            output_path = export_single_issue(args.browse_url.strip(), token, args.output, args.timeout)
            print(f"Export completed: {output_path}")
            return 0

        project_issues_url = os.getenv(ENV_PLAT_PROJECT_ISSUES_URL, "").strip()
        if not project_issues_url:
            raise RuntimeError(f"Missing required environment variable: {ENV_PLAT_PROJECT_ISSUES_URL}")

        summary = export_bulk_by_project(
            project_url=project_issues_url,
            token=token,
            output_dir=args.output_dir,
            timeout=args.timeout,
            max_issues=args.max_issues,
            workers=args.workers,
            list_page_size=args.list_page_size,
            skip_list_fetch=args.skip_list_fetch,
            list_file=args.list_file,
            success_record_file=args.success_record_file,
        )
        print("Bulk export completed")
        print(
            f"listCount={summary['stats']['listCount']}, "
            f"downloadPlanned={summary['stats']['downloadPlanned']}, "
            f"skippedAlreadySuccess={summary['stats']['skippedAlreadySuccess']}, "
            f"success={summary['stats']['successCount']}, "
            f"failed={summary['stats']['failedCount']}"
        )
        print(f"index={os.path.join(args.output_dir, 'index.json')}")
        return 0
    except KeyboardInterrupt:
        print("Execution interrupted by user.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Execution failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
