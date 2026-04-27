//
// app/static/js/toast.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Central toast notification system for tubeyou.
 * Usage:
 *   import { showToast } from "./toast.js";
 *   showToast("Settings saved", "success");
 *   showToast("Error occurred", "danger");
 */

const TOAST_ICONS = {
    success: "check_circle",
    danger: "error",
    warning: "warning",
    info: "info",
};

const TOAST_DURATION = 3000;

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

    container = document.createElement("div");
    container.id = "toastContainer";
    container.className = "toast-container";
    container.setAttribute("aria-live", "polite");
    container.setAttribute("aria-atomic", "true");
    document.body.appendChild(container);
    return container;
}

/**
 * Create a Material Symbols icon span.
 * @param {string} name
 * @returns {HTMLSpanElement}
 */
function icon(name) {
    const span = document.createElement("span");
    span.className = "material-symbols-outlined toast-icon";
    span.setAttribute("aria-hidden", "true");
    span.textContent = name;
    return span;
}

/**
 * Show a toast notification.
 * @param {string} message - The message to display
 * @param {"success" | "danger" | "warning" | "info"} [type="info"] - Toast type
 * @param {number} [duration=3000] - Duration in ms before auto-dismiss
 * @returns {HTMLDivElement} The toast element
 */
export function showToast(message, type = "info", duration = TOAST_DURATION) {
    const wrapper = getContainer();

    const toast = document.createElement("div");
    toast.className = `toast toast--${type}`;
    toast.setAttribute("role", "alert");

    const iconName = TOAST_ICONS[type] || TOAST_ICONS.info;
    toast.appendChild(icon(iconName));

    const text = document.createElement("span");
    text.className = "toast-message";
    text.textContent = message;
    toast.appendChild(text);

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "toast-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.innerHTML = "&times;";
    closeBtn.addEventListener("click", () => dismissToast(toast));
    toast.appendChild(closeBtn);

    wrapper.appendChild(toast);

    // Trigger reflow for animation
    void toast.offsetWidth;
    toast.classList.add("toast--visible");

    if (duration > 0) {
        setTimeout(() => dismissToast(toast), duration);
    }

    return toast;
}

/**
 * Dismiss a toast with animation.
 * @param {HTMLDivElement} toast
 */
function dismissToast(toast) {
    if (!toast || !toast.parentNode) return;

    toast.classList.remove("toast--visible");
    toast.classList.add("toast--hiding");

    toast.addEventListener("transitionend", () => {
        toast.remove();
    }, { once: true });

    // Fallback removal if transition doesn't fire
    setTimeout(() => toast.remove(), 300);
}

/**
 * Clear all active toasts.
 */
export function clearToasts() {
    const wrapper = getContainer();
    wrapper.querySelectorAll(".toast").forEach((t) => dismissToast(t));
}

// Convenience aliases
export const toast = {
    success: (msg, duration) => showToast(msg, "success", duration),
    error: (msg, duration) => showToast(msg, "danger", duration),
    warning: (msg, duration) => showToast(msg, "warning", duration),
    info: (msg, duration) => showToast(msg, "info", duration),
};

export default { showToast, clearToasts, toast };
