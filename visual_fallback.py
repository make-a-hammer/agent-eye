#!/usr/bin/env python3
"""
visual_fallback.py — agent-eye 向后兼容层

v2 重构后保持 search_visual / generate_search_urls 接口不变，
内部委托给 vision / hand / thinker / loop。

同时提供无 LLM 的 fallback 模式：直接提取页面内容，不做 AI 决策。
"""

import asyncio
import json
import textwrap

from vision import observe
from hand import BrowserSession
from sources import generate_search_urls


HAS_PLAYWRIGHT = False
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    pass


# ─── 单页提取（无 LLM fallback）───────────────────────


async def extract_page(url: str, query: str, timeout: int = 20000) -> dict | None:
    """提取单个页面（无 LLM fallback 模式）。"""
    if not HAS_PLAYWRIGHT:
        return None

    async with BrowserSession(timeout=timeout) as hand:
        ok, detail = await hand.navigate(url)
        if not ok:
            return {
                "title": f"[无法访问] {url}",
                "url": url,
                "desc": detail[:150],
                "info": "⚠️ 视觉通道失败",
                "source": "visual",
                "type": "web",
                "download_url": None,
            }

        obs = await observe(hand.page, url)
        desc = textwrap.shorten(
            (obs["meta_desc"] or obs["body_snippet"][:200]).strip(),
            width=200, placeholder="..."
        )

        return {
            "title": obs["title"],
            "url": obs["url"],
            "desc": desc,
            "info": f"{obs['source_label']} | 视觉提取",
            "source": "visual",
            "type": obs["source_type"],
            "download_url": None,
        }


async def visual_fallback(query: str, urls: list[str], max_results: int = 3) -> list[dict]:
    """异步批量提取多个页面。"""
    if not HAS_PLAYWRIGHT or not urls:
        return []
    tasks = [extract_page(url, query) for url in urls[:max_results]]
    raw = await asyncio.gather(*tasks)
    return [r for r in raw if r]


def search_visual(query: str, urls: list[str], max_results: int = 3) -> list[dict]:
    """同步入口（v1 兼容）。"""
    if not HAS_PLAYWRIGHT:
        print("⚠️ 视觉后备通道需要 Playwright: pip install playwright && playwright install chromium")
        return []
    return asyncio.run(visual_fallback(query, urls, max_results))


# ─── v2 入口（带 LLM）────────────────────────────────


def search_with_llm(query: str, llm, urls: list[str] | None = None, max_steps: int = 10) -> dict:
    """v2 主入口：AI 驱动的循环搜索。"""
    from loop import run_agent

    start_url = (urls or generate_search_urls(query))[0]
    return asyncio.run(run_agent(
        start_url=start_url, query=query, llm=llm, max_steps=max_steps,
    ))


# ─── CLI ────────────────────────────────────────────


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "transformer attention"
    print(f"🔍 visual_fallback: {q}")
    results = search_visual(q, generate_search_urls(q))
    print(json.dumps(results, indent=2, ensure_ascii=False))
