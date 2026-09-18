#!/usr/bin/env python3
"""
camofox_client.py — camofox-browser 的 Python 适配器（agent-eye 侧）

设计原则：**适配器模式，不合并代码**。camofox-browser 是独立进程（Node，REST API，
默认 http://127.0.0.1:9377），本模块只通过 HTTP 调它。

为什么给 agent-eye 加它（借鉴 camofox 的「接口层」，不是它的反检测引擎）：
  - **元素引用 e1/e2/e3**：比 CSS selector 稳、比像素坐标稳，页面改版不易碎
  - **可访问性快照（aria YAML）**：比 raw HTML 省约 90% token —— 与 agent-eye
    「首要受众是 AI agent，token 效率优先」的原则一致
  - 结构化抽取 / 稳定 ref / 分页快照：把散在 browser_worker.js 里的能力统一成契约

合规边界：本适配器**不启用** camofox 的指纹伪装与住宅代理，只用它作为
「另一个浏览器后端」。是否启用代理由服务器环境变量决定，与 agent-eye 的
ethics.py 红线（不搞隐秘性增强）保持一致。

用法:
    from camofox_client import Camofox
    c = Camofox()
    if not c.alive(): raise SystemExit("camofox-browser 没在跑")
    tab = c.create_tab("https://example.com")
    snap = c.snapshot(tab)
    print(snap["snapshot"][:500])       # aria YAML，含 [e1] [e2] 引用
    c.click(tab, ref="e1")
    c.close_tab(tab)

自测: python camofox_client.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:9377"
DEFAULT_USER = "agent-eye"
DEFAULT_SESSION = "default"


class CamofoxError(RuntimeError):
    """camofox-browser 返回的非 2xx 或传输层错误。"""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Camofox:
    """camofox-browser REST API 的薄封装（stdlib only，无第三方依赖）。"""

    def __init__(
        self,
        base: str = DEFAULT_BASE,
        user_id: str = DEFAULT_USER,
        session_key: str = DEFAULT_SESSION,
        timeout: float = 60.0,
        access_key: str | None = None,
    ):
        self.base = base.rstrip("/")
        self.user_id = user_id
        self.session_key = session_key
        self.timeout = timeout
        self.access_key = access_key

    # ── 传输层 ────────────────────────────────────────────────
    def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.access_key:
            req.add_header("Authorization", f"Bearer {self.access_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace") if e.fp else ""
            raise CamofoxError(f"{method} {path} → HTTP {e.code}", e.code, detail) from None
        except urllib.error.URLError as e:
            raise CamofoxError(f"{method} {path} → 连不上 {self.base}（camofox-browser 起了吗？）: {e.reason}") from None
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}

    # ── 系统 ─────────────────────────────────────────────────
    def alive(self) -> bool:
        """camofox-browser 是否在跑。"""
        try:
            self._req("GET", "/health")
            return True
        except CamofoxError:
            return False

    def health(self) -> dict:
        return self._req("GET", "/health")

    # ── Tab 生命周期 ──────────────────────────────────────────
    def create_tab(self, url: str | None = None, trace: bool = False) -> str:
        """新建 tab，返回 tabId。url 可选（给了就直接导航）。"""
        body: dict[str, Any] = {
            "userId": self.user_id,
            "sessionKey": self.session_key,
        }
        if url:
            body["url"] = url
        if trace:
            body["trace"] = True
        r = self._req("POST", "/tabs", body)
        tab_id = r.get("tabId") or r.get("id") or r.get("tab", {}).get("tabId")
        if not tab_id:
            raise CamofoxError(f"create_tab 没拿到 tabId: {r}")
        return tab_id

    def list_tabs(self) -> dict:
        return self._req("GET", f"/tabs?userId={self.user_id}")

    def close_tab(self, tab_id: str) -> dict:
        return self._req("DELETE", f"/tabs/{tab_id}?userId={self.user_id}")

    # ── 观察（核心：可访问性快照 + 元素引用）──────────────────
    def snapshot(
        self,
        tab_id: str,
        offset: int = 0,
        include_screenshot: bool = False,
    ) -> dict:
        """
        取页面快照。返回:
            {url, snapshot(str, aria YAML, 含 [e1][e2] 引用), structure,
             refsCount, truncated, totalChars, hasMore, nextOffset}
        大页面用 nextOffset 翻页（offset>0 走缓存，很快）。
        """
        q = f"?userId={self.user_id}&format=text&offset={offset}"
        if include_screenshot:
            q += "&includeScreenshot=true"
        return self._req("GET", f"/tabs/{tab_id}/snapshot{q}")

    def snapshot_full(self, tab_id: str, max_pages: int = 5) -> str:
        """把分页快照拼成完整文本。"""
        parts: list[str] = []
        offset = 0
        for _ in range(max_pages):
            s = self.snapshot(tab_id, offset=offset)
            parts.append(s.get("snapshot", ""))
            if not s.get("hasMore"):
                break
            offset = s.get("nextOffset") or 0
            if offset <= 0:
                break
        return "\n".join(parts)

    def screenshot(self, tab_id: str, path: str | None = None) -> bytes | str:
        """截图。给了 path 就写盘并返回路径，否则返回 PNG bytes。"""
        url = f"{self.base}/tabs/{tab_id}/screenshot?userId={self.user_id}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            data = resp.read()
        if path:
            with open(path, "wb") as fh:
                fh.write(data)
            return path
        return data

    def links(self, tab_id: str) -> dict:
        return self._req("GET", f"/tabs/{tab_id}/links?userId={self.user_id}")

    # ── 动作 ─────────────────────────────────────────────────
    def navigate(self, tab_id: str, url: str) -> dict:
        return self._req("POST", f"/tabs/{tab_id}/navigate",
                         {"userId": self.user_id, "url": url})

    def search(self, tab_id: str, macro: str, query: str) -> dict:
        """
        搜索宏（camofox 内置，如 @google_search / @youtube_search / @reddit_subreddit）。
        等价于 navigate 带 macro 参数。
        """
        return self._req("POST", f"/tabs/{tab_id}/navigate",
                         {"userId": self.user_id, "macro": macro, "query": query})

    def click(self, tab_id: str, ref: str | None = None, selector: str | None = None) -> dict:
        """按元素引用点击（ref='e3'）或 CSS selector。优先用 ref。"""
        body: dict[str, Any] = {"userId": self.user_id}
        if ref:
            body["ref"] = ref
        elif selector:
            body["selector"] = selector
        else:
            raise ValueError("click 需要 ref 或 selector")
        return self._req("POST", f"/tabs/{tab_id}/click", body)

    def type_text(
        self,
        tab_id: str,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        mode: str = "fill",
        submit: bool = False,
    ) -> dict:
        """
        输入文本。
          mode='fill'     → 填入指定元素（需 ref/selector）
          mode='keyboard' → 往当前焦点打字（可不带 ref）
        submit=True 输入后按回车。
        """
        body: dict[str, Any] = {"userId": self.user_id, "text": text, "mode": mode}
        if ref:
            body["ref"] = ref
        elif selector:
            body["selector"] = selector
        if submit:
            body["submit"] = True
        return self._req("POST", f"/tabs/{tab_id}/type", body)

    def press(self, tab_id: str, key: str) -> dict:
        """按键，如 'Enter' / 'Escape' / 'PageDown'。"""
        return self._req("POST", f"/tabs/{tab_id}/press",
                         {"userId": self.user_id, "key": key})

    def scroll(self, tab_id: str, direction: str = "down", amount: int = 3,
               ref: str | None = None) -> dict:
        body: dict[str, Any] = {"userId": self.user_id,
                                "direction": direction, "amount": amount}
        if ref:
            body["ref"] = ref
        return self._req("POST", f"/tabs/{tab_id}/scroll", body)

    def wait(self, tab_id: str, ms: int = 1000) -> dict:
        return self._req("POST", f"/tabs/{tab_id}/wait",
                         {"userId": self.user_id, "ms": ms})

    def extract(self, tab_id: str, schema: dict) -> dict:
        """
        结构化抽取：schema 里用 x-ref 把属性映射到快照 ref。
        例: {"title": {"x-ref": "e3", "type": "text"}}
        """
        return self._req("POST", f"/tabs/{tab_id}/extract",
                         {"userId": self.user_id, "schema": schema})

    def evaluate(self, tab_id: str, expression: str) -> Any:
        """
        在页面上下文执行 JS，返回 result。
        注意：aria 快照不含 <title>，所以拿 doctitle 只能靠这个。
        """
        r = self._req("POST", f"/tabs/{tab_id}/evaluate",
                      {"userId": self.user_id, "expression": expression})
        return r.get("result") if isinstance(r, dict) else r


# ── 自测 ─────────────────────────────────────────────────────
def _selftest(base: str) -> int:
    c = Camofox(base=base)
    print(f"→ 探活 {base} ...")
    if not c.alive():
        print("✗ camofox-browser 没在跑。先启动: node camofox/node_modules/@askjo/camofox-browser/server.js")
        return 1
    print("✓ /health OK")

    tab = c.create_tab()
    print(f"✓ 建 tab: {tab}")
    try:
        c.navigate(tab, "https://example.com")
        snap = c.snapshot(tab)
        print(f"✓ 快照  url={snap.get('url')}")
        print(f"  refsCount={snap.get('refsCount')} totalChars={snap.get('totalChars')} "
              f"truncated={snap.get('truncated')}")
        text = (snap.get("snapshot") or "")[:400]
        print("  ── 快照前 400 字 ──")
        for line in text.splitlines():
            print("   ", line)
    finally:
        c.close_tab(tab)
        print("✓ 关 tab")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="camofox-browser Python 适配器")
    ap.add_argument("--selftest", action="store_true", help="跑自测（探活+建tab+快照）")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"服务地址（默认 {DEFAULT_BASE}）")
    args = ap.parse_args()
    sys.exit(_selftest(args.base) if args.selftest else (ap.print_help() or 0))
