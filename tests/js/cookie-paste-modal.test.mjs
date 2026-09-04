//
// tests/js/cookie-paste-modal.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

import { findByClass, installFakeDom } from "./helpers/fake-dom.mjs";

// cookie-paste.js builds its dialog node once and reuses it for every call,
// so the body is never reset between tests here.
const { body, activeElement, modalCalls } = installFakeDom();

const { openCookiePasteDialog } = await import("../../app/static/js/cookie-paste.js");

function dialogParts() {
    const root = body.children.find((child) => child.id === "cookiePasteModal");
    return {
        root,
        title: findByClass(root, "cookie-connect-title"),
        subtitle: findByClass(root, "cookie-connect-subtitle"),
        platformBadge: findByClass(root, "cookie-connect-platform"),
        platformIcon: findByClass(root, "platform-pill__icon"),
        steps: findByClass(root, "cookie-paste-steps"),
        textarea: findByClass(root, "cookie-paste-input"),
        formats: findByClass(root, "cookie-connect-formats"),
        hint: findByClass(root, "cookie-paste-hint"),
        error: findByClass(root, "cookie-paste-error"),
        cancelBtn: findByClass(root, "cookie-connect-actions").children[0],
        saveBtn: findByClass(root, "cookie-connect-save"),
        saveLabel: findByClass(root, "cookie-connect-save").children[1],
    };
}

function type(textarea, value) {
    textarea.value = value;
    textarea.dispatchEvent("input");
}

test("opens with the platform's name and instructions", async () => {
    const pending = openCookiePasteDialog({
        platform: "youtube",
        label: "YouTube",
        onSubmit: async () => { },
    });
    const {
        root,
        title,
        subtitle,
        platformBadge,
        platformIcon,
        steps,
        textarea,
        formats,
        hint,
        saveBtn,
    } = dialogParts();

    assert.equal(title.textContent, "Connect YouTube");
    assert.match(subtitle.textContent, /signed-in YouTube session/);
    assert.equal(root.getAttribute("data-cookie-platform"), "youtube");
    assert.match(platformBadge.className, /platform-pill--youtube/);
    assert.match(platformIcon.className, /platform-pill__icon--youtube/);
    assert.match(textarea.placeholder, /youtube\.com.*SID=/);
    assert.equal(steps.children.length, 5);
    assert.match(steps.children[0].textContent, /www\.youtube\.com/);
    // Aiming at the page request is how this paste comes up empty: a service
    // worker answers it and the dev tools show only provisional headers.
    assert.match(steps.children[1].textContent, /Fetch\/XHR/);
    assert.match(steps.children[3].textContent, /Copy as cURL/);
    assert.deepEqual(
        formats.children.map((format) => format.textContent),
        ["cURL", "fetch", "Cookie header", "JSON"],
    );
    assert.match(hint.textContent, /Fetch\/XHR requests/);
    // Nothing pasted yet, so there is nothing to save.
    assert.equal(saveBtn.disabled, true);
    assert.equal(activeElement.current, textarea);

    root.dispatchEvent("hidden.bs.modal");
    assert.equal(await pending, false);
});

test("hands the trimmed paste to the submit handler and closes", async () => {
    const seen = [];
    const pending = openCookiePasteDialog({
        platform: "instagram",
        label: "Instagram",
        onSubmit: async (text) => {
            seen.push(text);
        },
    });
    const { textarea, saveBtn } = dialogParts();

    type(textarea, "  sessionid=abc; csrftoken=t  ");
    assert.equal(saveBtn.disabled, false);

    const hidesBefore = modalCalls.hide;
    saveBtn.click();

    assert.equal(await pending, true);
    assert.deepEqual(seen, ["sessionid=abc; csrftoken=t"]);
    assert.equal(modalCalls.hide, hidesBefore + 1);
});

test("while a submit is in flight the dialog is locked against a second one", async () => {
    let releaseSubmit;
    const pending = openCookiePasteDialog({
        platform: "youtube",
        label: "YouTube",
        onSubmit: () => new Promise((resolve) => {
            releaseSubmit = resolve;
        }),
    });
    const { textarea, saveBtn, cancelBtn, saveLabel } = dialogParts();

    type(textarea, "LOGIN_INFO=a; SAPISID=b");
    saveBtn.click();

    assert.equal(saveBtn.disabled, true, "cannot double-submit while busy");
    assert.equal(cancelBtn.disabled, true, "cannot dismiss mid-request via the button");
    assert.equal(textarea.readOnly, true, "the pasted text cannot be edited mid-request");
    assert.equal(saveLabel.textContent, "Checking\u2026");

    releaseSubmit();
    await pending;

    assert.equal(cancelBtn.disabled, false);
    assert.equal(textarea.readOnly, false);
    assert.equal(saveLabel.textContent, "Save cookies");
});

test("a rejected paste keeps the dialog open with the text and the reason", async () => {
    const pending = openCookiePasteDialog({
        platform: "youtube",
        label: "YouTube",
        onSubmit: async () => {
            throw new Error("No login cookie in this paste");
        },
    });
    const { root, textarea, error, saveBtn } = dialogParts();

    type(textarea, "PREF=f6=4");
    const hidesBefore = modalCalls.hide;
    saveBtn.click();
    await new Promise((resolve) => setImmediate(resolve));

    // The whole point: a paste the user cannot easily reproduce is not thrown
    // away just because the server said no.
    assert.equal(modalCalls.hide, hidesBefore);
    assert.equal(error.textContent, "No login cookie in this paste");
    assert.equal(error.classList.contains("d-none"), false);
    assert.equal(textarea.value, "PREF=f6=4");
    assert.equal(saveBtn.disabled, false);

    root.dispatchEvent("hidden.bs.modal");
    assert.equal(await pending, false);
});

test("reopening clears the previous paste, error and steps", async () => {
    // Produce the dirty state here rather than inheriting it from the
    // previous test, so this case survives reordering and
    // --test-name-pattern runs.
    const dirty = openCookiePasteDialog({
        platform: "youtube",
        label: "YouTube",
        onSubmit: async () => {
            throw new Error("No login cookie in this paste");
        },
    });
    const stale = dialogParts();
    type(stale.textarea, "PREF=f6=4");
    stale.saveBtn.click();
    await new Promise((resolve) => setImmediate(resolve));
    assert.notEqual(stale.error.textContent, "");
    stale.root.dispatchEvent("hidden.bs.modal");
    assert.equal(await dirty, false);

    const pending = openCookiePasteDialog({
        platform: "tiktok",
        label: "TikTok",
        onSubmit: async () => { },
    });
    const { root, title, steps, textarea, error } = dialogParts();

    assert.equal(title.textContent, "Connect TikTok");
    assert.equal(textarea.value, "");
    assert.equal(error.textContent, "");
    assert.equal(steps.children.length, 5);
    assert.match(steps.children[0].textContent, /www\.tiktok\.com/);

    root.dispatchEvent("hidden.bs.modal");
    assert.equal(await pending, false);
});

test("reuses the dialog with the correct branding for all four platforms", async () => {
    for (const [platform, label] of [
        ["youtube", "YouTube"],
        ["tiktok", "TikTok"],
        ["instagram", "Instagram"],
        ["facebook", "Facebook"],
    ]) {
        const pending = openCookiePasteDialog({ platform, label, onSubmit: async () => { } });
        const { root, title, platformBadge, platformIcon, textarea } = dialogParts();

        assert.equal(root.getAttribute("data-cookie-platform"), platform);
        assert.equal(title.textContent, `Connect ${label}`);
        assert.match(platformBadge.className, new RegExp(`platform-pill--${platform}`));
        assert.match(platformIcon.className, new RegExp(`platform-pill__icon--${platform}`));
        assert.match(textarea.placeholder, new RegExp(`www\\.${platform}\\.com`));

        root.dispatchEvent("hidden.bs.modal");
        assert.equal(await pending, false);
    }
});

test("a late response from a dismissed dialog cannot close the next one", async () => {
    // The dialog node and its state are shared. Escape and the backdrop stay
    // live while a submit is in flight, so this sequence is reachable.
    let releaseFirst;
    const firstDone = new Promise((resolve) => {
        releaseFirst = resolve;
    });

    const firstPending = openCookiePasteDialog({
        platform: "youtube",
        label: "YouTube",
        onSubmit: () => firstDone,
    });
    const first = dialogParts();
    type(first.textarea, "LOGIN_INFO=a; SAPISID=b");
    first.saveBtn.click();

    // Dismissed while the request is still running.
    first.root.dispatchEvent("hidden.bs.modal");
    assert.equal(await firstPending, false);

    let secondSubmitted = false;
    const secondPending = openCookiePasteDialog({
        platform: "instagram",
        label: "Instagram",
        onSubmit: async () => {
            secondSubmitted = true;
        },
    });
    const second = dialogParts();
    type(second.textarea, "sessionid=abc");
    const hidesBefore = modalCalls.hide;

    releaseFirst();
    await new Promise((resolve) => setImmediate(resolve));

    // The first dialog's answer belongs to nothing on screen any more.
    assert.equal(modalCalls.hide, hidesBefore);
    assert.equal(second.title.textContent, "Connect Instagram");
    assert.equal(second.textarea.value, "sessionid=abc");
    assert.equal(second.saveBtn.disabled, false);
    assert.equal(secondSubmitted, false);

    second.root.dispatchEvent("hidden.bs.modal");
    assert.equal(await secondPending, false);
});

test("the dialog is wired for assistive tech", async () => {
    const pending = openCookiePasteDialog({
        platform: "youtube",
        label: "YouTube",
        onSubmit: async () => { },
    });
    const { root, subtitle, steps, error } = dialogParts();

    assert.equal(root.getAttribute("aria-labelledby"), "cookiePasteModalLabel");
    assert.equal(root.getAttribute("aria-describedby"), "cookiePasteModalDescription");
    assert.equal(subtitle.id, "cookiePasteModalDescription");
    assert.equal(error.getAttribute("role"), "alert");
    assert.equal(steps.getAttribute("aria-label"), "How to copy YouTube cookies");

    root.dispatchEvent("hidden.bs.modal");
    assert.equal(await pending, false);
});

test("without Bootstrap the caller gets a readable reason", async () => {
    const savedBootstrap = globalThis.bootstrap;
    delete globalThis.bootstrap;
    try {
        await assert.rejects(
            openCookiePasteDialog({ platform: "youtube", label: "YouTube", onSubmit: async () => { } }),
            /reload the page/,
        );
    } finally {
        globalThis.bootstrap = savedBootstrap;
    }
});
