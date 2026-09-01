//
// app/static/js/confirm.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module confirm
 *
 * In-app confirmation dialog for fetchly. Replaces window.confirm() so every
 * "are you sure?" prompt renders as a Bootstrap modal styled like the rest of
 * the UI instead of a native browser popup.
 */

const ACCEPT_VARIANTS = new Set(["primary", "danger", "warning", "success"]);
const SAFE_ACCEPT_VARIANTS = new Set(["primary", "success"]);

/**
 * @typedef {{
 *   root: HTMLDivElement,
 *   title: HTMLElement,
 *   message: HTMLElement,
 *   acceptBtn: HTMLButtonElement,
 *   cancelBtn: HTMLButtonElement,
 * }} ConfirmElements
 */

/** @type {ConfirmElements | null} */
let els = null;
/** @type {{ show: () => void, hide: () => void } | null} */
let modalInstance = null;
/** @type {((confirmed: boolean) => void) | null} */
let pendingResolve = null;
/** @type {HTMLElement | null} */
let lastFocused = null;
/** @type {HTMLButtonElement | null} */
let focusTarget = null;

function settle(confirmed) {
    const resolve = pendingResolve;
    pendingResolve = null;
    if (resolve) {
        resolve(confirmed);
    }
}

function restoreFocus() {
    const target = lastFocused;
    lastFocused = null;
    if (target && document.body.contains(target)) {
        target.focus();
    }
}

/**
 * Build the single reusable modal node and wire its permanent listeners.
 * @returns {ConfirmElements}
 */
function buildModal() {
    const root = document.createElement("div");
    root.className = "modal fade";
    root.id = "confirmModal";
    root.tabIndex = -1;
    root.setAttribute("aria-labelledby", "confirmModalLabel");
    root.setAttribute("aria-hidden", "true");

    const dialog = document.createElement("div");
    dialog.className = "modal-dialog modal-dialog-centered";

    const content = document.createElement("div");
    content.className = "modal-content";

    const header = document.createElement("div");
    header.className = "modal-header";

    const title = document.createElement("h5");
    title.className = "modal-title";
    title.id = "confirmModalLabel";

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "btn-close";
    closeBtn.setAttribute("data-bs-dismiss", "modal");
    closeBtn.setAttribute("aria-label", "Close dialog");

    header.append(title, closeBtn);

    const body = document.createElement("div");
    body.className = "modal-body";

    const message = document.createElement("p");
    message.className = "mb-0";
    message.id = "confirmModalMessage";
    body.appendChild(message);

    const footer = document.createElement("div");
    footer.className = "modal-footer";

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-outline-secondary";
    cancelBtn.setAttribute("data-bs-dismiss", "modal");

    const acceptBtn = document.createElement("button");
    acceptBtn.type = "button";
    acceptBtn.className = "btn btn-primary";

    footer.append(cancelBtn, acceptBtn);

    content.append(header, body, footer);
    dialog.appendChild(content);
    root.appendChild(dialog);
    document.body.appendChild(root);

    acceptBtn.addEventListener("click", () => {
        settle(true);
        modalInstance?.hide();
    });

    // The confirm button leads for a safe action, the cancel button for a
    // destructive one - set per call, applied once the dialog is on screen.
    root.addEventListener("shown.bs.modal", () => {
        focusTarget?.focus();
    });

    // Covers the cancel button, the close icon, Escape and backdrop clicks -
    // every path that is not an explicit confirm resolves to false.
    root.addEventListener("hidden.bs.modal", () => {
        settle(false);
        restoreFocus();
    });

    return { root, title, message, acceptBtn, cancelBtn };
}

/**
 * Ask the user to confirm an action.
 *
 * @param {string | {
 *   message: string,
 *   title?: string,
 *   confirmText?: string,
 *   cancelText?: string,
 *   variant?: "primary" | "danger" | "warning" | "success",
 * }} options - The message, or an options object.
 * @returns {Promise<boolean>} Resolves true when confirmed, false otherwise.
 */
export function confirmModal(options) {
    const config = typeof options === "string" ? { message: options } : { ...options };
    const {
        message = "",
        title = "Please confirm",
        confirmText = "OK",
        cancelText = "Cancel",
        variant = "primary",
    } = config;

    // If Bootstrap failed to load (script blocked, stale cache), a native
    // prompt still beats swallowing the action or blocking the user outright.
    if (typeof bootstrap === "undefined" || !bootstrap?.Modal) {
        return Promise.resolve(window.confirm(message));
    }

    if (!els) {
        els = buildModal();
        modalInstance = bootstrap.Modal.getOrCreateInstance(els.root);
    }

    // A prior dialog still open: treat it as cancelled before reusing the node.
    settle(false);

    els.title.textContent = title;
    els.message.textContent = message;
    els.acceptBtn.textContent = confirmText;
    els.cancelBtn.textContent = cancelText;

    const acceptVariant = ACCEPT_VARIANTS.has(variant) ? variant : "primary";
    els.acceptBtn.className = `btn btn-${acceptVariant}`;

    // A destructive action opens with the cancel button focused so a stray
    // Enter cannot trigger it; a safe action lets the confirm button lead.
    focusTarget = SAFE_ACCEPT_VARIANTS.has(acceptVariant) ? els.acceptBtn : els.cancelBtn;
    lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    return new Promise((resolve) => {
        pendingResolve = resolve;
        modalInstance.show();
    });
}
