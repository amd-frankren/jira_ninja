#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Compatibility entry: LLM API invocation tool.

Notes:
- Core implementation has been unified in `rag_scet_qa.py` (`chat_completion` / `create_client`)
- This file is kept to avoid breaking existing caller scripts
"""

import argparse
import json

from rag_scet_qa import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    chat_completion,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simple LLM API invocation tool (compatibility entry)")
    parser.add_argument("--prompt", default="What is the weather today?", help="User prompt")
    parser.add_argument("--system", default="You are a helpful assistant.", help="System prompt")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Maximum output tokens")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Gateway URL")
    parser.add_argument(
        "--gateway-key",
        default="",
        help="Gateway subscription key (if empty, use env variable or default value)",
    )
    parser.add_argument("--user", default="", help="Request header `user` value (auto-resolve when empty)")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    messages = [
        {"role": "system", "content": args.system},
        {"role": "user", "content": args.prompt},
    ]

    result = chat_completion(
        messages=messages,
        model=args.model,
        max_tokens=args.max_tokens,
        gateway_key=args.gateway_key or None,
        base_url=args.base_url,
        user_name=args.user or None,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
