//
// tools/ui-lint/lib/browser-utils.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import fs from 'node:fs';
import path from 'node:path';

import pixelmatch from 'pixelmatch';
import { PNG } from 'pngjs';

export function sanitize(name) {
    return name.replace(/[^a-z0-9-_]+/g, '_').toLowerCase();
}

export function ensureDir(dirPath) {
    fs.mkdirSync(dirPath, { recursive: true });
}

export async function disableMotion(page, motionResetCss, viewName = 'unknown') {
    await page.addStyleTag({
        content: `
        ${motionResetCss}

        /* Kill ALL sources of visual drift */
        *, *::before, *::after {
            animation: none !important;
            transition: none !important;
            caret-color: transparent !important;
        }

        html {
            scroll-behavior: auto !important;
        }

        body {
            text-rendering: geometricPrecision !important;
        }
        `,
    }).catch((err) => console.warn(`[${viewName}] Failed to inject motion reset CSS: ${err.message}`));

    await page.emulateMedia({ reducedMotion: 'reduce' }).catch(() => {});
}

export async function login(page, { baseUrl, username, password, motionResetCss }) {
    await page.goto(`${baseUrl}/login`, { waitUntil: 'networkidle', timeout: 10000 });
    await disableMotion(page, motionResetCss, 'login');

    if (!page.url().includes('/login')) {
        return;
    }

    const loginForm = page.locator('#login-form');
    try {
        await loginForm.waitFor({ state: 'visible', timeout: 5000 });
    } catch (error) {
        throw new Error(`Login form not available at ${baseUrl}/login (landed on ${page.url()}): ${error.message}`);
    }

    await page.fill('#username', username);
    await page.fill('#password', password);
    await Promise.all([
        page.waitForURL((url) => !url.toString().includes('/login'), { timeout: 10000 }),
        page.click('#submit-btn'),
    ]);

    const errorText = await page.locator('.alert-danger, .login-error, .error-message').evaluateAll((elements) => {
        const isVisible = (el) => {
            if (!el || !el.isConnected) return false;
            if (el.closest('.d-none, [hidden], [aria-hidden="true"]')) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };

        const visibleError = elements.find((el) => isVisible(el));
        return visibleError ? (visibleError.textContent || '').trim() : '';
    });
    if (errorText) {
        throw new Error(`Login failed: ${errorText}`);
    }

    await disableMotion(page, motionResetCss, 'login');
}

export function collectConsoleAndNetwork(page) {
    const consoleEntries = [];
    const pageErrors = [];
    const requestFailures = [];
    const badResponses = [];
    const requests = [];

    const onConsole = (msg) => {
        if (['error', 'warning'].includes(msg.type())) {
            consoleEntries.push({ type: msg.type(), text: msg.text() });
        }
    };
    const onPageError = (err) => pageErrors.push(String(err?.message || err));
    const onRequest = (req) => {
        const url = req.url();

        // Ignore external font CDN noise — not relevant to same-origin lint checks.
        if (url.includes('fonts.googleapis.com') || url.includes('fonts.gstatic.com')) {
            return;
        }

        requests.push({
            url,
            method: req.method(),
            resourceType: req.resourceType(),
        });
    };
    const onRequestFailed = (req) => requestFailures.push({ url: req.url(), error: req.failure()?.errorText || 'unknown' });
    const onResponse = (res) => {
        if (res.status() >= 400) {
            badResponses.push({ url: res.url(), status: res.status() });
        }
    };

    page.on('console', onConsole);
    page.on('pageerror', onPageError);
    page.on('request', onRequest);
    page.on('requestfailed', onRequestFailed);
    page.on('response', onResponse);

    return () => {
        page.off('console', onConsole);
        page.off('pageerror', onPageError);
        page.off('request', onRequest);
        page.off('requestfailed', onRequestFailed);
        page.off('response', onResponse);
        return { consoleEntries, pageErrors, requestFailures, badResponses, requests };
    };
}

export async function captureStablePair(page, {
    motionResetCss,
    name,
    screenshotDir,
    screenshotSettleMs,
}) {
    await disableMotion(page, motionResetCss, name);

    // Await font loading before screenshots to eliminate font-swap drift.
    await page.evaluate(async () => {
        if (document.fonts?.ready) {
            await document.fonts.ready;
        }
    }).catch(() => {});

    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(screenshotSettleMs);
    const safeName = sanitize(name);
    const shotA = path.join(screenshotDir, `${safeName}-a.png`);
    const shotB = path.join(screenshotDir, `${safeName}-b.png`);
    await page.screenshot({ path: shotA, fullPage: true, animations: 'disabled' });

    // Force layout flush so second shot is taken after a full reflow cycle.
    await page.evaluate(() => document.body.offsetHeight).catch(() => {});

    await page.waitForTimeout(screenshotSettleMs);
    await page.screenshot({ path: shotB, fullPage: true, animations: 'disabled' });
    return { shotA, shotB };
}

export function diffScreenshots({ name, shotA, shotB, screenshotDir }) {
    const img1 = PNG.sync.read(fs.readFileSync(shotA));
    const img2 = PNG.sync.read(fs.readFileSync(shotB));
    const sizeMismatch = img1.width !== img2.width || img1.height !== img2.height;
    const width = Math.min(img1.width, img2.width);
    const height = Math.min(img1.height, img2.height);
    const pngA = new PNG({ width, height });
    const pngB = new PNG({ width, height });
    PNG.bitblt(img1, pngA, 0, 0, width, height, 0, 0);
    PNG.bitblt(img2, pngB, 0, 0, width, height, 0, 0);
    const diff = new PNG({ width, height });
    const mismatchedPixels = pixelmatch(
        pngA.data,
        pngB.data,
        diff.data,
        width,
        height,
        {
            threshold: 0.2,   // less sensitive to sub-pixel differences
            includeAA: false, // ignore anti-aliasing noise
        },
    );
    const diffPath = path.join(screenshotDir, `${sanitize(name)}-diff.png`);
    fs.writeFileSync(diffPath, PNG.sync.write(diff));
    const totalPixels = width * height;
    return {
        mismatchedPixels,
        totalPixels,
        ratio: totalPixels > 0 ? mismatchedPixels / totalPixels : 0,
        sizeMismatch,
        dimensions: sizeMismatch ? { img1: { width: img1.width, height: img1.height }, img2: { width: img2.width, height: img2.height } } : null,
        diffPath,
    };
}
