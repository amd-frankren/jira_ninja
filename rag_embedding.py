#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build a knowledge-base index offline (fully offline, local embedding + local vector store).

Output directory structure:
<your_output_directory>/
  - index.faiss
  - chunks.jsonl
  - bm25_tokens.jsonl
  - manifest.json

Examples:
python rag_embedding.py --input-dir resources/SCET_export_minified/ --output-dir rag_index_scet
python rag_embedding.py --input-dir resources/PLAT_export_minified/ --output-dir rag_index_plat

python rag_embedding.py --input-dir PLAT_export/PLAT_export --output-dir rag_plat_index --max-files 200

python rag_embedding.py --input-dir ./data --output-dir ./rag_index --embedding-model BAAI/bge-small-zh-v1.5
python rag_embedding.py --input-dir ./data --output-dir ./rag_index --doc-as-one-chunk
"""

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer


def collect_json_files(input_dir: Path, recursive: bool) -> List[Path]:
    if recursive:
        files = sorted(input_dir.rglob("*.json"))
    else:
        files = sorted(input_dir.glob("*.json"))
    return [p for p in files if p.is_file()]


def normalize_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def extract_strings(obj: Any) -> Iterable[str]:
    if obj is None:
        return
    if isinstance(obj, str):
        t = obj.strip()
        if t:
            yield t
        return
    if isinstance(obj, (int, float, bool)):
        yield str(obj)
        return
    if isinstance(obj, list):
        for item in obj:
            yield from extract_strings(item)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            yield from extract_strings(v)
        return


def extract_scet_plat_text(data: Dict[str, Any], file_path: Path) -> Tuple[str, Dict[str, Any]]:
    source = data.get("source", {}) if isinstance(data, dict) else {}
    issue = data.get("issue", {}) if isinstance(data, dict) else {}
    fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
    rendered = issue.get("renderedFields", {}) if isinstance(issue, dict) else {}

    issue_key = source.get("issueKey") or issue.get("key") or file_path.stem

    lines: List[str] = []
    lines.append(f"issue_key: {issue_key}")

    summary = fields.get("summary")
    if summary:
        lines.append(f"summary: {summary}")

    description = fields.get("description")
    if description:
        desc_text = "\n".join(extract_strings(description))
        if desc_text:
            lines.append("description:")
            lines.append(desc_text)

    rendered_description = rendered.get("description")
    if rendered_description:
        lines.append("rendered_description:")
        lines.append("\n".join(extract_strings(rendered_description)))

    labels = fields.get("labels")
    if labels:
        lines.append("labels: " + ", ".join([str(x) for x in labels]))

    components = fields.get("components")
    if components:
        comp_names = []
        for c in components:
            if isinstance(c, dict):
                n = c.get("name")
                if n:
                    comp_names.append(str(n))
        if comp_names:
            lines.append("components: " + ", ".join(comp_names))

    for key in ["issuetype", "priority", "status", "resolution", "assignee", "reporter"]:
        v = fields.get(key)
        if isinstance(v, dict):
            name = v.get("name") or v.get("displayName")
            if name:
                lines.append(f"{key}: {name}")

    comment_obj = fields.get("comment")
    if isinstance(comment_obj, dict):
        comments = comment_obj.get("comments")
        if isinstance(comments, list) and comments:
            lines.append("comments:")
            for c in comments:
                text = "\n".join(extract_strings(c))
                text = normalize_text(text)
                if text:
                    lines.append(text)

    joined = normalize_text("\n".join(lines))
    if len(joined) < 200:
        fallback = "\n".join(extract_strings(fields))
        fallback = normalize_text(fallback)
        if fallback:
            lines.append("fallback_fields:")
            lines.append(fallback[:20000])

    text = normalize_text("\n".join(lines))
    metadata = {
        "issue_key": issue_key,
        "source_file": str(file_path.as_posix()),
        "issue_url": source.get("issueUrl"),
        "exported_at_utc": source.get("exportedAtUtc"),
    }
    return text, metadata


def split_text(text: str, chunk_size: int = 50000, overlap: int = 0) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: List[str] = []
    start = 0
    n = len(text)

    while start < n:
        end = min(start + chunk_size, n)
        chunk = text[start:end]

        if end < n:
            cut_candidates = [chunk.rfind("\n\n"), chunk.rfind("\n"), chunk.rfind("。"), chunk.rfind("，"), chunk.rfind(" ")]
            cut = max(cut_candidates)
            if cut > int(chunk_size * 0.6):
                end = start + cut + 1
                chunk = text[start:end]

        chunk = normalize_text(chunk)
        if chunk:
            chunks.append(chunk)

        if end >= n:
            break
        start = max(end - overlap, start + 1)

    return chunks


def tokenize_for_bm25(text: str) -> List[str]:
    text = text.lower()
    en_tokens = re.findall(r"[a-z0-9_]+", text)
    zh_chars = re.findall(r"[\u4e00-\u9fff]", text)
    return en_tokens + zh_chars


def encode_with_model(model: SentenceTransformer, texts: List[str], batch_size: int) -> np.ndarray:
    vectors = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return vectors.astype(np.float32)


def main() -> int:
    start_ts = time.time()
    parser = argparse.ArgumentParser(description="Build Jira RAG index (fully offline)")
    parser.add_argument("--input-dir", required=True, help="Knowledge-base JSON root directory (required)")
    parser.add_argument("--output-dir", required=True, help="Index output directory (required)")
    parser.add_argument("--recursive", action="store_true", default=True, help="Read JSON recursively (default: on)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="Read top-level JSON only")
    parser.add_argument("--max-files", type=int, default=0, help="Process only first N files (0 = all)")

    parser.add_argument(
        "--doc-as-one-chunk",
        action="store_true",
        help="Treat each JSON as one complete chunk (recommended for your scenario)",
    )
    parser.add_argument("--chunk-size", type=int, default=50000, help="Chunk size (characters)")
    parser.add_argument("--chunk-overlap", type=int, default=0, help="Chunk overlap (characters)")

    parser.add_argument("--embedding-model", default="BAAI/bge-small-zh-v1.5", help="Local embedding model name")
    parser.add_argument("--batch-size", type=int, default=64, help="Embedding batch size")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Input directory does not exist: {input_dir}")
        return 1

    files = collect_json_files(input_dir, args.recursive)
    if args.max_files and args.max_files > 0:
        files = files[:args.max_files]

    if not files:
        print("No JSON files found.")
        return 1

    print(f"Discovered JSON files: {len(files)}")
    print("Start parsing and chunking...")

    chunks: List[str] = []
    chunk_metas: List[Dict[str, Any]] = []
    ok_files = 0
    failed_files = 0

    for fp in files:
        try:
            with fp.open("r", encoding="utf-8") as f:
                data = json.load(f)
            text, meta = extract_scet_plat_text(data, fp)

            if args.doc_as_one_chunk:
                pieces = [text] if text else []
            else:
                pieces = split_text(text, chunk_size=args.chunk_size, overlap=args.chunk_overlap)

            for idx, c in enumerate(pieces):
                chunks.append(c)
                chunk_metas.append(
                    {
                        **meta,
                        "chunk_id_local": idx,
                        "text_len": len(c),
                    }
                )
        except Exception as e:
            failed_files += 1
            print(f"[WARN] Failed to parse {fp}: {e}")
        else:
            ok_files += 1

    if not chunks:
        print("No usable text chunks; index build terminated.")
        return 2

    print(f"Total text chunks: {len(chunks)}")
    print(f"Loading local embedding model: {args.embedding_model}")
    model = SentenceTransformer(args.embedding_model)

    print("Start generating local embeddings...")
    vec_np = encode_with_model(model, chunks, args.batch_size)
    dim = vec_np.shape[1]

    faiss.normalize_L2(vec_np)
    index = faiss.IndexFlatIP(dim)
    index.add(vec_np)

    index_path = output_dir / "index.faiss"
    faiss.write_index(index, str(index_path))

    chunks_path = output_dir / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8") as f:
        for i, (text, meta) in enumerate(zip(chunks, chunk_metas)):
            obj = {
                "id": i,
                "text": text,
                "meta": meta,
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    bm25_path = output_dir / "bm25_tokens.jsonl"
    with bm25_path.open("w", encoding="utf-8") as f:
        for i, text in enumerate(chunks):
            obj = {"id": i, "tokens": tokenize_for_bm25(text)}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    manifest = {
        "mode": "offline_local_embedding",
        "input_dir": str(input_dir.as_posix()),
        "output_dir": str(output_dir.as_posix()),
        "json_file_count": len(files),
        "chunk_count": len(chunks),
        "embedding_model": args.embedding_model,
        "embedding_dim": int(dim),
        "doc_as_one_chunk": bool(args.doc_as_one_chunk),
        "chunk_size": args.chunk_size,
        "chunk_overlap": args.chunk_overlap,
        "metric": "cosine_via_ip_on_normalized_vectors",
        "files": {"faiss_index": "index.faiss", "chunks": "chunks.jsonl", "bm25_tokens": "bm25_tokens.jsonl"},
    }
    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("Index build completed")
    print(f"Output directory: {output_dir}")
    print(f"- {index_path.name}")
    print(f"- {chunks_path.name}")
    print(f"Processed files: total={len(files)}, success={ok_files}, failed={failed_files}")
    elapsed = time.time() - start_ts
    mins = int(elapsed // 60)
    secs = elapsed - mins * 60

    print(f"- {bm25_path.name}")
    print(f"- {manifest_path.name}")
    print(f"Total elapsed time: {elapsed:.2f}s ({mins} min {secs:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
