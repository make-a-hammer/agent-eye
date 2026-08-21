#!/usr/bin/env python3
"""
video_analyze.py — 视频分析流水线（本地+免费）

视觉: ffmpeg 抽帧 → 豆包看图（browser_worker 上传）
听觉: ffmpeg 提音频 → whisper 转录
合并: 输出结构化分析

用法:
    python3 video_analyze.py <视频路径> [--frames 6] [--lang en]
"""
import argparse
import json
import os
import subprocess
import sys

AGENT_EYE = os.path.dirname(os.path.abspath(__file__))
FRAMES_DIR = os.path.join(AGENT_EYE, ".screenshots", "video_frames")
AUDIO_WAV = os.path.join(AGENT_EYE, ".screenshots", "video_audio.wav")


def extract_frames(video: str, count: int) -> list[str]:
    """抽帧：均匀取 count 帧"""
    os.makedirs(FRAMES_DIR, exist_ok=True)
    for f in os.listdir(FRAMES_DIR):
        os.remove(os.path.join(FRAMES_DIR, f))
    # 先拿时长
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", video],
        capture_output=True, text=True
    ).stdout.strip()
    try:
        seconds = float(dur)
    except ValueError:
        seconds = 90
    interval = max(1, seconds / count)
    cmd = ["ffmpeg", "-y", "-i", video, "-vf", f"fps=1/{interval:.2f}",
           "-q:v", "3", os.path.join(FRAMES_DIR, "frame_%02d.jpg")]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    frames = sorted(os.path.join(FRAMES_DIR, f) for f in os.listdir(FRAMES_DIR))
    return frames[:count]


def extract_audio(video: str) -> str:
    """提音频（16k 单声道 WAV）"""
    cmd = ["ffmpeg", "-y", "-i", video, "-vn", "-ar", "16000", "-ac", "1", AUDIO_WAV]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return AUDIO_WAV


def transcribe(wav: str, lang: str) -> str:
    """whisper 转录"""
    import whisper
    model = whisper.load_model("small")
    result = model.transcribe(wav, language=lang)
    return result["text"]


def doubao_ask(frames: list[str], question: str) -> str:
    """豆包看图：上传多帧 + 提问，返回回答文本"""
    import json as _json
    js_code = f"""
    const {{ chromium }} = require('playwright');
    (async () => {{
      const ctx = await chromium.launchPersistentContext(
        process.env.USERPROFILE + '/ego_profile',
        {{ headless: false, viewport: {{width:1280,height:900}}, locale: 'zh-CN',
           args: ['--disable-blink-features=AutomationControlled'] }});
      const page = ctx.pages()[0] || await ctx.newPage();
      await page.goto('https://www.doubao.com/chat/', {{waitUntil: 'commit', timeout: 30000}});
      await page.waitForTimeout(5000);
      await page.setInputFiles('input[type="file"]', {_json.dumps(frames)});
      await page.waitForTimeout(6000);
      const inputSel = '.tiptap.ProseMirror, [contenteditable="true"]';
      await page.click(inputSel);
      await page.keyboard.type({_json.dumps(question)}, {{delay: 15}});
      await page.waitForTimeout(500);
      await page.keyboard.press('Enter');
      await page.waitForTimeout(25000);
      const answer = await page.evaluate(() => {{
        const texts = new Set();
        document.querySelectorAll('[class*="message"],[class*="Message"],[class*="content"]').forEach(m => {{
          const t = (m.textContent || '').trim();
          if (t.length > 80 && !t.includes('对话') && !t.includes('文件数量')) texts.add(t);
        }});
        return Array.from(texts).slice(-1);
      }});
      console.log('__DB_RESULT__' + JSON.stringify(answer) + '__END__');
      await ctx.close();
    }})();
    """
    tmp = os.path.join(AGENT_EYE, "_db_analyze_tmp.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(js_code)
    r = subprocess.run(["node", tmp], capture_output=True, text=True, timeout=120, cwd=AGENT_EYE)
    os.remove(tmp)
    out = r.stdout
    marker_s = out.find("__DB_RESULT__")
    marker_e = out.find("__END__")
    if marker_s >= 0 and marker_e > marker_s:
        try:
            arr = json.loads(out[marker_s + len("__DB_RESULT__"):marker_e])
            return arr[0] if arr else "(豆包无回答)"
        except Exception:
            pass
    return out[-1500:]


def deepseek_ask(frames: list[str], question: str, api_key: str = "") -> str:
    """DeepSeek Vision 看图：一次丢多帧（非 thinking 模式，快+便宜）"""
    import base64
    import urllib.request
    import urllib.error

    key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return "(未设置 DEEPSEEK_API_KEY)"

    # 统一转真实 JPEG（小红书图是 HEIF/PNG 伪装成 .jpg，DeepSeek 按内容检测格式）
    tmpdir = os.path.join(AGENT_EYE, ".screenshots", "ds_frames")
    os.makedirs(tmpdir, exist_ok=True)
    real_jpegs = []
    for i, f in enumerate(frames):
        out = os.path.join(tmpdir, f"f{i:02d}.jpg")
        subprocess.run(["ffmpeg", "-y", "-i", f, "-q:v", "3", out],
                       capture_output=True, text=True, timeout=30)
        if os.path.exists(out) and os.path.getsize(out) > 500:
            real_jpegs.append(out)

    # 多帧 → 多 image_url 块 + 文字
    content_blocks = []
    for f in real_jpegs:
        with open(f, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })
    content_blocks.append({"type": "text", "text": question})

    payload = {
        "model": "deepseek-v4-flash-vision-exp",
        "messages": [{"role": "user", "content": content_blocks}],
        "max_tokens": 1500,
        "thinking": {"type": "disabled"},   # 非思考模式：快 94%
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        return d["choices"][0]["message"].get("content", "(空)")
    except urllib.error.HTTPError as e:
        return f"(DeepSeek 错误 {e.code}: {e.read().decode()[:200]})"
    except Exception as e:
        return f"(DeepSeek 调用失败: {e})"


def main():
    p = argparse.ArgumentParser(description="视频分析流水线")
    p.add_argument("video", help="视频文件路径")
    p.add_argument("--frames", type=int, default=6, help="抽帧数")
    p.add_argument("--lang", default="en", help="音频语言")
    p.add_argument("--visual-only", action="store_true", help="只看图不转录")
    p.add_argument("--vision", choices=["doubao", "deepseek"], default="doubao",
                   help="视觉通道: doubao(网页模拟人) / deepseek(API快通道)")
    args = p.parse_args()

    print(f"🎬 分析视频: {os.path.basename(args.video)}")
    print(f"   ⏱ 抽帧 {args.frames} 张...")
    frames = extract_frames(args.video, args.frames)
    print(f"   ✅ {len(frames)} 帧: {os.path.basename(frames[0])} ...")

    question = (
        "这是同一个视频的多个关键帧截图。请分析：1)视频主题 2)讲解逻辑和结构 "
        "3)从画面能看出哪些具体内容（图表/动画/字幕/人物）4)视觉风格。用中文详细回答。"
    )
    if args.vision == "deepseek":
        print("   👁 DeepSeek Vision 看图分析（快通道）...")
        visual = deepseek_ask(frames, question)
        vision_name = "DeepSeek Vision"
    else:
        print("   👁 豆包看图分析（网页模拟人）...")
        visual = doubao_ask(frames, question)
        vision_name = "豆包"
    print("   ── 视觉分析 ──")
    print(visual[:2000])

    if not args.visual_only:
        print("   🔊 提取音频...")
        wav = extract_audio(args.video)
        print("   🧠 whisper 转录...")
        transcript = transcribe(wav, args.lang)
        print("   ── 语音转录 ──")
        print(transcript[:2000])

        # 保存
        out_path = os.path.join(AGENT_EYE, "video_analysis.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"# 视频分析: {os.path.basename(args.video)}\n\n")
            f.write(f"## 视觉分析（{vision_name}）\n" + visual + "\n\n")
            f.write("## 语音转录（whisper）\n" + transcript + "\n")
        print(f"\n📄 已保存: {out_path}")


if __name__ == "__main__":
    main()
