//
// tests/js/format-helpers.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

globalThis.document = {
    cookie: "",
    documentElement: { dataset: {} },
    querySelector() {
        return null;
    },
};

const utilsSource = await readFile(
    new URL("../../app/static/js/utils.js", import.meta.url),
    "utf8",
);
const utilsModuleUrl = `data:text/javascript;base64,${Buffer.from(utilsSource).toString("base64")}`;
const { formatDuration, formatLalalMinutes, EMPTY_VALUE } = await import(utilsModuleUrl);

// Kept in step with format_clock() in app/utils/duration.py, which renders the
// same job durations server-side on the job page.
test("renders hours only when the duration has any", () => {
    assert.equal(formatDuration(15690), "4:21:30");
    assert.equal(formatDuration(213.4), "3:33");
    assert.equal(formatDuration(59), "0:59");
});

test("pads the minutes once an hour is shown", () => {
    assert.equal(formatDuration(3605), "1:00:05");
});

test("unknown durations render as the shared placeholder", () => {
    for (const value of [null, undefined, -1, Number.NaN, Number.POSITIVE_INFINITY, "abc"]) {
        assert.equal(formatDuration(value), EMPTY_VALUE);
    }
});

// The Lalal.ai balance is reported in fractional minutes and shown in the
// Settings tile the same way Lalal.ai's own account page shows it.
test("renders a Lalal.ai balance as minutes and seconds", () => {
    assert.equal(formatLalalMinutes(261.5), "261m 30s");
    assert.equal(formatLalalMinutes(0), "0m 00s");
    assert.equal(formatLalalMinutes(1), "1m 00s");
    assert.equal(formatLalalMinutes("12.25"), "12m 15s");
});

test("an unknown balance formats to nothing rather than to zero", () => {
    for (const value of [null, undefined, "", -1, Number.NaN, "abc"]) {
        assert.equal(formatLalalMinutes(value), "");
    }
});
