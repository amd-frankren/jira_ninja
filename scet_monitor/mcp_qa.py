#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reusable MCP QA module extracted from web_workspace/server.py MCP answering flow.

Features:
1) Can be imported and called by other modules:
   - ask_mcp_qa_async(...)
   - ask_mcp_qa(...)
2) Can be used directly from CLI:
   - python scet_monitor/mcp_qa.py --ticket-url "https://ontrack.amd.com/browse/SCET-28231"
   - python scet_monitor/mcp_qa.py --question "请分析这个问题描述：..."

Input supports either:
- ticket url
- free-form question
(or both)

Notes:
- Requires env vars:
  - LLM_GATEWAY_API_URL
  - LLM_GATEWAY_API_TOKEN
- Optional:
  - WEBUI_LLM_MODEL
  - WEBUI_LLM_MAX_TOKENS
  - SCET_MCP_URL
  - SCET_MCP_AUTH_TOKEN
  - JIRA_INTERNAL_MCP_URL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

try:
    from mcp import ClientSession
except ImportError:
    from mcp.client.session import ClientSession  # type: ignore

try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client  # type: ignore


DEFAULT_MODEL = os.getenv("WEBUI_LLM_MODEL", "gpt-5-mini")
DEFAULT_MAX_TOKENS = int(os.getenv("WEBUI_LLM_MAX_TOKENS", "1200"))

MCP_SERVERS: Dict[str, Dict[str, Any]] = {
    "jira_external": {
        "url": os.getenv("SCET_MCP_URL", "http://127.0.0.1:8000/mcp"),
        "auth_token": os.getenv("SCET_MCP_AUTH_TOKEN", ""),
    },
    "jira_internal": {
        "url": os.getenv("JIRA_INTERNAL_MCP_URL", "http://127.0.0.1:8002/mcp"),
    },
}

SYSTEM_PROMPT = """你是一个 Jira Ticket 问答助手，可调用 MCP 工具。
你可以使用来自 jira_external、jira_internal 两个 MCP server 的工具。

规则：
1) 优先通过工具获取事实，不要编造。
2) 回答中给出关键信息和依据。
3) 如果用户问题不充分，或者你需要用户先做选择，请返回**严格 JSON**（不要额外文本）：
{"type":"ask_user","question":"你的追问","options":["可选项1","可选项2"]}
4) 如果不需要追问，直接给出最终回答（普通文本）。
"""


@dataclass
class SessionState:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    pending_question: Optional[Dict[str, Any]] = None


@dataclass
class QAResult:
    session_id: str
    answer: str
    pending_question: Optional[Dict[str, Any]]
    used_servers: List[str]
    issue_key: str
    ticket_url: str
    input_message: str


SESSIONS: Dict[str, SessionState] = {}


def resolve_user_name(user_name: Optional[str]) -> str:
    user = user_name or os.getenv("LLM_USER") or os.getenv("USER") or os.getenv("USERNAME")
    if user:
        return user
    try:
        return os.getlogin()
    except Exception:
        return "unknown-user"


def create_llm_client() -> AsyncOpenAI:
    base_url = os.getenv("LLM_GATEWAY_API_URL", "").strip()
    key = os.getenv("LLM_GATEWAY_API_TOKEN", "").strip()
    if not base_url:
        raise RuntimeError("Missing environment variable: LLM_GATEWAY_API_URL")
    if not key:
        raise RuntimeError("Missing environment variable: LLM_GATEWAY_API_TOKEN")

    user = resolve_user_name(None)
    return AsyncOpenAI(
        base_url=base_url,
        api_key="dummy",
        default_headers={
            "Ocp-Apim-Subscription-Key": key,
            "user": user,
        },
    )


def parse_json_text(text: str) -> Optional[Dict[str, Any]]:
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


def maybe_extract_ask_user(text: str) -> Optional[Dict[str, Any]]:
    data = parse_json_text(text)
    if not data:
        return None
    if data.get("type") != "ask_user":
        return None

    question = str(data.get("question", "")).strip()
    if not question:
        return None

    options = data.get("options", [])
    if not isinstance(options, list):
        options = []
    options = [str(x) for x in options if str(x).strip()]

    return {"question": question, "options": options}


def normalize_transport_streams(streams: Any) -> Tuple[Any, Any]:
    if isinstance(streams, (list, tuple)) and len(streams) >= 2:
        return streams[0], streams[1]
    raise RuntimeError("Unexpected streamable-http transport return value")


def extract_mcp_tool_list(list_tools_result: Any) -> List[Any]:
    tools = getattr(list_tools_result, "tools", None)
    if tools is None and isinstance(list_tools_result, dict):
        tools = list_tools_result.get("tools")
    if not tools:
        return []
    return list(tools)


def mcp_result_to_text(result: Any) -> str:
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
                    try:
                        parts.append(json.dumps(item.__dict__, ensure_ascii=False))
                    except Exception:
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


def to_openai_tool_schemas(all_tools: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    schemas: List[Dict[str, Any]] = []
    for server_name, tools in all_tools.items():
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("name", "")
                desc = tool.get("description", "")
                input_schema = tool.get("inputSchema") or tool.get("input_schema")
            else:
                name = getattr(tool, "name", "")
                desc = getattr(tool, "description", "")
                input_schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)

            if not name:
                continue
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}

            function_name = f"{server_name}__{name}"
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "description": f"[{server_name}] {desc or name}",
                        "parameters": input_schema,
                    },
                }
            )
    return schemas


def split_prefixed_tool_name(full_name: str) -> Tuple[str, str]:
    if "__" not in full_name:
        raise ValueError(f"Invalid tool name format: {full_name}")
    server_name, tool_name = full_name.split("__", 1)
    if server_name not in MCP_SERVERS:
        raise ValueError(f"Unknown server: {server_name}")
    if not tool_name:
        raise ValueError("Empty tool name")
    return server_name, tool_name


def tool_name_set(tools: List[Any]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        if isinstance(tool, dict):
            name = str(tool.get("name", "")).strip()
        else:
            name = str(getattr(tool, "name", "")).strip()
        if name:
            names.add(name)
    return names


def is_issue_fetch_tool(tool_name: str) -> bool:
    t = (tool_name or "").strip().lower()
    return t in {"get_issue", "jira_get_issue"}


def resolve_issue_tool_name_for_server(requested_tool_name: str, server_tool_names: set[str]) -> Optional[str]:
    if requested_tool_name in server_tool_names:
        return requested_tool_name
    if requested_tool_name == "get_issue" and "jira_get_issue" in server_tool_names:
        return "jira_get_issue"
    if requested_tool_name == "jira_get_issue" and "get_issue" in server_tool_names:
        return "get_issue"
    return None


def detect_server_hints(user_message: str) -> List[str]:
    text = (user_message or "").lower()
    picked: List[str] = []

    if "scet" in text or re.search(r"\bscet-\d+\b", text):
        picked.append("jira_external")
    if "internal" in text or "内网" in text or "plat" in text:
        picked.append("jira_internal")

    uniq: List[str] = []
    seen = set()
    for x in picked:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq


def extract_issue_key(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\b([A-Z][A-Z0-9_]+-\d+)\b", text.upper())
    return m.group(1) if m else ""


def build_input_message(ticket_url: str = "", question: str = "") -> Tuple[str, str]:
    turl = (ticket_url or "").strip()
    q = (question or "").strip()
    issue_key = extract_issue_key(f"{turl}\n{q}")

    if q and turl:
        return f"{q}\n\nTicket URL: {turl}", issue_key
    if q:
        return q, issue_key
    if turl:
        if issue_key:
            return f"请分析 ticket {issue_key} 的问题原因、依据和下一步建议。Ticket URL: {turl}", issue_key
        return f"请分析这个 ticket 链接中的问题并给出原因、依据和下一步建议：{turl}", issue_key

    raise ValueError("ticket_url 与 question 不能同时为空")


async def ask_mcp_qa_async(
    question: str = "",
    ticket_url: str = "",
    session_id: Optional[str] = None,
    max_rounds: int = 8,
) -> QAResult:
    sid = session_id or str(uuid.uuid4())
    user_message, issue_key = build_input_message(ticket_url=ticket_url, question=question)

    state = SESSIONS.setdefault(sid, SessionState())
    if state.pending_question:
        pending_q = state.pending_question.get("question", "")
        state.messages.append({"role": "assistant", "content": f"补充提问：{pending_q}"})
        state.pending_question = None

    state.messages.append({"role": "user", "content": user_message})
    llm_client = create_llm_client()

    async with AsyncExitStack() as stack:
        mcp_sessions: Dict[str, ClientSession] = {}
        all_tools: Dict[str, List[Any]] = {}

        for server_name, cfg in MCP_SERVERS.items():
            headers: Dict[str, str] = {}
            token = (cfg.get("auth_token") or "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"

            transport_kwargs: Dict[str, Any] = {}
            if headers:
                transport_kwargs["headers"] = headers

            streams = await stack.enter_async_context(streamablehttp_client(cfg["url"], **transport_kwargs))
            read_stream, write_stream = normalize_transport_streams(streams)

            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()

            list_tools_result = await session.list_tools()
            tools = extract_mcp_tool_list(list_tools_result)

            mcp_sessions[server_name] = session
            all_tools[server_name] = tools

        total_tool_count = sum(len(v) for v in all_tools.values())
        selected_servers = list(all_tools.keys())

        if total_tool_count > 128:
            hinted_servers = [s for s in detect_server_hints(user_message) if s in all_tools]
            if hinted_servers:
                selected_servers = hinted_servers
            else:
                ask_user = {
                    "question": "当前可用工具较多，请先选择要查询的数据源",
                    "options": ["jira_external", "jira_internal"],
                }
                state.pending_question = ask_user
                state.messages.append({"role": "assistant", "content": f"需要你补充信息：{ask_user['question']}"})
                return QAResult(
                    session_id=sid,
                    answer="",
                    pending_question=ask_user,
                    used_servers=selected_servers,
                    issue_key=issue_key,
                    ticket_url=ticket_url.strip(),
                    input_message=user_message,
                )

        chosen_tools = {k: all_tools[k] for k in selected_servers if k in all_tools}
        server_tool_names: Dict[str, set[str]] = {k: tool_name_set(v) for k, v in all_tools.items()}
        openai_tools = to_openai_tool_schemas(chosen_tools)
        messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}] + state.messages[:]

        for _ in range(max(1, max_rounds)):
            req: Dict[str, Any] = {
                "model": DEFAULT_MODEL,
                "max_tokens": DEFAULT_MAX_TOKENS,
                "messages": messages,
            }
            if openai_tools:
                req["tools"] = openai_tools

            completion = await llm_client.chat.completions.create(**req)
            msg = completion.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []

            assistant_message: Dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_message)

            if not tool_calls:
                answer = msg.content or ""
                ask_user = maybe_extract_ask_user(answer)
                if ask_user:
                    state.pending_question = ask_user
                    state.messages.append({"role": "assistant", "content": f"需要你补充信息：{ask_user['question']}"})
                    return QAResult(
                        session_id=sid,
                        answer="",
                        pending_question=ask_user,
                        used_servers=selected_servers,
                        issue_key=issue_key,
                        ticket_url=ticket_url.strip(),
                        input_message=user_message,
                    )

                state.messages.append({"role": "assistant", "content": answer})
                return QAResult(
                    session_id=sid,
                    answer=answer,
                    pending_question=None,
                    used_servers=selected_servers,
                    issue_key=issue_key,
                    ticket_url=ticket_url.strip(),
                    input_message=user_message,
                )

            for tc in tool_calls:
                tool_call_id = tc.id
                full_name = tc.function.name
                raw_args = tc.function.arguments or "{}"

                try:
                    tool_args = json.loads(raw_args)
                    if not isinstance(tool_args, dict):
                        tool_args = {"input": tool_args}
                except Exception:
                    tool_args = {"raw_arguments": raw_args}

                try:
                    server_name, tool_name = split_prefixed_tool_name(full_name)
                except Exception as e:
                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": f"工具名解析失败: {e}"})
                    continue

                # Special routing for issue fetch:
                # prefer jira_external first, fallback to jira_internal.
                if is_issue_fetch_tool(tool_name):
                    route_plan: List[Tuple[str, str]] = []
                    for preferred_server in ("jira_external", "jira_internal"):
                        if preferred_server not in mcp_sessions:
                            continue
                        real_tool_name = resolve_issue_tool_name_for_server(
                            tool_name,
                            server_tool_names.get(preferred_server, set()),
                        )
                        if real_tool_name:
                            route_plan.append((preferred_server, real_tool_name))

                    if not route_plan:
                        route_plan = [(server_name, tool_name)]

                    tool_text = ""
                    succeeded = False
                    last_err = ""
                    for route_server, route_tool in route_plan:
                        try:
                            route_session = mcp_sessions[route_server]
                            result = await route_session.call_tool(route_tool, arguments=tool_args)
                            tool_text = mcp_result_to_text(result)
                            succeeded = True
                            break
                        except Exception as e:
                            last_err = str(e)

                    if not succeeded:
                        tool_text = f"Tool call failed: {last_err or 'unknown error'}"

                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": tool_text})
                    continue

                session = mcp_sessions[server_name]
                try:
                    result = await session.call_tool(tool_name, arguments=tool_args)
                    tool_text = mcp_result_to_text(result)
                except Exception as e:
                    tool_text = f"Tool call failed: {e}"

                messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": tool_text})

        final_text = "达到最大工具调用轮次，未能生成最终答案。"
        state.messages.append({"role": "assistant", "content": final_text})
        return QAResult(
            session_id=sid,
            answer=final_text,
            pending_question=None,
            used_servers=selected_servers,
            issue_key=issue_key,
            ticket_url=ticket_url.strip(),
            input_message=user_message,
        )


def ask_mcp_qa(
    question: str = "",
    ticket_url: str = "",
    session_id: Optional[str] = None,
    max_rounds: int = 8,
) -> QAResult:
    return asyncio.run(
        ask_mcp_qa_async(
            question=question,
            ticket_url=ticket_url,
            session_id=session_id,
            max_rounds=max_rounds,
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP QA module for Jira ticket URL / question description")
    parser.add_argument("--ticket-url", default="", help="Ticket URL (e.g. .../browse/SCET-12345)")
    parser.add_argument("--question", default="", help="Question or problem description")
    parser.add_argument("--session-id", default="", help="Optional session id for multi-turn context")
    parser.add_argument("--max-rounds", type=int, default=8, help="Max MCP tool-call rounds")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    ticket_url = str(args.ticket_url or "").strip()
    question = str(args.question or "").strip()
    session_id = str(args.session_id or "").strip() or None
    max_rounds = int(args.max_rounds or 8)

    if not ticket_url and not question:
        parser.error("At least one of --ticket-url / --question is required.")

    result = ask_mcp_qa(
        question=question,
        ticket_url=ticket_url,
        session_id=session_id,
        max_rounds=max_rounds,
    )

    payload = {
        "session_id": result.session_id,
        "issue_key": result.issue_key,
        "ticket_url": result.ticket_url,
        "input_message": result.input_message,
        "used_servers": result.used_servers,
        "answer": result.answer,
        "pending_question": result.pending_question,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Session ID: {result.session_id}")
    print(f"Issue Key: {result.issue_key or '(unknown)'}")
    print(f"Ticket URL: {result.ticket_url or '(none)'}")
    print(f"Used Servers: {', '.join(result.used_servers) if result.used_servers else '(none)'}")
    print("-" * 60)

    if result.pending_question:
        print("需要补充信息：")
        print(result.pending_question.get("question", ""))
        options = result.pending_question.get("options") or []
        if options:
            print("可选项：")
            for idx, opt in enumerate(options, 1):
                print(f"{idx}. {opt}")
    else:
        print(result.answer)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
