#!/usr/bin/env python3
"""
openbiliclaw_adapter.py — agent-eye ↔ OpenBiliClaw 数据源适配器

从 OpenBiliClaw 的推荐/发现引擎拉取跨平台内容，
转换为 agent-eye 的 TrendItem 格式供选品使用。

平台：B站 / 小红书 / 抖音 / YouTube / 知乎 / X / Reddit
"""

import json
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# OpenBiliClaw 源码路径
OBC_PATH = Path("C:/Users/小白本/OpenBiliClaw/src")
if str(OBC_PATH) not in sys.path:
    sys.path.insert(0, str(OBC_PATH))


@dataclass
class OBCItem:
    """OpenBiliClaw 推荐条目的标准化格式"""
    title: str
    url: str
    platform: str  # bilibili / xiaohongshu / douyin / youtube / zhihu / twitter / reddit
    description: str = ""
    views: int = 0
    likes: int = 0
    published_days: int = 999
    author: str = ""
    tags: list = None
    cover_url: str = ""


def _parse_platform(raw_platform: str) -> str:
    """统一平台名称"""
    mapping = {
        "bilibili": "bilibili", "bili": "bilibili",
        "xiaohongshu": "xiaohongshu", "xhs": "xiaohongshu",
        "douyin": "douyin", "dy": "douyin",
        "youtube": "youtube", "yt": "youtube",
        "zhihu": "zhihu",
        "twitter": "twitter", "x": "twitter",
        "reddit": "reddit",
    }
    return mapping.get(raw_platform.lower(), raw_platform.lower())


def discover(query: str = "", max_results: int = 20,
             platforms: Optional[list] = None) -> list[OBCItem]:
    """
    从 OpenBiliClaw 发现内容。
    
    Args:
        query: 搜索关键词（空字符串 = 获取推荐流）
        max_results: 最大返回数
        platforms: 限定平台列表，如 ["bilibili", "xiaohongshu"]
    
    Returns:
        OBCItem 列表
    """
    items = []
    
    try:
        from openbiliclaw.sources.registry import get_source_registry
        registry = get_source_registry()
        
        target_platforms = platforms or ["bilibili", "xiaohongshu", "douyin", "youtube", "zhihu"]
        
        for p in target_platforms:
            p_normalized = _parse_platform(p)
            if p_normalized not in registry:
                continue
            
            # 尝试获取该平台的推荐/热门内容
            try:
                source = registry[p_normalized]
                # 优先用 trending strategy
                if hasattr(source, 'trending'):
                    raw_items = source.trending(limit=max_results // len(target_platforms))
                elif hasattr(source, 'discover'):
                    raw_items = source.discover(query=query, limit=max_results // len(target_platforms))
                else:
                    continue
                
                for raw in (raw_items or []):
                    items.append(OBCItem(
                        title=getattr(raw, 'title', '') or raw.get('title', ''),
                        url=getattr(raw, 'url', '') or raw.get('url', ''),
                        platform=p_normalized,
                        description=getattr(raw, 'description', '') or raw.get('desc', ''),
                        views=getattr(raw, 'view_count', 0) or raw.get('views', 0) or raw.get('play_count', 0),
                        likes=getattr(raw, 'like_count', 0) or raw.get('likes', 0),
                        author=getattr(raw, 'author', '') or raw.get('owner', {}).get('name', ''),
                        tags=getattr(raw, 'tags', []) or raw.get('tags', []),
                        cover_url=getattr(raw, 'cover_url', '') or raw.get('cover', ''),
                    ))
            except Exception:
                continue
                
    except ImportError:
        pass  # OpenBiliClaw 未安装时静默降级
    
    return items[:max_results]


def fetch_trending(platform: str = "bilibili", limit: int = 10) -> list[OBCItem]:
    """获取指定平台的热门内容"""
    return discover(platforms=[platform], max_results=limit)


def search_cross_platform(query: str, max_results: int = 30) -> list[OBCItem]:
    """跨平台搜索"""
    return discover(query=query, max_results=max_results)


# ─── 转换为 agent-eye 格式 ──────────────────


def to_trend_items(obc_items: list[OBCItem]) -> list:
    """转换为 agent-eye 的 TrendItem 格式"""
    from trend_scout import TrendItem
    
    items = []
    for obc in obc_items:
        items.append(TrendItem(
            title=obc.title,
            url=obc.url,
            platform=obc.platform,
            views=obc.views,
            likes=obc.likes,
            published_days=obc.published_days,
            raw={
                "description": obc.description,
                "author": obc.author,
                "tags": obc.tags,
                "cover_url": obc.cover_url,
                "source": "openbiliclaw",
            },
        ))
    return items


# ─── 自检 ────────────────────────────────────


def status() -> dict:
    """检查 OpenBiliClaw 集成状态"""
    result = {
        "installed": False,
        "path": str(OBC_PATH),
        "path_exists": OBC_PATH.exists(),
        "platforms_available": [],
    }
    
    try:
        from openbiliclaw.sources.registry import get_source_registry
        registry = get_source_registry()
        result["installed"] = True
        result["platforms_available"] = list(registry.keys())
    except ImportError:
        result["error"] = "OpenBiliClaw 未安装或路径不正确"
    except Exception as e:
        result["error"] = str(e)
    
    return result


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=2))
