//
// app/static/js/login.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module login
 *
 * Handles the login form: validation, CSRF-protected API submission,
 * redirect-safe navigation, and iOS keyboard scroll stabilization.
 */

import { reportError, reportWarning } from "./errors.js";
import { isSafeSameOriginRedirect } from "./utils.js";

let iosKeyboardController = null;

function initLogin() {
    const form = document.getElementById("login-form");
    const submitBtn = document.getElementById("submit-btn");
    const submitSpinner = document.getElementById("submit-spinner");
    const submitBtnLabel = document.getElementById("submit-btn-label");
    const usernameIn = document.getElementById("username");
    const passwordIn = document.getElementById("password");
    const honeypotIn = document.getElementById("hp-field");
    const captchaTokenIn = document.getElementById("captcha-token");
    const messageEl = document.getElementById("login-message");
    const messageText = document.getElementById("message-text");

    if (!form || !submitBtn || !usernameIn || !passwordIn) {
        return;
    }

    function csrfToken() {
        const token = document.querySelector('input[name="csrf_token"]')?.value
            || document.querySelector('meta[name="csrf-token"]')?.content
            || "";
        if (!token) {
            reportError(new Error("CSRF token missing"), {
                module: "login",
                action: "csrfToken",
            });
        }
        return token;
    }

    function setMessage(msg, type = "info") {
        if (!messageEl || !messageText) return;
        if (msg) {
            messageText.textContent = msg;
            messageEl.classList.remove("d-none", "error");
            if (type === "error") {
                messageEl.classList.add("error");
                messageEl.setAttribute("role", "alert");
                messageEl.setAttribute("aria-live", "assertive");
            } else {
                messageEl.setAttribute("role", "status");
                messageEl.setAttribute("aria-live", "polite");
            }
        } else {
            messageEl.classList.add("d-none");
            messageEl.classList.remove("error");
            messageEl.setAttribute("role", "status");
            messageEl.setAttribute("aria-live", "polite");
            messageText.textContent = "";
        }
    }

    function setFieldInvalid(input, invalid) {
        if (invalid) {
            input.setAttribute("aria-invalid", "true");
        } else {
            input.removeAttribute("aria-invalid");
        }
    }

    function clearFieldErrors() {
        setFieldInvalid(usernameIn, false);
        setFieldInvalid(passwordIn, false);
    }

    let isSubmitting = false;
    const DEFAULT_SUBMIT_LABEL = submitBtnLabel?.textContent ?? "Sign in";

    function clearSensitiveInput() {
        passwordIn.value = "";
    }

    /** Return the form to its idle, interactive state. */
    function resetSubmitState() {
        isSubmitting = false;
        submitBtn.disabled = false;
        usernameIn.disabled = false;
        passwordIn.disabled = false;
        submitBtn.removeAttribute("aria-busy");
        submitSpinner?.classList.add("d-none");
        if (submitBtnLabel) {
            submitBtnLabel.textContent = DEFAULT_SUBMIT_LABEL;
        }
    }

    window.addEventListener("pagehide", clearSensitiveInput);

    async function submitLogin(event) {
        event.preventDefault();

        if (isSubmitting) {
            return;
        }
        isSubmitting = true;

        setMessage(null);
        clearFieldErrors();

        const username = usernameIn.value.trim();
        const password = passwordIn.value;

        if (!username || !password) {
            setFieldInvalid(usernameIn, !username);
            setFieldInvalid(passwordIn, !password);
            setMessage("Please enter username and password.", "error");
            (!username ? usernameIn : passwordIn).focus();
            isSubmitting = false;
            return;
        }

        submitBtn.disabled = true;
        usernameIn.disabled = true;
        passwordIn.disabled = true;
        submitBtn.setAttribute("aria-busy", "true");
        submitSpinner?.classList.remove("d-none");
        if (submitBtnLabel) {
            submitBtnLabel.textContent = "Signing in...";
        }
        let success = false;
        const controller = new AbortController();
        const timeoutId = window.setTimeout(() => controller.abort(), 10_000);
        let focusAfterSubmit = null;

        try {
            const response = await fetch("/login", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrfToken(),
                },
                body: JSON.stringify({
                    username,
                    password,
                    honeypot: honeypotIn ? honeypotIn.value : "",
                    captcha_token: captchaTokenIn ? captchaTokenIn.value : "",
                }),
                signal: controller.signal,
            });

            let data = {};
            const contentType = response.headers.get("content-type") || "";
            if (contentType.includes("application/json")) {
                try {
                    data = await response.json();
                } catch {
                    reportWarning("Login response contained invalid JSON", {
                        module: "login",
                        action: "submitLogin",
                    });
                }
            }

            if (response.status === 429) {
                setMessage("Too many login attempts. Please wait a minute and try again.", "error");
                focusAfterSubmit = passwordIn;
                return;
            }

            if (response.status === 401) {
                setFieldInvalid(passwordIn, true);
                setMessage("Invalid username or password.", "error");
                focusAfterSubmit = passwordIn;
                return;
            }

            if (response.status === 403) {
                setMessage("The login page expired. Reload the page and try again.", "error");
                return;
            }

            if (response.status === 400) {
                setMessage(
                    data.detail || "We couldn't verify your submission. Please reload the page and try again.",
                    "error",
                );
                return;
            }

            if (!response.ok) {
                setMessage("Login service unavailable. Please try again later.", "error");
                return;
            }

            if (data.ok === false) {
                setFieldInvalid(passwordIn, true);
                setMessage("Invalid username or password.", "error");
                focusAfterSubmit = passwordIn;
                return;
            }

            success = true;
            clearSensitiveInput();
            const redirect = isSafeSameOriginRedirect(data.redirect) ? data.redirect : "/";
            window.location.assign(redirect);
        } catch (err) {
            reportError(err, {
                module: "login",
                action: "submitLogin",
            });
            if (err?.name === "AbortError") {
                setMessage(
                    "Request timed out. Check your connection and try again.",
                    "error",
                );
            } else {
                setMessage("Network error. Please try again.", "error");
            }
        } finally {
            window.clearTimeout(timeoutId);

            if (!success) {
                if (passwordIn) passwordIn.value = "";
                resetSubmitState();
                focusAfterSubmit?.focus();
            }
        }
    }

    form.addEventListener("submit", submitLogin);
    usernameIn.addEventListener("input", () => setFieldInvalid(usernameIn, false));
    passwordIn.addEventListener("input", () => setFieldInvalid(passwordIn, false));

    function setupIOSKeyboardStabilization() {
        if (!window.visualViewport) return;

        iosKeyboardController?.abort();
        iosKeyboardController = new AbortController();
        const { signal } = iosKeyboardController;

        const vv = window.visualViewport;
        let lastHeight = vv.height;
        let keyboardOpen = false;
        let scrollYBeforeKeyboard = window.scrollY;

        /** Delay in ms to let the iOS keyboard animation settle before scrolling. */
        const IOS_KEYBOARD_SETTLE_MS = 50;

        function activeInput() {
            return document.activeElement &&
                /input|textarea/i.test(document.activeElement.tagName)
                ? document.activeElement
                : null;
        }

        function stabilizeScroll() {
            const el = activeInput();
            if (!el) return;
            const rect = el.getBoundingClientRect();
            if (rect.bottom > vv.height - 20) {
                window.scrollBy({ top: rect.bottom - vv.height + 20, behavior: "instant" });
            }
            if (rect.top < 20) {
                window.scrollBy({ top: rect.top - 20, behavior: "instant" });
            }
        }

        function onViewportResize() {
            const heightDiff = lastHeight - vv.height;
            if (heightDiff > 120) {
                keyboardOpen = true;
                scrollYBeforeKeyboard = window.scrollY;
                setTimeout(stabilizeScroll, IOS_KEYBOARD_SETTLE_MS);
            }
            if (heightDiff < -120) {
                keyboardOpen = false;
                window.scrollTo({ top: scrollYBeforeKeyboard, behavior: "instant" });
            }
            lastHeight = vv.height;
        }

        function onFocusIn() {
            if (keyboardOpen) {
                setTimeout(stabilizeScroll, IOS_KEYBOARD_SETTLE_MS);
            }
        }

        vv.addEventListener("resize", onViewportResize, { signal });
        document.addEventListener("focusin", onFocusIn, { signal });
        window.addEventListener("pagehide", () => iosKeyboardController?.abort(), { once: true, signal });
    }

    setupIOSKeyboardStabilization();
    window.addEventListener("pageshow", (event) => {
        if (!event.persisted) {
            return;
        }
        // A successful login deliberately leaves the form locked while the
        // redirect runs. Coming back through the BFCache restores that locked
        // state, so the form has to be re-armed here.
        resetSubmitState();
        setupIOSKeyboardStabilization();
    });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initLogin, { once: true });
} else {
    initLogin();
}
