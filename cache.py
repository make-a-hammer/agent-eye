#!/usr/bin/env python3
"""
cache.py — agent-eye 缓存层（内容寻址 + TTL + ETag）

设计目标（源自架构报告 §5，个人项目版）：
    - 降低站点负担：同一 URL 不重复抓
    - 控制成本：缓存命中的 LLM 分析不重跑
    - 保证复现：证据快照留存（回答基于哪个版本）
    - 记录版本：内容 hash 变化即知页面更新

存储布局（文件系统 + SQLite，不用 Redis/PostgreSQL）：
    .cache/
      index.db              ← SQLite 索引（url → hash, etag, fetched_at, ttl）
      content/ab/abcdef...  ← 内容寻址存储（sha256 前2位分桶）

用法:
    from cache import FetchCache
    c = FetchCache()
    hit = c.get("https://api.example.com/x", ttl_hours=24)
    if hit is None:
        content = fetch(...)          # 真实抓取
        c.put("https://api.example.com/x", content, ttl_hours=24)
    else:
        content = hit["content"]
    print(c.stats())
"""

import hashlib
import json
import os
import sqlite3
import time
from typing import Any

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CONTENT_DIR = os.path.join(CACHE_DIR, "content")
DB_PATH = os.path.join(CACHE_DIR, "index.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    url         TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    etag        TEXT,
    last_modified TEXT,
    fetched_at  REAL NOT NULL,
    ttl_hours   REAL,
    content_type TEXT,
    size_bytes  INTEGER,
    source_id   TEXT,
    hits        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fetched ON entries(fetched_at);
CREATE INDEX IF NOT EXISTS idx_source ON entries(source_id);
"""


class FetchCache:
    """内容寻址缓存：URL → 内容哈希 → 原始内容。"""

    def __init__(self, cache_dir: str = CACHE_DIR):
        self.cache_dir = cache_dir
        self.content_dir = os.path.join(cache_dir, "content")
        self.db_path = os.path.join(cache_dir, "index.db")
        os.makedirs(self.content_dir, exist_ok=True)
        self._init_db()
        # 会话内统计
        self._hits = 0
        self._misses = 0

    # ── 内部 ──────────────────────────────

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.executescript(_SCHEMA)
        con.commit()
        con.close()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _hash(content: str | bytes) -> str:
        data = content.encode("utf-8") if isinstance(content, str) else content
        return hashlib.sha256(data).hexdigest()

    def _content_path(self, content_hash: str) -> str:
        bucket = content_hash[:2]
        d = os.path.join(self.content_dir, bucket)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, content_hash)

    # ── 公共 API ──────────────────────────

    def get(self, url: str, ttl_hours: float | None = None,
            source_id: str | None = None) -> dict | None:
        """
        取缓存。命中且未过期返回 dict，否则 None。

        Args:
            url: 规范化 URL（用 canonical，别用原始带 utm 的）
            ttl_hours: 覆盖条目默认 TTL
            source_id: 可选，用于统计

        Returns:
            {"content": str, "etag": str|None, "fetched_at": float,
             "hash": str, "from_cache": True}
        """
        try:
            con = self._conn()
            row = con.execute(
                "SELECT * FROM entries WHERE url = ?", (url,)).fetchone()
            if row is None:
                self._misses += 1
                con.close()
                return None

            # TTL 检查
            ttl = ttl_hours if ttl_hours is not None else row["ttl_hours"]
            if ttl is not None:
                age_h = (time.time() - row["fetched_at"]) / 3600.0
                if age_h > ttl:
                    self._misses += 1
                    con.close()
                    return None

            # 读内容
            path = self._content_path(row["content_hash"])
            if not os.path.exists(path):
                # 索引有但内容丢了 → 当作未命中
                con.execute("DELETE FROM entries WHERE url = ?", (url,))
                con.commit()
                self._misses += 1
                con.close()
                return None

            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()

            con.execute("UPDATE entries SET hits = hits + 1 WHERE url = ?", (url,))
            con.commit()
            con.close()
            self._hits += 1
            return {
                "content": content,
                "etag": row["etag"],
                "last_modified": row["last_modified"],
                "fetched_at": row["fetched_at"],
                "hash": row["content_hash"],
                "from_cache": True,
            }
        except Exception:
            self._misses += 1
            return None

    def put(self, url: str, content: str | bytes, etag: str | None = None,
            last_modified: str | None = None, ttl_hours: float | None = None,
            content_type: str | None = None, source_id: str | None = None):
        """写入缓存（内容寻址存储 + 索引更新）。"""
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        h = self._hash(content)
        path = self._content_path(h)

        # 内容寻址：同 hash 不重复写
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

        con = self._conn()
        con.execute("""
            INSERT INTO entries
              (url, content_hash, etag, last_modified, fetched_at, ttl_hours,
               content_type, size_bytes, source_id, hits)
            VALUES (?,?,?,?,?,?,?,?,?,0)
            ON CONFLICT(url) DO UPDATE SET
              content_hash = excluded.content_hash,
              etag = excluded.etag,
              last_modified = excluded.last_modified,
              fetched_at = excluded.fetched_at,
              ttl_hours = excluded.ttl_hours,
              content_type = excluded.content_type,
              size_bytes = excluded.size_bytes,
              source_id = excluded.source_id
        """, (url, h, etag, last_modified, time.time(), ttl_hours,
              content_type, len(content.encode("utf-8")), source_id))
        con.commit()
        con.close()
        return h

    def get_validator(self, url: str) -> dict:
        """取 ETag/Last-Modified（用于发条件请求，未变则 304 不重传）。"""
        try:
            con = self._conn()
            row = con.execute(
                "SELECT etag, last_modified FROM entries WHERE url = ?", (url,)).fetchone()
            con.close()
            if row:
                return {"etag": row["etag"], "last_modified": row["last_modified"]}
        except Exception:
            pass
        return {}

    def is_changed(self, url: str, content: str | bytes) -> bool:
        """内容是否相对缓存发生变化（用于变更检测）。"""
        try:
            con = self._conn()
            row = con.execute(
                "SELECT content_hash FROM entries WHERE url = ?", (url,)).fetchone()
            con.close()
            if row is None:
                return True
            return row["content_hash"] != self._hash(content)
        except Exception:
            return True

    def invalidate(self, url: str):
        """失效单个 URL。"""
        con = self._conn()
        con.execute("DELETE FROM entries WHERE url = ?", (url,))
        con.commit()
        con.close()

    def cleanup_expired(self) -> int:
        """清理过期条目 + 孤儿内容。返回清理条数。"""
        con = self._conn()
        rows = con.execute(
            "SELECT url, content_hash, fetched_at, ttl_hours FROM entries").fetchall()
        removed = 0
        for r in rows:
            if r["ttl_hours"] is None:
                continue
            age_h = (time.time() - r["fetched_at"]) / 3600.0
            if age_h > r["ttl_hours"]:
                con.execute("DELETE FROM entries WHERE url = ?", (r["url"],))
                removed += 1
        con.commit()

        # 清理孤儿内容文件（索引里不再引用的）
        used = {row["content_hash"] for row in
                con.execute("SELECT DISTINCT content_hash FROM entries").fetchall()}
        con.close()
        for bucket in os.listdir(self.content_dir) if os.path.exists(self.content_dir) else []:
            bpath = os.path.join(self.content_dir, bucket)
            if not os.path.isdir(bpath):
                continue
            for fname in os.listdir(bpath):
                if fname not in used:
                    try:
                        os.remove(os.path.join(bpath, fname))
                    except OSError:
                        pass
        return removed

    def stats(self) -> dict:
        """缓存统计（命中率、条目数、占用空间）。"""
        try:
            con = self._conn()
            n = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            size = con.execute("SELECT COALESCE(SUM(size_bytes),0) FROM entries").fetchone()[0]
            total_hits = con.execute("SELECT COALESCE(SUM(hits),0) FROM entries").fetchone()[0]
            by_source = {r["source_id"] or "unknown": r["c"]
                         for r in con.execute(
                             "SELECT source_id, COUNT(*) c FROM entries GROUP BY source_id"
                         ).fetchall()}
            con.close()
        except Exception:
            n, size, total_hits, by_source = 0, 0, 0, {}

        session_total = self._hits + self._misses
        return {
            "entries": n,
            "size_mb": round(size / 1048576, 2),
            "total_hits": total_hits,
            "session_hits": self._hits,
            "session_misses": self._misses,
            "session_hit_rate": (round(self._hits / session_total, 3)
                                 if session_total else None),
            "by_source": by_source,
        }


# ─── 便捷封装：带缓存的抓取 ──────────────────────

_default_cache: FetchCache | None = None


def get_cache() -> FetchCache:
    """全局单例缓存。"""
    global _default_cache
    if _default_cache is None:
        _default_cache = FetchCache()
    return _default_cache


def cached_fetch(url: str, fetcher, ttl_hours: float = 24,
                 source_id: str | None = None) -> str:
    """
    带缓存的抓取：命中则返回缓存，否则调用 fetcher(url) 并缓存。

    Args:
        url: 规范化 URL
        fetcher: callable(url) -> str
        ttl_hours: 缓存有效期
        source_id: 数据源 ID（用于统计）

    Returns:
        内容字符串
    """
    c = get_cache()
    hit = c.get(url, ttl_hours=ttl_hours, source_id=source_id)
    if hit is not None:
        return hit["content"]
    content = fetcher(url)
    if content:
        c.put(url, content, ttl_hours=ttl_hours, source_id=source_id)
    return content


# ─── CLI ─────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="agent-eye 缓存管理")
    p.add_argument("--stats", action="store_true", help="显示统计")
    p.add_argument("--cleanup", action="store_true", help="清理过期条目")
    p.add_argument("--url", help="查看某 URL 的缓存状态")
    args = p.parse_args()

    c = FetchCache()
    if args.cleanup:
        n = c.cleanup_expired()
        print(f"✅ 清理 {n} 条过期条目")
    if args.url:
        hit = c.get(args.url)
        print(json.dumps(hit, ensure_ascii=False, indent=2) if hit
              else f"❌ 未命中: {args.url}")
    if args.stats or not (args.cleanup or args.url):
        print(json.dumps(c.stats(), ensure_ascii=False, indent=2))
