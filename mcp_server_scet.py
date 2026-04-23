#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SCET MCP Server

Provides MCP tools to analyze SCET issues by leveraging local RAG index directory:
- rag_index_scet/

Main tool:
- analyze_scet: run SCET analysis using rag_scet_qa.ask_scet_rag()

Run examples:
    # Streamable HTTP mode (for remote/local MCP clients over HTTP)
    .venv/bin/python mcp_server_scet.py --transport streamable-http --host 127.0.0.1 --port 8001 --mount-path /mcp


python mcp_server_scet.py --transport streamable-http --host 0.0.0.0 --port 8001 --mount-path /mcp --index-dir rag_index_scet --default-mode offline

sudo .venv/bin/python mcp_server_scet.py --transport streamable-http --host 0.0.0.0 --port 443 --mount-path /mcp --index-dir rag_index_scet --default-mode offline

    # stdio mode (for local MCP clients)
    .venv/bin/python mcp_server_scet.py --transport stdio
"""

import argparse
import json
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from rag_scet_qa import ask_scet_rag


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SCET MCP Server (RAG over rag_index_scet)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="streamable-http",
        help="MCP transport type",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for HTTP transports")
    parser.add_argument("--port", type=int, default=8001, help="Bind port for HTTP transports")
    parser.add_argument(
        "--mount-path",
        default="/mcp",
        help="Path used by streamable-http transport (e.g. /mcp)",
    )
    parser.add_argument(
        "--index-dir",
        default="rag_index_scet",
        help="Local RAG index directory",
    )
    parser.add_argument(
        "--default-mode",
        choices=["offline", "llm"],
        default="offline",
        help="Default analyze mode when tool caller does not pass mode",
    )
    parser.add_argument("--default-top-k", type=int, default=10, help="Default retrieval top_k")
    parser.add_argument("--default-vector-weight", type=float, default=0.65, help="Default vector score weight")
    parser.add_argument("--default-keyword-boost", type=float, default=0.35, help="Default keyword boost")
    parser.add_argument("--default-snippet-max-len", type=int, default=480, help="Default snippet max length")
    parser.add_argument("--default-model", default="gpt-5-mini", help="Default LLM model if mode=llm")
    parser.add_argument("--default-max-tokens", type=int, default=2000, help="Default max tokens if mode=llm")
    return parser


def create_server(args: argparse.Namespace) -> FastMCP:
    app = FastMCP(
        name="scet-rag-mcp-server",
        instructions=(
            "Use tools in this server to analyze SCET issues with a local RAG index at rag_index_scet. "
            "Prefer mode=offline for fully local retrieval-only responses; use mode=llm when generative synthesis is needed."
        ),
        host=args.host,
        port=args.port,
        streamable_http_path=args.mount_path,
    )

    @app.tool(
        name="analyze_scet",
        description=(
            "Analyze SCET-related questions using local rag_index_scet. "
            "Supports offline (extractive) and llm (rag+generation) modes."
        ),
        structured_output=True,
    )
    def analyze_scet(
        question: str,
        mode: str = "",
        top_k: int = 0,
        vector_weight: float = -1.0,
        keyword_boost: float = -1.0,
        snippet_max_len: int = 0,
        model: str = "",
        max_tokens: int = 0,
        index_dir: str = "",
    ) -> Dict[str, Any]:
        """
        Analyze SCET question with local RAG index.
        Args:
            question: User question, e.g. "Analyze SCET-12345 root cause and next steps".
            mode: "offline" or "llm". Empty means server default.
            top_k: Retrieval top_k. <=0 means server default.
            vector_weight: Vector score weight (0~1). <0 means server default.
            keyword_boost: Keyword boost. <0 means server default.
            snippet_max_len: Context snippet max chars. <=0 means server default.
            model: LLM model for mode=llm. Empty means server default.
            max_tokens: LLM max output tokens for mode=llm. <=0 means server default.
            index_dir: Override index dir. Empty means server default from startup.
        """
        q = (question or "").strip()
        if not q:
            return {
                "ok": False,
                "error": "question cannot be empty",
            }

        mode_final = (mode or args.default_mode).strip().lower()
        if mode_final not in {"offline", "llm"}:
            return {
                "ok": False,
                "error": f"invalid mode: {mode_final}. expected one of ['offline', 'llm']",
            }

        effective_index_dir = (index_dir or args.index_dir).strip()
        effective_top_k = top_k if isinstance(top_k, int) and top_k > 0 else args.default_top_k
        effective_vector_weight = (
            vector_weight if isinstance(vector_weight, (int, float)) and vector_weight >= 0 else args.default_vector_weight
        )
        effective_keyword_boost = (
            keyword_boost if isinstance(keyword_boost, (int, float)) and keyword_boost >= 0 else args.default_keyword_boost
        )
        effective_snippet_max_len = (
            snippet_max_len if isinstance(snippet_max_len, int) and snippet_max_len > 0 else args.default_snippet_max_len
        )
        effective_model = (model or args.default_model).strip()
        effective_max_tokens = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else args.default_max_tokens

        try:
            result = ask_scet_rag(
                question=q,
                mode=mode_final,
                index_dir=effective_index_dir,
                top_k=effective_top_k,
                vector_weight=float(effective_vector_weight),
                keyword_boost=float(effective_keyword_boost),
                snippet_max_len=effective_snippet_max_len,
                model=effective_model,
                max_tokens=effective_max_tokens,
            )
            return {
                "ok": True,
                "result": result,
                "effective_config": {
                    "mode": mode_final,
                    "index_dir": effective_index_dir,
                    "top_k": effective_top_k,
                    "vector_weight": float(effective_vector_weight),
                    "keyword_boost": float(effective_keyword_boost),
                    "snippet_max_len": effective_snippet_max_len,
                    "model": effective_model,
                    "max_tokens": effective_max_tokens,
                },
            }
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
            }

    @app.tool(
        name="scet_index_health",
        description="Check if SCET RAG index is readable by running a tiny offline query.",
        structured_output=True,
    )
    def scet_index_health(index_dir: str = "") -> Dict[str, Any]:
        effective_index_dir = (index_dir or args.index_dir).strip()
        try:
            probe = ask_scet_rag(
                question="health check",
                mode="offline",
                index_dir=effective_index_dir,
                top_k=1,
            )
            return {
                "ok": True,
                "index_dir": effective_index_dir,
                "mode": probe.get("mode"),
                "sources_count": len(probe.get("sources", [])),
            }
        except Exception as e:
            return {
                "ok": False,
                "index_dir": effective_index_dir,
                "error": str(e),
            }

    @app.tool(
        name="scet_server_info",
        description="Return current SCET MCP server runtime configuration.",
        structured_output=True,
    )
    def scet_server_info() -> Dict[str, Any]:
        return {
            "ok": True,
            "name": "scet-rag-mcp-server",
            "transport": args.transport,
            "host": args.host,
            "port": args.port,
            "mount_path": args.mount_path,
            "defaults": {
                "index_dir": args.index_dir,
                "mode": args.default_mode,
                "top_k": args.default_top_k,
                "vector_weight": args.default_vector_weight,
                "keyword_boost": args.default_keyword_boost,
                "snippet_max_len": args.default_snippet_max_len,
                "model": args.default_model,
                "max_tokens": args.default_max_tokens,
            },
        }

    return app


def main() -> int:
    args = build_parser().parse_args()
    app = create_server(args)

    print(
        json.dumps(
            {
                "event": "starting_scet_mcp_server",
                "transport": args.transport,
                "host": args.host,
                "port": args.port,
                "mount_path": args.mount_path,
                "index_dir": args.index_dir,
                "default_mode": args.default_mode,
            },
            ensure_ascii=False,
        )
    )

    app.run(transport=args.transport, mount_path=args.mount_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
