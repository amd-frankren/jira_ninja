#!/usr/bin/env python3
"""
Main entrypoint:
- Monitor Jira SCET project changes.
- When created/updated Jira tickets are detected, export each ticket to JSON.


python main.py --interval 60 --since-minutes 0 --debug-skip-target-scp-check --debug-treat-updated-as-created


python main.py --interval 60 --since-minutes 60 --debug-skip-target-scp-check --debug-enable-add-comment


"""

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from jira_scet_monitor import JiraScetMonitor
from jira_add_comment import add_comment_to_jira


from jira_export_external_scet import export_issue_to_file



# Poll interval for Jira monitor (seconds)
POLL_INTERVAL_SECONDS = 60
DEFAULT_EXPORT_DIR = "SCET_export_runtime"
DEFAULT_AI_ANSWER_DIR = "SCET_ai_answers"
# TARGET_SCP_IDS = ["SCP-835", "SCP-738", "SCP-865"]

TARGET_SCP_IDS = ["SCP-835"]
ENV_EXTERNAL_JIRA_URL = "EXTERNAL_JIRA_URL"


def _exported_file_contains_target_scp(exported_file: str, targets: list[str] = TARGET_SCP_IDS) -> bool:
    """Match any target SCP string directly in downloaded JSON file text."""
    try:
        # Open exported file for text read
        with open(exported_file, "r", encoding="utf-8") as f:
            # Read all text content
            content = f.read()
    except Exception:
        return False

    upper_content = content.upper()

    ticket_label = os.path.splitext(os.path.basename(exported_file))[0].upper()
    # Extract all SCP IDs that appear in current ticket content (print even if no target matched)
    ticket_scp_ids = sorted(set(re.findall(r"SCP-\d+", upper_content)))
    print(f"[info] Current ticket SCP ID in {ticket_label} is: {ticket_scp_ids if ticket_scp_ids else 'None'}")

    # Case-insensitive substring match against target list
    matched_scp_ids = [t.strip().upper() for t in targets if t.strip() and t.strip().upper() in upper_content]
    print(f"[info] Matched target SCP IDs: {matched_scp_ids if matched_scp_ids else 'None'}")

    return len(matched_scp_ids) > 0


def _json_to_text(value) -> str:
    """Flatten Jira-like JSON field value into plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)

    if isinstance(value, dict):
        parts = []
        for key in ("text", "value", "content"):
            if key in value:
                # Recursively extract nested text candidate
                extracted = _json_to_text(value.get(key))
                if extracted:
                    parts.append(extracted)

        if not parts:
            for v in value.values():
                # Recursively extract nested dict values
                extracted = _json_to_text(v)
                if extracted:
                    parts.append(extracted)

        # Join extracted pieces with line breaks
        return "\n".join([p for p in parts if p]).strip()

    if isinstance(value, list):
        parts = []
        for item in value:
            # Recursively extract list item text
            extracted = _json_to_text(item)
            if extracted:
                parts.append(extracted)
        # Join extracted list text with line breaks
        return "\n".join(parts).strip()

    return ""


def _html_to_plain_text(text: str) -> str:
    """Convert HTML content to readable plain text."""
    if not text:
        return ""

    # Replace <br> with newline
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    # Replace block closing tags with newline
    text = re.sub(r"(?i)</\s*(div|p|h1|h2|h3|h4|h5|h6|li|tr|section)\s*>", "\n", text)
    # Replace list item opening with bullet prefix
    text = re.sub(r"(?i)<\s*li[^>]*>", "- ", text)

    # Remove remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)

    # Decode HTML entities
    text = html.unescape(text)

    # Normalize carriage returns
    text = text.replace("\r", "")
    # Normalize non-breaking spaces
    text = text.replace("\u00a0", " ")
    # Split lines and trim each line
    lines = [ln.strip() for ln in text.split("\n")]
    # Join non-empty lines
    cleaned = "\n".join(ln for ln in lines if ln)
    return cleaned.strip()


def _clean_description_noise(description: str) -> str:
    """
    Remove known template/placeholder sections that are not useful for analysis.
    """
    if not description:
        return ""

    invalid_headers = {
        "upload log(s):",
        "failure rate:",
        "mtbf:",
        "system config and peripheral device information:",
        "additional information",
    }
    valid_resume_headers = {
        "issue description:",
    }

    cleaned_lines: list[str] = []
    skipping = False

    # Split description into lines
    for raw_line in description.split("\n"):
        # Trim current line
        line = raw_line.strip()
        # Normalize line to lowercase
        lower = line.lower()

        if lower in invalid_headers:
            skipping = True
            continue

        if skipping:
            if lower in valid_resume_headers:
                skipping = False
                cleaned_lines.append(line)
            continue

        # Skip placeholder prompt lines
        if re.match(r"^\(please .*?\)$", line, flags=re.IGNORECASE):
            continue

        cleaned_lines.append(line)

    compact: list[str] = []
    prev_blank = False
    for line in cleaned_lines:
        blank = (line == "")
        if blank and prev_blank:
            continue
        compact.append(line)
        prev_blank = blank

    # Join compacted lines
    return "\n".join(compact).strip()


def _extract_ticket_title_description(exported_file: str) -> tuple[str, str]:
    """Read exported JSON and extract title(summary) + description."""
    try:
        # Open exported JSON file
        with open(exported_file, "r", encoding="utf-8") as f:
            # Parse JSON content
            data = json.load(f)
    except Exception:
        return "", ""

    if not isinstance(data, dict):
        return "", ""

    issue = data.get("issue", {}) if isinstance(data.get("issue"), dict) else {}
    issue_fields = issue.get("fields", {}) if isinstance(issue.get("fields"), dict) else {}
    versioned = (
        issue.get("versionedRepresentations", {})
        if isinstance(issue.get("versionedRepresentations"), dict)
        else {}
    )

    def _first_non_empty(*candidates) -> str:
        for v in candidates:
            # Convert any candidate into text
            text = _json_to_text(v)
            if text:
                return text
        return ""

    summary_vr = ""
    if isinstance(versioned.get("summary"), dict):
        # Extract summary from versioned field
        summary_vr = _json_to_text(versioned["summary"].get("1"))

    description_vr = ""
    if isinstance(versioned.get("description"), dict):
        # Extract description from versioned field
        description_vr = _json_to_text(versioned["description"].get("1"))

    # Pick first non-empty title candidate
    title = _first_non_empty(
        issue_fields.get("summary"),
        summary_vr,
        issue_fields.get("customfield_11801"),  # Ticket Summary
        data.get("summary"),
        data.get("title"),
    )
    # Pick first non-empty description candidate
    description = _first_non_empty(
        issue_fields.get("description"),
        description_vr,
        issue_fields.get("customfield_11900"),  # Ticket Description
        data.get("description"),
    )

    # Convert title HTML to plain text
    clean_title = _html_to_plain_text(title)
    # Convert and clean description HTML/text noise
    clean_description = _clean_description_noise(_html_to_plain_text(description))
    return clean_title, clean_description


def parse_args() -> argparse.Namespace:
    # Build CLI argument parser
    parser = argparse.ArgumentParser(
        description="Monitor SCET Jira changes and export changed ticket content."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f"Polling interval in seconds (default: {POLL_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_EXPORT_DIR,
        help=f"Local export directory for ticket JSON files (default: {DEFAULT_EXPORT_DIR})",
    )
    parser.add_argument(
        "--since-minutes",
        type=int,
        default=0,
        help="Initial look-back window in minutes for monitor mode (default: 0).",
    )
    parser.add_argument(
        "--debug-enable-add-comment",
        action="store_true",
        help="Debug option: enable posting internal comment to Jira (default: disabled).",
    )
    parser.add_argument(
        "--debug-skip-target-scp-check",
        action="store_true",
        help="Debug option: skip TARGET_SCP_IDS check and treat every ticket as SCP-matched.",
    )
    parser.add_argument(
        "--debug-treat-updated-as-created",
        action="store_true",
        help="Debug option: treat UPDATED events as CREATED events.",
    )
    # Parse CLI arguments
    return parser.parse_args()


def main() -> int:
    # Parse runtime arguments
    args = parse_args()

    try:
        # ===== Monitor mode =====
        # Clamp poll interval to at least 1 second
        poll_interval_seconds = max(args.interval, 1)
        # Initialize Jira monitor
        monitor = JiraScetMonitor(
            poll_interval_seconds=poll_interval_seconds,
        )

        if args.since_minutes > 0:
            # Import datetime tools for checkpoint rollback
            from datetime import datetime, timedelta, timezone

            # Roll back monitor checkpoint for look-back window
            monitor.last_checked = datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes)

        # Print export directory info
        print(f"[info] Export output dir: {args.output_dir}")

        while True:
            # Poll Jira change events
            events = monitor.poll_changes()
            if events:
                # Print number of detected changes
                print(f"[info] Detected {len(events)} Jira change(s), export for each ticket.")
                for ev in events:
                    # Read issue key from event
                    issue_key = ev.get("issue_key", "").strip()
                    # Read summary from event
                    summary = ev.get("summary", "").strip()
                    # Read event type from event
                    event_type = ev.get("event_type", "updated").strip()

                    if args.debug_treat_updated_as_created and event_type.lower() == "updated":
                        print("[info] Debug enabled: treat UPDATED event as CREATED.")
                        event_type = "created"

                    # Read issue URL from event
                    issue_url = ev.get("issue_url", "").strip()

                    # Print event summary line
                    print(f"[event] {event_type.upper()} {issue_key} - {summary} | {issue_url}")

                    if not issue_key:
                        # Print warning for invalid event data
                        print("[warn] Empty issue_key in event, skipped.", file=sys.stderr)
                        continue

                    try:
                        # Export issue JSON file to local directory
                        exported_file = export_issue_to_file(
                            issue_key=issue_key,
                            output_dir=args.output_dir,
                        )
                        # Print exported file path
                        print(f"[info] Exported: {exported_file}")

                        if args.debug_skip_target_scp_check:
                            print("[info] Debug skip enabled: bypass TARGET_SCP_IDS check for this ticket.")

                        if (
                            event_type.lower() == "created"
                            and (
                                args.debug_skip_target_scp_check
                                or _exported_file_contains_target_scp(exported_file)
                            )
                        ):
                            # Extract ticket title and description from exported JSON
                            ticket_title, ticket_desc = _extract_ticket_title_description(exported_file)
                            # Print SCP alert
                            print(
                                f"[alert] Found one of target SCP IDs {TARGET_SCP_IDS} in exported JSON | "
                                f"issue={issue_key} | event={event_type.upper()}"
                            )
                            ticket_text = (
                                f"Title: {ticket_title or '(empty)'}\n"
                                f"Description: {ticket_desc or '(empty)'}"
                            )

                            # Print extracted ticket text
                            print(f"[alert] Ticket text:\n{ticket_text}")

                            try:
                               
                                final_answer = "xxx"
                                if args.debug_enable_add_comment:
                                    try:
                                        # Post internal comment to Jira
                                        add_comment_to_jira(
                                            issue_key=issue_key,
                                            body=final_answer,
                                        )
                                        # Print comment success
                                        print(f"[info] Internal AI comment posted to {issue_key}.")
                                    except Exception as comment_exc:
                                        # Print comment failure warning
                                        print(
                                            f"[warn] Failed to post internal AI comment for {issue_key}: {comment_exc}",
                                            file=sys.stderr,
                                        )
                                else:
                                    # Print skip message when comment posting is disabled
                                    print("[info] Skip posting internal AI comment (debug flag not enabled).")
                            except Exception as rag_exc:
                                # Print RAG failure warning
                                print(
                                    f"[warn] rag_scet_qa call failed for {issue_key}: {rag_exc}",
                                    file=sys.stderr,
                                )

                    except Exception as ticket_exc:
                        # Print per-ticket failure
                        print(
                            f"[error] Failed processing ticket {issue_key}: {ticket_exc}",
                            file=sys.stderr,
                        )
            else:
                # Print no-change message
                print("[info] No Jira changes.")

            # Sleep until next poll cycle
            time.sleep(poll_interval_seconds)

    except KeyboardInterrupt:
        # Print exit info on Ctrl+C
        print("\n[info] Exiting by user interrupt.")
        return 0
    except Exception as exc:
        # Print fatal error
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Run program entrypoint
    raise SystemExit(main())
