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

from ethics import throttle, log

# OpenBiliClaw 跨平台源（可选）
try:
    from openbiliclaw_adapter import discover as obc_discover, to_trend_items as obc_to_trend
    HAS_OBC = True
except ImportError:
    HAS_OBC = False

# Telegram 数据源（Bot API，零反爬）
try:
    from telegram_source import get_channel_messages as tg_get_messages, to_trend_items as tg_to_trend
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False

# Sci-Hub 学术源（Node.js worker 渲染绕 Cloudflare）
try:
    from scihub_source import fetch_by_doi as sh_fetch_by_doi, to_trend_items as sh_to_trend
    HAS_SCIHUB = True
except ImportError:
    HAS_SCIHUB = False


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


def source_openbiliclaw(keyword: str = "", max_results: int = 20) -> list[TrendItem]:
    """OpenBiliClaw 跨平台推荐（B站/小红书/抖音/YouTube/知乎/X/Reddit）"""
    if not HAS_OBC:
        return []
    try:
        raw = obc_discover(query=keyword, max_results=max_results)
        return obc_to_trend(raw)
    except Exception:
        return []


def source_telegram(channel: str = "", keyword: str = "", max_results: int = 20) -> list[TrendItem]:
    """Telegram 频道消息——Bot API 直读，零反爬"""
    items = []
    if not HAS_TELEGRAM:
        return items
    from telegram_source import get_channel_messages as tg_msgs, to_trend_items as tg_items
    try:
        if channel:
            msgs = tg_msgs(channel, max_results)
            items = tg_items(msgs, source_label=channel)
        elif keyword:
            msgs = tg_msgs("", max_results * 2)
            filtered = [m for m in msgs if keyword.lower() in m.get("text","").lower()]
            items = tg_items(filtered[:max_results], source_label=f"search:{keyword}")
            log.log("telegram","search",keyword,f"{len(items)} items")
    except Exception as e:
        print(f"  ⚠️ Telegram: {e}")
    return items


def source_scihub(doi: str = "", title: str = "", max_results: int = 5) -> list[TrendItem]:
    """Sci-Hub 学术论文——按 DOI 取 PDF（Node.js worker 绕 Cloudflare）"""
    items = []
    if not HAS_SCIHUB:
        return items
    try:
        if doi:
            paper = sh_fetch_by_doi(doi)
            if paper:
                items = sh_to_trend([paper])
        elif title:
            # 标题 → 先用 Crossref 查 DOI，再取论文
            import urllib.request
            import urllib.parse
            q = urllib.parse.quote(title)
            with urllib.request.urlopen(
                f"https://api.crossref.org/works?query.bibliographic={q}&rows={max_results}&select=DOI,title",
                timeout=15,
            ) as resp:
                data = json.loads(resp.read())
            for item in data.get("message", {}).get("items", []):
                doi_id = item.get("DOI", "")
                if doi_id and doi_id.startswith("10."):
                    paper = sh_fetch_by_doi(doi_id)
                    if paper:
                        items.extend(sh_to_trend([paper]))
                        break  # 命中一篇就够
        log.log("scihub", "fetch", doi or title, f"{len(items)} papers")
    except Exception as e:
        print(f"  ⚠️ Sci-Hub: {e}")

    # dict → TrendItem 转换
    result = []
    for d in items:
        try:
            result.append(TrendItem(
                title=d.get("title", ""),
                url=d.get("url", ""),
                platform="scihub",
                views=d.get("views", 0),
            ))
        except Exception:
            continue
    return result


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
    """YouTube 搜索（需代理），含发布日期"""
    items = []
    if not proxy:
        return items
    try:
        # 第一步：flat 拿到 ID 列表（快）
        cmd = ["yt-dlp", "--proxy", proxy, "--flat-playlist", "--dump-json",
               f"ytsearch{max_results}:{keyword}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        ids = []
        flat_items = []
        for line in result.stdout.strip().split("\n"):
            if not line: continue
            try:
                d = json.loads(line)
                vid = d.get("id", "") or d.get("display_id", "")
                if vid:
                    ids.append(vid)
                    flat_items.append(d)
            except json.JSONDecodeError:
                continue

        # 第二步：用 ID 列表批量拿详情（含 upload_date）
        for i, fid in enumerate(flat_items):
            vid = fid.get("id", "") or fid.get("display_id", "")
            if not vid: continue

            # 🔒 频率节流——模拟人类浏览节奏
            throttle.wait()

            url = f"https://www.youtube.com/watch?v={vid}"
            try:
                cmd2 = ["yt-dlp", "--proxy", proxy, "-j", "--skip-download", url]
                r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=20)
                d = json.loads(r2.stdout)
            except Exception:
                d = fid  # 拿不到详情就用 flat 数据

            items.append(TrendItem(
                title=d.get("title", fid.get("title", "")),
                url=d.get("webpage_url", f"https://www.youtube.com/watch?v={vid}"),
                platform="youtube",
                views=d.get("view_count") or 0,
                likes=d.get("like_count") or 0,
                published_days=_days_ago(d.get("upload_date", "")),
                raw=d,
            ))
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

    # 新鲜度 (0-20) — 超过12个月直接0分
    if item.published_days <= 1:
        fresh = 20
    elif item.published_days <= 7:
        fresh = 15
    elif item.published_days <= 30:
        fresh = 10
    elif item.published_days <= 90:
        fresh = 5
    else:
        fresh = 0

    # 国内唯一性 (0-30)
    domestic_titles = {d.title.lower() for d in domestic_items}
    is_unique = item.title.lower() not in domestic_titles
    uniqueness = 30 if is_unique else 0

    total = heat + fresh + uniqueness

    # 置信度
    if item.views == 0:
        confidence = 0.3
    elif is_unique and item.views > 100_000:
        confidence = 0.85
    elif is_unique:
        confidence = 0.7
    else:
        confidence = 0.4

    # 超过 12 个月的旧视频 — 降分 + 降置信度（放在置信度计算之后）
    if item.published_days > 365:
        total = max(0, total - 30)
        if item.published_days != 999:
            confidence = 0.2

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

    # 1b. OpenBiliClaw 跨平台推荐（B站/小红书/抖音等）
    cross_platform = source_openbiliclaw(keyword, max_results=max_results)

    # 2. 拉取国内热榜（用于去重判断）
    domestic = source_bilibili_search(keyword, max_results=max_results)

    # 3. 评分（YouTube + OpenBiliClaw 结果）
    results = [score_item(item, keyword, domestic) for item in overseas + cross_platform]

    # 4. 按搬运价值排序
    results.sort(key=lambda r: r.move_score, reverse=True)

    # 📝 研究日志
    log.log("trend_scout", "scout", keyword,
            f"{len(results)} results from youtube",
            "个人学术研究——分析海外内容生态与趋势")

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


# ─── 下载 ──────────────────────────────────────────


def download_items(results: list[ScoutResult], min_score: int = 60,
                   output_dir: str = "downloads", proxy: str | None = None,
                   max_items: int = 5) -> list[str]:
    """
    下载选中的视频。
    返回下载成功的文件路径列表。
    """
    import os
    os.makedirs(output_dir, exist_ok=True)

    # 筛选：分值达标且是 YouTube 源
    to_download = [
        r for r in results
        if r.move_score >= min_score and r.item.platform == "youtube" and r.item.url
    ][:max_items]

    if not to_download:
        print("  ⚠️ 没有符合下载条件的视频")
        return []

    downloaded = []
    for i, r in enumerate(to_download):
        print(f"\n  📥 [{i+1}/{len(to_download)}] {r.item.title[:60]}...")
        cmd = [
            "yt-dlp",
            "-o", f"{output_dir}/%(title).50s.%(ext)s",
            "--no-playlist",
            r.item.url,
        ]
        if proxy:
            cmd.insert(1, "--proxy")
            cmd.insert(2, proxy)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                # 找到刚下载的文件
                for line in result.stdout.split("\n") + result.stderr.split("\n"):
                    if "[download] Destination:" in line:
                        path = line.split("Destination:", 1)[1].strip()
                        downloaded.append(path)
                        print(f"  ✅ {path}")
                        break
                else:
                    downloaded.append(r.item.title)
                    print(f"  ✅ 下载完成")
            else:
                print(f"  ❌ 下载失败: {result.stderr[-200:]}")
        except subprocess.TimeoutExpired:
            print(f"  ⏰ 超时")

        # 📝 研究日志
        log.log("trend_scout", "download_item", r.item.url[:80],
                f"{'ok' if downloaded else 'fail'}",
                "个人学习——下载海外公开内容用于研究分析")

    return downloaded


# ─── CLI ────────────────────────────────────────────


if __name__ == "__main__":
    # 分离关键词和参数
    raw_args = sys.argv[1:]
    skip_next = False
    args = []
    for i, a in enumerate(raw_args):
        if skip_next:
            skip_next = False
            continue
        if a == "--min-score":
            skip_next = True  # 下一个是值，跳过
            continue
        if not a.startswith("--"):
            args.append(a)

    keyword = " ".join(args) if args else "AI"

    flags = [a for a in raw_args if a.startswith("--")]

    from ethics import ProxyConfig
    proxy = ProxyConfig.detect()

    if proxy:
        print(f"🌐 代理已检测到: {proxy}")
    else:
        print(f"⚠️ 代理未检测到 (127.0.0.1:10808)，海外源将跳过")

    results = scout(keyword, proxy=proxy)
    print_results(results, keyword)

    # --json 输出
    if "--json" in flags:
        print(to_json(results, keyword))

    # --download 下载选中的视频
    if "--download" in flags:
        min_score = 60
        # 从原始 sys.argv 中取 --min-score 的值
        for i, a in enumerate(sys.argv):
            if a == "--min-score" and i + 1 < len(sys.argv):
                min_score = int(sys.argv[i + 1])
        print(f"\n{'='*70}")
        print(f"  📥 开始下载（搬运价值 ≥ {min_score}）")
        print(f"{'='*70}")
        download_items(results, min_score=min_score, proxy=proxy)
