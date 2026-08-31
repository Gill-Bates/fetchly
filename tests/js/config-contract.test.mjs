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

async function importConfigWithDuration(duration) {
    globalThis.document = {
        documentElement: {
            dataset: { lalalMaxDurationSeconds: String(duration) },
        },
    };
    const moduleUrl = `data:text/javascript;base64,${Buffer.from(configSource).toString("base64")}#${duration}`;
    return import(moduleUrl);
}

test("takes the Lalal duration contract from server-rendered bootstrap data", async () => {
    const config = await importConfigWithDuration(420);
    assert.equal(config.LALAL_MAX_DURATION_SECONDS, 420);
    assert.equal(config.LALAL_MAX_DURATION_MINUTES, 7);
});
