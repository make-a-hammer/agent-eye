#!/usr/bin/env python3
"""
visual_fallback.py — 视觉后备通道

当 API 搜索返回结果不足时，用 Playwright 打开目标网页，
提取结构化内容，返回与 search.py 兼容的结果格式。

用法:
    from visual_fallback import search_visual, generate_search_urls
    results = search_visual("关键词", generate_search_urls("关键词"))
"""

import asyncio
import json
import re
import textwrap
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote

HAS_PLAYWRIGHT = False
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    pass

SCREENSHOT_DIR = Path(__file__).parent / ".screenshots"


async def _get_title(page) -> str:
    try:
        return (await page.evaluate("document.title")).strip() or ""
    except Exception:
        return ""


async def _get_meta_desc(page) -> str:
    try:
        return await page.evaluate("""() => {
            const m = document.querySelector('meta[name="description"]');
            return m ? (m.getAttribute('content') || '') : '';
        }""") or ""
    except Exception:
        return ""


async def _get_body_text(page, max_chars=2000) -> str:
    try:
        return await page.evaluate("""(mc) => {
            const sel = document.querySelector(
                'article,[role="main"],main,.content,#content,.post,.entry');
            const root = (sel && sel.textContent.trim().length > 80) ? sel : document.body;
            const els = root.querySelectorAll('p,li,h2,h3,h4,pre code');
            return Array.from(els).map(e => e.textContent.trim())
                .filter(t => t.length > 10).join('\\n').slice(0, mc);
        }""", max_chars) or ""
    except Exception:
        return ""


async def _screenshot(page, url: str) -> str | None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[^a-zA-Z0-9]', '_', urlparse(url).netloc)[:40]
    ts = datetime.now().strftime("%H%M%S")
    path = str(SCREENSHOT_DIR / f"{name}_{ts}.png")
    try:
        await page.screenshot(path=path, full_page=False)
        return path
    except Exception:
        return None


def _infer_type(netloc: str) -> str:
    if "scholar.google" in netloc: return "academic"
    if "github.com" in netloc: return "github"
    if "arxiv.org" in netloc: return "paper"
    if any(x in netloc for x in ["reddit", "v2ex", "zhihu", "csdn"]): return "forum"
    return "web"


def _infer_source_label(netloc: str, source_type: str) -> str:
    labels = {"academic": "📄 学术", "github": "🗂️ GitHub", "paper": "📄 论文", "forum": "💬 论坛"}
    return labels.get(source_type, f"🌐 {netloc}")


async def extract_page(url: str, query: str, timeout: int = 20000) -> dict | None:
    if not HAS_PLAYWRIGHT:
        return None

    async with async_playwright() as pw:
        ctx = None
        browser = None
        is_persistent = False
        try:
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir="C:/Users/小白本/ego_profile",
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                is_persistent = True
            except Exception:
                browser = await pw.chromium.launch(headless=True)
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )

            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(2000)

            title = await _get_title(page)
            meta_desc = await _get_meta_desc(page)
            body = await _get_body_text(page)
            await _screenshot(page, url)

            netloc = urlparse(url).netloc
            stype = _infer_type(netloc)
            label = _infer_source_label(netloc, stype)

            desc = textwrap.shorten((meta_desc or body[:200]).strip(), width=200, placeholder="...")

            return {
                "title": title or url,
                "url": url,
                "desc": desc,
                "info": f"{label} | 视觉提取",
                "source": "visual",
                "type": stype,
                "download_url": None,
            }

        except Exception as e:
            netloc = urlparse(url).netloc
            return {
                "title": f"[无法访问] {netloc}",
                "url": url,
                "desc": str(e)[:150],
                "info": "⚠️ 视觉通道失败",
                "source": "visual",
                "type": "web",
                "download_url": None,
            }
        finally:
            if is_persistent and ctx:
                await ctx.close()
            elif browser:
                await browser.close()


async def visual_fallback(query: str, urls: list[str], max_results: int = 3) -> list[dict]:
    if not HAS_PLAYWRIGHT or not urls:
        return []
    tasks = [extract_page(url, query) for url in urls[:max_results]]
    raw = await asyncio.gather(*tasks)
    return [r for r in raw if r]


def search_visual(query: str, urls: list[str], max_results: int = 3) -> list[dict]:
    if not HAS_PLAYWRIGHT:
        print("⚠️ 视觉后备通道需要 Playwright: pip install playwright && playwright install chromium")
        return []
    return asyncio.run(visual_fallback(query, urls, max_results))


def generate_search_urls(query: str) -> list[str]:
    encoded = quote(query)
    return [
        f"https://scholar.google.com/scholar?q={encoded}&hl=en",
        f"https://github.com/search?q={encoded}&type=repositories&s=stars&o=desc",
        f"https://www.google.com/search?q={encoded}",
    ]


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "transformer attention"
    print(f"🔍 视觉搜索: {q}")
    results = search_visual(q, generate_search_urls(q))
    print(json.dumps(results, indent=2, ensure_ascii=False))
