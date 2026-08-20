#!/usr/bin/env python3
"""
intel.py — agent-eye 情报简报（多源 → 分析 → 报告）

一条命令：多源拉取 → LLM 去重分析 → 带来源标注的情报简报。

数据源（可插拔，失败不影响其他）:
    web     — free_search.py（免费通用搜索）
    papers  — OpenAlex API（2.5亿+ 学术论文）
    trends  — trend_scout（YouTube + OpenBiliClaw 跨平台）

用法:
    python3 intel.py "固态电池 2026"                  # 全部源
    python3 intel.py "AI Agent" --sources web,trends  # 指定源
    python3 intel.py "报告标题" --json                 # JSON 输出
    python3 intel.py "主题" --max 5                    # 每源最多 5 条
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.parse
from collections import defaultdict

from ethics import log, ProxyConfig

FREE_SEARCH = "C:/Users/小白本/Downloads/aria2/free_search.py"


# ─── 数据源层 ────────────────────────────────────


def fetch_web(query: str, max_results: int = 8) -> list[dict]:
    """free_search.py 子进程调用。"""
    try:
        r = subprocess.run(
            [sys.executable, FREE_SEARCH, "--json", query, "--max", str(max_results)],
            capture_output=True, text=True, timeout=60,
        )
        d = json.loads(r.stdout)
        results = d.get("results", d) if isinstance(d, dict) else d
        items = []
        for x in results:
            title = (x.get("title") or "?").strip()
            if not title or title == "?" or "搜索失败" in title:
                continue
            items.append({
                "title": title[:120],
                "url": x.get("url", ""),
                "source": "web",
                "snippet": (x.get("content") or "")[:300],
            })
        return items
    except Exception as e:
        print(f"  ⚠️ web: {e}")
        return []


def fetch_papers(query: str, max_results: int = 5) -> list[dict]:
    """OpenAlex API（免费，无限制）。走代理（国外 API）。"""
    try:
        url = ("https://api.openalex.org/works?"
               f"search={urllib.parse.quote(query)}&per_page={max_results}")
        req = urllib.request.Request(url, headers={"User-Agent": "agent-eye-intel/2.0"})

        # 代理：urllib 不支持 socks5h，委托 curl（OpenAlex 需 HTTP 代理，socks 不通）
        proxy = ProxyConfig.detect()
        if proxy:
            import subprocess
            # 统一转成 http://127.0.0.1:PORT 格式（socks5h:// 也走 http 代理端口）
            host = proxy.replace("socks5h://", "").replace("socks5://", "")
            r = subprocess.run(
                ["curl", "-s", "--insecure", "-x", f"http://{host}", url],
                capture_output=True, text=True, timeout=30,
            )
            d = json.loads(r.stdout)
        else:
            with urllib.request.urlopen(req, timeout=30) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        items = []
        for w in d.get("results", []):
            title = w.get("title") or "?"
            authors = ", ".join(
                a.get("author", {}).get("display_name", "?")
                for a in (w.get("authorships") or [])[:3]
            )
            host = (w.get("primary_location") or {}).get("landing_page_url") or ""
            items.append({
                "title": title[:150],
                "url": host or f"https://doi.org/{w.get('doi','')}",
                "source": "openalex",
                "snippet": f"作者: {authors} | 年份: {w.get('publication_year','?')} | "
                           f"引用: {w.get('cited_by_count', 0)}",
            })
        return items
    except Exception as e:
        print(f"  ⚠️ papers: {e}")
        return []


def fetch_trends(query: str, max_results: int = 8) -> list[dict]:
    """trend_scout.scout() — YouTube + OpenBiliClaw。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from trend_scout import scout
        proxy = ProxyConfig.detect()
        results = scout(query, proxy=proxy, max_results=max_results)
        return [{
            "title": r.item.title[:120],
            "url": r.item.url,
            "source": r.item.platform,
            "snippet": f"播放:{r.item.views:,} 搬运价值:{r.move_score}/100 "
                       f"置信度:{r.confidence:.0%}",
        } for r in results[:max_results]]
    except Exception as e:
        print(f"  ⚠️ trends: {e}")
        return []


SOURCES = {
    "web": fetch_web,
    "papers": fetch_papers,
    "trends": fetch_trends,
}


def collect(query: str, source_list: list[str], max_per: int) -> list[dict]:
    """多源并行拉取（失败源自动跳过）。"""
    print(f"🔍 情报收集: \"{query}\"")
    print(f"   源: {', '.join(source_list)} | 每源上限: {max_per}\n")

    all_items = []
    for name in source_list:
        fn = SOURCES.get(name)
        if not fn:
            print(f"  ⚠️ 未知源: {name}（可用: {', '.join(SOURCES)}）")
            continue
        print(f"  📡 {name} ...", end="", flush=True)
        items = fn(query, max_per)
        print(f" {len(items)} 条")
        all_items.extend(items)

    log.log("intel", "collect", query,
            f"{len(all_items)} items from {','.join(source_list)}",
            "个人学术研究——多源情报收集与趋势分析")
    return all_items


# ─── 分析层 ──────────────────────────────────────


ANALYZE_PROMPT = """你是情报分析师。给你一份多源收集的资料（来源标注: web/openalex/youtube/bilibili），
你需要：
1. 去重合并（同主题不同来源的条目合并为一条，保留各来源）
2. 按主题分组，每组写 1-2 句洞察（趋势/信号/风险）
3. 标注最重要的 3 条（最有价值的发现）
4. 最后给一个总结（该主题当前态势 + 值得关注的方向）

严格输出 JSON（不要 markdown 代码块）:
{{
  "groups": [
    {{"topic": "主题名", "insight": "洞察", "items": ["标题1（来源）", ...]}}
  ],
  "top_picks": ["最重要的条目标题（来源）", ...],
  "summary": "150字以内的总结"
}}"""


def analyze(query: str, items: list[dict], llm) -> dict:
    """LLM 分析去重，输出结构化简报。"""
    if not items:
        return {"groups": [], "top_picks": [], "summary": "没有收集到数据"}

    # 压缩数据：每条约 200 字符
    compact = []
    for i, it in enumerate(items, 1):
        compact.append(
            f"[{i}] ({it['source']}) {it['title']}\n"
            f"    {it['snippet'][:200]}\n"
            f"    {it['url'][:150]}"
        )
    user_msg = f"## 主题\n{query}\n\n## 收集到的资料\n" + "\n".join(compact)

    try:
        raw = llm(ANALYZE_PROMPT, user_msg)
        data = json.loads(raw)
        return {
            "groups": data.get("groups", []),
            "top_picks": data.get("top_picks", []),
            "summary": data.get("summary", ""),
        }
    except Exception as e:
        # LLM 挂了就降级：直接按源分组
        groups = defaultdict(list)
        for it in items:
            groups[it["source"]].append(f"{it['title']}（{it['source']}）")
        return {
            "groups": [{"topic": k, "insight": "（LLM 分析失败，原始分组）",
                        "items": v[:10]} for k, v in groups.items()],
            "top_picks": [],
            "summary": f"LLM 分析失败: {e}",
        }


# ─── 输出层 ──────────────────────────────────────


def print_report(query: str, report: dict, items: list[dict]):
    """终端友好输出。"""
    print(f"\n{'='*60}")
    print(f"  📋 情报简报 — \"{query}\"")
    print(f"{'='*60}")

    print(f"\n📌 总结:\n{report['summary']}\n")

    for g in report["groups"]:
        print(f"▌{g['topic']}")
        print(f"   {g['insight']}")
        for it in g.get("items", [])[:5]:
            print(f"   • {it}")
        print()

    if report["top_picks"]:
        print("⭐ 最重要的发现:")
        for p in report["top_picks"][:3]:
            print(f"   • {p}")

    print(f"\n{'='*60}")
    print(f"  来源明细（{len(items)} 条）:")
    for it in items[:15]:
        src = it["source"]
        print(f"   [{src}] {it['title'][:70]}")
        if it["url"]:
            print(f"        {it['url'][:100]}")


def to_json(query: str, report: dict, items: list[dict]) -> str:
    return json.dumps({
        "query": query,
        "summary": report["summary"],
        "groups": report["groups"],
        "top_picks": report["top_picks"],
        "items": items,
    }, ensure_ascii=False, indent=2)


# ─── CLI ─────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description="agent-eye 情报简报")
    p.add_argument("query", help="情报主题")
    p.add_argument("--sources", default="web,papers,trends",
                   help="数据源列表，逗号分隔（web,papers,trends）")
    p.add_argument("--max", type=int, default=8, help="每源最多条数")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--no-llm", action="store_true", help="跳过 LLM 分析（原始分组）")
    args = p.parse_args()

    source_list = [s.strip() for s in args.sources.split(",") if s.strip()]
    items = collect(args.query, source_list, args.max)

    if args.no_llm:
        groups = defaultdict(list)
        for it in items:
            groups[it["source"]].append(f"{it['title']}（{it['source']}）")
        report = {
            "groups": [{"topic": k, "insight": "", "items": v} for k, v in groups.items()],
            "top_picks": [],
            "summary": f"共收集 {len(items)} 条（未启用 LLM 分析）",
        }
    else:
        from llm_client import create_llm
        try:
            llm = create_llm(provider="deepseek")
            report = analyze(args.query, items, llm)
        except Exception as e:
            print(f"  ⚠️ LLM 不可用（{e}），降级为原始分组")
            groups = defaultdict(list)
            for it in items:
                groups[it["source"]].append(f"{it['title']}（{it['source']}）")
            report = {
                "groups": [{"topic": k, "insight": "", "items": v} for k, v in groups.items()],
                "top_picks": [],
                "summary": f"共收集 {len(items)} 条（LLM 降级）",
            }

    if args.json:
        print(to_json(args.query, report, items))
    else:
        print_report(args.query, report, items)


if __name__ == "__main__":
    main()
