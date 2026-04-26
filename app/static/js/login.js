//
// app/static/js/login.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(function () {
    "use strict";

    const $ = function (id) {
        return document.getElementById(id);
    };

    const form = $("login-form");
    const submitBtn = $("submit-btn");
    const usernameIn = $("username");
    const passwordIn = $("password");
    const alertEl = $("error-alert");

    if (!form || !submitBtn) {
        return;
    }

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content ?? "";
    }

    function setError(msg) {
        if (!alertEl) {
            return;
        }
        if (msg) {
            alertEl.textContent = msg;
            alertEl.classList.remove("d-none");
        } else {
            alertEl.classList.add("d-none");
            alertEl.textContent = "";
        }
    }

    async function submitLogin(event) {
        event.preventDefault();

        if (submitBtn.disabled) {
            return;
        }

        setError(null);

        const username = usernameIn?.value?.trim() ?? "";
        const password = passwordIn?.value ?? "";

        if (!username || !password) {
            setError("Please enter username and password.");
            return;
        }

        submitBtn.disabled = true;
        const originalText = submitBtn.textContent;
        submitBtn.textContent = "Logging in...";

        const controller = new AbortController();
        const timeoutId = setTimeout(function () {
            controller.abort();
        }, 10000);
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
                signal: controller.signal,
            });

            clearTimeout(timeoutId);

            let data = {};
            const contentType = response.headers.get("content-type") || "";
            if (contentType.includes("application/json")) {
                data = await response.json();
            }

            if (!response.ok || !data.ok) {
                setError(data.detail || "Login failed.");
                passwordIn?.focus();
                return;
            }

            success = true;
            window.location.assign(data.redirect || "/");
        } catch (err) {
            clearTimeout(timeoutId);
            if (err?.name === "AbortError") {
                setError("Timeout. Server not responding.");
            } else {
                setError("Network error. Please try again.");
            }
            console.error("Login error:", err);
        } finally {
            if (!success) {
                submitBtn.disabled = false;
                submitBtn.textContent = originalText;
            }
        }
    }

    form.addEventListener("submit", submitLogin);
})();
