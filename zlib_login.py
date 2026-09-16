#!/usr/bin/env python3
"""
zlib_login.py — Z-Library 登录态保存工具

流程（密码不经过脚本，安全）：
    ① 脚本打开浏览器窗口，导航到 Z-Library 登录页
    ② 你手动输入账号密码并登录
    ③ 脚本检测到登录成功后，保存登录态到本地
    ④ 之后 ebook_source.py 自动复用这个登录态（提高下载额度）

用法:
    python zlib_login.py              # 打开浏览器，登录后自动保存
    python zlib_login.py --check      # 检查现有登录态是否有效

登录态保存位置: ~/mc_audit/zlib_session.json（含 cookie，勿外传）
"""

import argparse
import asyncio
import json
import os
import sys
import time

from playwright.async_api import async_playwright

BASE = "https://zh.z-lib.by"
LOGIN_URL = f"{BASE}/login"
SESSION_FILE = os.path.expanduser("~/mc_audit/zlib_session.json")
PROFILE_DIR = os.path.expanduser("~/ego_profile") if os.name != "nt" else \
    os.path.join(os.environ.get("USERPROFILE", ""), "ego_profile")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def _is_logged_in(page) -> bool:
    """检测是否已登录：出现个人书库/退出登录等迹象。"""
    try:
        return await page.evaluate("""() => {
            const t = document.body.innerText || '';
            const links = Array.from(document.querySelectorAll('a')).map(a => (a.textContent||'') + ' ' + (a.getAttribute('href')||''));
            const hasLib = links.some(s => /my library|我的书库|我的书架|\\/profile/i.test(s));
            const hasLogout = links.some(s => /logout|sign out|退出|注销/i.test(s));
            const notLoginBtn = !links.some(s => /\\/login/i.test(s));
            return (hasLib || hasLogout) && notLoginBtn;
        }""")
    except Exception:
        return False


async def login_flow(headless: bool = False, timeout_s: int = 300):
    """打开浏览器 → 等用户登录 → 保存登录态。"""
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)

    async with async_playwright() as pw:
        # 用持久化 profile（也顺带保存到 ego_profile，供其他脚本复用）
        browser = await pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        print(f"🌐 打开: {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(4000)

        print("""
┌─────────────────────────────────────────────────────────┐
│  👉 请在弹出的浏览器窗口中手动登录 Z-Library              │
│     （邮箱 + 密码，密码不会经过本脚本）                   │
│                                                          │
│  登录成功后本脚本会自动检测并保存登录态。                 │
│  最多等待 5 分钟，中途可 Ctrl+C 取消。                    │
└─────────────────────────────────────────────────────────┘
""")

        deadline = time.time() + timeout_s
        ok = False
        while time.time() < deadline:
            if await _is_logged_in(page):
                ok = True
                break
            await asyncio.sleep(3)

        if not ok:
            print("❌ 未检测到登录成功（超时或未完成登录）")
            await browser.close()
            return False

        print("✅ 检测到登录成功！正在保存登录态...")
        # 保存 storage_state（cookie + localStorage）
        state = await browser.storage_state()
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

        n_cookies = len(state.get("cookies", []))
        print(f"✅ 已保存: {SESSION_FILE}")
        print(f"   cookie 数: {n_cookies}")
        print(f"   profile: {PROFILE_DIR}")
        await browser.close()
        return True


async def check_session():
    """检查已保存的登录态是否有效。"""
    if not os.path.exists(SESSION_FILE):
        print(f"❌ 未找到登录态文件: {SESSION_FILE}")
        print("   先运行: python zlib_login.py")
        return False

    with open(SESSION_FILE, encoding="utf-8") as f:
        state = json.load(f)
    print(f"📂 登录态文件存在（cookie 数: {len(state.get('cookies', []))}）")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="zh-CN",
                                        storage_state=SESSION_FILE)
        page = await ctx.new_page()
        try:
            await page.goto(BASE, wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(6000)
            ok = await _is_logged_in(page)
            print("✅ 登录态有效" if ok else "⚠️ 登录态可能已失效（重新运行 zlib_login.py）")
        except Exception as e:
            print(f"⚠️ 检查失败: {str(e)[:100]}")
            ok = False
        await browser.close()
        return ok


def main():
    p = argparse.ArgumentParser(description="Z-Library 登录态保存工具")
    p.add_argument("--check", action="store_true", help="检查现有登录态")
    p.add_argument("--headless", action="store_true",
                   help="无头模式（不推荐——Cloudflare 会拦）")
    p.add_argument("--timeout", type=int, default=300, help="等待登录秒数")
    args = p.parse_args()

    if args.check:
        sys.exit(0 if asyncio.run(check_session()) else 1)

    ok = asyncio.run(login_flow(headless=args.headless, timeout_s=args.timeout))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
