#!/usr/bin/env python3
"""
telegram_source.py — agent-eye Telegram 数据源

通过 Bot API 读公开频道消息，零反爬，秒级延迟。
Bot 与用户主号隔离，封了也不影响个人号。

用法:
    python3 telegram_source.py --channel @xiaohongshu_feed
    python3 telegram_source.py --search 数码好物

环境变量:
    TELEGRAM_BOT_TOKEN — Bot Token（从 @BotFather 获取）
    TELEGRAM_PROXY      — 可选，socks5h://127.0.0.1:10808
"""

import json
import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

# ─── Token 管理 ──────────────────────────────

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PROXY = os.environ.get("TELEGRAM_PROXY", "socks5h://127.0.0.1:10808")

if not TOKEN:
    TOKEN_FILE = os.path.expanduser("~/AppData/Local/hermes/config/telegram_bot_token.txt")
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            TOKEN = f.read().strip()


def _call_api(method: str, params: dict | None = None, timeout: int = 15) -> dict:
    """调用 Telegram Bot API。需要代理时自动走 PROXY。"""
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)

    req = Request(url, headers={"User-Agent": "agent-eye/2.0"})

    if PROXY and "socks5h" in PROXY:
        # urllib 不原生支持 socks5h，委托 subprocess curl
        import subprocess
        host = PROXY.replace("socks5h://", "")
        result = subprocess.run(
            ["curl", "-s", "--insecure", "-x", f"socks5h://{host}", url],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"Invalid JSON: {result.stdout[:200]}"}

    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except URLError as e:
        return {"ok": False, "error": str(e)}


# ─── 频道消息拉取 ──────────────────────────────

def get_channel_messages(channel: str, limit: int = 20) -> list[dict]:
    """
    拉取频道最近消息。

    Args:
        channel: 频道用户名（如 @xiaohongshu_feed）
        limit: 最大消息数

    Returns:
        [{"id": 123, "text": "...", "date": ..., "views": ..., "links": [...]}, ...]
    """
    resp = _call_api("getUpdates", {"limit": limit})  # Bot 需要先在频道里
    messages = []

    # getUpdates 只对 Bot 是管理员的频道生效。
    # 对于公开频道，用 forwardMessage 技巧或直接读 channel 的 chat_id。
    # 更可靠的方式：用 getChat + getChatHistory（需要用户帐号 MTProto）
    # Bot API 的轻量替代：读 Bot 所在群的消息。

    if resp.get("ok"):
        for update in resp.get("result", []):
            msg = update.get("message") or update.get("channel_post")
            if not msg:
                continue
            text = msg.get("text") or msg.get("caption") or ""
            chat = msg.get("chat", {})
            if channel and chat.get("username", "") != channel.lstrip("@"):
                continue
            messages.append({
                "id": msg.get("message_id"),
                "chat": chat.get("title") or chat.get("username", ""),
                "text": text,
                "date": msg.get("date", 0),
                "views": msg.get("views", 0),
                "forwards": msg.get("forwards", 0),
                "has_media": "photo" in msg or "video" in msg or "document" in msg,
                "links": _extract_links(text),
            })
    return messages


def _extract_links(text: str) -> list[str]:
    """从文本提取 URL。"""
    import re
    return re.findall(r'https?://[^\s]+', text)


# ─── 频道搜索 ──────────────────────────────

async def search_channels(query: str, max_results: int = 10) -> list[dict]:
    """
    搜索 Telegram 频道。（注：Bot API 不支持原生频道搜索，
    此方法用 getUpdates 中积累的数据做本地匹配。）
    """
    results = []
    resp = _call_api("getUpdates", {"limit": 100})
    seen = set()

    if resp.get("ok"):
        for update in resp.get("result", []):
            msg = update.get("message") or update.get("channel_post")
            if not msg:
                continue
            chat = msg.get("chat", {})
            title = chat.get("title", "")
            username = chat.get("username", "")

            key = username or title
            if key in seen:
                continue
            seen.add(key)

            if query.lower() in title.lower() or query.lower() in username.lower():
                results.append({
                    "title": title,
                    "username": username,
                    "type": chat.get("type", ""),
                    "member_count": _get_chat_member_count(chat.get("id")),
                })
            if len(results) >= max_results:
                break
    return results


def _get_chat_member_count(chat_id: int) -> int:
    """获取群/频道成员数。"""
    resp = _call_api("getChatMemberCount", {"chat_id": chat_id})
    return resp.get("result", 0) if resp.get("ok") else 0


# ─── 适配器：转 TrendItem ──────────────────────

def to_trend_items(messages: list[dict], source_label: str = "telegram") -> list[dict]:
    """
    将 Teleegram 消息转为 trend_scout 标准格式。
    搬运价值 = 浏览量 + 转发数 + 含链接加成。
    """
    items = []
    for msg in messages:
        text = msg.get("text", "")
        title = text.split("\n")[0][:80] if text else "(无文字)"
        views = msg.get("views", 0)
        forwards = msg.get("forwards", 0)

        # 搬运价值评分
        score = min(30, views // 100) + min(30, forwards * 5)
        if msg.get("has_media"):
            score += 10
        if msg.get("links"):
            score += 10
        score = min(95, score)

        # 置信度
        if views > 1000:
            confidence = 0.85
        elif views > 100:
            confidence = 0.70
        else:
            confidence = 0.50

        items.append({
            "title": title,
            "url": f"https://t.me/{msg.get('chat','')}/{msg.get('id','')}",
            "platform": "telegram",
            "source": source_label,
            "views": views,
            "forwards": forwards,
            "text_snippet": text[:200],
            "has_media": msg.get("has_media", False),
            "links": msg.get("links", []),
            "date": msg.get("date", 0),
            "move_score": score,
            "confidence": confidence,
            "reason": _build_reason(views, forwards, msg),
        })
    return items


def _build_reason(views: int, forwards: int, msg: dict) -> str:
    parts = []
    if msg.get("has_media"):
        parts.append("含图/视频")
    if msg.get("links"):
        parts.append(f"含{len(msg['links'])}个链接")
    if views > 500:
        parts.append(f"浏览{views}")
    if forwards > 0:
        parts.append(f"转发{forwards}")
    return " | ".join(parts) if parts else "文本"


# ─── CLI ──────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="agent-eye Telegram 数据源")
    p.add_argument("--channel", help="频道用户名，如 @xiaohongshu_feed")
    p.add_argument("--search", help="搜索频道关键词")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--limit", type=int, default=20, help="最大消息数")
    args = p.parse_args()

    if not TOKEN:
        print("错误: 请设置 TELEGRAM_BOT_TOKEN 环境变量或 ~/AppData/Local/hermes/config/telegram_bot_token.txt")
        sys.exit(1)

    if args.search:
        import asyncio
        channels = asyncio.run(search_channels(args.search, args.limit))
        if args.json:
            print(json.dumps(channels, ensure_ascii=False, indent=2))
        else:
            for c in channels:
                print(f"@{c['username']} — {c['title']} ({c['member_count']} 人)")

    elif args.channel:
        msgs = get_channel_messages(args.channel, args.limit)
        items = to_trend_items(msgs)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            for item in items:
                print(f"[{item['move_score']}分] {item['title']}")
                print(f"  {item['url']}")
                print(f"  {item['reason']}")
    else:
        p.print_help()
