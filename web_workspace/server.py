#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import os
import re
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

try:
    from mcp import ClientSession
except ImportError:
    from mcp.client.session import ClientSession  # type: ignore

try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client  # type: ignore


# -----------------------------
# Configuration
# -----------------------------
DEFAULT_MODEL = os.getenv("WEBUI_LLM_MODEL", "gpt-5-mini")
DEFAULT_MAX_TOKENS = int(os.getenv("WEBUI_LLM_MAX_TOKENS", "1200"))
KNOWLEDGE_STORE_PATH = Path("knowledge_store.json")

MCP_SERVERS: Dict[str, Dict[str, Any]] = {
    "scet": {
        "url": os.getenv("SCET_MCP_URL", "http://127.0.0.1:8000/mcp"),
        "auth_token": os.getenv("SCET_MCP_AUTH_TOKEN", ""),
    },
    "jira_internal": {
        "url": os.getenv("JIRA_INTERNAL_MCP_URL", "http://127.0.0.1:8002/mcp"),
        "auth_token": os.getenv("JIRA_INTERNAL_MCP_AUTH_TOKEN", ""),
    },
}

Q1_OPTIONS: List[Dict[str, str]] = [
    {"id": "a", "label": "[3rd Party]"},
    {"id": "b", "label": "[Firmware]"},
    {"id": "c", "label": "[CPU]"},
    {"id": "d", "label": "[Platform Hardware]"},
    {"id": "e", "label": "[Tool]"},
    {"id": "f", "label": "[SI]"},
    {"id": "g", "label": "[OS]"},
    {"id": "h", "label": "[Electrical Validation]"},
    {"id": "i", "label": "[Design Collateral]"},
    {"id": "j", "label": "[Design Review]"},
    {"id": "k", "label": "[Others]"},
]

Q2_OPTIONS_MAP: Dict[str, List[str]] = {
    "a": ["[3rd Party] CXL", "[3rd Party] GPU card", "[3rd Party] Memory", "[3rd Party] NIC card", "[3rd Party] Others"],
    "b": ["[Firmware] Agesa/ DXIO", "[Firmware] Agesa/ PSP", "[Firmware] Agesa/ UEFI", "[Firmware] Open BMC", "[Firmware] Others"],
    "c": ["[CPU] Core", "[CPU] DDR", "[CPU] PCIe", "[CPU] Power", "[CPU] Others"],
    "d": ["[Platform Hardware] CRB", "[Platform Hardware] PCIe", "[Platform Hardware] Thermal", "[Platform Hardware] USB", "[Platform Hardware] Others"],
    "e": ["[Tool] AMD Checker", "[Tool] AMD Debug Tool", "[Tool] AMD Stress Tool", "[Tool] Non-AMD tool", "[Tool] Others"],
    "f": ["[SI] Impedance/loss", "[SI] SATA", "[SI] USB", "[SI] Simulation report", "[SI] Others"],
    "g": ["[OS] Driver", "[OS] kernel", "[OS] patch", "[OS] fix"],
    "h": ["[Electrical Validation] CXL", "[Electrical Validation] DDR", "[Electrical Validation] PCIe", "[Electrical Validation] USB", "[Electrical Validation] Others"],
    "i": ["[Design Collateral] Datasheet", "[Design Collateral] Checklist", "[Design Collateral] Technical Advisory", "[Design Collateral] Thermal Guide", "[Design Collateral] Others"],
    "j": ["[Design Review] BIOS review", "[Design Review] Block diagram review", "[Design Review] PCB Layout review", "[Design Review] Schematic review", "[Design Review] Others"],
    "k": ["[Others]", "[Others] Can Not Duplicate", "[Others] Customer Education", "[Others] New Feature Enhancement", "[Others] Work as designed"],
}

Q1_KEYWORDS: Dict[str, List[str]] = {
    "a": ["3rd party", "third-party", "vendor", "ibv", "retimer", "redriver", "nvme", "nic", "gpu", "memory"],
    "b": ["firmware", "agesa", "bios", "uefi", "bmc", "smu", "psp", "dxio"],
    "c": ["cpu", "silicon", "pcie", "core", "ddr", "xgmi", "svi", "jtag"],
    "d": ["platform hardware", "board", "schematic", "thermal", "mechanical", "crb", "fpga"],
    "e": ["tool", "checker", "debug tool", "stress tool", "script", "automation"],
    "f": ["signal integrity", "si", "simulation model", "seasim", "s2eye", "impedance", "loss"],
    "g": ["os", "kernel", "driver", "windows", "linux", "patch"],
    "h": ["electrical validation", "compliance", "validation guidance", "pcie test", "cxl test", "usb test"],
    "i": ["document", "datasheet", "checklist", "guide", "collateral", "advisory"],
    "j": ["design review", "review ticket", "layout review", "stack-up review", "schematic review"],
}


# -----------------------------
# Models
# -----------------------------
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class TicketClassifyRequest(BaseModel):
    text: str = Field(..., min_length=1)
    ticket_id: Optional[str] = None


class KnowledgeItemInput(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(default="", max_length=20000)
    url: str = Field(default="", max_length=1000)
    type: str = Field(default="article", max_length=32)


@dataclass
class SessionState:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    pending_question: Optional[Dict[str, Any]] = None


SESSIONS: Dict[str, SessionState] = {}
KNOWLEDGE_LOCK = asyncio.Lock()


# -----------------------------
# Utilities
# -----------------------------
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


def flatten_exception_messages(exc: BaseException) -> List[str]:
    group_children = getattr(exc, "exceptions", None)
    if isinstance(group_children, tuple) and group_children:
        msgs: List[str] = []
        for child in group_children:
            msgs.extend(flatten_exception_messages(child))
        return msgs

    msg = str(exc).strip()
    if not msg:
        msg = exc.__class__.__name__
    return [msg]


def format_exception_for_user(exc: BaseException) -> str:
    msgs = flatten_exception_messages(exc)
    seen = set()
    ordered: List[str] = []
    for m in msgs:
        if m in seen:
            continue
        seen.add(m)
        ordered.append(m)

    if not ordered:
        return "Unknown server error"
    if len(ordered) == 1:
        return ordered[0]
    return " | ".join(ordered)


def detect_server_hints(user_message: str) -> List[str]:
    text = (user_message or "").lower()
    picked: List[str] = []

    if "scet" in text or re.search(r"\bscet-\d+\b", text):
        picked.append("scet")
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


def sse_event(event_type: str, payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_type}\ndata: {body}\n\n"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"[\s,.;:()/_\-\[\]]+", text.lower()) if len(t) > 1]


def classify_ticket(text: str) -> Dict[str, Any]:
    lowered = text.lower()

    scored: List[Tuple[str, int, List[str]]] = []
    for q1_id, keywords in Q1_KEYWORDS.items():
        hits = [kw for kw in keywords if kw in lowered]
        scored.append((q1_id, len(hits), hits))

    scored.sort(key=lambda x: x[1], reverse=True)
    best_q1, best_score, hit_keywords = scored[0]
    if best_score == 0:
        best_q1 = "k"
        hit_keywords = []

    q1_label = next((x["label"] for x in Q1_OPTIONS if x["id"] == best_q1), "[Others]")
    q2_candidates = Q2_OPTIONS_MAP.get(best_q1, [])
    best_q2 = q2_candidates[0] if q2_candidates else "[Others]"

    if q2_candidates:
        text_tokens = set(tokenize(text))
        best_score_q2 = -1
        for option in q2_candidates:
            score = len(text_tokens.intersection(set(tokenize(option))))
            if score > best_score_q2:
                best_score_q2 = score
                best_q2 = option

    if hit_keywords:
        reasoning = f"命中关键词: {', '.join(hit_keywords[:6])}，推荐 {q1_label} / {best_q2}"
    else:
        reasoning = f"未命中明显关键词，采用兜底分类 {q1_label} / {best_q2}"

    return {
        "q1_id": best_q1,
        "q1_label": q1_label,
        "q2_text": best_q2,
        "reasoning": reasoning,
    }


def ensure_knowledge_store_file() -> None:
    if KNOWLEDGE_STORE_PATH.exists():
        return
    payload = {"items": [], "updated_at": now_iso()}
    KNOWLEDGE_STORE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_knowledge_items() -> List[Dict[str, Any]]:
    ensure_knowledge_store_file()
    try:
        raw = KNOWLEDGE_STORE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        items = data.get("items", [])
        if not isinstance(items, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            normalized.append(
                {
                    "id": str(item.get("id", str(uuid.uuid4()))),
                    "type": str(item.get("type", "article")),
                    "title": title,
                    "content": str(item.get("content", "")),
                    "url": str(item.get("url", "")),
                    "updated_at": str(item.get("updated_at", now_iso())),
                }
            )
        return normalized
    except Exception:
        return []


def write_knowledge_items(items: List[Dict[str, Any]]) -> None:
    payload = {"items": items, "updated_at": now_iso()}
    tmp_path = KNOWLEDGE_STORE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(KNOWLEDGE_STORE_PATH)


# -----------------------------
# Agent core
# -----------------------------
SYSTEM_PROMPT = """你是一个 Jira Ticket 问答助手，可调用 MCP 工具。
你可以使用来自 scet、jira_internal 两个 MCP server 的工具。

规则：
1) 优先通过工具获取事实，不要编造。
2) 回答中给出关键信息和依据。
3) 如果用户问题不充分，或者你需要用户先做选择，请返回**严格 JSON**（不要额外文本）：
{"type":"ask_user","question":"你的追问","options":["可选项1","可选项2"]}
4) 如果不需要追问，直接给出最终回答（普通文本）。
"""


async def run_agent_stream(
    session_id: str,
    user_message: str,
) -> AsyncGenerator[str, None]:
    state = SESSIONS.setdefault(session_id, SessionState())

    if state.pending_question:
        pending_q = state.pending_question.get("question", "")
        state.messages.append(
            {
                "role": "assistant",
                "content": f"补充提问：{pending_q}",
            }
        )
        state.pending_question = None

    state.messages.append({"role": "user", "content": user_message})

    yield sse_event("session", {"session_id": session_id})
    yield sse_event("status", {"message": "连接 MCP servers..."})

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

            streams = await stack.enter_async_context(
                streamablehttp_client(cfg["url"], **transport_kwargs)
            )
            read_stream, write_stream = normalize_transport_streams(streams)

            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()

            list_tools_result = await session.list_tools()
            tools = extract_mcp_tool_list(list_tools_result)

            mcp_sessions[server_name] = session
            all_tools[server_name] = tools

            yield sse_event(
                "status",
                {
                    "message": f"{server_name} 已连接，工具数: {len(tools)}",
                    "server": server_name,
                    "tool_count": len(tools),
                },
            )

        total_tool_count = sum(len(v) for v in all_tools.values())
        selected_servers = list(all_tools.keys())

        if total_tool_count > 128:
            hinted_servers = [s for s in detect_server_hints(user_message) if s in all_tools]
            if hinted_servers:
                selected_servers = hinted_servers
                yield sse_event(
                    "status",
                    {"message": f"工具总数超限({total_tool_count})，已按问题线索自动选择: {', '.join(selected_servers)}"},
                )
            else:
                ask_user = {
                    "question": "当前可用工具较多，请先选择要查询的数据源",
                    "options": ["scet", "jira_internal"],
                }
                state.pending_question = ask_user
                state.messages.append({"role": "assistant", "content": f"需要你补充信息：{ask_user['question']}"})
                yield sse_event("ask_user", ask_user)
                return

        chosen_tools = {k: all_tools[k] for k in selected_servers if k in all_tools}
        server_tool_names: Dict[str, set[str]] = {k: tool_name_set(v) for k, v in all_tools.items()}
        openai_tools = to_openai_tool_schemas(chosen_tools)
        messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}] + state.messages[:]

        for _ in range(8):
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
                    yield sse_event("ask_user", {"question": ask_user["question"], "options": ask_user.get("options", [])})
                    return

                state.messages.append({"role": "assistant", "content": answer})
                yield sse_event("final", {"answer": answer})
                return

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
                    err = f"工具名解析失败: {e}"
                    yield sse_event("tool_error", {"tool": full_name, "error": err})
                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": err})
                    continue

                # Special routing for issue fetch:
                # prefer SCET first, fallback to jira_internal when SCET fails/not found.
                if is_issue_fetch_tool(tool_name):
                    route_plan: List[Tuple[str, str]] = []
                    for preferred_server in ("scet", "jira_internal"):
                        if preferred_server not in mcp_sessions:
                            continue
                        real_tool_name = resolve_issue_tool_name_for_server(
                            tool_name,
                            server_tool_names.get(preferred_server, set()),
                        )
                        if real_tool_name:
                            route_plan.append((preferred_server, real_tool_name))

                    # Fallback to original route when no special route is available
                    if not route_plan:
                        route_plan = [(server_name, tool_name)]

                    tool_text = ""
                    last_err: Optional[str] = None
                    succeeded = False

                    for idx, (route_server, route_tool) in enumerate(route_plan, start=1):
                        yield sse_event(
                            "tool_start",
                            {
                                "server": route_server,
                                "tool": route_tool,
                                "arguments": tool_args,
                                "route_step": idx,
                                "route_total": len(route_plan),
                            },
                        )
                        try:
                            route_session = mcp_sessions[route_server]
                            result = await route_session.call_tool(route_tool, arguments=tool_args)
                            tool_text = mcp_result_to_text(result)
                            yield sse_event(
                                "tool_result",
                                {"server": route_server, "tool": route_tool, "result": tool_text},
                            )
                            succeeded = True
                            break
                        except Exception as e:
                            last_err = str(e)
                            yield sse_event(
                                "tool_error",
                                {"server": route_server, "tool": route_tool, "error": last_err},
                            )

                    if not succeeded:
                        tool_text = f"Tool call failed: {last_err or 'unknown error'}"

                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": tool_text})
                    continue

                yield sse_event("tool_start", {"server": server_name, "tool": tool_name, "arguments": tool_args})
                session = mcp_sessions[server_name]
                try:
                    result = await session.call_tool(tool_name, arguments=tool_args)
                    tool_text = mcp_result_to_text(result)
                    yield sse_event("tool_result", {"server": server_name, "tool": tool_name, "result": tool_text})
                except Exception as e:
                    tool_text = f"Tool call failed: {e}"
                    yield sse_event("tool_error", {"server": server_name, "tool": tool_name, "error": str(e)})

                messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": tool_text})

        final_text = "达到最大工具调用轮次，未能生成最终答案。"
        state.messages.append({"role": "assistant", "content": final_text})
        yield sse_event("final", {"answer": final_text})


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="Jira Ticket QA WebUI")


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "servers": {k: v["url"] for k, v in MCP_SERVERS.items()},
        "model": DEFAULT_MODEL,
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    session_id = req.session_id or str(uuid.uuid4())
    message = req.message.strip()

    async def event_gen() -> AsyncGenerator[str, None]:
        if not message:
            yield sse_event("error", {"message": "message 不能为空"})
            return
        try:
            async for ev in run_agent_stream(session_id=session_id, user_message=message):
                yield ev
                await asyncio.sleep(0)
        except BaseException as e:
            yield sse_event("error", {"message": format_exception_for_user(e)})

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/ticket/classify/options")
def ticket_classify_options() -> Dict[str, Any]:
    return {"q1_options": Q1_OPTIONS, "q2_options_map": Q2_OPTIONS_MAP}


@app.post("/api/ticket/classify")
def ticket_classify(req: TicketClassifyRequest) -> Dict[str, Any]:
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    result = classify_ticket(text)
    if req.ticket_id:
        result["ticket_id"] = req.ticket_id
    return result


@app.get("/api/knowledge")
async def list_knowledge() -> Dict[str, Any]:
    async with KNOWLEDGE_LOCK:
        return {"items": read_knowledge_items()}


@app.post("/api/knowledge")
async def create_knowledge(req: KnowledgeItemInput) -> Dict[str, Any]:
    async with KNOWLEDGE_LOCK:
        items = read_knowledge_items()
        normalized_url = req.url.strip()
        new_item = {
            "id": str(uuid.uuid4()),
            "type": "link" if normalized_url else "article",
            "title": req.title.strip(),
            "content": req.content.strip(),
            "url": normalized_url,
            "updated_at": now_iso(),
        }
        items.insert(0, new_item)
        write_knowledge_items(items)
        return new_item


@app.put("/api/knowledge/{item_id}")
async def update_knowledge(item_id: str, req: KnowledgeItemInput) -> Dict[str, Any]:
    async with KNOWLEDGE_LOCK:
        items = read_knowledge_items()
        idx = next((i for i, x in enumerate(items) if x.get("id") == item_id), -1)
        if idx < 0:
            raise HTTPException(status_code=404, detail="知识条目不存在")

        normalized_url = req.url.strip()
        items[idx] = {
            "id": item_id,
            "type": "link" if normalized_url else "article",
            "title": req.title.strip(),
            "content": req.content.strip(),
            "url": normalized_url,
            "updated_at": now_iso(),
        }
        write_knowledge_items(items)
        return items[idx]


@app.delete("/api/knowledge/{item_id}")
async def delete_knowledge(item_id: str) -> Dict[str, Any]:
    async with KNOWLEDGE_LOCK:
        items = read_knowledge_items()
        new_items = [x for x in items if x.get("id") != item_id]
        if len(new_items) == len(items):
            raise HTTPException(status_code=404, detail="知识条目不存在")
        write_knowledge_items(new_items)
        return {"ok": True, "deleted_id": item_id}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")
