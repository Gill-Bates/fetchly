#!/usr/bin/env node
//
// tools/ui-lint/check-source-contracts.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// The source-contract half of the UI audit as a plain lint step.
//
// Runs the checks in lib/source-contracts.mjs and nothing else: no browser, no
// server, no database, no login. That is the whole point - these rules are
// regexes over app/static/js and app/templates, so they are deterministic and
// cheap enough to gate every push, while the visual and layout half of the
// audit stays a manual run (see setup.conf) because it is timing-sensitive.
//
// Deliberately dependency-free, so `node tools/ui-lint/check-source-contracts.mjs`
// works from a bare checkout with only the root `npm ci` in place. Importing
// run-ui-lint.mjs here would pull in playwright and undo that.
//
// Exit code 0 when every contract holds, 1 otherwise. Under GitHub Actions it
// also emits ::error annotations so the failures land on the files.
//
import {
    collectSourceContractMetrics,
    SOURCE_CONTRACT_KEYS,
    sourceContractViolations,
} from './lib/source-contracts.mjs';

async function run() {
    const metrics = await collectSourceContractMetrics();
    const violations = sourceContractViolations(metrics);
    const checked = SOURCE_CONTRACT_KEYS.length;

    if (violations.length === 0) {
        console.log(`source contracts: ${checked} checks, all satisfied`);
        return 0;
    }

    const onGitHub = process.env.GITHUB_ACTIONS === 'true';
    console.error(`source contracts: ${violations.length} of ${checked} checks failed`);
    for (const violation of violations) {
        console.error(`  FAIL ${violation.key}: ${violation.message}`);
        if (violation.files.length) {
            console.error(`       source: ${violation.files.join(', ')}`);
        }
        if (!onGitHub) continue;
        // One annotation per file: the checks match across whole files, so
        // there is no line number to point at.
        for (const file of violation.files) {
            console.log(`::error file=${file},title=${violation.key}::${violation.message}`);
        }
    }
    console.error('');
    console.error('These are source-level invariants, not style: read the rationale in');
    console.error('tools/ui-lint/lib/source-contracts.mjs before relaxing one.');
    return 1;
}

try {
    process.exitCode = await run();
} catch (error) {
    console.error(`source contracts: check could not run: ${error?.message || error}`);
    process.exitCode = 1;
}
