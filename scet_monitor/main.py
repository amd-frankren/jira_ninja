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

try:
    from mcp_client import (
        DEFAULT_MAX_TOKENS,
        DEFAULT_MODEL,
        ENV_LLM_GATEWAY_API_TOKEN,
        ENV_LLM_GATEWAY_API_URL,
        ENV_MCP_SERVER_URL_ATLASSIAN_INTERNAL,
        parse_header_items,
        run_chat_with_mcp,
    )
except Exception:
    DEFAULT_MAX_TOKENS = 4096
    DEFAULT_MODEL = ""
    ENV_LLM_GATEWAY_API_TOKEN = ""
    ENV_LLM_GATEWAY_API_URL = ""
    ENV_MCP_SERVER_URL_ATLASSIAN_INTERNAL = ""
    parse_header_items = None  # type: ignore
    run_chat_with_mcp = None  # type: ignore

try:
    from mcp import ClientSession
except ImportError:
    try:
        from mcp.client.session import ClientSession  # type: ignore
    except ImportError:
        ClientSession = None  # type: ignore

try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    try:
        from mcp.client.streamable_http import streamable_http_client as streamablehttp_client  # type: ignore
    except ImportError:
        streamablehttp_client = None  # type: ignore

# Poll interval for Jira monitor (seconds)
POLL_INTERVAL_SECONDS = 60
DEFAULT_EXPORT_DIR = "SCET_export_runtime"
DEFAULT_AI_ANSWER_DIR = "SCET_ai_answers"
# TARGET_SCP_IDS = ["SCP-835", "SCP-738", "SCP-865"]

TARGET_SCP_IDS = ["SCP-835"]
ENV_EXTERNAL_JIRA_URL = "EXTERNAL_JIRA_URL"
ENV_SCET_MCP_URL = "SCET_MCP_URL"
ENV_SCET_MCP_AUTH_TOKEN = "SCET_MCP_AUTH_TOKEN"
ENV_JIRA_INTERNAL_MCP_URL = "JIRA_INTERNAL_MCP_URL"

MCP_SERVERS: Dict[str, Dict[str, str]] = {
    "jira_external": {
        "url": os.getenv(ENV_SCET_MCP_URL, "http://127.0.0.1:8000/mcp"),
        "auth_token": os.getenv(ENV_SCET_MCP_AUTH_TOKEN, ""),
    },
    "jira_internal": {
        "url": os.getenv(ENV_JIRA_INTERNAL_MCP_URL, "http://127.0.0.1:8002/mcp"),
    },
}


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


def _extract_mcp_tools(list_tools_result: Any) -> List[Any]:
    tools = getattr(list_tools_result, "tools", None)
    if tools is None and isinstance(list_tools_result, dict):
        tools = list_tools_result.get("tools")
    if not tools:
        return []
    return list(tools)


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", "")).strip()
    return str(getattr(tool, "name", "")).strip()


def _tool_schema(tool: Any) -> Dict[str, Any]:
    if isinstance(tool, dict):
        schema = tool.get("inputSchema") or tool.get("input_schema")
    else:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}


def _mcp_result_to_text(result: Any) -> str:
    parts: List[str] = []

    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")

    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                txt = item.get("text")
                if txt:
                    parts.append(str(txt))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                txt = getattr(item, "text", None)
                if txt:
                    parts.append(str(txt))
                else:
                    parts.append(str(item))
    elif isinstance(content, str):
        parts.append(content)

    structured = getattr(result, "structuredContent", None)
    if structured is None and isinstance(result, dict):
        structured = result.get("structuredContent")
    if structured:
        parts.append(json.dumps(structured, ensure_ascii=False, indent=2))

    if parts:
        return "\n".join(parts).strip()

    try:
        if hasattr(result, "model_dump"):
            return json.dumps(result.model_dump(), ensure_ascii=False, indent=2)
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return str(result)


def _format_exception(exc: Exception) -> str:
    # Python 3.11+ ExceptionGroup (e.g., "unhandled errors in a TaskGroup")
    sub_excs = getattr(exc, "exceptions", None)
    if isinstance(sub_excs, (list, tuple)) and sub_excs:
        details = "; ".join(str(e) for e in sub_excs[:3])
        return f"{exc} | details: {details}"
    return str(exc)


def _pick_mcp_analysis_tool(tools: List[Any], server_name: str = "") -> Optional[Any]:
    """
    Pick analysis-oriented tool first.
    For jira_internal, avoid retrieval/watcher tools and only select analysis-like tools.
    """
    by_name: Dict[str, Any] = {}
    for t in tools:
        name = _tool_name(t)
        if name:
            by_name[name] = t

    preferred_order = [
        "analyze_ticket",
        "analyze_issue",
        "ticket_analysis",
        "issue_analysis",
        "rag_scet_qa",
        "ask_ticket",
        "ask_issue",
        "qa_ticket",
        "qa_issue",
    ]
    for name in preferred_order:
        if name in by_name:
            return by_name[name]

    # fuzzy prefer analysis-like names
    for t in tools:
        lname = _tool_name(t).lower()
        if any(k in lname for k in ("analy", "rag", "qa", "recommend", "suggest", "diagnos")):
            return t

    # jira_internal: do NOT fallback to generic issue/ticket tools (e.g. get_issue_watchers)
    if server_name == "jira_internal":
        return None

    # other servers: fallback to retrieval tools as last resort
    for name in ("jira_get_issue", "get_issue"):
        if name in by_name:
            return by_name[name]

    for t in tools:
        lname = _tool_name(t).lower()
        if "issue" in lname or "ticket" in lname:
            return t

    return None


def _build_tool_args(
    schema: Dict[str, Any],
    issue_key: str,
    ticket_text: str,
    text_only: bool = False,
) -> Dict[str, Any]:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        properties = {}

    args: Dict[str, Any] = {}
    for key in properties.keys():
        lk = str(key).lower()
        if (not text_only) and lk in {"issue_key", "issue", "key", "ticket_key", "ticket_id", "id"}:
            args[key] = issue_key
        elif lk in {"text", "content", "description", "prompt", "query", "ticket_text"}:
            args[key] = ticket_text

    if args:
        return args

    return {"text": ticket_text} if text_only else {"issue_key": issue_key}


async def _call_mcp_server_for_analysis(
    server_name: str,
    server_cfg: Dict[str, str],
    issue_key: str,
    ticket_text: str,
) -> str:
    mcp_url = (server_cfg.get("url") or "").strip()
    auth_token = (server_cfg.get("auth_token") or "").strip()
    if not mcp_url:
        raise RuntimeError(f"{server_name} MCP URL is empty")

    headers: Dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    transport_kwargs: Dict[str, Any] = {}
    if headers:
        transport_kwargs["headers"] = headers

    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        streams = await stack.enter_async_context(streamablehttp_client(mcp_url, **transport_kwargs))
        if not (isinstance(streams, (list, tuple)) and len(streams) >= 2):
            raise RuntimeError("Unexpected streamable-http transport return value")
        read_stream, write_stream = streams[0], streams[1]

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()

        list_tools_result = await session.list_tools()
        tools = _extract_mcp_tools(list_tools_result)
        if not tools:
            raise RuntimeError(f"No MCP tools available from {server_name}")

        picked_tool = _pick_mcp_analysis_tool(tools, server_name=server_name)
        if picked_tool is None:
            raise RuntimeError(f"No suitable MCP analysis tool found in {server_name}")

        name = _tool_name(picked_tool)
        schema = _tool_schema(picked_tool)

        # jira_internal: send prompt + ticket text for analysis, avoid key-based get_issue path
        text_only_mode = (server_name == "jira_internal")
        args = _build_tool_args(
            schema,
            issue_key=issue_key,
            ticket_text=ticket_text,
            text_only=text_only_mode,
        )

        try:
            result = await session.call_tool(name, arguments=args)
            return _mcp_result_to_text(result)
        except Exception:
            if text_only_mode:
                fallback_args_list = [
                    {"prompt": ticket_text},
                    {"query": ticket_text},
                    {"text": ticket_text},
                    {"description": ticket_text},
                    {"content": ticket_text},
                ]
            else:
                fallback_args_list = [
                    {"issue_key": issue_key},
                    {"key": issue_key},
                    {"issue": issue_key},
                    {"ticket_key": issue_key},
                    {"id": issue_key},
                    {"issue_key": issue_key, "text": ticket_text},
                ]
            last_exc: Optional[Exception] = None
            for fa in fallback_args_list:
                try:
                    result = await session.call_tool(name, arguments=fa)
                    return _mcp_result_to_text(result)
                except Exception as exc:
                    last_exc = exc
            raise RuntimeError(
                f"MCP tool '{name}' on {server_name} failed with all argument variants: {last_exc}"
            )


async def _analyze_ticket_via_single_mcp(
    server_name: str,
    issue_key: str,
    ticket_text: str,
) -> str:
    cfg = MCP_SERVERS.get(server_name)
    if not cfg:
        return "(error) server not configured"
    try:
        return await _call_mcp_server_for_analysis(
            server_name=server_name,
            server_cfg=cfg,
            issue_key=issue_key,
            ticket_text=ticket_text,
        )
    except Exception as exc:
        return f"(error) {_format_exception(exc)}"


def _run_jira_internal_chat_analysis(issue_key: str, ticket_text: str) -> str:
    """
    Use jira_internal with prompt + ticket text.
    Prefer mcp_client.run_chat_with_mcp when available; otherwise fallback to existing
    streamable MCP tool-call path in text-only mode (no issue-key lookup intent).
    """
    # Preferred path: chat-style MCP client (same as main_test.py pattern)
    if run_chat_with_mcp is not None:
        mcp_server_url = os.getenv(ENV_JIRA_INTERNAL_MCP_URL, "http://127.0.0.1:8002/mcp").strip()
        if not mcp_server_url:
            return "(error) Missing jira_internal MCP server URL"

        # jira_internal MCP server does not require client-side token/header.
        headers: Dict[str, str] = {}

        try:
            answer = asyncio.run(
                run_chat_with_mcp(
                    user_prompt=ticket_text,
                    mcp_server_url=mcp_server_url,
                    mcp_headers=headers,
                    model=DEFAULT_MODEL,
                    max_tokens=DEFAULT_MAX_TOKENS,
                    gateway_key=ENV_LLM_GATEWAY_API_TOKEN,
                    base_url=ENV_LLM_GATEWAY_API_URL,
                    llm_user=None,
                    max_tool_rounds=8,
                )
            )
            return str(answer).strip() or "(empty)"
        except Exception as exc:
            return f"(error) {exc}"

    # Fallback path: use existing MCP tool-call route for jira_internal with text prompt.
    cfg = MCP_SERVERS.get("jira_internal") or {}
    internal_url = (cfg.get("url") or "").strip()
    if not internal_url:
        return "(error) jira_internal server URL is empty (set JIRA_INTERNAL_MCP_URL)"

    try:
        return asyncio.run(
            _analyze_ticket_via_single_mcp(
                server_name="jira_internal",
                issue_key=issue_key,
                ticket_text=ticket_text,
            )
        )
    except Exception as exc:
        return f"(error) {_format_exception(exc)}"


def _run_single_server_analysis_via_mcp(server_name: str, issue_key: str, ticket_text: str) -> str:
    # jira_internal must use chat prompt + ticket text, not issue-key lookup
    if server_name == "jira_internal":
        return _run_jira_internal_chat_analysis(issue_key=issue_key, ticket_text=ticket_text)

    try:
        return asyncio.run(
            _analyze_ticket_via_single_mcp(
                server_name=server_name,
                issue_key=issue_key,
                ticket_text=ticket_text,
            )
        )
    except Exception as exc:
        return f"(error) MCP analysis failed: {exc}"


def _build_external_mcp_analysis_prompt(issue_key: str, ticket_text: str) -> str:
    """Prompt for jira_external MCP (SCET-focused)."""
    return (
        "你是资深 Jira 工单分析助手（external/scet 视角）。请基于以下 ticket 内容输出中文分析。\n"
        "请包含：\n"
        "1) 原因分析\n"
        "2) 下一步建议（按优先级）\n"
        "3) 相似 SCET ticket Top10（每条给出 key + 相似原因 + 借鉴点）\n"
        "【重要】不要返回原始 JSON 字段转储，只输出可读分析。\n\n"
        f"Issue Key: {issue_key}\n"
        "Ticket Content:\n"
        f"{ticket_text}\n"
    )


def _build_internal_mcp_analysis_prompt(issue_key: str, ticket_text: str) -> str:
    """Prompt for jira_internal MCP (internal atlassian/PLAT/Confluence-focused)."""
    return (
        "你是资深 Jira 工单分析助手（internal 视角）。请基于以下 ticket 内容输出中文分析。\n"
        "请包含：\n"
        "1) 原因分析\n"
        "2) 下一步建议（按优先级）\n"
        "3) 最相关 internal ticket / 文档线索 Top10（每条给出 key 或链接 + 相似原因 + 借鉴点）\n"
        "【重要】不要返回原始 JSON 字段转储，只输出可读分析。\n\n"
        f"Issue Key: {issue_key}\n"
        "Ticket Content:\n"
        f"{ticket_text}\n"
    )


def _looks_like_raw_ticket_dump(text: str) -> bool:
    """Detect MCP returning raw issue payload instead of analysis."""
    if not text:
        return True

    stripped = text.strip()
    # quick path for obvious raw payload
    if '"summary"' in stripped and '"description"' in stripped and '"comments"' in stripped:
        return True

    candidate = stripped
    try:
        obj = json.loads(candidate)
    except Exception:
        return False

    if isinstance(obj, dict):
        # direct issue payload
        if {"id", "key", "summary"}.issubset(set(obj.keys())):
            return True
        # wrapped payload
        nested = obj.get("result")
        if isinstance(nested, str):
            try:
                nested_obj = json.loads(nested)
                if isinstance(nested_obj, dict) and {"id", "key", "summary"}.issubset(set(nested_obj.keys())):
                    return True
            except Exception:
                pass

    return False


def _has_dual_server_sections(text: str) -> bool:
    if not text:
        return False
    return ("【Answer 1 - jira_external】" in text) and ("【Answer 2 - jira_internal】" in text)


def _build_local_fallback_analysis(issue_key: str, ticket_text: str) -> str:
    """
    Build a local readable analysis when MCP returns raw payload only.
    This guarantees stable output format for debug and Jira comment draft.
    """
    return (
        "【原因分析】\n"
        f"- 当前 MCP 返回结果主要是工单原始字段而非分析结论（issue={issue_key}）。\n"
        "- 从 ticket 文本看，问题集中在 RO off 场景下 PCIe 带宽受限，可能与流控更新节奏/credit 消耗相关。\n"
        "- 现有讨论提到 C&R patch 可显著改善带宽，但引入了读延迟上升风险，需要分场景验证。\n\n"
        "【下一步建议（优先级）】\n"
        "1. 先固定测试矩阵：平台版本、BIOS/patch 版本、RO on/off、流量模型，统一复现实验口径。\n"
        "2. 同步采集证据：PCIe trace、PM log、关键寄存器快照，分别对比 patch 前后。\n"
        "3. 将“带宽提升”与“延迟变差”拆成两个子问题，分别定义准出标准。\n"
        "4. 明确 owner 与时间点：补丁版本、正式发布计划、A0/B0 适配差异说明。\n\n"
        "【相似度最高的10条 ticket 供参考】\n"
        "- 当前 MCP 响应未提供可检索的相似 ticket 列表能力，暂无法给出真实 Top10。\n"
        "- 建议在 MCP 服务端增加相似检索接口（embedding/全文检索），并返回 key + 相似度 + 摘要。\n"
        "- 临时占位（待服务端返回真实数据后替换）：\n"
        "  1) N/A  2) N/A  3) N/A  4) N/A  5) N/A\n"
        "  6) N/A  7) N/A  8) N/A  9) N/A  10) N/A\n\n"
        "【原始输入摘要】\n"
        f"{ticket_text[:1200]}{'...' if len(ticket_text) > 1200 else ''}"
    )


def _write_ticket_answer_to_file(
    issue_key: str,
    final_answer: str,
    output_dir: str = DEFAULT_AI_ANSWER_DIR,
) -> str:
    """Write final answer to per-ticket file under a unified output directory."""
    os.makedirs(output_dir, exist_ok=True)
    safe_issue_key = re.sub(r"[^A-Za-z0-9._-]", "_", (issue_key or "unknown").strip())
    file_path = os.path.join(output_dir, f"{safe_issue_key}.txt")

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    content = (
        f"Issue Key: {issue_key}\n"
        f"Generated At: {timestamp}\n"
        f"{'-' * 40}\n"
        f"{final_answer}\n"
    )

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return file_path


def _analyze_and_print_ticket_result(issue_key: str, ticket_text: str) -> str:
    """Run two MCP servers with independent prompts and print combined output."""
    external_prompt = _build_external_mcp_analysis_prompt(
        issue_key=issue_key,
        ticket_text=ticket_text,
    )
    internal_prompt = _build_internal_mcp_analysis_prompt(
        issue_key=issue_key,
        ticket_text=ticket_text,
    )

    external_answer = _run_single_server_analysis_via_mcp(
        server_name="jira_external",
        issue_key=issue_key,
        ticket_text=external_prompt,
    )
    internal_answer = _run_single_server_analysis_via_mcp(
        server_name="jira_internal",
        issue_key=issue_key,
        ticket_text=internal_prompt,
    )

    # per-server retry/fallback to avoid raw dump
    if _looks_like_raw_ticket_dump(external_answer):
        external_retry = _run_single_server_analysis_via_mcp(
            server_name="jira_external",
            issue_key=issue_key,
            ticket_text=external_prompt + "\n\n再次强调：禁止返回原始 JSON，只输出分析结论。",
        )
        external_answer = (
            external_retry
            if not _looks_like_raw_ticket_dump(external_retry)
            else _build_local_fallback_analysis(issue_key=issue_key, ticket_text=ticket_text)
        )

    if _looks_like_raw_ticket_dump(internal_answer):
        internal_retry = _run_single_server_analysis_via_mcp(
            server_name="jira_internal",
            issue_key=issue_key,
            ticket_text=internal_prompt + "\n\n再次强调：禁止返回原始 JSON，只输出分析结论。",
        )
        internal_answer = (
            internal_retry
            if not _looks_like_raw_ticket_dump(internal_retry)
            else _build_local_fallback_analysis(issue_key=issue_key, ticket_text=ticket_text)
        )

    analysis_result = (
        "【Answer 1 - jira_external】\n"
        f"{external_answer}\n\n"
        "【Answer 2 - jira_internal】\n"
        f"{internal_answer}"
    )

    # Add required notice at the top and reminder link at the bottom
    top_notice = "以下 comment 为 AI 生成内容，仅供参考。"
    bottom_notice = "如需从 AI 获取更多关于此 ticket 的信息，请访问：http://127.0.0.1:8090/"

    decorated_analysis_result = f"{top_notice}\n\n{analysis_result}\n\n{bottom_notice}"
    print(f"[alert][mcp-analysis] {issue_key}:\n{decorated_analysis_result}\n")

    try:
        answer_file = _write_ticket_answer_to_file(
            issue_key=issue_key,
            final_answer=decorated_analysis_result,
        )
        print(f"[info] AI final answer saved: {answer_file}")
    except Exception as file_exc:
        print(
            f"[warn] Failed to save AI final answer for {issue_key}: {file_exc}",
            file=sys.stderr,
        )

    return decorated_analysis_result


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
                                final_answer = _analyze_and_print_ticket_result(
                                    issue_key=issue_key,
                                    ticket_text=ticket_text,
                                )

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
