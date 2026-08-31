//
// tests/js/safe-redirect.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

globalThis.window = {
    location: {
        origin: "https://fetchly.example",
    },
};

const utilsSource = await readFile(
    new URL("../../app/static/js/utils.js", import.meta.url),
    "utf8",
);
const utilsModuleUrl = `data:text/javascript;base64,${Buffer.from(utilsSource).toString("base64")}`;
const { isSafeSameOriginRedirect } = await import(utilsModuleUrl);

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
