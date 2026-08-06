#!/usr/bin/env python3
"""
hand.py — agent-eye v2 手组件

BrowserSession 封装 Playwright 原子操作：
    navigate / click / type / scroll / wait / shoot

所有操作返回 (success: bool, detail: str)。
"""

import os
import asyncio

HAS_PLAYWRIGHT = False
try:
    from playwright.async_api import async_playwright, Page
    HAS_PLAYWRIGHT = True
except ImportError:
    Page = None  # type hint fallback


class BrowserSession:
    """管理一个 Playwright 浏览器会话。"""

    def __init__(self, headless: bool = False, timeout: int = 20000):
        self.headless = headless
        self.timeout = timeout
        self._playwright = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._is_persistent = False

    async def __aenter__(self):
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("Playwright 未安装: pip install playwright && playwright install chromium")
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=os.path.expanduser("~/ego_profile"),
                headless=self.headless,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            self._is_persistent = True
        except Exception:
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
        self._page = await self._context.new_page()
        return self

    async def __aexit__(self, *args):
        if self._is_persistent and self._context:
            await self._context.close()
        elif self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("BrowserSession 未启动，请使用 async with")
        return self._page

    async def navigate(self, url: str) -> tuple[bool, str]:
        """导航到 URL。"""
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            await self._page.wait_for_timeout(2000)
            title = await self._page.title()
            return True, title or url
        except Exception as e:
            return False, str(e)[:200]

    async def click(self, selector: str) -> tuple[bool, str]:
        """点击元素。"""
        try:
            await self._page.click(selector, timeout=self.timeout)
            await self._page.wait_for_timeout(1000)
            return True, f"clicked: {selector}"
        except Exception as e:
            return False, f"click failed ({selector}): {e}"

    async def type_text(self, selector: str, text: str) -> tuple[bool, str]:
        """在输入框中输入文字。"""
        try:
            await self._page.fill(selector, text, timeout=self.timeout)
            return True, f"typed into {selector}"
        except Exception as e:
            return False, f"type failed ({selector}): {e}"

    async def scroll(self, amount: int = 500) -> tuple[bool, str]:
        """向下滚动。"""
        try:
            await self._page.evaluate(f"window.scrollBy(0, {amount})")
            await self._page.wait_for_timeout(500)
            return True, f"scrolled {amount}px"
        except Exception as e:
            return False, f"scroll failed: {e}"

    async def wait(self, ms: int = 2000) -> tuple[bool, str]:
        """等待指定毫秒。"""
        await self._page.wait_for_timeout(ms)
        return True, f"waited {ms}ms"

    async def shoot(self, path: str | None = None) -> str | None:
        """截图。返回路径或 None。"""
        try:
            p = path or ".screenshots/latest.png"
            await self._page.screenshot(path=p, full_page=False)
            return p
        except Exception:
            return None
