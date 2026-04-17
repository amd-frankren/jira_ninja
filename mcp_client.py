#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MCP client example:
- Connects to remote MCP server via Streamable HTTP transport
- Uses OpenAI-compatible gateway
- Lets LLM decide and call MCP tools in a loop

Usage:
    python mcp_client.py --prompt "Analyze PLAT-185969"
    python mcp_client.py --prompt "MCTP bridge routing table abnormal"
    python mcp_client.py --prompt "Placeholder prompt, any text is fine" --list-tools

"""

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import openai

try:
    from mcp import ClientSession
except ImportError:
    # Fallback for some SDK layouts
    from mcp.client.session import ClientSession  # type: ignore

try:
    # Newer python MCP SDK naming
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    # Possible alias in other versions
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client  # type: ignore


ENV_MCP_SERVER_URL_ATLASSIAN_INTERNAL = os.getenv("MCP_SERVER_URL_ATLASSIAN_INTERNAL", "")

ENV_LLM_GATEWAY_API_URL = os.getenv("LLM_GATEWAY_API_URL", "")
ENV_LLM_GATEWAY_API_TOKEN = os.getenv("LLM_GATEWAY_API_TOKEN", "")

DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_MAX_TOKENS = 1024


def resolve_user_name(user_name: Optional[str]) -> str:
    user = user_name or os.getenv("LLM_USER") or os.getenv("USER") or os.getenv("USERNAME")
    if user:
        return user
    try:
        return os.getlogin()
    except Exception:
        return "unknown-user"


def create_llm_client(
    gateway_key: Optional[str] = None,
    base_url: str = ENV_LLM_GATEWAY_API_URL,
    user_name: Optional[str] = None,
) -> openai.OpenAI:
    key = gateway_key or ENV_LLM_GATEWAY_API_TOKEN
    if not key:
        raise RuntimeError("Missing required environment variable: LLM_GATEWAY_API_TOKEN")
    if not base_url:
        raise RuntimeError("Missing required environment variable: LLM_GATEWAY_API_URL")
    user = resolve_user_name(user_name)
    return openai.OpenAI(
        base_url=base_url,
        api_key="dummy",
        default_headers={
            "Ocp-Apim-Subscription-Key": key,
            "user": user,
        },
    )

def parse_header_items(header_items: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in header_items:
        if ":" not in item:
            raise ValueError(f"Invalid header format: {item!r}. Use --mcp-header 'Key: Value'")
        k, v = item.split(":", 1)
        headers[k.strip()] = v.strip()
    return headers


def normalize_transport_streams(streams: Any) -> Tuple[Any, Any]:
    if isinstance(streams, (list, tuple)) and len(streams) >= 2:
        return streams[0], streams[1]
    raise RuntimeError(
        "Unexpected streamable-http transport return value. "
        "Expected tuple/list with at least (read_stream, write_stream)."
    )


def extract_mcp_tool_list(list_tools_result: Any) -> List[Any]:
    tools = getattr(list_tools_result, "tools", None)
    if tools is None and isinstance(list_tools_result, dict):
        tools = list_tools_result.get("tools")
    if not tools:
        return []
    return list(tools)


def to_openai_tool_schema(mcp_tools: List[Any]) -> List[Dict[str, Any]]:
    schemas: List[Dict[str, Any]] = []
    for tool in mcp_tools:
        if isinstance(tool, dict):
            name = tool.get("name", "")
            description = tool.get("description", "")
            input_schema = tool.get("inputSchema") or tool.get("input_schema")
        else:
            name = getattr(tool, "name", "")
            description = getattr(tool, "description", "")
            input_schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)

        if not name:
            continue

        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}

        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description or f"MCP tool: {name}",
                    "parameters": input_schema,
                },
            }
        )
    return schemas


def mcp_result_to_text(result: Any) -> str:
    parts: List[str] = []

    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")

    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
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

    if not parts:
        try:
            if hasattr(result, "model_dump"):
                return json.dumps(result.model_dump(), ensure_ascii=False, indent=2)
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False, indent=2)
            return str(result)
        except Exception:
            return str(result)

    return "\n".join(parts).strip()


def format_mcp_tools_for_display(mcp_tools: List[Any]) -> str:
    if not mcp_tools:
        return "No tools found on MCP server."

    lines: List[str] = []
    for idx, tool in enumerate(mcp_tools, start=1):
        if isinstance(tool, dict):
            name = tool.get("name", "")
            description = tool.get("description", "")
            input_schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        else:
            name = getattr(tool, "name", "")
            description = getattr(tool, "description", "")
            input_schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}

        lines.append(f"{idx}. {name or '<unknown>'}")
        if description:
            lines.append(f"   description: {description}")
        lines.append(f"   input_schema: {json.dumps(input_schema, ensure_ascii=False)}")

    return "\n".join(lines)


async def list_mcp_tools(
    mcp_server_url: str,
    mcp_headers: Dict[str, str],
) -> str:
    transport_kwargs: Dict[str, Any] = {}
    if mcp_headers:
        transport_kwargs["headers"] = mcp_headers

    async with streamablehttp_client(mcp_server_url, **transport_kwargs) as streams:
        read_stream, write_stream = normalize_transport_streams(streams)

        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            list_tools_result = await session.list_tools()
            mcp_tools = extract_mcp_tool_list(list_tools_result)
            return format_mcp_tools_for_display(mcp_tools)


async def run_chat_with_mcp(
    user_prompt: str,
    mcp_server_url: str,
    mcp_headers: Dict[str, str],
    model: str,
    max_tokens: int,
    gateway_key: Optional[str],
    base_url: str,
    llm_user: Optional[str],
    max_tool_rounds: int = 8,
) -> str:
    llm_client = create_llm_client(gateway_key=gateway_key, base_url=base_url, user_name=llm_user)

    # Keep system prompt short and explicit for MCP tool use
    system_prompt = (
        "You are a helpful assistant with access to MCP tools. "
        "When needed, call appropriate tools to answer the user. "
        "If tool data is insufficient, clearly say what is missing."
    )

    transport_kwargs: Dict[str, Any] = {}
    if mcp_headers:
        transport_kwargs["headers"] = mcp_headers

    async with streamablehttp_client(mcp_server_url, **transport_kwargs) as streams:
        read_stream, write_stream = normalize_transport_streams(streams)

        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            list_tools_result = await session.list_tools()
            mcp_tools = extract_mcp_tool_list(list_tools_result)
            openai_tools = to_openai_tool_schema(mcp_tools)

            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            for _ in range(max_tool_rounds):
                request_kwargs: Dict[str, Any] = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": messages,
                }
                if openai_tools:
                    request_kwargs["tools"] = openai_tools

                resp = llm_client.chat.completions.create(**request_kwargs)
                choice = resp.choices[0]
                msg = choice.message
                tool_calls = getattr(msg, "tool_calls", None) or []

                assistant_message: Dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or "",
                }

                if tool_calls:
                    assistant_message["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in tool_calls
                    ]

                messages.append(assistant_message)

                if not tool_calls:
                    return msg.content or ""

                for tc in tool_calls:
                    tool_name = tc.function.name
                    raw_args = tc.function.arguments or "{}"
                    try:
                        tool_args = json.loads(raw_args)
                        if not isinstance(tool_args, dict):
                            tool_args = {"input": tool_args}
                    except json.JSONDecodeError:
                        tool_args = {"raw_arguments": raw_args}

                    tool_result = await session.call_tool(tool_name, arguments=tool_args)
                    tool_text = mcp_result_to_text(tool_result)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_text,
                        }
                    )

            return "Stopped because max tool rounds were reached before final answer."



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP client with LLM gateway")
    parser.add_argument("--prompt", required=True, help="User prompt")
    parser.add_argument("--mcp-server-url", default=ENV_MCP_SERVER_URL_ATLASSIAN_INTERNAL, help="MCP server URL")
    parser.add_argument(
        "--mcp-header",
        action="append",
        default=[],
        help="Extra MCP header, repeatable. Format: 'Key: Value'",
    )

    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Max output tokens")
    parser.add_argument(
        "--gateway-key",
        default=ENV_LLM_GATEWAY_API_TOKEN,
        help="LLM gateway subscription key",
    )
    parser.add_argument("--user", default="", help="LLM user header value")
    parser.add_argument("--max-tool-rounds", type=int, default=8, help="Max LLM tool-call iterations")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Only list all tools exposed by MCP server and exit",
    )
    return parser


async def amain() -> int:
    args = build_parser().parse_args()

    if not os.getenv("MCP_SERVER_URL_ATLASSIAN_INTERNAL"):
        raise RuntimeError(
            "Missing required environment variable: MCP_SERVER_URL_ATLASSIAN_INTERNAL"
        )

    mcp_auth_token = os.getenv("INTERNAL_JIRA_TOKEN", "")
    if not mcp_auth_token:
        raise RuntimeError("Missing required environment variable: INTERNAL_JIRA_TOKEN")

    headers = parse_header_items(args.mcp_header)
    headers["Authorization"] = f"Bearer {mcp_auth_token}"

    if args.list_tools:
        tools_text = await list_mcp_tools(
            mcp_server_url=args.mcp_server_url,
            mcp_headers=headers,
        )
        print(tools_text)
        return 0

    answer = await run_chat_with_mcp(
        user_prompt=args.prompt,
        mcp_server_url=args.mcp_server_url,
        mcp_headers=headers,
        model=args.model,
        max_tokens=args.max_tokens,
        gateway_key=args.gateway_key,
        base_url=ENV_LLM_GATEWAY_API_URL,
        llm_user=args.user or None,
        max_tool_rounds=args.max_tool_rounds,
    )

    print(answer)
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
