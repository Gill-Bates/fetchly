//
// tests/js/ui-lint-network-findings.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The failure mode this file guards against: the SSE stream closing is counted
// as an application network failure, which made the dashboard views pass or fail
// depending on when the teardown happened to land - while a stream that really
// cannot connect must still be reported.
//

import assert from "node:assert/strict";
import test from "node:test";

// Pinned before the import so BASE_ORIGIN is known: isEventStreamCancellation
// only accepts same-origin URLs, and the module reads the base URL at load time.
process.env.UI_LINT_BASE_URL = "http://127.0.0.1:8000";
const BASE = process.env.UI_LINT_BASE_URL;

const { isEventStreamCancellation } = await import("../../tools/ui-lint/run-ui-lint.mjs");

test("each engine's wording for a cancelled stream is recognized", () => {
    for (const error of [
        "net::ERR_ABORTED",                 // chromium
        "Load request cancelled",           // webkit
        "NS_BINDING_ABORTED",               // firefox
        "The operation was aborted",
    ]) {
        assert.equal(
            isEventStreamCancellation({ url: `${BASE}/events`, error }),
            true,
            `not recognized: ${error}`,
        );
    }
});

test("a stream that genuinely failed is still reported", () => {
    for (const error of [
        "net::ERR_CONNECTION_REFUSED",
        "net::ERR_CONNECTION_RESET",
        "net::ERR_NAME_NOT_RESOLVED",
        "socket hang up",
    ]) {
        assert.equal(
            isEventStreamCancellation({ url: `${BASE}/events`, error }),
            false,
            `wrongly suppressed: ${error}`,
        );
    }
});

test("only the event stream endpoint is exempt", () => {
    // A cancelled API call is not the same thing: those are finite requests, so
    // a cancellation there is a real finding.
    for (const path of ["/api/jobs", "/api/settings", "/", "/eventsomething", "/events/extra"]) {
        assert.equal(
            isEventStreamCancellation({ url: `${BASE}${path}`, error: "net::ERR_ABORTED" }),
            false,
            `wrongly suppressed: ${path}`,
        );
    }
});

test("a query string or fragment does not defeat the path match", () => {
    assert.equal(
        isEventStreamCancellation({ url: `${BASE}/events?since=12`, error: "net::ERR_ABORTED" }),
        true,
    );
});

test("a cancelled stream on another origin is not exempt", () => {
    // Same path, different host: nothing about a third party's stream is known.
    assert.equal(
        isEventStreamCancellation({ url: "http://example.invalid/events", error: "net::ERR_ABORTED" }),
        false,
    );
});

test("malformed input is refused rather than thrown on", () => {
    for (const entry of [{}, { url: "" }, { url: "not a url", error: "aborted" }, { url: `${BASE}/events` }]) {
        assert.equal(isEventStreamCancellation(entry), false);
    }
    assert.equal(isEventStreamCancellation(undefined), false);
});
