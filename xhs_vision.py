#!/usr/bin/env python3
"""
xhs_vision.py — 小红书图文情报（签名 API + 豆包识图）

小红书笔记 → 封面图 → 豆包识图 → 图文双料情报。
这就是"Tavily 永远拿不到"的独家数据源：文字(标题+数据) + 图(豆包理解)。

用法:
    python3 xhs_vision.py                  # homefeed 图文情报
    python3 xhs_vision.py --num 5          # 拉 5 条
    python3 xhs_vision.py --json           # JSON 输出
    python3 xhs_vision.py --image-only     # 只拉图不看（测试网络）

流程:
    xhs_api 拉笔记 → 提取封面图 URL → 下载图片 → 豆包识图 → 情报条目
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xhs_api import XhsClient, notes_to_trenditems

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".xhs_vision")


def _download_image(url: str, path: str) -> bool:
    """下载小红书图片到本地（带 UA，绕过防盗链）。"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Referer": "https://www.xiaohongshu.com/",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        if len(data) < 500:  # 防 404/防盗链占位图
            return False
        with open(path, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


def _note_image(note: dict) -> str:
    """从笔记里提取封面图 URL（兼容 homefeed/搜索两种结构）。"""
    card = note.get("note_card") or {}
    cover = card.get("cover") or note.get("cover") or {}
    # 小红书封面 URL 在 cover.url.default / urlPre / url 等字段
    for key in ("url", "urlPre", "urlDefault", "default", "pre"):
        v = cover.get(key)
        if v:
            return v if v.startswith("http") else "https://sns-webpic-qc.xhscdn.com" + v
    # 图片列表兜底
    images = card.get("image_list") or []
    if images:
        img = images[0]
        for key in ("url", "urlPre", "urlDefault"):
            v = img.get(key)
            if v:
                return v if v.startswith("http") else "https://sns-webpic-qc.xhscdn.com" + v
    return ""


async def collect_notes(max_results: int = 5) -> list[dict]:
    """拉取小红书笔记（homefeed），返回带封面图 URL 的结构。"""
    async with XhsClient() as client:
        notes = await client.fetch_homefeed(num=max_results)
    out = []
    for n in notes:
        card = n.get("note_card") or {}
        nid = n.get("id") or card.get("note_id") or ""
        title = (n.get("display_title") or card.get("display_title")
                 or card.get("title") or "").strip()
        if not title or not nid:
            continue
        interact = n.get("interact_info") or card.get("interact_info") or {}
        img_url = _note_image(n)
        out.append({
            "note_id": nid,
            "title": title,
            "url": f"https://www.xiaohongshu.com/explore/{nid}",
            "views": str(interact.get("viewed_count", "0")),
            "likes": str(interact.get("liked_count", "0")),
            "image_url": img_url,
        })
    return out


def build_intel(notes: list[dict], use_vision: bool = True) -> list[dict]:
    """下载封面图 + 豆包识图，组装图文情报。"""
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 豆包桥（懒启动：只在需要识图时）
    bridge = None
    if use_vision:
        from doubao_bridge import DoubaoBridge
        bridge = DoubaoBridge()
        ok = bridge.start(timeout=25)
        if not ok:
            print("⚠️ 豆包桥启动失败，降级为纯文字情报")
            bridge = None
        else:
            print("👁 豆包已连接（识图模式）")

    intel = []
    try:
        for i, note in enumerate(notes, 1):
            print(f"\n[{i}/{len(notes)}] {note['title'][:40]}...")
            entry = {
                "note_id": note["note_id"],
                "title": note["title"],
                "url": note["url"],
                "views": note["views"],
                "likes": note["likes"],
                "image_desc": "",
            }

            # 下载封面图
            img_path = ""
            if note["image_url"]:
                img_path = os.path.join(CACHE_DIR, f"{note['note_id']}.jpg")
                if _download_image(note["image_url"], img_path):
                    print(f"   🖼 封面图已下载 ({os.path.getsize(img_path)//1024}KB)")
                else:
                    img_path = ""
                    print("   ⚠️ 封面图下载失败")

            # 豆包识图
            if bridge and img_path:
                q = ("这是小红书笔记封面图。请描述：1)图里有什么 2)视觉风格/调性 "
                     "3)如果用于电商选品，这张图传达了什么卖点。用中文，100字内。")
                ans = bridge.ask(q, images=[img_path], timeout=60)
                entry["image_desc"] = ans
                print(f"   👁 {ans[:80]}")

            intel.append(entry)

            # 人类节奏：每条间隔随机 3-6 秒（模拟人浏览）
            if i < len(notes):
                import time
                time.sleep(3 + i % 3)
    finally:
        if bridge:
            bridge.stop()

    return intel


def print_intel(intel: list[dict]):
    print(f"\n{'='*60}")
    print(f"  📕 小红书图文情报（{len(intel)} 条）")
    print(f"{'='*60}")
    for i, e in enumerate(intel, 1):
        print(f"\n[{i}] {e['title']}")
        print(f"    {e['url']}")
        print(f"    📊 浏览:{e['views']} 赞:{e['likes']}")
        if e.get("image_desc"):
            print(f"    👁 图意: {e['image_desc'][:120]}")


def main():
    p = argparse.ArgumentParser(description="小红书图文情报")
    p.add_argument("--num", type=int, default=5, help="笔记条数")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--image-only", action="store_true", help="只拉图不看（测试网络）")
    p.add_argument("--no-vision", action="store_true", help="跳过豆包识图")
    args = p.parse_args()

    print(f"📕 小红书图文情报: 拉取 {args.num} 条 homefeed...")
    notes = asyncio.run(collect_notes(args.num))
    print(f"   ✅ {len(notes)} 条笔记")

    if not notes:
        print("❌ 无数据（登录态失效？运行 xhs_login.py 重新扫码）")
        sys.exit(1)

    intel = build_intel(notes, use_vision=not args.no_vision)

    if args.json:
        print(json.dumps(intel, ensure_ascii=False, indent=2))
    else:
        print_intel(intel)


if __name__ == "__main__":
    main()
