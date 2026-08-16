#!/usr/bin/env python3
"""
login_site.py — agent-eye 登录辅助脚本

对有登录墙的网站（闲鱼/知乎/小红书...）做一次性手动登录。
登录态自动保存在 ~/ego_profile，之后 agent-eye 无头模式复用。

用法:
    python3 login_site.py https://www.goofish.com/
    python3 login_site.py https://www.zhihu.com/

流程:
    1. 弹出有头浏览器，打开目标网站
    2. 你手动登录（扫码/账号）
    3. 登录成功后回到终端按 Enter
    4. 浏览器关闭，cookie 已保存
"""

import json
import subprocess
import sys
import os

WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "browser_worker.js")


def login(url: str):
    print(f"🔓 打开有头浏览器: {url}")
    print("   ⏳ 请在弹出的浏览器窗口完成登录（扫码/账号均可）")
    print("   ✅ 登录成功后，回到这里按 Enter 保存登录态...\n")

    worker = subprocess.Popen(
        ["node", WORKER_SCRIPT, "--headed"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
        env={**os.environ.copy(), "HEADED": "1"},
    )
    try:
        ready = worker.stdout.readline()
        if "ready" not in ready:
            print(f"❌ Worker 启动失败: {ready}")
            return False

        # 导航到目标站
        worker.stdin.write(json.dumps({"action": "navigate", "url": url, "timeout": 30000}) + "\n")
        worker.stdin.flush()
        nav = worker.stdout.readline()
        print(f"📡 已打开: {nav[:80]}")

        # 等用户登录完成
        input("   按 Enter 保存登录态并退出... ")

        # 关闭（persistent context 自动保存 cookie 到 ~/ego_profile）
        worker.stdin.write(json.dumps({"action": "exit"}) + "\n")
        worker.stdin.flush()
        worker.wait(timeout=10)
        print("\n✅ 登录态已保存到 ~/ego_profile")
        print("   之后 agent-eye 无头模式自动复用，无需再登录")
        return True
    except Exception as e:
        print(f"❌ 错误: {e}")
        worker.kill()
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 login_site.py <URL>")
        print("示例: python3 login_site.py https://www.goofish.com/")
        sys.exit(1)
    login(sys.argv[1])
