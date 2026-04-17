#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Minify JSON files (remove whitespace/indentation) and write results to the target directory.

- Supports processing a single JSON file (`--input-file`)
- Supports batch processing all JSON files under a directory (`--input-dir`)

Usage examples:
python json_minify.py
python json_minify.py --input-dir input_json --output-dir output_minified
python json_minify.py --input-file input_json/sample.json --output-dir output_minified
"""

import argparse
import json
import os
from typing import Tuple


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def minify_one_file(src: str, dst: str) -> Tuple[int, int]:
    """
    Returns: (original bytes, output bytes)
    """
    src_size = os.path.getsize(src)

    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)

    ensure_dir(os.path.dirname(dst) or ".")
    with open(dst, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, separators=(",", ":"))

    dst_size = os.path.getsize(dst)
    return src_size, dst_size


def main() -> int:
    parser = argparse.ArgumentParser(description="Run minify processing for JSON files")
    parser.add_argument("--input-dir", default="json_input", help="Input directory (default: json_input)")
    parser.add_argument(
        "--input-file",
        default=None,
        help="Input single JSON file path (takes precedence over --input-dir)",
    )
    parser.add_argument("--output-dir", default="json_minified", help="Output directory (default: json_minified)")
    args = parser.parse_args()

    ensure_dir(args.output_dir)

    total_files = 0
    ok_files = 0
    fail_files = 0
    total_src_bytes = 0
    total_dst_bytes = 0

    if args.input_file:
        src = args.input_file

        if not os.path.isfile(src):
            print(f"Input file does not exist: {src}")
            return 1
        if not src.lower().endswith(".json"):
            print(f"Input file is not a .json file: {src}")
            return 1

        src_name = os.path.basename(src)
        dst = os.path.join(args.output_dir, src_name)
        total_files = 1

        try:
            src_size, dst_size = minify_one_file(src, dst)
            total_src_bytes += src_size
            total_dst_bytes += dst_size
            ok_files = 1
            print(f"[OK] {src_name} -> {src_name} ({src_size} -> {dst_size} bytes)")
        except Exception as e:
            fail_files = 1
            print(f"[FAIL] {src_name}: {e}")
    else:
        if not os.path.isdir(args.input_dir):
            print(f"Input directory does not exist: {args.input_dir}")
            return 1

        for name in os.listdir(args.input_dir):
            if not name.lower().endswith(".json"):
                continue

            src = os.path.join(args.input_dir, name)
            if not os.path.isfile(src):
                continue

            dst = os.path.join(args.output_dir, name)
            total_files += 1

            try:
                src_size, dst_size = minify_one_file(src, dst)
                total_src_bytes += src_size
                total_dst_bytes += dst_size
                ok_files += 1
                print(f"[OK] {name} -> {name} ({src_size} -> {dst_size} bytes)")
            except Exception as e:
                fail_files += 1
                print(f"[FAIL] {name}: {e}")

    if total_files == 0:
        print("No .json files found to process.")
        return 1

    ratio = (total_dst_bytes / total_src_bytes * 100) if total_src_bytes else 0.0
    print("Processing completed")
    if args.input_file:
        print(f"input_file={args.input_file}")
    else:
        print(f"input_dir={args.input_dir}")
    print(f"output_dir={args.output_dir}")
    print(f"total_files={total_files}, success={ok_files}, failed={fail_files}")
    print(f"total_size: {total_src_bytes} -> {total_dst_bytes} bytes ({ratio:.2f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
