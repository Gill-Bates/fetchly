//
// app/static/js/toast.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module toast
 *
 * Central toast notification system for fetchly.
 * Provides accessible, dismissible notifications with a single
 * container attached to document.body.
 */

const TOAST_ICONS = {
    success: "check_circle",
    danger: "error",
    warning: "warning",
    info: "info",
};

const TOAST_DURATION = 3000;
const TOAST_DISMISS_TIMEOUT = 350;

const VALID_TYPES = new Set(Object.keys(TOAST_ICONS));

/** @type {WeakMap<HTMLDivElement, { dismissing: boolean, autoTimerId: number | null, fallbackTimerId: number | null, removed: boolean }>} */
const toastState = new WeakMap();

/** @type {HTMLDivElement | null} */
let container = null;

/**
 * Ensure toast container exists in DOM.
 * @returns {HTMLDivElement}
 */
function getContainer() {
    if (container && document.body.contains(container)) {
        return container;
    }

    // Plain wrapper on purpose: each toast is its own live region via its role
    // (alert = assertive for errors, status = polite otherwise). An aria-live
    // container around them makes screen readers announce the message twice.
    container = document.createElement("div");
    container.id = "toastContainer";
    container.className = "fx-toast-container";
    document.body.appendChild(container);
    return container;
}

/**
 * Get or initialize bookkeeping for a toast element.
 * @param {HTMLDivElement} toast
 */
function getToastState(toast) {
    let state = toastState.get(toast);
    if (!state) {
        state = {
            dismissing: false,
            autoTimerId: null,
            fallbackTimerId: null,
            removed: false,
        };
        toastState.set(toast, state);
    }
    return state;
}

function normalizeToastType(type) {
    if (type === "error") return "danger";
    return VALID_TYPES.has(type) ? type : "info";
}

function removeToast(toast, state) {
    if (state.removed) return;
    state.removed = true;
    state.dismissing = true;

    if (state.autoTimerId !== null) {
        clearTimeout(state.autoTimerId);
        state.autoTimerId = null;
    }
    if (state.fallbackTimerId !== null) {
        clearTimeout(state.fallbackTimerId);
        state.fallbackTimerId = null;
    }

    toast.remove();
}

/**
 * Create a Material Symbols icon span.
 * @param {string} name
 * @returns {HTMLSpanElement}
 */
function icon(name) {
    const span = document.createElement("span");
    span.className = "material-symbols-outlined fx-toast__icon";
    span.setAttribute("aria-hidden", "true");
    span.textContent = name;
    return span;
}

/**
 * Find a currently visible (non-dismissing) toast with the same type and
 * message text.
 * @param {HTMLDivElement} wrapper
 * @param {string} resolvedType
 * @param {string} text
 * @returns {HTMLDivElement | null}
 */
function findActiveToast(wrapper, resolvedType, text) {
    const candidates = wrapper.querySelectorAll(`.fx-toast--${resolvedType}`);
    for (const candidate of candidates) {
        const state = toastState.get(candidate);
        if (state?.dismissing) continue;
        const messageEl = candidate.querySelector(".fx-toast__message");
        if (messageEl?.textContent === text) {
            return candidate;
        }
    }
    return null;
}

/**
 * Show a toast notification.
 *
 * A repeat call with the same type and message while an identical toast is
 * still showing does not stack a duplicate - it restarts that toast's
 * auto-dismiss timer instead. This is what keeps a repeating failure (e.g. an
 * infinite-scroll page load that keeps failing while the sentinel stays in
 * view) from flooding the toast container with copies of the same message.
 * @param {string} message - The message to display
 * @param {"success" | "danger" | "warning" | "info" | "error"} [type="info"] - Toast type
 * @param {number} [duration=3000] - Duration in ms before auto-dismiss
 * @returns {HTMLDivElement} The toast element
 */
export function showToast(message, type = "info", duration = TOAST_DURATION) {
    const wrapper = getContainer();
    const resolvedType = normalizeToastType(type);
    const messageText = typeof message === "string" ? message : String(message ?? "");

    const active = findActiveToast(wrapper, resolvedType, messageText);
    if (active) {
        const activeState = getToastState(active);
        if (activeState.autoTimerId !== null) {
            clearTimeout(activeState.autoTimerId);
            activeState.autoTimerId = null;
        }
        if (duration > 0) {
            activeState.autoTimerId = window.setTimeout(() => dismissToast(active), duration);
        }
        return active;
    }

    const toast = document.createElement("div");
    const state = getToastState(toast);
    toast.className = `fx-toast fx-toast--${resolvedType}`;
    toast.setAttribute("role", resolvedType === "danger" ? "alert" : "status");
    toast.setAttribute("aria-atomic", "true");

    const iconName = TOAST_ICONS[resolvedType] || TOAST_ICONS.info;
    toast.appendChild(icon(iconName));

    const text = document.createElement("span");
    text.className = "fx-toast__message";
    text.textContent = messageText;
    toast.appendChild(text);

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "fx-toast__close";
    closeBtn.setAttribute("aria-label", "Close notification");
    closeBtn.textContent = "×";
    closeBtn.addEventListener("click", () => dismissToast(toast));
    toast.appendChild(closeBtn);

    wrapper.appendChild(toast);

    requestAnimationFrame(() => {
        if (!state.dismissing && toast.isConnected) {
            toast.classList.add("fx-toast--visible");
        }
    });

    if (duration > 0) {
        state.autoTimerId = window.setTimeout(() => dismissToast(toast), duration);
    }

    return toast;
}

/**
 * Dismiss a toast with animation.
 * @param {HTMLDivElement} toast
 */
function dismissToast(toast) {
    if (!toast || !toast.parentNode) return;

    const state = getToastState(toast);
    if (state.dismissing) return;
    state.dismissing = true;

    if (state.autoTimerId !== null) {
        clearTimeout(state.autoTimerId);
        state.autoTimerId = null;
    }

    toast.classList.remove("fx-toast--visible");
    toast.classList.add("fx-toast--hiding");

    const doRemove = () => {
        toast.removeEventListener("transitionend", onTransitionEnd);
        removeToast(toast, state);
    };
    function onTransitionEnd(event) {
        if (event.target !== toast || event.propertyName !== "opacity") {
            return;
        }
        toast.removeEventListener("transitionend", onTransitionEnd);
        doRemove();
    }

    toast.addEventListener("transitionend", onTransitionEnd);

    // Fallback removal if transition doesn't fire
    state.fallbackTimerId = window.setTimeout(doRemove, TOAST_DISMISS_TIMEOUT);
}

/**
 * Dismiss all active toasts.
 * Pending auto-dismiss timers are cancelled with each toast.
 * @returns {void}
 */
export function clearToasts() {
    if (!container || !document.body.contains(container)) {
        return;
    }

    const wrapper = container;
    wrapper.querySelectorAll(".fx-toast").forEach((t) => dismissToast(t));
}

// Convenience aliases
export const toast = {
    success: (msg, duration) => showToast(msg, "success", duration),
    error: (msg, duration) => showToast(msg, "danger", duration),
    warning: (msg, duration) => showToast(msg, "warning", duration),
    info: (msg, duration) => showToast(msg, "info", duration),
};
