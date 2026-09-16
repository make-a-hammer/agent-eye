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


# ─── 深挖源（报告 §1/§2：一手权威层 + 结构化知识层）───

def _deep(source: str):
    """生成深挖源包装函数。"""
    def fn(query: str, max_results: int = 5) -> list[dict]:
        try:
            from sources_deep import fetch_deep
            return fetch_deep(source, query, max_results)
        except Exception as e:
            print(f"  ⚠️ {source}: {str(e)[:80]}")
            return []
    return fn


SOURCES = {
    "web": fetch_web,
    "papers": fetch_papers,
    "trends": fetch_trends,
    "openalex_deep": _deep("openalex_deep"),   # 论文+作者+机构+引用网络
    "crossref": _deep("crossref"),             # DOI 元数据（OpenAlex 备源）
    "wikidata": _deep("wikidata"),             # 实体消歧（结构化事实）
    "github": _deep("github"),                 # 仓库深挖（stars/语言/许可/标签）
    "youtube_deep": _deep("youtube_deep"),     # 视频深挖（播放/时长/频道/日期）
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


ANALYZE_PROMPT = """你是情报分析师。给你一份多源收集的资料（来源标注: web/openalex/youtube/bilibili）。

**核心原则：先建立 claim-evidence 映射，再写结论。** 不要把没有直接证据的说法写进去。

你需要：
1. 去重合并（同主题不同来源的条目合并，保留各来源）
2. 按主题分组，每组写 1-2 句洞察
3. **为每个关键主张（claim）绑定证据**：明确它来自哪条资料的哪句话，标注支持强度
4. 标注最重要的 3 条发现
5. 对**证据不足或存在冲突**的主张，必须显式标注 `"confidence": "low"` 并说明原因

严格输出 JSON（不要 markdown 代码块）:
{{
  "groups": [
    {{
      "topic": "主题名",
      "insight": "洞察",
      "claims": [
        {{
          "text": "这个主张的原文",
          "evidence": [
            {{"ref": "资料编号(如[3])", "support": "direct|partial|circumstantial"}}
          ],
          "confidence": "high|medium|low"
        }}
      ]
    }}
  ],
  "top_picks": ["最重要的条目标题（来源）", ...],
  "summary": "150字以内的总结",
  "gaps": ["证据不足、无法判断的问题", ...]
}}

**支持强度定义**：
- direct：资料直接陈述了该主张
- partial：资料部分支持，或需要推断
- circumstantial：仅有间接线索"""


def _parse_json_lenient(raw: str) -> dict:
    """健壮 JSON 解析：去 markdown 包裹、修截断、提取首尾大括号。"""
    s = raw.strip()
    # 去 ```json ... ``` 包裹
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    # 提取首个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j+1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 截断修复：补齐未闭合的括号
        for suffix in ['"}]}', '"}]}}', '}]}', '}]', '}', '"]}}}', ']}}}']:
            try:
                return json.loads(s + suffix)
            except json.JSONDecodeError:
                continue
        raise


def analyze(query: str, items: list[dict], llm) -> dict:
    """LLM 分析去重 + Claim-Evidence 绑定，输出可审计简报。"""
    if not items:
        return {"groups": [], "top_picks": [], "summary": "没有收集到数据", "gaps": []}

    # 压缩数据：每条约 200 字符（带编号，供 evidence.ref 引用）
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
        data = _parse_json_lenient(raw)
        return {
            "groups": data.get("groups", []),
            "top_picks": data.get("top_picks", []),
            "summary": data.get("summary", ""),
            "gaps": data.get("gaps", []),
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
    """终端友好输出（含 claim-evidence 展示）。"""
    print(f"\n{'='*60}")
    print(f"  📋 情报简报 — \"{query}\"")
    print(f"{'='*60}")

    print(f"\n📌 总结:\n{report['summary']}\n")

    for g in report["groups"]:
        print(f"▌{g['topic']}")
        if g.get("insight"):
            print(f"   {g['insight']}")

        # Claim-Evidence 绑定展示
        for c in g.get("claims", [])[:4]:
            conf = c.get("confidence", "?")
            mark = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "⚪")
            print(f"   {mark} {c.get('text', '')}")
            refs = []
            for ev in c.get("evidence", [])[:3]:
                sup = {"direct": "直接", "partial": "部分", "circumstantial": "间接"}.get(
                    ev.get("support", ""), ev.get("support", ""))
                refs.append(f"{ev.get('ref','?')}({sup})")
            if refs:
                print(f"      └ 证据: {', '.join(refs)}")

        # 兼容旧格式
        for it in g.get("items", [])[:5]:
            print(f"   • {it}")
        print()

    if report.get("top_picks"):
        print("⭐ 最重要的发现:")
        for p in report["top_picks"][:3]:
            print(f"   • {p}")

    # 证据缺口（报告最有价值的部分之一）
    if report.get("gaps"):
        print("\n⚠️ 证据缺口（无法从当前资料判断）:")
        for gp in report["gaps"][:5]:
            print(f"   • {gp}")

    print(f"\n{'='*60}")
    print(f"  来源明细（{len(items)} 条，按综合分排序）:")
    for it in items[:15]:
        src = it["source"]
        score = it.get("_score")
        s = f" [{score:.0f}分]" if score is not None else ""
        print(f"   [{src}]{s} {it['title'][:65]}")
        if it.get("url"):
            print(f"        {it['url'][:100]}")


def to_json(query: str, report: dict, items: list[dict]) -> str:
    return json.dumps({
        "query": query,
        "query_type": report.get("query_type", ""),
        "summary": report["summary"],
        "groups": report["groups"],
        "top_picks": report["top_picks"],
        "gaps": report.get("gaps", []),
        "items": items,
    }, ensure_ascii=False, indent=2)


# ─── CLI ─────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description="agent-eye 情报简报")
    p.add_argument("query", help="情报主题")
    p.add_argument("--sources", default="",
                   help="数据源列表，逗号分隔（web,papers,trends）；留空=自动分类选择")
    p.add_argument("--max", type=int, default=8, help="每源最多条数")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--no-llm", action="store_true", help="跳过 LLM 分析（原始分组）")
    args = p.parse_args()

    # 查询理解分类 → 决定源 + 加权特征
    from sources import classify_query, score_and_dedup
    qtype = classify_query(args.query)
    print(f"🧭 查询类型: {qtype['type']} | 加权: "
          f"时效={'✓' if qtype.get('freshness') else '✗'} "
          f"权威={'✓' if qtype.get('authority') else '✗'} "
          f"多样性={'✓' if qtype.get('diversity') else '✗'}")

    if args.sources:
        source_list = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        # 自动选源，过滤掉本模块不支持的（如 code）
        source_list = [s for s in qtype["sources"] if s in SOURCES]
        if not source_list:
            source_list = ["web"]

    items = collect(args.query, source_list, args.max)

    # 多源结果统一打分 + 多样性降权
    if items:
        items = score_and_dedup(items, args.query)

    if args.no_llm:
        groups = defaultdict(list)
        for it in items:
            groups[it["source"]].append(f"{it['title']}（{it['source']}）")
        report = {
            "query_type": qtype["type"],
            "groups": [{"topic": k, "insight": "", "items": v} for k, v in groups.items()],
            "top_picks": [],
            "summary": f"共收集 {len(items)} 条（未启用 LLM 分析）",
            "gaps": [],
        }
    else:
        from llm_client import create_llm
        try:
            llm = create_llm(provider="deepseek")
            report = analyze(args.query, items, llm)
            report["query_type"] = qtype["type"]
        except Exception as e:
            print(f"  ⚠️ LLM 不可用（{e}），降级为原始分组")
            groups = defaultdict(list)
            for it in items:
                groups[it["source"]].append(f"{it['title']}（{it['source']}）")
            report = {
                "query_type": qtype["type"],
                "groups": [{"topic": k, "insight": "", "items": v} for k, v in groups.items()],
                "top_picks": [],
                "summary": f"共收集 {len(items)} 条（LLM 降级）",
                "gaps": [],
            }

    if args.json:
        print(to_json(args.query, report, items))
    else:
        print_report(args.query, report, items)


if __name__ == "__main__":
    main()
