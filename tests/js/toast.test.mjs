//
// tests/js/toast.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test, { afterEach, after } from "node:test";

import { installFakeDom } from "./helpers/fake-dom.mjs";

const { body } = installFakeDom({ withBootstrap: false });
globalThis.requestAnimationFrame = (cb) => cb();

// Fake timer registry so we can assert on scheduling without real delays.
let nextTimerId = 1;
const timers = new Map();
function fakeSetTimeout(fn, delay) {
    const id = nextTimerId++;
    timers.set(id, { fn, delay });
    return id;
}
function fakeClearTimeout(id) {
    timers.delete(id);
}
// toast.js calls the bare globals (clearTimeout) as well as window.setTimeout,
// mirroring real browsers where both refer to the same function.
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = fakeSetTimeout;
globalThis.clearTimeout = fakeClearTimeout;
globalThis.window.setTimeout = fakeSetTimeout;
globalThis.window.clearTimeout = fakeClearTimeout;

after(() => {
    // Nothing in this suite is async, but leaving the real timers faked would
    // hang any later test file run in the same process on its first await.
    globalThis.setTimeout = realSetTimeout;
    globalThis.clearTimeout = realClearTimeout;
});

const { showToast } = await import("../../app/static/js/toast.js");

function messageOf(toastEl) {
    return toastEl.children.find((c) => c.className === "fx-toast__message")?.textContent;
}

function closeButtonOf(toastEl) {
    return toastEl.children.find((c) => c.className === "fx-toast__close");
}

// The fake DOM never fires "transitionend", so dismissToast()'s real removal
// path can't run here. Resetting body.children directly (rather than relying
// on clearToasts()) makes getContainer() rebuild a fresh container on the
// next showToast() call, keeping tests isolated.
function resetDom() {
    body.children = [];
}

afterEach(() => {
    resetDom();
    timers.clear();
});

test("shows a new toast for a first-time message", () => {
    const t = showToast("Load failed", "danger");
    assert.equal(messageOf(t), "Load failed");
    assert.equal(t.classList.contains("fx-toast--danger"), true);
});

test("repeated identical failures do not stack duplicate toasts", () => {
    // Simulates loadMore() firing jobs-load-error repeatedly while the
    // sentinel keeps crossing the viewport during a sustained outage.
    const first = showToast("Could not load more jobs.", "danger");
    const second = showToast("Could not load more jobs.", "danger");
    const third = showToast("Could not load more jobs.", "danger");

    assert.equal(second, first, "second call should reuse the active toast");
    assert.equal(third, first, "third call should reuse the active toast");

    const containerChildren = body.children[0].children.filter((c) =>
        c.classList?.contains("fx-toast--danger"),
    );
    assert.equal(containerChildren.length, 1, "only one toast node should exist in the DOM");
});

test("repeated identical calls restart the auto-dismiss timer instead of adding a new one", () => {
    const t1 = showToast("Could not load more jobs.", "danger", 3000);
    const timerCountAfterFirst = timers.size;
    assert.equal(timerCountAfterFirst, 1);

    const firstTimerId = [...timers.keys()][0];

    const t2 = showToast("Could not load more jobs.", "danger", 3000);
    assert.equal(t2, t1);
    assert.equal(timers.size, 1, "old timer must be cleared, not accumulated");
    assert.notEqual([...timers.keys()][0], firstTimerId, "a fresh timer id should be scheduled");
});

test("different messages of the same type are not deduped against each other", () => {
    const a = showToast("Could not load more jobs.", "danger");
    const b = showToast("Something else failed.", "danger");

    assert.notEqual(a, b);
    const containerChildren = body.children[0].children.filter((c) =>
        c.classList?.contains("fx-toast--danger"),
    );
    assert.equal(containerChildren.length, 2);
});

test("same message with a different type is not deduped", () => {
    const a = showToast("Retrying...", "warning");
    const b = showToast("Retrying...", "info");

    assert.notEqual(a, b);
});

test("a dismissing toast is not reused; a fresh one is shown instead", () => {
    const t1 = showToast("Could not load more jobs.", "danger");
    // Real dismiss path: the fake DOM never fires "transitionend", so the
    // node stays in the container mid-hide - exactly the window findActiveToast()
    // has to recognize and skip.
    closeButtonOf(t1).click();
    assert.equal(t1.classList.contains("fx-toast--hiding"), true);
    assert.notEqual(t1.parentNode, null, "must still be in the DOM while hiding");

    const t2 = showToast("Could not load more jobs.", "danger");
    assert.notEqual(t2, t1);
    assert.equal(
        body.children[0].children.filter((c) => c.classList?.contains("fx-toast--danger")).length,
        2,
    );
});

test("an error is announced assertively, everything else politely", () => {
    assert.equal(showToast("boom", "danger").attributes.get("role"), "alert");
    resetDom();

    assert.equal(showToast("saved", "success").attributes.get("role"), "status");
    resetDom();

    // "error" is an accepted alias and must map onto the danger styling/role.
    const aliased = showToast("boom", "error");
    assert.equal(aliased.classList.contains("fx-toast--danger"), true);
    assert.equal(aliased.attributes.get("role"), "alert");
});
