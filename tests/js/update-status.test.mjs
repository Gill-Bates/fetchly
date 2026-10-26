//
// tests/js/update-status.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { FakeElement, installFakeDom } from "./helpers/fake-dom.mjs";

installFakeDom({ withBootstrap: false });

const { UPDATE_STATE_VIEW, applyUpdateBadge, renderUpdateState, resolveUpdateState } = await import(
    "../../app/static/js/update-status.js"
);

const COMPONENTS = ["ytdlp", "ytdlp_ejs", "js_runtime", "ffmpeg", "wavesurfer"];

function makeCell() {
    const cell = new FakeElement("dd");
    const badge = new FakeElement("a");
    badge.hidden = true;
    badge.href = "#";
    const status = new FakeElement("span");
    status.dataset = { state: "checking" };
    const icon = new FakeElement("span");
    const text = new FakeElement("span");
    status.append(icon, text);
    const parts = new Map([
        ["[data-update-badge]", badge],
        ["[data-update-status]", status],
        ["[data-update-status-icon]", icon],
        ["[data-update-status-text]", text],
    ]);
    cell.querySelector = (selector) => parts.get(selector) ?? null;
    status.querySelector = (selector) => parts.get(selector) ?? null;
    return { cell, badge, status, icon, text };
}

for (const key of COMPONENTS) {
    const label = `component ${key}`;
    const info = (extra) => ({ label: key, latest: "9.9.9", url: `https://example.test/${key}`, ...extra });

    test(`${label}: checking state`, () => {
        const { cell, badge, status, text, icon } = makeCell();
        renderUpdateState(cell, "checking");
        assert.equal(badge.hidden, true);
        assert.equal(status.hidden, false);
        assert.equal(status.dataset.state, "checking");
        assert.equal(text.textContent, "Checking…");
        assert.equal(icon.textContent, "sync");
    });

    test(`${label}: up to date`, () => {
        const { cell, badge, status, text } = makeCell();
        applyUpdateBadge(cell, info({ status: "current", update_available: false }));
        assert.equal(badge.hidden, true);
        assert.equal(status.hidden, false);
        assert.equal(status.dataset.state, "current");
        assert.equal(text.textContent, "Up to date");
    });

    test(`${label}: update available links to the release page`, () => {
        const { cell, badge, status } = makeCell();
        applyUpdateBadge(cell, info({ status: "available", update_available: true }));
        assert.equal(badge.hidden, false);
        assert.equal(badge.href, `https://example.test/${key}`);
        assert.match(badge.getAttribute("aria-label"), /9\.9\.9 is available/);
        assert.equal(status.hidden, true);
    });

    test(`${label}: unknown when the source is unreachable`, () => {
        const { cell, badge, status, text } = makeCell();
        applyUpdateBadge(cell, { label: key, latest: "", status: "unknown", update_available: false });
        assert.equal(badge.hidden, true);
        assert.equal(status.dataset.state, "unknown");
        assert.equal(text.textContent, "Check unavailable");
    });
}

test("a stale available badge is hidden again when the state changes", () => {
    const { cell, badge } = makeCell();
    applyUpdateBadge(cell, { latest: "2", url: "u", status: "available", update_available: true });
    renderUpdateState(cell, "checking");
    assert.equal(badge.hidden, true);
});

test("resolveUpdateState falls back for payloads without a status field", () => {
    assert.equal(resolveUpdateState(undefined), "unknown");
    assert.equal(resolveUpdateState({ latest: "1", update_available: true }), "available");
    assert.equal(resolveUpdateState({ latest: "1", update_available: false }), "current");
    assert.equal(resolveUpdateState({ latest: "", update_available: false }), "unknown");
});

const STATES = [
    ["checking", null],
    ["current", { label: "x", latest: "1", status: "current", url: "u" }],
    ["available", { label: "x", latest: "2", status: "available", update_available: true, url: "u" }],
    ["unknown", { label: "x", latest: "", status: "unknown", url: "u" }],
];

test("every state renders icon and text from the one shared view table", () => {
    for (const [state, info] of STATES) {
        const { cell, badge, status, icon, text } = makeCell();
        renderUpdateState(cell, state, info);
        const view = UPDATE_STATE_VIEW[state];
        if (state === "available") {
            // The link badge carries the icon/text in the markup; the status label is hidden.
            assert.equal(badge.hidden, false);
            assert.equal(status.hidden, true);
            continue;
        }
        assert.equal(status.hidden, false, state);
        assert.equal(status.dataset.state, state);
        assert.equal(icon.textContent, view.icon, state);
        assert.equal(text.textContent, view.text, state);
    }
});

test("an unknown state explains whether the installed or the latest version is missing", () => {
    const { cell, status } = makeCell();
    renderUpdateState(cell, "unknown", { reason: "installed_unknown" });
    assert.match(status.title, /installed version/);
    renderUpdateState(cell, "unknown", { reason: "latest_unknown" });
    assert.match(status.title, /latest upstream version/);
    renderUpdateState(cell, "unknown", {});
    assert.equal(status.title, "");
});

const template = readFileSync(new URL("../../app/templates/settings.html", import.meta.url), "utf8");
const css = readFileSync(new URL("../../app/static/style.css", import.meta.url), "utf8");

test("settings.html gives every component cell the same badge and status markup", () => {
    const cells = [...template.matchAll(/<dd class="settings-version-cell" data-update-component="(\w+)">([\s\S]*?)<\/dd>/g)];
    assert.deepEqual(cells.map((m) => m[1]), COMPONENTS);
    const normalise = (html) => html.replace(/\{\{[\s\S]*?\}\}/g, "").replace(/\s+/g, " ").trim();
    const first = normalise(cells[0][2]);
    for (const [, key, body] of cells) {
        assert.equal(normalise(body), first, key);
    }
    assert.match(first, /class="settings-update-badge"/);
    assert.match(first, /class="settings-update-status"/);
});

test("the badge classes share one geometry rule and define a colour per state", () => {
    const shared = css.match(/\.settings-update-badge,\s*\.settings-update-status,\s*\.settings-app-update-badge \{([^}]*)\}/);
    assert.ok(shared, "shared badge rule missing");
    for (const prop of ["padding", "border:", "border-radius", "font-size", "min-height", "line-height"]) {
        assert.ok(shared[1].includes(prop), prop);
    }
    assert.match(css, /\.settings-update-status\[data-state="current"\]/);
    assert.match(css, /\.settings-app-update-badge\[data-state="current"\]/);
    // No second, parallel set of per-state geometry.
    assert.doesNotMatch(css, /\.settings-update-status\[data-state="[a-z]+"\][^{]*\{[^}]*padding/);
});

test("the fetchly update tile uses the same badge for its states", () => {
    assert.match(template, /class="settings-app-update-badge" data-app-update-badge/);
});

test("the Pre-release marker renders inline, left of the Current version badge", () => {
    const currentRow = template.match(
        /<dt>Current<\/dt>\s*<dd class="settings-app-update-current-cell">([\s\S]*?)<\/dd>/
    );
    assert.ok(currentRow, "Current row with the inline cell is missing");
    const cell = currentRow[1];
    // The Pre-release badge sits before the version badge inside the same cell.
    const prereleaseAt = cell.indexOf("data-app-update-prerelease");
    const versionAt = cell.indexOf("data-app-update-current");
    assert.ok(prereleaseAt >= 0, "inline Pre-release badge is missing");
    assert.ok(versionAt >= 0, "version badge is missing");
    assert.ok(prereleaseAt < versionAt, "Pre-release badge must precede the version badge");
    assert.match(cell, /data-app-update-prerelease[^>]*\bhidden\b/, "Pre-release badge starts hidden");
    assert.match(cell, /class="settings-app-update-badge settings-app-update-prerelease"/);
});
