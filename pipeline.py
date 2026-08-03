#!/usr/bin/env python3
"""
pipeline.py — agent-eye 搬运视频全自动流水线

一键：选品 → 下载 → 转录 → 翻译 → 配音 → 合成视频

用法:
    python3 pipeline.py "AI工具"                  # 全自动
    python3 pipeline.py "AI工具" --steps 1,2       # 只跑选品+下载
"""

import os
import subprocess
import sys
import json
import urllib.request

# ─── 配置 ──────────────────────────────────────

OUTPUT_DIR = "downloads"
PROXY = None  # 自动检测
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# ─── 步骤 ──────────────────────────────────────


def step1_scout(keyword: str) -> list[dict]:
    """① 选品"""
    print("\n" + "=" * 60)
    print(f"  ① 选品: \"{keyword}\"")
    print("=" * 60)

    from trend_scout import scout
    results = scout(keyword, proxy=PROXY)

    # 只取搬运价值 ≥ 60 的
    good = [r for r in results if r.move_score >= 50]
    if not good:
        print("  ⚠️ 没有符合条件的内容")
        return []

    for r in good[:5]:
        print(f"  {r.move_score}/100 | {r.confidence:.0%} | {r.item.title[:60]}")

    return [
        {"title": r.item.title, "url": r.item.url, "platform": r.item.platform}
        for r in good[:3]  # 最多 3 个
    ]


def step2_download(items: list[dict]) -> list[str]:
    """② 下载"""
    print("\n" + "=" * 60)
    print(f"  ② 下载 {len(items)} 个视频")
    print("=" * 60)

    from trend_scout import ScoutResult, TrendItem, download_items
    results = [
        ScoutResult(TrendItem(it["title"], it["url"], it["platform"]),
                    move_score=80, confidence=0.9, reason="")
        for it in items
    ]
    paths = download_items(results, min_score=0, max_items=len(items),
                           output_dir=OUTPUT_DIR, proxy=PROXY)
    return paths


def step3_transcribe(video_path: str) -> str:
    """③ 转录（从视频提取音频 → whisper）"""
    print("\n" + "=" * 60)
    print(f"  ③ 转录: {os.path.basename(video_path)}")
    print("=" * 60)

    # 从视频提取音频
    mp3_path = os.path.splitext(video_path)[0] + ".mp3"
    if not os.path.exists(mp3_path):
        subprocess.run(["ffmpeg", "-i", video_path, "-vn", "-acodec", "libmp3lame",
                        "-q:a", "2", mp3_path, "-y"],
                       capture_output=True, timeout=300)

    result = subprocess.run(
        ["whisper", mp3_path, "--model", "tiny", "--language", "en",
         "--output_format", "txt", "--output_dir", OUTPUT_DIR],
        capture_output=True, text=True, timeout=600,
    )

    # 找输出的 txt
    name = os.path.splitext(os.path.basename(audio_path))[0]
    txt_path = os.path.join(OUTPUT_DIR, f"{name}.txt")
    if os.path.exists(txt_path):
        with open(txt_path, encoding="utf-8") as f:
            lines = f.readlines()
        print(f"  ✅ {len(lines)} 行转录")
        return txt_path
    print(f"  ❌ 转录失败")
    return ""


def step4_translate(txt_path: str) -> str:
    """④ 翻译"""
    print("\n" + "=" * 60)
    print(f"  ④ 翻译")
    print("=" * 60)

    with open(txt_path, encoding="utf-8") as f:
        text = f.read()

    if not DEEPSEEK_KEY:
        print("  ⚠️ 无 API Key，跳过翻译")
        return ""

    # 分批翻译
    lines = text.split("\n")
    chunk_size = 100
    parts = []

    for i in range(0, len(lines), chunk_size):
        chunk = "\n".join(lines[i:i + chunk_size])
        payload = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你是专业翻译。英文转中文。保持技术词不翻。只输出翻译。"},
                {"role": "user", "content": chunk},
            ],
            "max_tokens": 4096, "temperature": 0.1,
        }).encode()

        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {DEEPSEEK_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            parts.append(json.loads(resp.read())["choices"][0]["message"]["content"])

        print(f"  {i+1}-{min(i+chunk_size, len(lines))}/{len(lines)}")

    zh = "\n".join(parts)
    zh_path = os.path.join(OUTPUT_DIR,
                           os.path.splitext(os.path.basename(txt_path))[0] + "_zh.txt")
    with open(zh_path, "w", encoding="utf-8") as f:
        f.write(zh)
    print(f"  ✅ {len(zh)} 字符中文")
    return zh_path


def step5_voice(zh_path: str) -> str:
    """⑤ 配音"""
    print("\n" + "=" * 60)
    print(f"  ⑤ 配音 (edge-tts)")
    print("=" * 60)

    mp3_path = zh_path.replace(".txt", ".mp3")
    subprocess.run(["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
                    "-f", zh_path, "--write-media", mp3_path],
                   capture_output=True, timeout=300)

    size = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
    print(f"  ✅ {size / 1024 / 1024:.0f}MB")
    return mp3_path


def step6_render(audio_path: str) -> str:
    """⑥ 合成视频"""
    print("\n" + "=" * 60)
    print(f"  ⑥ 合成视频")
    print("=" * 60)

    out = os.path.join(OUTPUT_DIR, "final_output.mp4")
    subprocess.run([
        "ffmpeg", "-f", "lavfi", "-i",
        "color=c=0x0D1117:s=1920x1080",
        "-i", audio_path,
        "-filter_complex",
        "[0:v]drawtext=text='AI 自动搬运':fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2[outv]",
        "-map", "[outv]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k", "-shortest", out, "-y",
    ], capture_output=True, timeout=600)

    size = os.path.getsize(out) if os.path.exists(out) else 0
    print(f"  ✅ {size / 1024 / 1024:.0f}MB → {out}")
    return out


# ─── 主流程 ──────────────────────────────────────


def run_pipeline(keyword: str, steps: str = "1,2,3,4,5,6"):
    """完整流水线"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 代理检测
    global PROXY
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    if s.connect_ex(("127.0.0.1", 10808)) == 0:
        PROXY = "socks5://127.0.0.1:10808"
        print(f"🌐 代理: {PROXY}")
    s.close()

    step_ids = [int(s) for s in steps.split(",")]

    items = []
    paths = []

    if 1 in step_ids:
        items = step1_scout(keyword)
        if not items:
            return

    if 2 in step_ids:
        paths = step2_download(items)

    if 3 in step_ids and paths:
        audio = paths[0]  # 处理第一个下载的文件
        txt_path = step3_transcribe(audio)
    else:
        # 找已有转录
        txts = [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".txt") and "_zh" not in f]
        txt_path = os.path.join(OUTPUT_DIR, txts[0]) if txts else ""

    if 4 in step_ids and txt_path:
        zh_path = step4_translate(txt_path)
    else:
        zhs = [f for f in os.listdir(OUTPUT_DIR) if f.endswith("_zh.txt")]
        zh_path = os.path.join(OUTPUT_DIR, zhs[0]) if zhs else ""

    if 5 in step_ids and zh_path:
        mp3_path = step5_voice(zh_path)
    else:
        mp3s = [f for f in os.listdir(OUTPUT_DIR) if f.endswith("_zh.mp3")]
        mp3_path = os.path.join(OUTPUT_DIR, mp3s[0]) if mp3s else ""

    if 6 in step_ids and mp3_path:
        step6_render(mp3_path)

    print("\n" + "=" * 60)
    print("  🎉 流水线完成！")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 pipeline.py <关键词> [--steps 1,2,3]")
        sys.exit(1)

    keyword = sys.argv[1]
    steps = "1,2,3,4,5,6"

    for i, a in enumerate(sys.argv):
        if a == "--steps" and i + 1 < len(sys.argv):
            steps = sys.argv[i + 1]

    run_pipeline(keyword, steps)
