//
// tests/js/confirm-modal.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

import { findByClass, installFakeDom } from "./helpers/fake-dom.mjs";

// confirm.js builds its modal node once and reuses it for every call, so the
// body must not be reset between tests here - that would strand the node
// nothing in confirm.js would ever recreate.
const { body, activeElement, modalCalls } = installFakeDom();

const { confirmModal } = await import("../../app/static/js/confirm.js");

function modalParts() {
    const root = body.children.find((child) => child.id === "confirmModal");
    const footer = findByClass(root, "modal-footer");
    return {
        root,
        title: findByClass(root, "modal-title"),
        message: findByClass(root, "mb-0"),
        closeBtn: findByClass(root, "btn-close"),
        cancelBtn: findByClass(root, "btn-outline-secondary"),
        // confirm.js appends [cancelBtn, acceptBtn] to the footer in that
        // order; acceptBtn's own class changes with the variant, so its
        // stable identity is "the footer's second control".
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

test("every accepted variant renders its own button class", async () => {
    for (const variant of ["primary", "success", "warning", "danger"]) {
        const pending = confirmModal({ message: "Go?", variant });
        assert.equal(modalParts().acceptBtn.className, `btn btn-${variant}`);
        modalParts().root.dispatchEvent("hidden.bs.modal");
        assert.equal(await pending, false);
    }
});

test("an unknown variant falls back to primary, focused as a safe action", async () => {
    const pending = confirmModal({ message: "Go?", variant: "chartreuse" });
    const { root, acceptBtn } = modalParts();

    assert.equal(acceptBtn.className, "btn btn-primary");
    assert.equal(activeElement.current, acceptBtn);

    root.dispatchEvent("hidden.bs.modal");
    assert.equal(await pending, false);
});

test("restores focus to the triggering element after closing", async () => {
    const trigger = new (Object.getPrototypeOf(body).constructor)("button");
    body.appendChild(trigger);
    trigger.focus();

    const pending = confirmModal({ message: "Go?", variant: "primary" });
    modalParts().acceptBtn.click();
    await pending;

    assert.equal(activeElement.current, trigger);
});

test("opening a second dialog resolves the first one as cancelled", async () => {
    // The modal node and its pending promise are shared module state: a
    // second confirmModal() call while the first is still open must not
    // leave the first caller's promise dangling forever.
    const first = confirmModal({ message: "First?", variant: "danger" });
    const second = confirmModal({ message: "Second?", variant: "primary" });

    assert.equal(await first, false);
    assert.equal(modalParts().message.textContent, "Second?");

    modalParts().acceptBtn.click();
    assert.equal(await second, true);
});

test("the dialog is wired for assistive tech", async () => {
    const pending = confirmModal({ message: "Go?" });
    const { root, title, closeBtn } = modalParts();

    assert.equal(root.getAttribute("aria-labelledby"), "confirmModalLabel");
    assert.equal(title.id, "confirmModalLabel");
    assert.equal(root.tabIndex, -1);
    assert.equal(closeBtn.getAttribute("aria-label"), "Close dialog");

    root.dispatchEvent("hidden.bs.modal");
    assert.equal(await pending, false);
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
