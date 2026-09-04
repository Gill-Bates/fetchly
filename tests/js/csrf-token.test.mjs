//
// tests/js/csrf-token.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

const nodes = new Map();
globalThis.document = {
    cookie: "",
    documentElement: { dataset: { csrfCookieName: "configured_csrf" } },
    querySelector(selector) {
        return nodes.get(selector) ?? null;
    },
};

const { getCsrfToken } = await import("../../app/static/js/utils.js");

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
