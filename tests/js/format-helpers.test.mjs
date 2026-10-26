//
// tests/js/format-helpers.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

globalThis.document = {
    cookie: "",
    documentElement: { dataset: {} },
    querySelector() {
        return null;
    },
};

const { formatDateTime, formatDuration, formatLalalMinutes, formatNumber, humanSize, localizeValues, EMPTY_VALUE } = await import("../../app/static/js/utils.js");

// Kept in step with format_clock() in app/utils/duration.py
// (tests/test_job_duration.py::FormatClockTests holds the matching table).
// Booleans are excluded here: the server rejects them before storage and no
// API field ever carries one, so JS coercing True to 1 is not a real gap.
test("renders hours only when the duration has any", () => {
    assert.equal(formatDuration(15690), "4:21:30");
    assert.equal(formatDuration(213.4), "3:33");
    assert.equal(formatDuration(59), "0:59");
});

test("pads the minutes once an hour is shown", () => {
    assert.equal(formatDuration(3605), "1:00:05");
});

test("unknown durations render as the shared placeholder", () => {
    for (const value of ["", null, undefined, -1, Number.NaN, Number.POSITIVE_INFINITY, "abc"]) {
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

// The server sends UTC instants and plain numbers; every visible separator,
// date order and clock comes from the browser's locale and time zone.
test("numbers follow the locale's decimal and grouping separators", () => {
    assert.equal(formatNumber(1234.5, { locale: "de-DE", minimumFractionDigits: 2 }), "1.234,50");
    assert.equal(formatNumber(1234.5, { locale: "en-US", minimumFractionDigits: 2 }), "1,234.50");
    assert.equal(humanSize(1536, "de-DE"), "1,5 KiB");
    assert.equal(humanSize(1536, "en-US"), "1.5 KiB");
    for (const value of [null, undefined, "", "abc", Number.NaN]) {
        assert.equal(formatNumber(value), EMPTY_VALUE);
    }
});

test("UTC timestamps render in the requested time zone, SQLite text read as UTC", () => {
    const options = { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "America/New_York" };
    assert.equal(formatDateTime("2026-01-02 15:04:05", { locale: "en-US", ...options }), "10:04");
    assert.equal(formatDateTime("2026-01-02T15:04:05Z", { locale: "de-DE", ...options }), "10:04");
    const date = { year: "numeric", month: "2-digit", day: "2-digit", timeZone: "Asia/Tokyo" };
    assert.equal(formatDateTime("2026-01-02T20:00:00Z", { locale: "de-DE", ...date }), "03.01.2026");
    assert.equal(formatDateTime("2026-01-02T20:00:00Z", { locale: "en-US", ...date }), "01/03/2026");
    assert.equal(formatDateTime("not a date"), EMPTY_VALUE);
});

test("localizeValues rewrites server-rendered times and numbers in place", () => {
    const time = {
        textContent: "2026-01-02 15:04 UTC",
        title: "",
        getAttribute: (name) => (name === "datetime" ? "2026-01-02T15:04:05Z" : null),
    };
    const confidence = { textContent: "0.50", dataset: { localizeNumber: "0.5", fractionDigits: "2" } };
    const broken = { textContent: "fallback", dataset: { localizeNumber: "n/a" } };
    const root = {
        querySelectorAll: (selector) => (selector.startsWith("time") ? [time] : [confidence, broken]),
    };
    localizeValues(root, { locale: "de-DE", timeZone: "Europe/Berlin" });
    assert.match(time.textContent, /16:04/);
    assert.equal(time.title, "2026-01-02T15:04:05Z");
    assert.equal(confidence.textContent, "0,50");
    assert.equal(broken.textContent, "fallback");
    assert.doesNotThrow(() => localizeValues({}));
});
