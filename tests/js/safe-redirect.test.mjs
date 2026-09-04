//
// tests/js/safe-redirect.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

globalThis.window = {
    location: {
        origin: "https://fetchly.example",
    },
};

const { isSafeSameOriginRedirect, isSafeRedirect } = await import("../../app/static/js/utils.js");

test("accepts relative and absolute redirects on the current origin", () => {
    for (const value of [
        "/",
        "/settings?tab=security#password",
        "jobs/123",
        "https://fetchly.example/settings",
        "/%2f%2fevil.example",
    ]) {
        assert.equal(isSafeSameOriginRedirect(value), true, value);
    }
});

test("rejects empty, malformed, active-scheme, and cross-origin redirects", () => {
    for (const value of [
        "",
        null,
        undefined,
        "//evil.example/path",
        "///evil.example/path",
        "\\\\evil.example/path",
        "javascript:alert(1)",
        "https://evil.example/",
        "https://fetchly.example.evil.example/",
        "https://fetchly.example@evil.example/",
        "http://[",
    ]) {
        assert.equal(isSafeSameOriginRedirect(value), false, String(value));
    }
});

test("isSafeRedirect accepts only same-origin download paths", () => {
    for (const value of [
        "/download/123",
        "/api/lalal/download/123",
        "https://fetchly.example/download/123",
    ]) {
        assert.equal(isSafeRedirect(value), true, value);
    }
});

test("isSafeRedirect rejects same-origin non-download paths and unsafe/cross-origin values", () => {
    for (const value of [
        "",
        null,
        undefined,
        "/",
        "/settings",
        "/downloads/123",
        "//evil.example/download/123",
        "https://evil.example/download/123",
        "javascript:alert(1)",
        "http://[",
        // pathname normalization/case must not widen the allowed prefix
        "/download/../settings",
        "/DOWNLOAD/123",
        "/api/lalal/download/../../settings",
    ]) {
        assert.equal(isSafeRedirect(value), false, String(value));
    }
});
