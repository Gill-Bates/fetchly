//
// tests/js/helpers/fake-dom.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module helpers/fake-dom
 *
 * One DOM stand-in shared by every modal/toast test, instead of three
 * hand-rolled copies that quietly drifted apart (confirm.js, cookie-paste.js
 * and toast.js each got their own FakeElement with different semantics).
 * Not collected by `npm test` (glob is tests/js/*.test.mjs, not this
 * directory).
 */

class ClassList {
    constructor(owner) {
        this._owner = owner;
    }

    _set() {
        return new Set(String(this._owner.className || "").split(/\s+/).filter(Boolean));
    }

    add(...names) {
        const set = this._set();
        names.forEach((n) => set.add(n));
        this._owner.className = [...set].join(" ");
    }

    remove(...names) {
        const set = this._set();
        names.forEach((n) => set.delete(n));
        this._owner.className = [...set].join(" ");
    }

    toggle(name, force) {
        const set = this._set();
        const shouldAdd = force ?? !set.has(name);
        if (shouldAdd) set.add(name);
        else set.delete(name);
        this._owner.className = [...set].join(" ");
        return shouldAdd;
    }

    contains(name) {
        return this._set().has(name);
    }

    toString() {
        return String(this._owner.className || "");
    }
}

export class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
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
        this.isConnected = false;
        this._className = "";
        this._text = "";
        this._listeners = new Map();
        this.classList = new ClassList(this);
    }

    get className() {
        return this._className;
    }

    set className(value) {
        this._className = String(value ?? "");
    }

    // Assigning textContent replaces every child - real DOM behavior that
    // cookie-paste.js relies on to clear its step list before re-rendering.
    set textContent(value) {
        this._text = String(value ?? "");
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
            node.isConnected = this.isConnected;
        }
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
        // Only supports ".class-name" selectors, which is all the modules
        // under test ever query for.
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

    addEventListener(type, handler, options = {}) {
        const bucket = this._listeners.get(type) ?? [];
        bucket.push({ handler, once: Boolean(options.once) });
        this._listeners.set(type, bucket);
    }

    removeEventListener(type, handler) {
        const bucket = this._listeners.get(type) ?? [];
        this._listeners.set(type, bucket.filter((entry) => entry.handler !== handler));
    }

    dispatchEvent(type, event = {}) {
        const bucket = this._listeners.get(type) ?? [];
        this._listeners.set(type, bucket.filter((entry) => !entry.once));
        for (const entry of bucket) {
            entry.handler({ type, target: this, ...event });
        }
    }

    // Browsers never deliver a click to a disabled control - matters for
    // assertions like "the save button is disabled" to mean something.
    click() {
        if (this.disabled) return;
        this.dispatchEvent("click");
    }

    focus() {
        if (this.disabled) return;
        this.focusCount += 1;
        activeElementRegistry.current = this;
    }
}

/** Shared by every FakeElement instance so document.activeElement agrees. */
const activeElementRegistry = { current: null };

/**
 * Depth-first search for the first descendant (or self) carrying `className`.
 * Stand-in for the tests reaching into `.children[n]` by position, which
 * breaks the moment a module inserts one more node.
 * @param {FakeElement} node
 * @param {string} className
 * @returns {FakeElement | null}
 */
export function findByClass(node, className) {
    if (node.classList?.contains(className)) return node;
    for (const child of node.children ?? []) {
        const hit = findByClass(child, className);
        if (hit) return hit;
    }
    return null;
}

/**
 * Installs a minimal document/window/bootstrap on globalThis, enough for the
 * confirm/cookie-paste/toast modules to build their DOM and run through
 * Bootstrap's modal lifecycle.
 * @param {{ withBootstrap?: boolean }} [options]
 * @returns {{ body: FakeElement, activeElement: { current: FakeElement }, modalCalls: { show: number, hide: number }, reset: () => void }}
 */
export function installFakeDom({ withBootstrap = true } = {}) {
    const body = new FakeElement("body");
    body.isConnected = true;
    activeElementRegistry.current = body;

    const modalCalls = { show: 0, hide: 0 };

    globalThis.HTMLElement = FakeElement;
    globalThis.document = {
        body,
        createElement: (tag) => new FakeElement(tag),
        get activeElement() {
            return activeElementRegistry.current;
        },
    };
    globalThis.window = { confirm: () => true };

    if (withBootstrap) {
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
    }

    return {
        body,
        activeElement: activeElementRegistry,
        modalCalls,
        reset() {
            body.children = [];
            modalCalls.show = 0;
            modalCalls.hide = 0;
        },
    };
}
