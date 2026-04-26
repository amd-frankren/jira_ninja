#!/usr/bin/env python3
"""
AI-driven ticket classification + owner routing helper.

Design:
1) Read SCP/member mapping from scp_member_mapping.json
2) Let AI infer the best category/module from ticket text
3) Let AI pick owner email(s) from mapped members under matched SCP ID
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from mcp_qa import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, create_llm_client


def extract_scp_ids_from_text(text: str) -> List[str]:
    if not text:
        return []
    return sorted(set(re.findall(r"SCP-\d+", text.upper())))


def _parse_json_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def _load_mapping(mapping_file: str) -> Dict[str, Any]:
    with open(mapping_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError("scp_member_mapping.json must be a JSON object.")
    return data


def _collect_candidate_categories(mapping: Dict[str, Any], scp_ids: List[str]) -> List[str]:
    categories: List[str] = []
    target_scp_ids = scp_ids[:] if scp_ids else list(mapping.keys())

    for sid in target_scp_ids:
        entry = mapping.get(sid, {})
        if not isinstance(entry, dict):
            continue
        members = entry.get("members", [])
        if not isinstance(members, list):
            continue

        for m in members:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "")).strip()
            if role and role not in categories:
                categories.append(role)

            responsibilities = m.get("responsibilities", [])
            if isinstance(responsibilities, list):
                for r in responsibilities:
                    rv = str(r).strip()
                    if rv and rv not in categories:
                        categories.append(rv)

    if "Others" not in categories:
        categories.append("Others")
    return categories


def _build_member_pool(mapping: Dict[str, Any], scp_ids: List[str]) -> List[Dict[str, Any]]:
    pool: List[Dict[str, Any]] = []
    target_scp_ids = scp_ids[:] if scp_ids else list(mapping.keys())

    for sid in target_scp_ids:
        entry = mapping.get(sid, {})
        if not isinstance(entry, dict):
            continue
        members = entry.get("members", [])
        if not isinstance(members, list):
            continue

        for m in members:
            if not isinstance(m, dict):
                continue
            email = str(m.get("email", "")).strip()
            if not email:
                continue
            pool.append(
                {
                    "scp_id": sid,
                    "email": email,
                    "role": str(m.get("role", "")).strip(),
                    "responsibilities": [str(x).strip() for x in m.get("responsibilities", []) if str(x).strip()],
                }
            )
    return pool


def _normalize_selected_owner(selected_owner_email: str, member_pool: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    target = (selected_owner_email or "").strip().lower()
    if not target:
        return []

    for m in member_pool:
        email = str(m.get("email", "")).strip()
        if not email:
            continue
        if email.lower() == target:
            return [
                {
                    "email": email,
                    "role": str(m.get("role", "")).strip(),
                    "scp_id": str(m.get("scp_id", "")).strip(),
                }
            ]
    return []


async def _ai_classify_and_route_async(
    ticket_text: str,
    mapping: Dict[str, Any],
    scp_ids: List[str],
) -> Dict[str, Any]:
    categories = _collect_candidate_categories(mapping, scp_ids)
    member_pool = _build_member_pool(mapping, scp_ids)

    if not member_pool:
        return {
            "category": "Others",
            "reason": "No member candidates found in mapping for provided SCP IDs.",
            "matched_scp_id": "",
            "owners": [],
            "raw_ai": {},
        }

    llm = create_llm_client()

    prompt = (
        "You are an issue router.\n"
        "Task:\n"
        "1) Infer the most suitable module/category from ticket text.\n"
        "2) Select ONE most relevant owner email from the provided member pool.\n"
        "3) Prefer SCP IDs explicitly present in ticket context.\n\n"
        "Output STRICT JSON only with keys:\n"
        "{"
        "\"category\":\"...\","
        "\"matched_scp_id\":\"SCP-xxx or empty\","
        "\"owner_email\":\"a@amd.com or empty string\","
        "\"reason\":\"short explanation\""
        "}\n"
        "Return exactly one owner_email (or empty string if no suitable owner)."
    )

    user_payload = {
        "ticket_text": ticket_text,
        "scp_ids_in_ticket": scp_ids,
        "candidate_categories": categories,
        "member_pool": member_pool,
    }

    completion = await llm.chat.completions.create(
        model=DEFAULT_MODEL,
        max_tokens=DEFAULT_MAX_TOKENS,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    )

    text = completion.choices[0].message.content or ""
    parsed = _parse_json_text(text) or {}
    owner_email = str(parsed.get("owner_email", "")).strip()

    if not owner_email:
        owner_emails = parsed.get("owner_emails", [])
        if isinstance(owner_emails, list) and owner_emails:
            owner_email = str(owner_emails[0]).strip()

    owners = _normalize_selected_owner(owner_email, member_pool)

    matched_scp_id = str(parsed.get("matched_scp_id", "")).strip()
    if not matched_scp_id and owners:
        matched_scp_id = owners[0].get("scp_id", "")

    return {
        "category": str(parsed.get("category", "Others")).strip() or "Others",
        "reason": str(parsed.get("reason", "")).strip(),
        "matched_scp_id": matched_scp_id,
        "owners": owners,
        "raw_ai": parsed,
    }


def classify_and_route_ticket(
    ticket_text: str,
    mapping_file: str | None = None,
    scp_ids: List[str] | None = None,
) -> Dict[str, Any]:
    if not mapping_file:
        mapping_file = os.path.join(os.path.dirname(__file__), "scp_member_mapping.json")

    mapping = _load_mapping(mapping_file)
    found_scp_ids = scp_ids[:] if scp_ids else extract_scp_ids_from_text(ticket_text)

    result = asyncio.run(
        _ai_classify_and_route_async(
            ticket_text=ticket_text,
            mapping=mapping,
            scp_ids=found_scp_ids,
        )
    )

    return {
        "category": result.get("category", "Others"),
        "reason": result.get("reason", ""),
        "scp_ids": found_scp_ids,
        "routed_scp_id": result.get("matched_scp_id", ""),
        "owners": result.get("owners", []),
        "mapping_file": mapping_file,
        "raw_ai": result.get("raw_ai", {}),
    }
