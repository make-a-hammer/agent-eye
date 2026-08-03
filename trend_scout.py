#!/usr/bin/env python3
"""
trend_scout.py — agent-eye 选品探测器 v1

从多个平台拉取热榜内容，过滤去重，按搬运价值排序，输出置信度。
数据源做成插件式，海外源需要代理时自动切换。

用法:
    python3 trend_scout.py "AI工具"          # 搜一个品类
    python3 trend_scout.py --category AI     # 搜预设品类
    python3 trend_scout.py --source bilibili # 指定数据源
"""

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable


# ─── 数据模型 ────────────────────────────────────


@dataclass
class TrendItem:
    title: str
    url: str
    platform: str          # youtube / bilibili / tiktok / douyin
    views: int = 0
    likes: int = 0
    published_days: int = 999
    raw: dict = field(default_factory=dict)

    @property
    def engagement_score(self) -> float:
        """热度分 = 播放量 + 点赞加权（防僵尸号刷量）"""
        return self.views * 1.0 + self.likes * 3.0

    @property
    def freshness_score(self) -> float:
        """新鲜度：越新越好"""
        if self.published_days <= 1:
            return 1.0
        elif self.published_days <= 3:
            return 0.7
        elif self.published_days <= 7:
            return 0.4
        return 0.1


@dataclass
class ScoutResult:
    item: TrendItem
    move_score: float          # 搬运价值分（0-100）
    confidence: float          # 置信度（0-1）
    reason: str                # 为什么推荐


# ─── 数据源插件 ──────────────────────────────────


def source_bilibili_hot(keyword: str = "") -> list[TrendItem]:
    """B站热门（模拟浏览器绕过风控）"""
    items = []
    try:
        cmd = [
            "yt-dlp", "--flat-playlist", "--dump-json",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "https://www.bilibili.com/v/popular/rank/all",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                d = json.loads(line)
                items.append(TrendItem(
                    title=d.get("title", ""),
                    url=d.get("webpage_url", d.get("original_url", "")),
                    platform="bilibili",
                    views=d.get("view_count") or 0,
                    likes=0,
                    raw=d,
                ))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return items


def source_bilibili_search(keyword: str, max_results: int = 20) -> list[TrendItem]:
    """B站搜索（按播放量排序）"""
    items = []
    try:
        cmd = [
            "yt-dlp", "--flat-playlist", "--dump-json",
            f"bilisearch{max_results}:{keyword}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                d = json.loads(line)
                items.append(TrendItem(
                    title=d.get("title", ""),
                    url=d.get("webpage_url", d.get("original_url", "")),
                    platform="bilibili",
                    views=d.get("view_count") or 0,
                    likes=0,
                    raw=d,
                ))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return items


def source_youtube(keyword: str, max_results: int = 10, proxy: str | None = None) -> list[TrendItem]:
    """YouTube 搜索（需代理）"""
    items = []
    if not proxy:
        return items  # 没代理，跳过
    try:
        cmd = ["yt-dlp", "--proxy", proxy, "--flat-playlist", "--dump-json",
               f"ytsearch{max_results}:{keyword}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                d = json.loads(line)
                items.append(TrendItem(
                    title=d.get("title", ""),
                    url=d.get("webpage_url", d.get("original_url", "")),
                    platform="youtube",
                    views=d.get("view_count") or 0,
                    likes=d.get("like_count") or 0,
                    published_days=_days_ago(d.get("upload_date", "")),
                    raw=d,
                ))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return items


# ─── 搬运价值评分 ────────────────────────────────


def score_item(item: TrendItem, keyword: str, domestic_items: list[TrendItem]) -> ScoutResult:
    """
    给一个内容打分：
    - 热度越高越好
    - 越新越好
    - 国内还没人搬 = 加分
    - 标题和关键词匹配度高 = 加分
    """
    # 热度分 (0-50)
    if item.views > 1_000_000:
        heat = 50
    elif item.views > 100_000:
        heat = 30
    elif item.views > 10_000:
        heat = 15
    else:
        heat = 5

    # 新鲜度 (0-20)
    fresh = int(item.freshness_score * 20)

    # 国内唯一性 (0-30)
    domestic_titles = {d.title.lower() for d in domestic_items}
    is_unique = item.title.lower() not in domestic_titles
    uniqueness = 30 if is_unique else 0

    total = heat + fresh + uniqueness

    # 置信度
    if item.views == 0:
        confidence = 0.3  # 数据不全
    elif is_unique and item.views > 100_000:
        confidence = 0.85
    elif is_unique:
        confidence = 0.7
    else:
        confidence = 0.4

    # 推荐理由
    reasons = []
    if item.views > 100_000:
        reasons.append(f"高播放({item.views:,})")
    if item.published_days <= 3:
        reasons.append(f"新发布({item.published_days}天前)")
    if is_unique:
        reasons.append("国内未搬运")
    else:
        reasons.append("国内已有")

    return ScoutResult(
        item=item,
        move_score=total,
        confidence=confidence,
        reason=" | ".join(reasons),
    )


# ─── 主流程 ──────────────────────────────────────


def scout(keyword: str, proxy: str | None = None, max_results: int = 20) -> list[ScoutResult]:
    """主入口：拉取各平台热榜 → 评分 → 排序输出"""

    # 1. 拉取国外热榜（需要代理）
    overseas = source_youtube(keyword, max_results=max_results, proxy=proxy)

    # 2. 拉取国内热榜（用于去重判断）
    domestic = source_bilibili_search(keyword, max_results=max_results)

    # 3. 评分
    results = [score_item(item, keyword, domestic) for item in overseas]

    # 4. 按搬运价值排序
    results.sort(key=lambda r: r.move_score, reverse=True)

    return results


def _days_ago(date_str: str) -> int:
    """估算发布日期距今几天（简化版）"""
    if not date_str or len(date_str) < 8:
        return 999
    try:
        from datetime import datetime
        d = datetime.strptime(date_str[:8], "%Y%m%d")
        return (datetime.now() - d).days
    except Exception:
        return 999


# ─── 输出格式 ────────────────────────────────────


def print_results(results: list[ScoutResult], keyword: str):
    """终端友好输出"""
    print(f"\n{'='*70}")
    print(f"  🎯 选品探测器 — \"{keyword}\"")
    print(f"{'='*70}")
    print(f"  {'搬运价值':>6}  {'置信度':>6}  {'播放量':>10}  标题")
    print(f"  {'-'*60}")

    if not results:
        print("  ⚠️ 没有找到海外热榜数据（需要开代理 v2rayN）")
        print("  提示：打开 v2rayN 全局模式后重试")
        return

    for r in results[:15]:
        bar = "█" * int(r.move_score / 5)
        print(f"  {r.move_score:>3}/100 {r.confidence:.0%}  {r.item.views:>10,}  {r.item.title[:60]}")
        print(f"         {bar}  {r.reason}")

    print(f"\n  💡 搬运价值 > 60 的建议优先考虑")


def to_json(results: list[ScoutResult], keyword: str) -> str:
    """JSON 输出（给 agent-eye 消费）"""
    return json.dumps([{
        "title": r.item.title,
        "url": r.item.url,
        "platform": r.item.platform,
        "views": r.item.views,
        "move_score": r.move_score,
        "confidence": r.confidence,
        "reason": r.reason,
    } for r in results], ensure_ascii=False, indent=2)


# ─── CLI ────────────────────────────────────────────


if __name__ == "__main__":
    keyword = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "AI"

    proxy = None
    # 检测代理是否可用
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    if s.connect_ex(("127.0.0.1", 10808)) == 0:
        proxy = "socks5://127.0.0.1:10808"
    s.close()

    if proxy:
        print(f"🌐 代理已检测到: {proxy}")
    else:
        print(f"⚠️ 代理未检测到 (127.0.0.1:10808)，海外源将跳过")

    results = scout(keyword, proxy=proxy)
    print_results(results, keyword)

    # 同时输出 JSON（可管道给 agent-eye）
    if "--json" in sys.argv:
        print(to_json(results, keyword))
