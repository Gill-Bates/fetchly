//
// tests/js/csrf-token.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const nodes = new Map();
globalThis.document = {
    cookie: "",
    documentElement: { dataset: { csrfCookieName: "configured_csrf" } },
    querySelector(selector) {
        return nodes.get(selector) ?? null;
    },
};

const utilsSource = await readFile(
    new URL("../../app/static/js/utils.js", import.meta.url),
    "utf8",
);
const utilsModuleUrl = `data:text/javascript;base64,${Buffer.from(utilsSource).toString("base64")}`;
const { getCsrfToken } = await import(utilsModuleUrl);

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
