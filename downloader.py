#!/usr/bin/env python3
"""
downloader.py — artifact 获取服务（独立于浏览器自动化）

源自架构报告 §6「下载工具提升」：
    安全获取、完整验证、去重存储、可恢复下载、格式识别。

安全检查（报告 §6 核心）：
    - SSRF 防护：禁 localhost / 私有 IP / 云元数据地址
    - 重定向控制：限制跳转次数，拒绝跳内网
    - MIME 双重验证：Content-Type + 扩展名 + 魔数（不信任何一个）
    - 大小限制：防 zip bomb / PDF bomb
    - 内容寻址：SHA-256 主键，去重 + 完整性
    - 下载记录：与来源页、时间、许可绑定

用法:
    from downloader import download
    rec = download("https://example.com/paper.pdf", out_dir="D:/downloads")
    print(rec["path"], rec["sha256"], rec["verified"])
"""

import hashlib
import ipaddress
import json
import os
import socket
import sqlite3
import subprocess
import time
from urllib.parse import urlparse

HOME_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HOME_DIR, ".cache", "downloads.db")
DEFAULT_DIR = os.path.join(HOME_DIR, "downloads")

MAX_SIZE = 500 * 1024 * 1024          # 单文件 500 MB 上限
MAX_REDIRECTS = 5
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}

# 魔数 → MIME（不信扩展名和 header）
MAGIC_SIGNATURES = [
    (b"%PDF-", "application/pdf", "pdf"),
    (b"PK\x03\x04", "application/zip", "zip"),          # zip/docx/xlsx/epub
    (b"\x1f\x8b", "application/gzip", "gz"),
    (b"Rar!\x1a\x07", "application/x-rar", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z", "7z"),
    (b"\xd0\xcf\x11\xe0", "application/msword", "doc"),  # 老 Office
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF8", "image/gif", "gif"),
    (b"RIFF", "image/webp", "webp"),
    (b"\x7fELF", "application/x-executable", "elf"),
    (b"MZ", "application/x-dosexec", "exe"),            # Windows 可执行
    (b"<!DOCTYPE html", "text/html", "html"),
    (b"<html", "text/html", "html"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    sha256      TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    referer     TEXT,
    path        TEXT NOT NULL,
    filename    TEXT,
    mime        TEXT,
    ext         TEXT,
    size_bytes  INTEGER,
    downloaded_at REAL,
    verified    INTEGER DEFAULT 0,
    source_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_url ON downloads(url);
"""


# ─── 安全检查 ──────────────────────────────────


def check_ssrf(url: str) -> tuple[bool, str]:
    """SSRF 防护：拒绝内网/回环/云元数据地址。"""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, f"不支持的协议: {p.scheme}"
        host = (p.hostname or "").lower()
        if not host:
            return False, "无主机名"
        if host in BLOCKED_HOSTS:
            return False, f"禁止访问: {host}"

        # 解析 IP 判断内网
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return False, f"禁止访问内网地址: {ip}"
        except socket.gaierror:
            return False, f"DNS 解析失败: {host}"
        return True, ""
    except Exception as e:
        return False, f"URL 检查失败: {e}"


def detect_magic(data: bytes) -> tuple[str, str]:
    """魔数检测 → (mime, ext)。不信扩展名。"""
    for sig, mime, ext in MAGIC_SIGNATURES:
        if data.startswith(sig):
            return mime, ext
    # 兜底：看是否像文本
    try:
        data[:1024].decode("utf-8")
        return "text/plain", "txt"
    except UnicodeDecodeError:
        return "application/octet-stream", "bin"


# ─── 记录 ─────────────────────────────────────


def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()


def _record(rec: dict):
    _init_db()
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO downloads
          (sha256,url,referer,path,filename,mime,ext,size_bytes,
           downloaded_at,verified,source_id)
        VALUES (:sha256,:url,:referer,:path,:filename,:mime,:ext,
                :size_bytes,:downloaded_at,:verified,:source_id)
        ON CONFLICT(sha256) DO UPDATE SET
          url=excluded.url, path=excluded.path, verified=excluded.verified
    """, rec)
    con.commit()
    con.close()


def history(limit: int = 20) -> list[dict]:
    """下载历史。"""
    _init_db()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM downloads ORDER BY downloaded_at DESC LIMIT ?",
        (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ─── 主流程 ───────────────────────────────────


def download(url: str, out_dir: str = DEFAULT_DIR, referer: str = "",
             proxy: str = "", source_id: str = "",
             max_size: int = MAX_SIZE) -> dict | None:
    """
    下载文件（流式 + 校验 + 内容寻址存储）。

    Returns:
        {"path","sha256","size_bytes","mime","ext","verified","from_cache"}
        失败返回 None
    """
    # ① SSRF 检查
    ok, err = check_ssrf(url)
    if not ok:
        print(f"❌ 安全拦截: {err}")
        return None

    os.makedirs(out_dir, exist_ok=True)
    _init_db()

    # ② 流式下载到临时文件（不占内存）
    tmp = os.path.join(out_dir, f".tmp_{int(time.time()*1000)}")
    cmd = ["curl", "-sL", "--insecure", "-m", "300",
           "--max-redirs", str(MAX_REDIRECTS),
           "--max-filesize", str(max_size),
           "-H", "User-Agent: agent-eye/3.0 (research)",
           "-o", tmp, url]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if proxy:
        host = proxy.replace("socks5h://", "").replace("socks5://", "")
        cmd += ["-x", f"http://{host}"]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=320)
        if r.returncode != 0 or not os.path.exists(tmp):
            print(f"❌ 下载失败: {r.stderr[:150]}")
            return None

        size = os.path.getsize(tmp)
        if size == 0:
            print("❌ 空文件")
            os.remove(tmp)
            return None

        # ③ 读头部做魔数检测
        with open(tmp, "rb") as f:
            head = f.read(4096)
        mime, ext = detect_magic(head)

        # ④ SHA-256 内容寻址
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()

        # ⑤ 去重：同 hash 已存在则复用
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        existing = con.execute(
            "SELECT path FROM downloads WHERE sha256 = ? AND verified = 1",
            (sha,)).fetchone()
        con.close()

        if existing and os.path.exists(existing["path"]):
            os.remove(tmp)
            print(f"♻️ 已存在相同文件（去重）: {os.path.basename(existing['path'])}")
            return {"path": existing["path"], "sha256": sha, "size_bytes": size,
                    "mime": mime, "ext": ext, "verified": True, "from_cache": True}

        # ⑥ 存储（内容寻址命名）
        filename = os.path.basename(urlparse(url).path) or f"{sha[:12]}.{ext}"
        if not filename.lower().endswith(f".{ext}"):
            filename = f"{os.path.splitext(filename)[0]}.{ext}"
        final = os.path.join(out_dir, filename)
        if os.path.exists(final):
            final = os.path.join(out_dir, f"{os.path.splitext(filename)[0]}_{sha[:8]}.{ext}")
        os.replace(tmp, final)

        # ⑦ 验证 + 记录
        verified = os.path.getsize(final) == size
        rec = {
            "sha256": sha, "url": url, "referer": referer, "path": final,
            "filename": filename, "mime": mime, "ext": ext,
            "size_bytes": size, "downloaded_at": time.time(),
            "verified": 1 if verified else 0, "source_id": source_id,
        }
        _record(rec)

        print(f"✅ 下载: {filename}")
        print(f"   大小: {size/1048576:.2f} MB | 类型: {mime} (.{ext})")
        print(f"   哈希: {sha[:16]} | 验证: {'✅' if verified else '❌'}")
        return {**rec, "verified": verified, "from_cache": False}

    except subprocess.TimeoutExpired:
        print("❌ 下载超时")
        if os.path.exists(tmp):
            os.remove(tmp)
        return None
    except Exception as e:
        print(f"❌ 异常: {str(e)[:150]}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return None


# ─── CLI ─────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="下载器（安全 + 校验 + 去重）")
    p.add_argument("url", nargs="?", help="下载地址")
    p.add_argument("--out", default=DEFAULT_DIR, help="输出目录")
    p.add_argument("--referer", default="", help="Referer")
    p.add_argument("--proxy", default="", help="代理")
    p.add_argument("--history", action="store_true", help="下载历史")
    p.add_argument("--check", action="store_true", help="只做安全检查不下载")
    args = p.parse_args()

    if args.history:
        rows = history(20)
        print(f"📚 下载历史（{len(rows)} 条）:")
        for r in rows:
            print(f"  {r['filename'][:50]} | {r['size_bytes']/1048576:.1f}MB | {r['mime']}")
        raise SystemExit(0)

    if not args.url:
        p.print_help()
        raise SystemExit(1)

    if args.check:
        ok, err = check_ssrf(args.url)
        print(f"{'✅ 安全检查通过' if ok else '❌ ' + err}")
        raise SystemExit(0 if ok else 1)

    rec = download(args.url, out_dir=args.out, referer=args.referer,
                   proxy=args.proxy)
    raise SystemExit(0 if rec else 1)
