//
// tests/js/shared-helpers.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

globalThis.document = {
    cookie: "",
    documentElement: {
        dataset: {
            // api.js pulls in config.js, which refuses to load without these.
            lalalMaxDurationSeconds: "600",
            statusContract: "{\"downloadable\":[\"analysis\",\"analysis_done\",\"done\"],\"cancellable\":[\"downloading\",\"processing\",\"queued\",\"transcoding\"],\"retryable\":[\"cancelled\",\"error\"],\"terminal\":[\"analysis_done\",\"cancelled\",\"done\",\"error\"],\"progress\":[\"downloading\",\"transcoding\"],\"meta\":{\"analysis\":{\"label\":\"Analyzing\",\"color\":\"primary\"},\"analysis_done\":{\"label\":\"Done\",\"color\":\"success\"},\"cancelled\":{\"label\":\"Cancelled\",\"color\":\"secondary\"},\"done\":{\"label\":\"Done\",\"color\":\"success\"},\"downloading\":{\"label\":\"Downloading\",\"color\":\"primary\"},\"error\":{\"label\":\"Error\",\"color\":\"danger\"},\"processing\":{\"label\":\"Preparing\",\"color\":\"primary\"},\"queued\":{\"label\":\"Queued\",\"color\":\"primary\"},\"transcoding\":{\"label\":\"Transcoding\",\"color\":\"primary\"}}}",
        },
    },
    querySelector() {
        return null;
    },
};

const { toErrorMessage } = await import("../../app/static/js/api.js");
const { isSameTrimSelection, releaseLalalRun, resetLalalLabel, parseUtcTimestamp, splitSize, statParts } = await import("../../app/static/js/utils.js");

test("toErrorMessage reads every error shape the backend produces", () => {
    assert.equal(toErrorMessage({ detail: "Bad input" }), "Bad input");
    assert.equal(toErrorMessage({ detail: [{ msg: "field required" }, { msg: "too short" }] }), "field required; too short");
    assert.equal(toErrorMessage({ error: "Trim failed" }), "Trim failed");
    // trim.py wraps its message in a dict, which must not render as [object Object].
    assert.equal(toErrorMessage({ detail: { error: "Invalid trim_id format" } }), "Invalid trim_id format");
    assert.equal(toErrorMessage({}), "");
    assert.equal(toErrorMessage(null), "");
});

test("parseUtcTimestamp reads SQLite timestamps as UTC and passes ISO strings through", () => {
    assert.equal(parseUtcTimestamp("2026-01-02 03:04:05").toISOString(), "2026-01-02T03:04:05.000Z");
    assert.equal(parseUtcTimestamp("2026-01-02T03:04:05+02:00").toISOString(), "2026-01-02T01:04:05.000Z");
    assert.ok(Number.isNaN(parseUtcTimestamp("").getTime()));
    assert.ok(Number.isNaN(parseUtcTimestamp(null).getTime()));
});

test("splitSize separates the number from its unit", () => {
    assert.deepEqual(splitSize(1536, "en-US"), { value: "1.5", unit: "KiB" });
    assert.deepEqual(splitSize(0, "en-US"), { value: "0", unit: "B" });
});

test("statParts formats each dashboard tile", () => {
    assert.deepEqual(statParts("total_jobs", 12.9, "en-US"), { value: "12", unit: "" });
    assert.deepEqual(statParts("total_jobs", -3, "en-US"), { value: "0", unit: "" });
    assert.deepEqual(statParts("total_minutes", 3.14159, "en-US"), { value: "3.1", unit: "" });
    assert.deepEqual(statParts("total_lalal_minutes", "x", "en-US"), { value: "0", unit: "" });
    assert.deepEqual(statParts("total_bytes", 1536, "en-US"), splitSize(1536, "en-US"));
    assert.deepEqual(statParts("unknown", undefined, "en-US"), { value: "0", unit: "" });
});

test("isSameTrimSelection ties a pending result to the selection it was started for", () => {
    const frozen = { session: 3, revision: 7, trimId: "5000_30000" };
    assert.equal(isSameTrimSelection(frozen, { ...frozen }), true);
    // The user picked another range: revision moves and the trim id is discarded.
    assert.equal(isSameTrimSelection(frozen, { session: 3, revision: 8, trimId: null }), false);
    // Same revision, but the trim was re-materialised under another id.
    assert.equal(isSameTrimSelection(frozen, { ...frozen, trimId: "1000_2000" }), false);
    // The modal was closed and reopened.
    assert.equal(isSameTrimSelection(frozen, { ...frozen, session: 4 }), false);
});

const lalalButton = (text, dataset = {}) => ({ textContent: text, dataset: { lalalLabel: "Vocals", ...dataset } });

test("resetLalalLabel clears a finished label but keeps run progress", () => {
    const done = lalalButton("✓ Done", { lalalDone: "1" });
    assert.equal(resetLalalLabel(done), true);
    assert.equal(done.textContent, "Vocals");
    assert.equal(done.dataset.lalalDone, undefined);

    const running = lalalButton("Splitting 40%");
    assert.equal(resetLalalLabel(running), false);
    assert.equal(running.textContent, "Splitting 40%");

    // A reopened modal also drops stale progress text.
    assert.equal(resetLalalLabel(running, { force: true }), true);
    assert.equal(running.textContent, "Vocals");
});

test("resetLalalLabel leaves a never-used button alone", () => {
    const fresh = { textContent: "Vocals", dataset: {} };
    assert.equal(resetLalalLabel(fresh, { force: true }), false);
    assert.equal(fresh.textContent, "Vocals");
    assert.equal(resetLalalLabel(null), false);
});

test("releaseLalalRun only releases a run that still owns the stem", () => {
    const runs = new Map([["vocals", 2]]);
    assert.equal(releaseLalalRun(runs, "vocals", 1), false);
    assert.equal(runs.get("vocals"), 2);
    assert.equal(releaseLalalRun(runs, "vocals", 2), true);
    assert.equal(runs.has("vocals"), false);
});
