//
// tests/js/csrf-token.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

const nodes = new Map();
globalThis.document = {
    cookie: "",
    documentElement: {
        dataset: {
            csrfCookieName: "configured_csrf",
            // api.js pulls in config.js, which refuses to load without it.
            lalalMaxDurationSeconds: "600",
        },
    },
    querySelector(selector) {
        return nodes.get(selector) ?? null;
    },
};

const { getCsrfToken } = await import("../../app/static/js/utils.js");
const { submitJob } = await import("../../app/static/js/api.js");

test("reads the current cookie using the server-rendered cookie name", () => {
    document.cookie = "unrelated=ignore; configured_csrf=current%20token";
    nodes.set('input[name="csrf_token"]', { value: "stale form token" });
    nodes.set('meta[name="csrf-token"]', { content: "stale meta token" });

    assert.equal(getCsrfToken(), "current token");
});

test("falls back to rendered form and meta tokens when the cookie is absent", () => {
    document.cookie = "";
    nodes.set('input[name="csrf_token"]', { value: "form token" });
    nodes.set('meta[name="csrf-token"]', { content: "meta token" });
    assert.equal(getCsrfToken(), "form token");

    nodes.delete('input[name="csrf_token"]');
    assert.equal(getCsrfToken(), "meta token");

    nodes.clear();
    assert.equal(getCsrfToken(), "");
});

test("a cookie name that could not have come from the server is ignored", () => {
    document.cookie = "configured_csrf=cookie token";
    nodes.set('input[name="csrf_token"]', { value: "form token" });

    // A name carrying a cookie-header delimiter would let one cookie forge
    // another via string matching; readCookie() refuses to look it up.
    document.documentElement.dataset.csrfCookieName = "a=b;c";
    assert.equal(getCsrfToken(), "form token");

    document.documentElement.dataset.csrfCookieName = "";
    assert.equal(getCsrfToken(), "form token");

    document.documentElement.dataset.csrfCookieName = "configured_csrf";
});

test("a cookie name is not matched as a suffix of another cookie", () => {
    document.documentElement.dataset.csrfCookieName = "configured_csrf";
    document.cookie = "x_configured_csrf=wrong; configured_csrf=right";
    nodes.clear();

    assert.equal(getCsrfToken(), "right");
});

test("an unescapable cookie value falls back to the raw text", () => {
    document.documentElement.dataset.csrfCookieName = "configured_csrf";
    document.cookie = "configured_csrf=%E0%A4%A";  // truncated percent-escape
    nodes.clear();

    assert.equal(getCsrfToken(), "%E0%A4%A");
});

// The other half of the client-side contract: reading the token is useless if
// the request does not carry it. CSRFMiddleware expects the X-CSRF-Token
// header (middleware/csrf.py), so the exact spelling is the contract - a
// renamed or dropped header turns every submit into a 403 that no
// getCsrfToken() test above would notice.
test("a submitted job carries the token as the X-CSRF-Token header", async () => {
    const calls = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = (url, options) => {
        calls.push({ url, options });
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ id: "job-1" }) });
    };

    try {
        document.documentElement.dataset.csrfCookieName = "configured_csrf";
        document.cookie = "configured_csrf=header%20token";
        nodes.clear();

        const body = new FormData();
        body.set("url", "https://example.invalid/watch");
        const result = await submitJob(body, getCsrfToken());

        assert.deepEqual(result, { id: "job-1" });
        assert.equal(calls.length, 1);

        const { url, options } = calls[0];
        assert.equal(url, "/api/submit");
        assert.equal(options.method, "POST");
        assert.equal(new Headers(options.headers).get("X-CSRF-Token"), "header token");
        // Same-origin credentials are what makes the double-submit cookie
        // comparison possible at all.
        assert.equal(options.credentials, "same-origin");
        assert.equal(options.body, body);
    } finally {
        globalThis.fetch = realFetch;
    }
});

test("caller-supplied headers do not displace the CSRF header", async () => {
    const calls = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = (url, options) => {
        calls.push({ url, options });
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    };

    try {
        await submitJob(new FormData(), "token", { headers: { "X-Requested-With": "fetchly" } });

        const headers = new Headers(calls[0].options.headers);
        assert.equal(headers.get("X-CSRF-Token"), "token");
        assert.equal(headers.get("X-Requested-With"), "fetchly");
    } finally {
        globalThis.fetch = realFetch;
    }
});
