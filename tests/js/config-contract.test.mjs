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

// A valid status contract, shaped exactly as status_contract() in
// app/utils/template_filters.py serializes it. Kept in sync with
// tests/test_status_mapping_parity.py, which checks the server side.
const VALID_CONTRACT = {
    downloadable: ["analysis", "analysis_done", "done"],
    cancellable: ["downloading", "processing", "queued", "transcoding"],
    retryable: ["cancelled", "error"],
    terminal: ["analysis_done", "cancelled", "done", "error"],
    progress: ["downloading", "transcoding"],
    meta: {
        analysis: { label: "Analyzing", color: "primary" },
        analysis_done: { label: "Done", color: "success" },
        cancelled: { label: "Cancelled", color: "secondary" },
        done: { label: "Done", color: "success" },
        downloading: { label: "Downloading", color: "primary" },
        error: { label: "Error", color: "danger" },
        processing: { label: "Preparing", color: "primary" },
        queued: { label: "Queued", color: "primary" },
        transcoding: { label: "Transcoding", color: "primary" },
    },
};

function importConfig({ duration = 600, contract = VALID_CONTRACT } = {}) {
    const dataset = {};
    if (duration !== ABSENT) {
        dataset.lalalMaxDurationSeconds = String(duration);
    }
    if (contract !== ABSENT) {
        dataset.statusContract = typeof contract === "string" ? contract : JSON.stringify(contract);
    }
    globalThis.document = { documentElement: { dataset } };
    const cacheBuster = `${String(duration)}#${Math.random()}`;
    const moduleUrl = `data:text/javascript;base64,${Buffer.from(configSource).toString("base64")}#${cacheBuster}`;
    return import(moduleUrl);
}

test("takes the Lalal duration contract from server-rendered bootstrap data", async () => {
    const config = await importConfig({ duration: 420 });
    assert.equal(config.LALAL_MAX_DURATION_SECONDS, 420);
    assert.equal(config.LALAL_MAX_DURATION_MINUTES, 7);
});

test("refuses to load without a usable duration contract", async () => {
    // A missing or unparseable bootstrap attribute must fail loudly at
    // import time - the module graph dies with it - rather than exporting a
    // NaN that would silently read as "0 minutes left" in the UI.
    for (const broken of ["", "0", "-5", "abc", ABSENT]) {
        await assert.rejects(
            importConfig({ duration: broken }),
            /Invalid Lalal\.ai duration limit/,
            String(broken),
        );
    }
});

test("takes the status vocabulary from the server-rendered contract", async () => {
    // The Python counterpart is tests/test_status_mapping_parity.py; both read
    // from the same source of truth in app/db.py, now surfaced through the
    // page bootstrap payload rather than hardcoded here.
    const config = await importConfig();

    assert.deepEqual([...config.DOWNLOADABLE_STATUSES].sort(), ["analysis", "analysis_done", "done"]);
    assert.deepEqual(
        [...config.CANCELLABLE_STATUSES].sort(),
        ["downloading", "processing", "queued", "transcoding"],
    );
    assert.deepEqual([...config.RETRYABLE_STATUSES].sort(), ["cancelled", "error"]);
    assert.deepEqual(
        [...config.TERMINAL_STATUSES].sort(),
        ["analysis_done", "cancelled", "done", "error"],
    );
    assert.deepEqual([...config.PROGRESS_STATUSES].sort(), ["downloading", "transcoding"]);

    // A job cannot be offered both a cancel and a retry action.
    for (const status of config.RETRYABLE_STATUSES) {
        assert.equal(config.CANCELLABLE_STATUSES.has(status), false, status);
    }
    // Nothing cancellable is terminal - the two sets partition the lifecycle.
    for (const status of config.CANCELLABLE_STATUSES) {
        assert.equal(config.TERMINAL_STATUSES.has(status), false, status);
    }
});

test("exposes the status display metadata from the contract", async () => {
    const config = await importConfig();

    assert.deepEqual(config.STATUS_META.done, { label: "Done", color: "success" });
    assert.deepEqual(config.STATUS_META.error, { label: "Error", color: "danger" });
    assert.deepEqual(config.STATUS_META.processing, { label: "Preparing", color: "primary" });
    // Serving a value the server did not send would re-introduce a client copy.
    assert.deepEqual(Object.keys(config.STATUS_META).sort(), Object.keys(VALID_CONTRACT.meta).sort());
});

test("reflects the server contract rather than a hardcoded copy", async () => {
    // A contract the server could emit for a different vocabulary must flow
    // through unchanged; a stale client copy would ignore it.
    const altered = {
        ...VALID_CONTRACT,
        downloadable: ["done"],
        meta: { ...VALID_CONTRACT.meta, done: { label: "Finished", color: "info" } },
    };
    const config = await importConfig({ contract: altered });

    assert.deepEqual([...config.DOWNLOADABLE_STATUSES], ["done"]);
    assert.deepEqual(config.STATUS_META.done, { label: "Finished", color: "info" });
});

test("refuses to load without a usable status contract", async () => {
    for (const broken of ["", "not json", "null", "[]", ABSENT]) {
        await assert.rejects(
            importConfig({ contract: broken }),
            /job-status contract/,
            String(broken),
        );
    }
});

test("refuses to load when a status group is missing", async () => {
    const missingTerminal = { ...VALID_CONTRACT };
    delete missingTerminal.terminal;
    await assert.rejects(
        importConfig({ contract: missingTerminal }),
        /"terminal" group/,
    );
});
