#!/usr/bin/env python3
"""
ego_win_poc.py — Windows Ego-Lite Browser Engine Proof of Concept

Demonstrates:
  1) Persistent Chrome profile (cookies/storage survive restart)
  2) Multiple isolated "spaces" (contexts) from one browser
  3) Parallel async context operation

Usage:
    python ego_win_poc.py                          # temp profile
    python ego_win_poc.py --real-profile           # real Chrome profile
"""

import argparse
import asyncio
import os
import tempfile
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

TMP = Path(tempfile.mkdtemp(prefix="ego_poc_"))
PROFILE_DIR = TMP / "profile"
SCREENSHOT_DIR = TMP / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Real Chrome profiles on Windows
REAL_PROFILES = [
    r"C:\Users\%s\AppData\Local\Google\Chrome\User Data" % os.environ.get("USERNAME", "Default"),
    r"C:\Users\%s\AppData\Local\Microsoft\Edge\User Data" % os.environ.get("USERNAME", "Default"),
]


async def take_screenshot(page, label: str) -> str:
    ts = datetime.now().strftime("%H%M%S%f")
    path = str(SCREENSHOT_DIR / f"{label}_{ts}.png")
    await page.screenshot(path=path, full_page=True)
    return path


async def main(use_real_profile: bool = False):
    print("=" * 70)
    print(" Ego-Lite POC: Persistent Profile + Isolated Spaces + Parallel Async")
    print("=" * 70)

    if use_real_profile:
        profile = next((p for p in REAL_PROFILES if Path(p).exists()), None)
        if profile:
            print(f" Profile: {profile} (REAL Chrome data)")
        else:
            print(" ! Real profile not found, falling back to temp.")
            use_real_profile = False

    if not use_real_profile:
        profile = str(PROFILE_DIR)
        print(f" Profile: {profile} (temp — simulating Chrome user-data-dir)")

    print(f" Screenshots: {SCREENSHOT_DIR}\n")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=True,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        browser = ctx.browser

        # Phase 1: Space A — set cookie via JS (real profile would have real cookies)
        print("Phase 1 — Space A (set ego_session cookie):")
        page = await ctx.new_page()
        await page.goto("https://www.google.com", wait_until="domcontentloaded")
        await page.evaluate("document.cookie = 'ego_session=poc-42; path=/; domain=.google.com'")
        cookies = await ctx.cookies()
        has_ego = any(c["name"] == "ego_session" for c in cookies)
        sp = await take_screenshot(page, "Space-A")
        print(f"  cookies={len(cookies)}  ego_cookie_set={has_ego}  ss={Path(sp).name}")
        await page.close()

        # Phase 2: Space B — isolated context (separate cookie jar)
        print("Phase 2 — Space B (isolated context):")
        ctx_b = await browser.new_context()
        page_b = await ctx_b.new_page()
        await page_b.goto("https://www.google.com", wait_until="domcontentloaded")
        cookies_b = await ctx_b.cookies()
        has_ego_b = any(c["name"] == "ego_session" for c in cookies_b)
        sp_b = await take_screenshot(page_b, "Space-B")
        print(f"  cookies={len(cookies_b)}  ego_cookie={has_ego_b} (isolated → clean)  ss={Path(sp_b).name}")
        await ctx_b.close()

        # Phase 3: Space C — clones A's storage_state
        print("Phase 3 — Space C (clone A's storage_state):")
        state = await ctx.storage_state()
        ctx_c = await browser.new_context(storage_state=state)
        page_c = await ctx_c.new_page()
        await page_c.goto("https://www.google.com", wait_until="domcontentloaded")
        cookies_c = await ctx_c.cookies()
        has_ego_c = any(c["name"] == "ego_session" for c in cookies_c)
        sp_c = await take_screenshot(page_c, "Space-C-Clone")
        print(f"  cookies={len(cookies_c)}  ego_cookie={has_ego_c} (cloned)  ss={Path(sp_c).name}")
        await ctx_c.close()

        # Phase 4: Parallel — 3 spaces at once, proving asyncio concurrency
        print("Phase 4 — Parallel spaces (asyncio.gather):")
        async def parallel_space(ctx_in, label: str) -> dict:
            p = await ctx_in.new_page()
            await p.goto("https://www.google.com", wait_until="domcontentloaded")
            c = await ctx_in.cookies()
            h = any(x["name"] == "ego_session" for x in c)
            s = await take_screenshot(p, label)
            await p.close()
            print(f"  [{label}] cookies={len(c)}  ego={h}  ss={Path(s).name}")
            return {"label": label, "ego": h}

        ctx_p1 = await browser.new_context()
        ctx_p2 = await browser.new_context(storage_state=await ctx.storage_state())
        ctx_p3 = await browser.new_context()
        results = await asyncio.gather(
            parallel_space(ctx_p1, "Parallel-1-Fresh"),
            parallel_space(ctx_p2, "Parallel-2-Cloned"),
            parallel_space(ctx_p3, "Parallel-3-Fresh"),
        )
        for r in results:
            print(f"    → {r['label']}: ego={r['ego']}")
        await asyncio.gather(ctx_p1.close(), ctx_p2.close(), ctx_p3.close())

        # Phase 5: Restart — prove profile persistence
        print("\nPhase 5 — Restart with same profile (persistence check):")
        await ctx.close()
        ctx2 = await pw.chromium.launch_persistent_context(
            user_data_dir=profile, headless=True, viewport={"width": 1280, "height": 900},
        )
        page2 = await ctx2.new_page()
        await page2.goto("https://www.google.com", wait_until="domcontentloaded")
        cookies2 = await ctx2.cookies()
        has_ego2 = any(c["name"] == "ego_session" for c in cookies2)
        sp2 = await take_screenshot(page2, "Phase5-Restart")
        print(f"  cookies={len(cookies2)}  ego_persisted_after_restart={has_ego2}  ss={Path(sp2).name}")
        await ctx2.close()

    print(f"\n Done. {len(list(SCREENSHOT_DIR.iterdir()))} screenshots in: {SCREENSHOT_DIR}")
    for p in sorted(SCREENSHOT_DIR.iterdir()):
        print(f"   {p.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-profile", action="store_true", help="Use real Chrome profile")
    args = parser.parse_args()
    asyncio.run(main(args.real_profile))
