#!/usr/bin/env python3
"""
Jira monitor module.

Features:
- Fixed Jira target:
    JIRA_BASE_URL=https://ontrack.amd.com
    JIRA_PROJECT_KEY=SCET
- Requires JIRA_TOKEN from environment (missing => raise error).
- Polls Jira issues and detects "created" or "updated" events since last checkpoint.
- Can be imported as a module, and can also run standalone for debugging.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib import error, parse, request

# ===== Fixed Jira target config =====
JIRA_BASE_URL = "https://ontrack.amd.com"
JIRA_PROJECT_KEY = "SCET"

# Jira API endpoints
JIRA_SEARCH_API = "/rest/api/2/search"


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def require_jira_token() -> str:
    """Read JIRA_TOKEN from environment. Raise on missing token."""
    token = os.getenv("JIRA_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing required env var: JIRA_TOKEN")
    return token


def _parse_jira_datetime(value: str) -> datetime:
    """
    Parse Jira datetime like:
      2026-04-09T10:20:30.123+0000
      2026-04-09T10:20:30+0000
    """
    fmts = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
    for fmt in fmts:
        try:
            return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unsupported Jira datetime format: {value}")


def _jira_get_json(base_url: str, endpoint: str, token: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send GET request to Jira and return JSON as dict.
    Uses Bearer token auth.
    """
    query = parse.urlencode(params)
    url = f"{base_url.rstrip('/')}{endpoint}?{query}"
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


class JiraMonitor:
    """Monitor Jira project changes by polling updated issues."""

    def __init__(
        self,
        base_url: str = JIRA_BASE_URL,
        project_key: str = JIRA_PROJECT_KEY,
        token: Optional[str] = None,
        poll_interval_seconds: int = 60,
        max_results: int = 100,
        initial_since: Optional[datetime] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.token = token or require_jira_token()
        self.poll_interval_seconds = poll_interval_seconds
        self.max_results = max_results
        self.last_checked = initial_since or datetime.now(timezone.utc)

    def poll_changes(self) -> List[Dict[str, Any]]:
        """
        Poll Jira once and return issue events changed after previous checkpoint.
        Event format:
        {
            "issue_key": "...",
            "summary": "...",
            "event_type": "created" | "updated",
            "created": "...",
            "updated": "...",
            "issue_url": "..."
        }
        """
        checkpoint = self.last_checked
        now = datetime.now(timezone.utc)

        params = {
            "jql": f"project = {self.project_key} ORDER BY updated DESC",
            "fields": "summary,created,updated",
            "maxResults": str(self.max_results),
            "startAt": "0",
        }
        data = _jira_get_json(self.base_url, JIRA_SEARCH_API, self.token, params)
        issues = data.get("issues", [])
        if not isinstance(issues, list):
            self.last_checked = now
            return []

        events: List[Dict[str, Any]] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue

            fields = issue.get("fields", {}) or {}
            if not isinstance(fields, dict):
                continue

            updated_raw = str(fields.get("updated", "")).strip()
            created_raw = str(fields.get("created", "")).strip()
            if not updated_raw or not created_raw:
                continue

            try:
                updated_dt = _parse_jira_datetime(updated_raw)
                created_dt = _parse_jira_datetime(created_raw)
            except ValueError:
                continue

            if updated_dt <= checkpoint:
                continue

            issue_key = str(issue.get("key", "")).strip()
            summary = str(fields.get("summary", "")).strip()
            event_type = "created" if created_dt > checkpoint else "updated"
            issue_url = f"{self.base_url}/browse/{issue_key}" if issue_key else self.base_url

            events.append(
                {
                    "issue_key": issue_key,
                    "summary": summary,
                    "event_type": event_type,
                    "created": created_raw,
                    "updated": updated_raw,
                    "issue_url": issue_url,
                }
            )

        # Oldest first for readable output/order
        events.sort(key=lambda x: x.get("updated", ""))
        self.last_checked = now
        return events


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Jira project for new/updated tickets.")
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--since-minutes",
        type=int,
        default=0,
        help="Initial look-back window in minutes for debug (default: 0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit (useful for debugging).",
    )
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        initial_since = datetime.now(timezone.utc) - timedelta(minutes=max(args.since_minutes, 0))

        monitor = JiraMonitor(
            poll_interval_seconds=max(args.interval, 1),
            initial_since=initial_since,
        )

        print(
            f"[info] Start Jira monitor: base={monitor.base_url}, "
            f"project={monitor.project_key}, interval={monitor.poll_interval_seconds}s"
        )
        print(f"[info] Initial checkpoint (UTC): {monitor.last_checked.isoformat()}")

        while True:
            events = monitor.poll_changes()
            if events:
                print(f"[info] Detected {len(events)} changed ticket(s).")
                for ev in events:
                    print(
                        f"[event] {ev['event_type'].upper()} "
                        f"{ev['issue_key']} - {ev['summary']} | {ev['issue_url']}"
                    )
            else:
                print("[info] No new Jira changes.")

            if args.once:
                break

            time.sleep(monitor.poll_interval_seconds)

        return 0
    except Exception as exc:
        eprint(f"[error] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
