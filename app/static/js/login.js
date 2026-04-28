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

(function () {
    "use strict";

    const form = document.getElementById("login-form");
    const submitBtn = document.getElementById("submit-btn");
    const usernameIn = document.getElementById("username");
    const passwordIn = document.getElementById("password");
    const messageEl = document.getElementById("login-message");
    const messageText = document.getElementById("message-text");

    if (!form || !submitBtn) {
        return;
    }

    /** @type {HTMLMetaElement | null} */
    const csrfMeta = document.querySelector('meta[name="csrf-token"]');

    function csrfToken() {
        const token = csrfMeta?.content ?? "";
        if (!token) {
            console.error(
                "CSRF token missing from <meta name=\"csrf-token\">. "
                + "Login requests will be rejected."
            );
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
            }
        } else {
            messageEl.classList.add("d-none");
            messageEl.classList.remove("error");
            messageText.textContent = "";
        }
    }

    /**
     * Validates that a redirect URL targets the current origin.
     * Rejects external URLs, protocol-relative URLs, and
     * javascript: schemes by parsing via the URL constructor.
     * @param {unknown} url
     * @returns {boolean}
     */
    function isSafeLocalRedirect(url) {
        if (typeof url !== "string" || !url) return false;
        try {
            // Resolve against current origin — rejects external URLs and javascript:
            const parsed = new URL(url, window.location.origin);
            return parsed.origin === window.location.origin;
        } catch {
            return false;
        }
    }

    let isSubmitting = false;

    async function submitLogin(event) {
        event.preventDefault();

        if (isSubmitting) {
            return;
        }
        isSubmitting = true;

        setMessage(null);

        const username = usernameIn?.value?.trim() ?? "";
        const password = passwordIn?.value ?? "";

        if (!username || !password) {
            setMessage("Please enter username and password.", "error");
            isSubmitting = false;
            return;
        }

        submitBtn.disabled = true;
        const originalText = submitBtn.textContent;
        submitBtn.textContent = "Logging in...";
        let success = false;

        try {
            const response = await fetch("/login", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrfToken(),
                },
                body: JSON.stringify({ username, password }),
                signal: AbortSignal.timeout(10_000),
            });

            let data = {};
            const contentType = response.headers.get("content-type") || "";
            if (contentType.includes("application/json")) {
                try {
                    data = await response.json();
                } catch {
                    // Malformed JSON: treat as empty data, rely on HTTP status
                    console.warn("Login response contained invalid JSON");
                }
            }

            // Fail on HTTP error OR explicitly marked failure only.
            if (!response.ok || data.ok === false) {
                setMessage(data.detail || "Login failed.", "error");
                // Focus on the field that likely caused the error
                const focusTarget =
                    data.error_code === "USER_NOT_FOUND"
                        ? usernameIn
                        : passwordIn;
                focusTarget?.focus();
                return;
            }

            success = true;
            const redirect = isSafeLocalRedirect(data.redirect) ? data.redirect : "/";
            window.location.assign(redirect);
        } catch (err) {
            console.error("Login error:", err);
            if (err?.name === "TimeoutError" || err?.name === "AbortError") {
                setMessage(
                    "Request timed out. If this persists, try refreshing the page.",
                    "error"
                );
            } else {
                setMessage("Network error. Please try again.", "error");
            }
        } finally {
            // Always clear the password field to prevent credential exposure
            if (passwordIn) passwordIn.value = "";

            if (!success) {
                isSubmitting = false;
                submitBtn.disabled = false;
                submitBtn.textContent = originalText;
            }
            // On success: isSubmitting stays true to prevent re-entry
        }
    }

    form.addEventListener("submit", submitLogin);

    /**
     * iOS Keyboard Scroll Stabilization
     *
     * On iOS Safari, the virtual keyboard resize causes the viewport to scroll
     * unpredictably, sometimes hiding the focused input field behind the keyboard.
     * This listener detects keyboard open/close via visualViewport height changes
     * and scrolls the active input into view.
     */
    (function () {
        if (!window.visualViewport) return;

        const vv = window.visualViewport;
        let lastHeight = vv.height;
        let keyboardOpen = false;

        /** Delay in ms to let the iOS keyboard animation settle before scrolling. */
        const IOS_KEYBOARD_SETTLE_MS = 50;

        const activeInput = function () {
            return document.activeElement &&
                /input|textarea/i.test(document.activeElement.tagName)
                ? document.activeElement
                : null;
        };

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

        vv.addEventListener("resize", function () {
            const heightDiff = lastHeight - vv.height;
            if (heightDiff > 120) {
                keyboardOpen = true;
                setTimeout(stabilizeScroll, IOS_KEYBOARD_SETTLE_MS);
            }
            if (heightDiff < -120) {
                keyboardOpen = false;
                window.scrollTo({ top: 0, behavior: "instant" });
            }
            lastHeight = vv.height;
        });

        document.addEventListener("focusin", function () {
            if (keyboardOpen) {
                setTimeout(stabilizeScroll, IOS_KEYBOARD_SETTLE_MS);
            }
        });
    })();
})();
