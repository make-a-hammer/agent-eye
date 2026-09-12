#!/usr/bin/env python3
"""
ebook_source.py — 电子书资源搜索（agent-eye 数据源）

Z-Library 多入口自动切换 + 浏览器渲染（过 Cloudflare/DiamWall 反爬）。
结果来自 <z-bookcard> Web Component，元数据完整（ISBN/出版社/年份/格式/大小/下载链接）。

用法（需 python 3.11，greenlet 兼容）:
    python ebook_source.py "数学建模"
    python ebook_source.py "深入理解计算机系统" --json
    python ebook_source.py "线性代数" --limit 10
"""

import argparse
import asyncio
import json
import sys

from playwright.async_api import async_playwright

# 入口列表（按 2026-09-12 实测可用性排序）
ENTRIES = [
    "https://zh.z-lib.by",       # ✅ 实测可用（中文）
    "https://z-lib.by",          # ✅ 英文站
    "https://zh.z-library.sk",   # 🟡 DiamWall 反爬
    "https://zh.z-lib.gd",       # 🟡 备用
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def search_ebook(keyword: str, max_results: int = 20,
                       entry: str | None = None,
                       headless: bool = True) -> list[dict]:
    """
    搜索电子书。自动尝试多个入口，失败则切换。

    Returns:
        [{"title","author","year","publisher","language","extension",
          "filesize","isbn","rating","url","download"}, ...]
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("需要 playwright: pip install playwright && playwright install chromium")

    entries = [entry] if entry else ENTRIES
    last_err = None

    for base in entries:
        try:
            results = await _search_one(base, keyword, max_results, headless)
            if results:
                return results
        except Exception as e:
            last_err = f"{base}: {type(e).__name__} {str(e)[:60]}"
            continue

    if last_err:
        print(f"⚠️ 所有入口失败: {last_err}", file=sys.stderr)
    return []


async def _search_one(base: str, keyword: str, max_results: int,
                      headless: bool) -> list[dict]:
    """在单个入口搜索。"""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(user_agent=UA, locale="zh-CN",
                                        viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        # 直接用搜索 URL（比模拟输入快）
        from urllib.parse import quote
        await page.goto(f"{base}/s/{quote(keyword)}",
                        wait_until="domcontentloaded", timeout=35000)
        # 等结果渲染（Cloudflare 挑战 + SPA 渲染）
        try:
            await page.wait_for_selector("z-bookcard", timeout=20000)
        except Exception:
            await page.wait_for_timeout(8000)

        items = await page.evaluate("""(limit) => {
            const cards = document.querySelectorAll('z-bookcard');
            const out = [];
            cards.forEach((c, i) => {
                if (i >= limit) return;
                const get = (a) => c.getAttribute(a) || '';
                const slot = (n) => {
                    const el = c.querySelector(`[slot="${n}"]`);
                    return el ? el.textContent.trim() : '';
                };
                out.push({
                    title: slot('title') || '',
                    author: slot('author') || '',
                    year: get('year'),
                    publisher: get('publisher'),
                    language: get('language'),
                    extension: get('extension'),
                    filesize: get('filesize'),
                    isbn: get('isbn'),
                    rating: get('rating'),
                    href: get('href'),
                    download: get('download'),
                });
            });
            return out;
        }""", max_results)

        await browser.close()

    # 补全 URL
    for it in items:
        if it.get("href"):
            it["url"] = base + it["href"] if it["href"].startswith("/") else it["href"]
        if it.get("download"):
            it["download_url"] = base + it["download"] if it["download"].startswith("/") else it["download"]
    return [i for i in items if i.get("title")]


def print_results(results: list[dict], keyword: str):
    if not results:
        print(f"❌ 没找到 '{keyword}' 的结果（所有入口失败或确实没有）")
        return
    print(f"\n📚 '{keyword}' 电子书搜索结果（{len(results)} 条）:")
    print("=" * 78)
    for i, r in enumerate(results, 1):
        meta = " | ".join(filter(None, [
            r.get("author"), r.get("year"), r.get("publisher"),
            r.get("language"), r.get("extension", "").upper(),
            r.get("filesize"), f"★{r['rating']}" if r.get("rating") else ""
        ]))
        print(f"\n[{i}] {r['title']}")
        print(f"    {meta}")
        if r.get("isbn"):
            print(f"    ISBN: {r['isbn']}")
        if r.get("url"):
            print(f"    {r['url']}")


def main():
    p = argparse.ArgumentParser(description="电子书资源搜索（Z-Library 多入口）")
    p.add_argument("keyword", help="书名/作者/ISBN/关键词")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--limit", type=int, default=20, help="最大结果数")
    p.add_argument("--entry", help="指定入口（默认自动尝试多个）")
    p.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    args = p.parse_args()

    print(f"🔍 搜索电子书: '{args.keyword}'")
    results = asyncio.run(search_ebook(
        args.keyword, max_results=args.limit,
        entry=args.entry, headless=not args.headful))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_results(results, args.keyword)


if __name__ == "__main__":
    main()
