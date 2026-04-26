#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SCET ticket export module.

用途：
1) 被 main.py 调用：当监控到 Jira ticket 创建/更新时，下载对应 ticket 的完整内容到本地 JSON
2) 独立调试：可手动指定 --browse-url 下载单个 ticket

说明：
- 导出内容包含：
  - issue 主体（含 expand）
  - comments / worklogs / changelog 分页数据（尽量拉全）

python scet_ticket_exporter.py --browse-url [SCET Ticket URL]

"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, parse, request

DEFAULT_OUTPUT_DIR = "SCET_export_runtime"
ENV_EXTERNAL_JIRA_TOKEN = "EXTERNAL_JIRA_TOKEN"

def parse_browse_url(browse_url: str) -> Dict[str, str]:

    m = re.match(r"^(https?://[^/]+)/browse/([A-Za-z][A-Za-z0-9_]*-\d+)$", browse_url.strip())
    if not m:
        raise ValueError(f"无法解析 browse URL: {browse_url}")
    return {"base_url": m.group(1), "issue_key": m.group(2)}


def build_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "scet-ticket-exporter/1.0",
    }


def http_get_text(url: str, token: str, timeout: int = 60) -> str:
    req = request.Request(url=url, method="GET", headers=build_headers(token))
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} 请求失败: {url}\n响应: {body}") from e
    except error.URLError as e:
        raise RuntimeError(f"网络请求失败: {url}\n原因: {e.reason}") from e


def http_get_json(url: str, token: str, timeout: int = 60) -> Any:
    text = http_get_text(url, token, timeout=timeout)
    return json.loads(text) if text else {}


def fetch_paginated(url: str, token: str, item_keys: List[str], page_size: int = 100) -> Dict[str, Any]:
    """
    拉取 Jira 常见分页接口数据。
    典型分页字段:
      - startAt, maxResults, total
      - 数据数组字段可能是 values / comments / worklogs / histories
    """
    start_at = 0
    all_items: List[Any] = []
    total: Optional[int] = None
    pages = 0

    while True:
        parsed = parse.urlparse(url)
        query = dict(parse.parse_qsl(parsed.query, keep_blank_values=True))
        query["startAt"] = str(start_at)
        query["maxResults"] = str(page_size)

        paged_url = parse.urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                parse.urlencode(query),
                parsed.fragment,
            )
        )

        data = http_get_json(paged_url, token)
        pages += 1

        items = None
        for key in item_keys:
            if isinstance(data, dict) and key in data and isinstance(data[key], list):
                items = data[key]
                break

        if items is None:
            if pages == 1:
                return {"items": [], "total": 0, "raw": data}
            break

        all_items.extend(items)

        if isinstance(data, dict):
            total = data.get("total", total)
            max_results = data.get("maxResults", len(items))
        else:
            max_results = len(items)

        if len(items) == 0:
            break

        start_at += max_results if isinstance(max_results, int) and max_results > 0 else len(items)

        if total is not None and len(all_items) >= total:
            break

    return {
        "items": all_items,
        "total": total if total is not None else len(all_items),
        "count": len(all_items),
    }


def fetch_issue_data(base_url: str, issue_key: str, token: str) -> Dict[str, Any]:
    api_v2 = f"{base_url.rstrip('/')}/rest/api/2"
    api_v3 = f"{base_url.rstrip('/')}/rest/api/3"

    # 1) 主 issue 数据
    expand = "names,schema,operations,editmeta,changelog,renderedFields,versionedRepresentations"
    issue_url = f"{api_v2}/issue/{issue_key}?expand={parse.quote(expand, safe=',')}"
    issue = http_get_json(issue_url, token)

    result: Dict[str, Any] = {
        "source": {
            "baseUrl": base_url,
            "issueKey": issue_key,
            "issueUrl": f"{base_url.rstrip('/')}/browse/{issue_key}",
            "exportedAtUtc": datetime.now(timezone.utc).isoformat(),
        },
        "issue": issue,
        "extras": {},
        "warnings": [],
    }

    # 2) comments 分页
    try:
        comments_url = f"{api_v2}/issue/{issue_key}/comment"
        comments = fetch_paginated(comments_url, token, item_keys=["comments", "values"])
        result["extras"]["comments"] = comments
    except Exception as e:
        result["warnings"].append(f"comments 拉取失败: {e}")

    # 3) worklogs 分页
    try:
        worklogs_url = f"{api_v2}/issue/{issue_key}/worklog"
        worklogs = fetch_paginated(worklogs_url, token, item_keys=["worklogs", "values"])
        result["extras"]["worklogs"] = worklogs
    except Exception as e:
        result["warnings"].append(f"worklogs 拉取失败: {e}")

    # 4) changelog 分页（优先 v2，失败尝试 v3）
    changelog_fetched = False
    for base in (api_v2, api_v3):
        try:
            changelog_url = f"{base}/issue/{issue_key}/changelog"
            changelog = fetch_paginated(changelog_url, token, item_keys=["histories", "values"])
            result["extras"]["changelog"] = changelog
            changelog_fetched = True
            break
        except Exception:
            continue

    if not changelog_fetched:
        result["warnings"].append("changelog 分页接口不可用，已保留 issue.expand.changelog 中返回的数据（如有）。")

    return result


def save_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def export_issue_to_file(base_url: str, issue_key: str, token: str, output_dir: str = DEFAULT_OUTPUT_DIR) -> str:
    """
    下载指定 issue 并保存到 output_dir/{issue_key}.json
    返回：输出文件绝对路径（str）
    """
    safe_issue_key = issue_key.strip()
    if not safe_issue_key:
        raise ValueError("issue_key 不能为空")

    data = fetch_issue_data(base_url, safe_issue_key, token)
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{safe_issue_key}.json"
    save_json(str(out_file), data)
    return str(out_file)


def export_issue_by_browse_url(browse_url: str, token: str, output_dir: str = DEFAULT_OUTPUT_DIR) -> str:
    """
    通过 browse URL 下载 ticket 并保存到本地 JSON。
    """
    parsed = parse_browse_url(browse_url)
    return export_issue_to_file(
        base_url=parsed["base_url"],
        issue_key=parsed["issue_key"],
        token=token,
        output_dir=output_dir,
    )


def resolve_jira_token(cli_token: str) -> str:
    """
    token 获取策略（用于独立调试）：
    1) 优先使用 --token
    2) 若未传，则从环境变量中查找
    """
    token = (cli_token or "").strip()
    if token:
        return token

    env_token = os.getenv(ENV_EXTERNAL_JIRA_TOKEN, "").strip()
    if env_token:
        return env_token

    raise RuntimeError(f"缺少 Jira token。请通过 --token 传入，或设置环境变量: {ENV_EXTERNAL_JIRA_TOKEN}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载单个 Jira ticket 数据并保存为 JSON")
    parser.add_argument("--browse-url", required=True, help="Jira issue browse URL")
    parser.add_argument("--token", default="", help="Jira Bearer Token（可选；不传则自动从环境变量读取）")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="输出目录")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        token = resolve_jira_token(args.token)
        out_file = export_issue_by_browse_url(args.browse_url, token, args.output_dir)
        print(f"[ok] 导出完成: {out_file}")
        return 0
    except Exception as e:
        print(f"[error] 执行失败: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
