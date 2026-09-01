//
// tests/js/confirm-modal.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// Minimal DOM + Bootstrap stand-ins: enough for confirm.js to build its one
// reusable modal node and drive it through show / accept / dismiss.
class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.className = "";
        this.id = "";
        this.tabIndex = 0;
        this.type = "";
        this.textContent = "";
        this.children = [];
        this.parentNode = null;
        this.attributes = new Map();
        this.focusCount = 0;
        this._listeners = new Map();
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    append(...nodes) {
        for (const node of nodes) {
            this.children.push(node);
            node.parentNode = this;
        }
    }

    appendChild(node) {
        this.children.push(node);
        node.parentNode = this;
        return node;
    }

    contains(node) {
        return node === this || this.children.some((child) => child.contains?.(node));
    }

    addEventListener(type, handler, options = {}) {
        const bucket = this._listeners.get(type) ?? [];
        bucket.push({ handler, once: Boolean(options.once) });
        this._listeners.set(type, bucket);
    }

    dispatchEvent(type, event = {}) {
        const bucket = this._listeners.get(type) ?? [];
        this._listeners.set(type, bucket.filter((entry) => !entry.once));
        for (const entry of bucket) {
            entry.handler({ type, ...event });
        }
    }

    click() {
        this.dispatchEvent("click");
    }

    focus() {
        this.focusCount += 1;
        activeElement.current = this;
    }
}

const body = new FakeElement("body");
const activeElement = { current: body };
const modalCalls = { show: 0, hide: 0 };

globalThis.HTMLElement = FakeElement;
globalThis.window = { confirm: () => true };
globalThis.document = {
    body,
    createElement: (tag) => new FakeElement(tag),
    get activeElement() {
        return activeElement.current;
    },
};
globalThis.bootstrap = {
    Modal: {
        getOrCreateInstance(root) {
            return {
                show() {
                    modalCalls.show += 1;
                    root.dispatchEvent("shown.bs.modal");
                },
                hide() {
                    modalCalls.hide += 1;
                    root.dispatchEvent("hidden.bs.modal");
                },
            };
        },
    },
};

const source = await readFile(
    new URL("../../app/static/js/confirm.js", import.meta.url),
    "utf8",
);
const { confirmModal } = await import(
    `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

function modalParts() {
    const root = body.children.find((child) => child.id === "confirmModal");
    const content = root.children[0].children[0];
    const [header, bodyEl, footer] = content.children;
    return {
        root,
        title: header.children[0],
        message: bodyEl.children[0],
        cancelBtn: footer.children[0],
        acceptBtn: footer.children[1],
    };
}

test("resolves true when the confirm button is clicked", async () => {
    const pending = confirmModal({ message: "Proceed?", confirmText: "Do it", variant: "primary" });
    const { acceptBtn, title, message } = modalParts();

    assert.equal(title.textContent, "Please confirm");
    assert.equal(message.textContent, "Proceed?");
    assert.equal(acceptBtn.textContent, "Do it");
    assert.equal(acceptBtn.className, "btn btn-primary");
    // A non-destructive dialog lets the confirm button lead.
    assert.equal(activeElement.current, acceptBtn);

    acceptBtn.click();
    assert.equal(await pending, true);
    assert.equal(modalCalls.hide > 0, true);
});

test("resolves false when the dialog is dismissed without confirming", async () => {
    const pending = confirmModal("Delete it?");
    const { root, message } = modalParts();

    assert.equal(message.textContent, "Delete it?");

    root.dispatchEvent("hidden.bs.modal");
    assert.equal(await pending, false);
});

test("a destructive variant opens with the cancel button focused", async () => {
    const pending = confirmModal({
        message: "Turn it off?",
        confirmText: "Turn off",
        variant: "danger",
    });
    const { root, acceptBtn, cancelBtn } = modalParts();

    assert.equal(activeElement.current, cancelBtn);
    assert.equal(acceptBtn.className, "btn btn-danger");
    assert.equal(acceptBtn.textContent, "Turn off");

    root.dispatchEvent("hidden.bs.modal");
    assert.equal(await pending, false);
});

test("restores focus to the triggering element after closing", async () => {
    const trigger = new FakeElement("button");
    body.appendChild(trigger);
    trigger.focus();

    const pending = confirmModal({ message: "Go?", variant: "primary" });
    modalParts().acceptBtn.click();
    await pending;

    assert.equal(activeElement.current, trigger);
});

test("falls back to a native prompt when Bootstrap is unavailable", async () => {
    const savedBootstrap = globalThis.bootstrap;
    delete globalThis.bootstrap;
    const seen = [];
    globalThis.window.confirm = (msg) => {
        seen.push(msg);
        return false;
    };

    try {
        assert.equal(await confirmModal({ message: "No bootstrap here" }), false);
        assert.deepEqual(seen, ["No bootstrap here"]);
    } finally {
        globalThis.bootstrap = savedBootstrap;
        globalThis.window.confirm = () => true;
    }
});
