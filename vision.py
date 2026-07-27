#!/usr/bin/env python3
"""
vision.py — agent-eye v2 眼睛组件

截图 + DOM 提取 + 类型推断。
为 thinker 提供标准化的观测输入。

返回 Observation dict:
    {
        "url": str,
        "title": str,
        "body_snippet": str,     # 页面正文前 2000 字符
        "meta_desc": str,
        "screenshot_path": str,  # 截图文件路径
        "source_type": str,      # academic/github/paper/forum/web
    }
"""

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

SCREENSHOT_DIR = Path(__file__).parent / ".screenshots"


async def _get_title(page) -> str:
    try:
        return (await page.evaluate("document.title")).strip() or ""
    except Exception:
        return ""


async def _get_meta_desc(page) -> str:
    try:
        return (await page.evaluate("""() => {
            const m = document.querySelector('meta[name="description"]');
            return m ? (m.getAttribute('content') || '') : '';
        }""")) or ""
    except Exception:
        return ""


async def _get_body_snippet(page, max_chars=2000) -> str:
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


def infer_type(netloc: str) -> str:
    """从域名推断来源类型。"""
    if "scholar.google" in netloc: return "academic"
    if "github.com" in netloc: return "github"
    if "arxiv.org" in netloc: return "paper"
    if any(x in netloc for x in ["reddit", "v2ex", "zhihu", "csdn"]): return "forum"
    return "web"


def infer_source_label(netloc: str, source_type: str) -> str:
    labels = {
        "academic": "📄 学术", "github": "🗂️ GitHub",
        "paper": "📄 论文", "forum": "💬 论坛"
    }
    return labels.get(source_type, f"🌐 {netloc}")


async def observe(page, url: str) -> dict:
    """
    对当前页面截图 + 提取内容，返回标准化 Observation。
    page 必须是已导航到目标 URL 的 Playwright Page 对象。
    """
    title = await _get_title(page)
    meta_desc = await _get_meta_desc(page)
    body = await _get_body_snippet(page)
    shot = await _screenshot(page, url)

    netloc = urlparse(url).netloc
    stype = infer_type(netloc)
    label = infer_source_label(netloc, stype)

    return {
        "url": url,
        "title": title or url,
        "body_snippet": body,
        "meta_desc": meta_desc,
        "screenshot_path": shot,
        "source_type": stype,
        "source_label": label,
    }
