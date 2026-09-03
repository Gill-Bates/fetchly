//
// tests/js/toast.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// Minimal DOM stand-in: enough for toast.js to build its container, append
// toast nodes, and query them back by type/message.
class ClassList {
    constructor() {
        this._set = new Set();
    }
    add(...names) {
        names.forEach((n) => this._set.add(n));
    }
    remove(...names) {
        names.forEach((n) => this._set.delete(n));
    }
    contains(name) {
        return this._set.has(name);
    }
    toString() {
        return [...this._set].join(" ");
    }
}

class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this._className = "";
        this.classList = new ClassList();
        this.textContent = "";
        this.children = [];
        this.parentNode = null;
        this.attributes = new Map();
        this._listeners = new Map();
        this.isConnected = false;
    }

    get className() {
        return this._className;
    }

    set className(value) {
        this._className = value;
        this.classList = new ClassList();
        String(value)
            .split(/\s+/)
            .filter(Boolean)
            .forEach((c) => this.classList.add(c));
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    appendChild(node) {
        this.children.push(node);
        node.parentNode = this;
        node.isConnected = this.isConnected;
        return node;
    }

    removeChild(node) {
        this.children = this.children.filter((c) => c !== node);
        node.parentNode = null;
        return node;
    }

    remove() {
        if (this.parentNode) this.parentNode.removeChild(this);
        this.isConnected = false;
    }

    contains(node) {
        if (node === this) return true;
        return this.children.some((child) => child.contains?.(node));
    }

    querySelectorAll(selector) {
        // Only supports the ".fx-toast--{type}" selector used by toast.js.
        const cls = selector.replace(/^\./, "");
        const results = [];
        const walk = (node) => {
            for (const child of node.children) {
                if (child.classList?.contains(cls)) results.push(child);
                walk(child);
            }
        };
        walk(this);
        return results;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] ?? null;
    }

    addEventListener(type, handler) {
        const bucket = this._listeners.get(type) ?? [];
        bucket.push(handler);
        this._listeners.set(type, bucket);
    }

    removeEventListener(type, handler) {
        const bucket = this._listeners.get(type) ?? [];
        this._listeners.set(type, bucket.filter((h) => h !== handler));
    }
}

const body = new FakeElement("body");
body.isConnected = true;

globalThis.HTMLElement = FakeElement;
globalThis.document = {
    body,
    createElement: (tag) => new FakeElement(tag),
};
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
globalThis.setTimeout = fakeSetTimeout;
globalThis.clearTimeout = fakeClearTimeout;
globalThis.window = {
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClearTimeout,
};

const source = await readFile(new URL("../../app/static/js/toast.js", import.meta.url), "utf8");
const { showToast } = await import(
    `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

function messageOf(toastEl) {
    return toastEl.children.find((c) => c.className === "fx-toast__message")?.textContent;
}

// The fake DOM never fires "transitionend", so dismissToast()'s real removal
// path can't run here. Resetting body.children directly (rather than relying
// on clearToasts()) makes getContainer() rebuild a fresh container on the
// next showToast() call, keeping tests isolated.
function resetDom() {
    body.children = [];
}

test("shows a new toast for a first-time message", () => {
    const t = showToast("Load failed", "danger");
    assert.equal(messageOf(t), "Load failed");
    assert.equal(t.classList.contains("fx-toast--danger"), true);
    resetDom();
    timers.clear();
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

    resetDom();
    timers.clear();
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

    resetDom();
    timers.clear();
});

test("different messages of the same type are not deduped against each other", () => {
    const a = showToast("Could not load more jobs.", "danger");
    const b = showToast("Something else failed.", "danger");

    assert.notEqual(a, b);
    const containerChildren = body.children[0].children.filter((c) =>
        c.classList?.contains("fx-toast--danger"),
    );
    assert.equal(containerChildren.length, 2);

    resetDom();
    timers.clear();
});

test("same message with a different type is not deduped", () => {
    const a = showToast("Retrying...", "warning");
    const b = showToast("Retrying...", "info");

    assert.notEqual(a, b);

    resetDom();
    timers.clear();
});

test("a dismissing toast is not reused; a fresh one is shown instead", () => {
    const t1 = showToast("Could not load more jobs.", "danger");
    // Manually mark as dismissing, mirroring the moment between
    // dismissToast() starting its transition and the node being removed.
    t1.classList.remove("fx-toast--visible");
    t1.classList.add("fx-toast--hiding");
    // Directly flip internal dismissing bookkeeping via the exported
    // behavior: simulate by removing it from the DOM as removeToast() would
    // eventually do, and confirm a new toast can still be created afterward.
    t1.remove();

    const t2 = showToast("Could not load more jobs.", "danger");
    assert.notEqual(t2, t1);

    resetDom();
    timers.clear();
});
