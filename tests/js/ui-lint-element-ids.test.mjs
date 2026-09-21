//
// tests/js/ui-lint-element-ids.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The failure this contract exists for is silent: a module-level
// `const el = getElementById("x")` that misses is null, and every later
// `el?.…` quietly does nothing. So the check has to be exact in both
// directions - a typo must be reported, and the ids the scripts create
// themselves must not be, or the contract grows an allowlist and rots.
//

import assert from "node:assert/strict";
import test from "node:test";

const {
    markupElementIds,
    unresolvedElementIds,
} = await import("../../tools/ui-lint/lib/source-contracts.mjs");

const script = (source) => [{ name: "page.js", source }];

test("an id the markup renders resolves", () => {
    const ids = markupElementIds(['<div id="authForm"></div>']);
    assert.deepEqual(
        unresolvedElementIds(script('getElementById("authForm")'), ids),
        [],
    );
});

test("a typo is reported against the module that made it", () => {
    const ids = markupElementIds(['<div id="authForm"></div>']);
    assert.deepEqual(
        unresolvedElementIds(script('getElementById("authFrom")'), ids),
        [{ name: "page.js", ids: ["authFrom"] }],
    );
});

test("ids the script assigns itself are not reported", () => {
    // The three spellings in app/static/js: an options bag (main.js builds the
    // filter empty states that way), a direct assignment, and markup inside a
    // template literal.
    const source = `
        build({ id: "filterEmptyRow" });
        node.id = "toastContainer";
        host.innerHTML = \`<div id="confirmModal"></div>\`;
        getElementById("filterEmptyRow");
        getElementById("toastContainer");
        getElementById("confirmModal");
    `;
    assert.deepEqual(unresolvedElementIds(script(source), new Set()), []);
});

test("an id one module creates counts for another that reads it", () => {
    const scripts = [
        { name: "toast.js", source: 'node.id = "toastContainer";' },
        { name: "main.js", source: 'getElementById("toastContainer")' },
    ];
    assert.deepEqual(unresolvedElementIds(scripts, new Set()), []);
});

test("a computed lookup is out of scope, not a violation", () => {
    const source = 'getElementById(id); getElementById(btn.dataset.target);';
    assert.deepEqual(unresolvedElementIds(script(source), new Set()), []);
});

test("a Jinja-interpolated id is not treated as a literal one", () => {
    // `id="{{ job['id'] | e }}"` renders a row's data, so no script can name
    // it literally - letting it into the known set would mask a real typo.
    const ids = markupElementIds(["<tr id=\"{{ job['id'] | e }}\"></tr>"]);
    assert.equal(ids.size, 0);
    assert.deepEqual(
        unresolvedElementIds(script('getElementById("{{ job[")'), ids),
        [{ name: "page.js", ids: ['{{ job['] }],
    );
});

test("ignores data-id attributes and commented-out markup", () => {
    const ids = markupElementIds([
        '<div data-id="ghost"></div><!-- <div id="commented"></div> -->',
        '<div id="real"></div>',
    ]);
    assert.deepEqual([...ids], ["real"]);
});

test("each module is reported once with its ids sorted and deduplicated", () => {
    const source = 'getElementById("zeta"); getElementById("alpha"); getElementById("zeta");';
    assert.deepEqual(
        unresolvedElementIds(script(source), new Set()),
        [{ name: "page.js", ids: ["alpha", "zeta"] }],
    );
});
