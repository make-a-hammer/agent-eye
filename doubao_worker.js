#!/usr/bin/env node
/**
 * doubao_worker.js — 豆包网页版常驻对话 worker
 *
 * 与 browser_worker.js 同一通信模式：stdin 收命令，stdout 回 JSON。
 * 区别：浏览器常驻不关闭，会话保持，随机人类节奏。
 *
 * 命令:
 *   {"action":"ask","question":"...","images":["/path/a.png",...]}
 *   {"action":"new_chat"}          // 开新对话（可选）
 *   {"action":"status"}            // 会话状态
 *   {"action":"exit"}
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const os = require('os');

const PROFILE = path.join(os.homedir(), 'ego_profile');
const DOUBAO_URL = 'https://www.doubao.com/chat/';

let browser, page, launched = false;

// ─── 人类节奏工具 ──────────────────────────────
const jitter = (min, max) => min + Math.random() * (max - min);

async function humanPause(min, max) {
    await new Promise(r => setTimeout(r, jitter(min, max)));
}

async function humanType(text) {
    for (const ch of text) {
        await page.keyboard.type(ch, { delay: jitter(8, 28) });
    }
}

// ─── 页面交互 ──────────────────────────────────

async function ensurePage() {
    if (launched && page && !page.isClosed()) return;
    browser = await chromium.launchPersistentContext(PROFILE, {
        headless: false,
        viewport: { width: 1280, height: 900 },
        locale: 'zh-CN',
        args: ['--disable-blink-features=AutomationControlled'],
    });
    const pages = browser.pages();
    // 诊断：打印恢复的所有页面
    console.log('[DBG] restored pages:', pages.map(p => p.url()).join(' | '));
    page = pages.find(p => p.url().includes('doubao.com')) || pages[0] || await browser.newPage();
    launched = true;
}

async function gotoChat() {
    await ensurePage();
    // 已在豆包对话页就直接用（会话保持），否则打开
    if (!page.url().includes('doubao.com')) {
        await page.goto(DOUBAO_URL, { waitUntil: 'commit', timeout: 30000 });
        await humanPause(3000, 6000);
    }
}

function extractAnswer() {
    // 不挑气泡——直接抓整个消息区全文（Python 端做 diff）
    return page.evaluate(() => {
        const lists = document.querySelectorAll('[class*="message-list"]');
        let text = '';
        lists.forEach(l => { text += (l.textContent || '') + '\n'; });
        return text;
    });
}

// ─── 命令处理 ──────────────────────────────────

async function handle(msg) {
    const { action } = msg;
    try {
        switch (action) {
            case 'ask': {
                await gotoChat();
                const images = msg.images || [];

                // 记录发送前的全文基线
                const baseline = await extractAnswer();

                // 上传图片（如果有）
                if (images.length > 0) {
                    const fileInput = page.locator('input[type="file"]').first();
                    if (await fileInput.count() > 0) {
                        await fileInput.setInputFiles(images);
                        await humanPause(4000, 7000);
                    }
                }

                // 点击输入框
                const inputSel = '.tiptap.ProseMirror, [contenteditable="true"]';
                await page.click(inputSel, { timeout: 10000 });
                await humanPause(300, 900);

                // 人类节奏打字
                await humanType(msg.question || '');
                await humanPause(400, 1200);
                await page.keyboard.press('Enter');

                // 等回答：全文 diff——比基线多出的内容就是新回复
                const deadline = Date.now() + (msg.timeout || 60000);
                let answer = '';
                while (Date.now() < deadline) {
                    await humanPause(2000, 3000);
                    const now = await extractAnswer();
                    if (now.length <= baseline.length) continue;
                    // 新增部分 = 全文去掉基线后剩下的尾巴
                    const added = now.slice(baseline.length).trim();
                    // 过滤：只含用户问题本身（无新增回复）→ 继续等
                    if (!added || added.replace(/\s+/g, '') === (msg.question || '').replace(/\s+/g, '')) continue;
                    answer = added;
                    break;
                }
                return { ok: true, answer: answer || '(豆包未在时限内回答)' };
            }
            case 'new_chat': {
                await gotoChat();
                // 豆包网页「新建对话」按钮——常见选择器，失败不阻塞
                const newBtn = page.locator('[class*="new"],[class*="New"],button:has-text("新对话"),a:has-text("新建")');
                if (await newBtn.count() > 0) {
                    await newBtn.first().click();
                    await humanPause(2000, 4000);
                    return { ok: true, note: '已尝试开新对话' };
                }
                return { ok: true, note: '未找到新建按钮（可能没有）' };
            }
            case 'status': {
                await ensurePage();
                return {
                    ok: true,
                    launched,
                    url: page ? page.url() : '',
                    title: page ? await page.title() : '',
                };
            }
            case 'exit': {
                if (browser) await browser.close();
                return { ok: true, exit: true };
            }
            default:
                return { ok: false, error: `Unknown action: ${action}` };
        }
    } catch (e) {
        return { ok: false, error: e.message };
    }
}

// ─── 主循环 ────────────────────────────────────

async function main() {
    process.stdout.write(JSON.stringify({ ready: true, worker: 'doubao' }) + '\n');
    let buf = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', async (chunk) => {
        buf += chunk;
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
            if (!line.trim()) continue;
            try {
                const msg = JSON.parse(line);
                const result = await handle(msg);
                process.stdout.write(JSON.stringify(result) + '\n');
                if (msg.action === 'exit' || result.exit) process.exit(0);
            } catch (e) {
                process.stdout.write(JSON.stringify({ ok: false, error: e.message }) + '\n');
            }
        }
    });
    process.stdin.on('end', async () => {
        if (browser) await browser.close();
        process.exit(0);
    });
}

main().catch(e => {
    process.stderr.write(JSON.stringify({ error: e.message }) + '\n');
    process.exit(1);
});
