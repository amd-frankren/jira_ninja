#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Assign Jira ticket to an owner, then add an internal visibility comment.

Reusable API:
- assign_ticket_and_notify(ticket_url, assignee, cpm_email, ...)

CLI usage:
python jira_assign_and_notify.py \
  --ticket-url "https://ontrack.amd.com/browse/SCET-12345" \
  --assignee "frank.ren@amd.com" \
  --cpm "eric.gao@amd.com"


python jira_assign_and_notify.py --ticket-url https://ontrack.amd.com/browse/SCET-22716 --assignee Josh.Ji@amd.com --cpm Eric.Gao@amd.com

Behavior:
1) Parse issue key from ticket URL
2) Assign issue to target user
3) After assign succeeds, add comment with visibility role "AMD Internal Users"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, Optional
from urllib import error, parse, request

from jira_add_comment import add_comment_to_jira, DEFAULT_VISIBILITY_ROLE

ENV_EXTERNAL_JIRA_URL = "EXTERNAL_JIRA_URL"
ENV_EXTERNAL_JIRA_TOKEN = "EXTERNAL_JIRA_TOKEN"


def _build_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "jira-assign-and-notify/1.0",
    }


def _resolve_jira_base_url() -> str:
    base_url = os.getenv(ENV_EXTERNAL_JIRA_URL, "").strip()
    if not base_url:
        raise RuntimeError(f"Missing Jira URL. Please set environment variable: {ENV_EXTERNAL_JIRA_URL}")
    return base_url.rstrip("/")


def _resolve_jira_token(cli_token: str = "") -> str:
    t = (cli_token or "").strip()
    if t:
        return t
    env_t = os.getenv(ENV_EXTERNAL_JIRA_TOKEN, "").strip()
    if env_t:
        return env_t
    raise RuntimeError(
        f"Missing Jira token. Provide it via --token or set environment variable: {ENV_EXTERNAL_JIRA_TOKEN}"
    )


def _http_json(method: str, url: str, token: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Any:
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = request.Request(
        url=url,
        method=method.upper(),
        headers=_build_headers(token),
        data=body,
    )

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {}
            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw_response": text}
    except error.HTTPError as e:
        err_text = ""
        try:
            err_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} request failed: {url}\nResponse: {err_text}") from e
    except error.URLError as e:
        raise RuntimeError(f"Network request failed: {url}\nReason: {e.reason}") from e


def _extract_issue_key(ticket_url_or_key: str) -> str:
    text = (ticket_url_or_key or "").strip()
    if not text:
        return ""
    m = re.search(r"\b([A-Z][A-Z0-9_]*-\d+)\b", text.upper())
    return m.group(1) if m else ""


def _extract_mention_name(email_or_user: str) -> str:
    s = (email_or_user or "").strip()
    if not s:
        return "owner"
    if "@" in s:
        return s.split("@", 1)[0]
    return s


def _resolve_account_id_by_query(base_url: str, token: str, assignee: str, timeout: int = 60) -> str:
    """
    Try to resolve accountId (Jira Cloud style) by user search endpoint.
    If not available, return empty string.
    """
    q = parse.quote(assignee.strip())
    candidates = [
        f"{base_url}/rest/api/3/user/search?query={q}&maxResults=10",
        f"{base_url}/rest/api/2/user/search?username={q}&maxResults=10",
    ]

    for url in candidates:
        try:
            data = _http_json("GET", url, token, timeout=timeout)
        except Exception:
            continue

        users = data if isinstance(data, list) else []
        for u in users:
            if not isinstance(u, dict):
                continue
            account_id = str(u.get("accountId", "")).strip()
            email = str(u.get("emailAddress", "")).strip().lower()
            name = str(u.get("name", "")).strip().lower()
            key = str(u.get("key", "")).strip().lower()

            a = assignee.strip().lower()
            if a and (a == email or a == name or a == key):
                if account_id:
                    return account_id

        if users:
            first = users[0]
            if isinstance(first, dict):
                fallback = str(first.get("accountId", "")).strip()
                if fallback:
                    return fallback

    return ""


def assign_issue(
    issue_key: str,
    assignee: str,
    token: str = "",
    timeout: int = 60,
) -> Dict[str, Any]:
    """
    Assign Jira issue to a target user.
    Tries multiple payload formats for better compatibility across Jira deployments.
    """
    safe_issue = _extract_issue_key(issue_key)
    if not safe_issue:
        raise ValueError("issue_key is invalid or empty")
    safe_assignee = (assignee or "").strip()
    if not safe_assignee:
        raise ValueError("assignee cannot be empty")

    base_url = _resolve_jira_base_url()
    resolved_token = _resolve_jira_token(token)
    url = f"{base_url}/rest/api/2/issue/{safe_issue}/assignee"

    account_id = _resolve_account_id_by_query(base_url, resolved_token, safe_assignee, timeout=timeout)
    payloads = []
    if account_id:
        payloads.append({"accountId": account_id})
    payloads.append({"name": safe_assignee})
    payloads.append({"key": safe_assignee})

    last_error = ""
    for payload in payloads:
        try:
            _http_json("PUT", url, resolved_token, payload=payload, timeout=timeout)
            return {
                "issue_key": safe_issue,
                "assignee": safe_assignee,
                "account_id": account_id,
                "payload_used": payload,
            }
        except Exception as exc:
            last_error = str(exc)

    raise RuntimeError(f"Failed to assign issue {safe_issue} to {safe_assignee}. Last error: {last_error}")


def build_assign_notice_comment(assignee: str, cpm_email: str) -> str:
    mention = _extract_mention_name(assignee)
    safe_cpm = (cpm_email or "").strip() or "cpm@amd.com"
    return (
        f"Hi @{mention}, this ticket is assigned by AI. "
        f"If this issue does not belong to you, please assign this ticket to CPM {safe_cpm} "
        f"or assign it to the correct module owner."
    )


def assign_ticket_and_notify(
    ticket_url: str,
    assignee: str,
    cpm_email: str,
    visibility_role: str = DEFAULT_VISIBILITY_ROLE,
    token: str = "",
    timeout: int = 60,
) -> Dict[str, Any]:
    issue_key = _extract_issue_key(ticket_url)
    if not issue_key:
        raise ValueError(f"Cannot extract issue key from ticket_url: {ticket_url}")

    assign_result = assign_issue(
        issue_key=issue_key,
        assignee=assignee,
        token=token,
        timeout=timeout,
    )

    comment_body = build_assign_notice_comment(assignee=assignee, cpm_email=cpm_email)
    comment_result = add_comment_to_jira(
        issue_key=issue_key,
        body=comment_body,
        visibility_role=visibility_role,
        timeout=timeout,
    )

    return {
        "issue_key": issue_key,
        "ticket_url": ticket_url.strip(),
        "assignee": assignee.strip(),
        "cpm_email": cpm_email.strip(),
        "assign_result": assign_result,
        "comment_visibility_role": visibility_role,
        "comment_result": comment_result,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Assign Jira ticket and add internal notify comment")
    p.add_argument("--ticket-url", required=True, help="Ticket URL or issue key (e.g. SCET-12345)")
    p.add_argument("--assignee", required=True, help="Assignee user/email")
    p.add_argument("--cpm", required=True, help="Project CPM email")
    p.add_argument("--visibility-role", default=DEFAULT_VISIBILITY_ROLE, help="Comment visibility role")
    p.add_argument("--token", default="", help="Bearer token (optional; read from env if omitted)")
    p.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds")
    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        result = assign_ticket_and_notify(
            ticket_url=args.ticket_url,
            assignee=args.assignee,
            cpm_email=args.cpm,
            visibility_role=args.visibility_role,
            token=args.token,
            timeout=max(args.timeout, 1),
        )
        print(
            f"[ok] assign + comment succeeded | issue={result['issue_key']} "
            f"| assignee={result['assignee']} | visibility_role={result['comment_visibility_role']}"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
