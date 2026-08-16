#!/usr/bin/env node
/**
 * browser_worker.js — agent-eye Playwright worker (Node.js)
 *
 * 通过 stdin 接收 JSON 命令，通过 stdout 返回 JSON 结果。
 * 不依赖 Python greenlet，用 Node.js 原生 async/await。
 *
 * 命令格式: {"action": "navigate|screenshot|extract|click|scroll|wait", ...}
 * 退出: {"action": "exit"}
 *
 * 用法: node browser_worker.js
 *       python 通过 subprocess stdin/stdout 通信
 */

const { chromium } = require('playwright');

let browser, context, page;

async function handle(msg) {
    const { action } = msg;
    try {
        switch (action) {
            case 'navigate': {
                const { url, timeout = 20000 } = msg;
                if (!page) throw new Error('Browser not started');
                await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
                await page.waitForTimeout(2000);
                const title = await page.title();
                return { ok: true, title };
            }
            case 'screenshot': {
                const { path = '.screenshots/latest.png' } = msg;
                if (!page) throw new Error('Browser not started');
                const fs = require('fs');
                const dir = path.replace(/[/\\][^/\\]*$/, '');
                if (dir) fs.mkdirSync(dir, { recursive: true });
                await page.screenshot({ path, fullPage: false });
                const url = page.url();
                const title = await page.title();
                return { ok: true, path, url, title };
            }
            case 'extract': {
                if (!page) throw new Error('Browser not started');
                const url = page.url();
                const title = await page.title();
                const meta = await page.evaluate(() => {
                    const m = document.querySelector('meta[name="description"]');
                    return m ? (m.getAttribute('content') || '') : '';
                });
                const body = await page.evaluate(() => {
                    const sel = document.querySelector(
                        'article,[role="main"],main,.content,#content,.post,.entry');
                    const root = (sel && sel.textContent.trim().length > 80) ? sel : document.body;
                    const els = root.querySelectorAll('p,li,h2,h3,h4,pre code');
                    return Array.from(els).map(e => e.textContent.trim())
                        .filter(t => t.length > 10).join('\n').slice(0, 2000);
                });
                // raw_html 模式：返回完整页面 HTML（Sci-Hub 等需要解析 iframe/pdf 链接）
                let html = '';
                if (msg.raw_html) {
                    html = await page.content();
                }
                return { ok: true, url, title, meta, body, html };
            }
            case 'click': {
                const { selector } = msg;
                if (!page) throw new Error('Browser not started');
                await page.click(selector, { timeout: 10000 });
                await page.waitForTimeout(1000);
                return { ok: true, selector };
            }
            case 'scroll': {
                const { amount = 500 } = msg;
                if (!page) throw new Error('Browser not started');
                await page.evaluate((a) => window.scrollBy(0, a), amount);
                await page.waitForTimeout(500);
                return { ok: true, amount };
            }
            case 'wait': {
                const { ms = 2000 } = msg;
                await new Promise(r => setTimeout(r, ms));
                return { ok: true, ms };
            }
            case 'exit': {
                if (context) await context.close();
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

async function main() {
    // 有头模式：--headed 或环境变量 HEADED=1（登录墙网站需要手动登录一次）
    const headed = process.argv.includes('--headed') || process.env.HEADED === '1';
    // 启动浏览器（持久化 profile：~/ego_profile，登录态自动保存复用）
    browser = await chromium.launchPersistentContext(
        process.env.USERPROFILE + '/ego_profile',
        {
            headless: !headed,
            viewport: { width: 1280, height: 900 },
            locale: 'zh-CN',
        }
    );
    context = browser;
    const pages = context.pages();
    page = pages.length > 0 ? pages[0] : await context.newPage();

    // 通知 ready
    process.stdout.write(JSON.stringify({ ready: true }) + '\n');

    // 读取命令循环
    let buf = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', async (chunk) => {
        buf += chunk;
        const lines = buf.split('\n');
        buf = lines.pop(); // 不完整的最后一行保留

        for (const line of lines) {
            if (!line.trim()) continue;
            try {
                const msg = JSON.parse(line);
                const result = await handle(msg);
                process.stdout.write(JSON.stringify(result) + '\n');
                if (msg.action === 'exit' || result.exit) {
                    process.exit(0);
                }
            } catch (e) {
                process.stdout.write(JSON.stringify({ ok: false, error: e.message }) + '\n');
            }
        }
    });

    process.stdin.on('end', async () => {
        if (context) await context.close();
        process.exit(0);
    });
}

main().catch(e => {
    process.stderr.write(JSON.stringify({ error: e.message }) + '\n');
    process.exit(1);
});
