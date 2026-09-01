//
// tests/js/cookie-paste-modal.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// Minimal DOM + Bootstrap stand-ins: enough for cookie-paste.js to build its
// one reusable dialog and drive it through paste / save / failure / dismiss.
class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.className = "";
        this.id = "";
        this.tabIndex = 0;
        this.type = "";
        this.value = "";
        this.rows = 0;
        this.disabled = false;
        this.readOnly = false;
        this.placeholder = "";
        this.spellcheck = true;
        this.htmlFor = "";
        this.children = [];
        this.parentNode = null;
        this.attributes = new Map();
        this.focusCount = 0;
        this._text = "";
        this._listeners = new Map();
        this.classList = {
            toggle: (name, force) => {
                const classes = new Set(this.className.split(" ").filter(Boolean));
                const shouldAdd = force ?? !classes.has(name);
                if (shouldAdd) classes.add(name);
                else classes.delete(name);
                this.className = [...classes].join(" ");
            },
            contains: (name) => this.className.split(" ").includes(name),
        };
    }

    // Assigning textContent replaces every child, which is how the dialog
    // clears its step list before rendering the next platform's.
    set textContent(value) {
        this._text = String(value);
        this.children = [];
    }

    get textContent() {
        return this.children.length
            ? this.children.map((child) => child.textContent).join("")
            : this._text;
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
globalThis.window = {};
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
    new URL("../../app/static/js/cookie-paste.js", import.meta.url),
    "utf8",
);
const { openCookiePasteDialog } = await import(
    `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

function dialogParts() {
    const root = body.children.find((child) => child.id === "cookiePasteModal");
    const content = root.children[0].children[0];
    const [header, bodyEl, footer] = content.children;
    const [identity] = header.children;
    const [platformBadge, heading] = identity.children;
    const [, title, subtitle] = heading.children;
    const [layout] = bodyEl.children;
    const [guide, pastePanel] = layout.children;
    const [, steps] = guide.children;
    const [, textarea, formats, hint, error] = pastePanel.children;
    const [, actions] = footer.children;
    return {
        root,
        title,
        subtitle,
        platformBadge,
        platformIcon: platformBadge.children[0],
        steps,
        textarea,
        formats,
        hint,
        error,
        cancelBtn: actions.children[0],
        saveBtn: actions.children[1],
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
        onSubmit: async () => {},
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
    const pending = openCookiePasteDialog({
        platform: "tiktok",
        label: "TikTok",
        onSubmit: async () => {},
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
        const pending = openCookiePasteDialog({ platform, label, onSubmit: async () => {} });
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

test("without Bootstrap the caller gets a readable reason", async () => {
    const savedBootstrap = globalThis.bootstrap;
    delete globalThis.bootstrap;
    try {
        await assert.rejects(
            openCookiePasteDialog({ platform: "youtube", label: "YouTube", onSubmit: async () => {} }),
            /reload the page/,
        );
    } finally {
        globalThis.bootstrap = savedBootstrap;
    }
});
