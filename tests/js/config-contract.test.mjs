//
// tests/js/config-contract.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const configSource = await readFile(
    new URL("../../app/static/js/config.js", import.meta.url),
    "utf8",
);

// `undefined` means the attribute is absent, not the string "undefined":
// base.html either renders data-lalal-max-duration-seconds or it does not, and
// String(undefined) would test an unparseable value a second time instead of
// the missing one.
const ABSENT = Symbol("absent");

async function importConfigWithDuration(duration) {
    const dataset = {};
    if (duration !== ABSENT) {
        dataset.lalalMaxDurationSeconds = String(duration);
    }
    globalThis.document = { documentElement: { dataset } };
    const moduleUrl = `data:text/javascript;base64,${Buffer.from(configSource).toString("base64")}#${String(duration)}`;
    return import(moduleUrl);
}

test("takes the Lalal duration contract from server-rendered bootstrap data", async () => {
    const config = await importConfigWithDuration(420);
    assert.equal(config.LALAL_MAX_DURATION_SECONDS, 420);
    assert.equal(config.LALAL_MAX_DURATION_MINUTES, 7);
});

test("refuses to load without a usable duration contract", async () => {
    // A missing or unparseable bootstrap attribute must fail loudly at
    // import time - the module graph dies with it - rather than exporting a
    // NaN that would silently read as "0 minutes left" in the UI.
    for (const broken of ["", "0", "-5", "abc", ABSENT]) {
        await assert.rejects(
            importConfigWithDuration(broken),
            /Invalid Lalal\.ai duration limit/,
            String(broken),
        );
    }
});

test("the status vocabulary mirrors the backend's job states", async () => {
    // The Python counterpart is tests/test_status_mapping_parity.py; both read
    // from the same source of truth in app/db.py. A status added there and
    // forgotten here silently disables a button in the UI.
    const config = await importConfigWithDuration(600);

    assert.deepEqual(
        [...config.DOWNLOADABLE_STATUSES].sort(),
        ["analysis", "analysis_done", "done"],
    );
    assert.deepEqual(
        [...config.CANCELLABLE_STATUSES].sort(),
        ["downloading", "processing", "queued", "transcoding"],
    );
    assert.deepEqual([...config.RETRYABLE_STATUSES].sort(), ["cancelled", "error"]);
    assert.deepEqual(
        [...config.TERMINAL_STATUSES].sort(),
        ["analysis_done", "cancelled", "done", "error"],
    );

    // A job cannot be offered both a cancel and a retry action.
    for (const status of config.RETRYABLE_STATUSES) {
        assert.equal(config.CANCELLABLE_STATUSES.has(status), false, status);
    }
    // Nothing cancellable is terminal - the two sets partition the lifecycle.
    for (const status of config.CANCELLABLE_STATUSES) {
        assert.equal(config.TERMINAL_STATUSES.has(status), false, status);
    }
});
