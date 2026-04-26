#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Add an internal-only comment to a Jira ticket.

Usage examples:

python jira_add_comment.py \
  --issue-key SCET-22716 \
  --body "Ignore: test This comment is visible to internal users only."

python jira_add_comment.py \
  --issue-key SCET-22716 \
  --body "internal note" \
  --visibility-role "AMD Internal Users"

Base URL resolution:
1) env EXTERNAL_JIRA_URL

Token resolution order:
1) --token
2) env EXTERNAL_JIRA_TOKEN
"""

import argparse
import gzip
import json
import os
import sys
from typing import Dict
from urllib import error, request


DEFAULT_VISIBILITY_ROLE = "AMD Internal Users"
ENV_EXTERNAL_JIRA_URL = "EXTERNAL_JIRA_URL"
ENV_EXTERNAL_JIRA_TOKEN = "EXTERNAL_JIRA_TOKEN"


def build_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "jira-add-internal-comment/1.0",
    }


def resolve_jira_base_url() -> str:
    base_url = os.getenv(ENV_EXTERNAL_JIRA_URL, "").strip()
    if base_url:
        return base_url
    raise RuntimeError(f"Missing Jira URL. Please set environment variable: {ENV_EXTERNAL_JIRA_URL}")


def resolve_jira_token(cli_token: str) -> str:
    token = (cli_token or "").strip()
    if token:
        return token

    env_token = os.getenv(ENV_EXTERNAL_JIRA_TOKEN, "").strip()
    if env_token:
        return env_token

    raise RuntimeError(
        f"Missing Jira token. Provide it via --token or set environment variable: {ENV_EXTERNAL_JIRA_TOKEN}"
    )


def add_comment_to_jira(
    issue_key: str,
    body: str,
    visibility_role: str = DEFAULT_VISIBILITY_ROLE,
    timeout: int = 60,
) -> Dict:
    token = resolve_jira_token("")
    safe_base = resolve_jira_base_url().rstrip("/")
    safe_issue = issue_key.strip().upper()
    if not safe_issue:
        raise ValueError("issue_key cannot be empty")
    if not body.strip():
        raise ValueError("body cannot be empty")

    url = f"{safe_base}/rest/api/2/issue/{safe_issue}/comment"
    payload = {
        "body": body,
        "visibility": {
            "type": "role",
            "value": visibility_role,
        },
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = request.Request(
        url=url,
        method="POST",
        headers=build_headers(token),
        data=payload_bytes,
    )

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()

            # Some Jira gateways may return a gzip-compressed response body
            content_encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if raw_bytes and (content_encoding == "gzip" or raw_bytes[:2] == b"\x1f\x8b"):
                try:
                    raw_bytes = gzip.decompress(raw_bytes)
                except Exception:
                    pass

            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw_bytes.decode(charset, errors="replace")

            if not text.strip():
                return {}

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Some endpoints may return non-JSON text even on success
                return {"raw_response": text}
    except error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} request failed: {url}\nResponse: {body_text}") from e
    except error.URLError as e:
        raise RuntimeError(f"Network request failed: {url}\nReason: {e.reason}") from e


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add an internal comment to a Jira ticket")
    parser.add_argument("--issue-key", required=True, help="For example: SCET-22716")
    parser.add_argument("--body", required=True, help="Comment content")
    parser.add_argument("--visibility-role", default=DEFAULT_VISIBILITY_ROLE, help="Visible role name")
    parser.add_argument("--token", default="", help="Bearer token (optional; read from env if omitted)")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        result = add_comment_to_jira(
            issue_key=args.issue_key,
            body=args.body,
            visibility_role=args.visibility_role,
            timeout=max(args.timeout, 1),
        )
        comment_id = result.get("id", "unknown")
        print(
            f"[ok] comment created | issue={args.issue_key.strip().upper()} "
            f"| id={comment_id} | visibility_role={args.visibility_role}"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
