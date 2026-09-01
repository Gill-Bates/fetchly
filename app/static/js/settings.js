//
// app/static/js/settings.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { showToast } from "./toast.js";
import { getCsrfToken, isSafeSameOriginRedirect } from "./utils.js";

const AUTO_SAVE_DELAY_MS = 800;
const REQUEST_TIMEOUT_MS = 10_000;

function getBootstrapData() {
    const node = document.getElementById("settingsBootstrapData");
    if (!node) {
        return {
            lalal_configured: false,
            lalal_status: "Not configured",
            lalal_email: "",
        };
    }

    try {
        const payload = JSON.parse(node.textContent || "{}");
        return {
            lalal_configured: Boolean(payload?.lalal_configured),
            lalal_status: String(payload?.lalal_status || "Not configured"),
            lalal_email: String(payload?.lalal_email || ""),
        };
    } catch {
        return {
            lalal_configured: false,
            lalal_status: "Not configured",
            lalal_email: "",
        };
    }
}

function requireCsrfToken() {
    const token = getCsrfToken();
    if (!token) {
        throw new Error("CSRF token missing — please reload the page");
    }
    return token;
}

function setAlert(el, type, message) {
    if (!el) return;
    el.classList.remove("d-none", "alert-success", "alert-danger", "alert-warning", "alert-info");
    el.classList.add("alert", `alert-${type}`);
    el.textContent = message;
}

function clearAlert(el) {
    if (!el) return;
    el.classList.add("d-none");
    el.textContent = "";
}

async function parseResponsePayload(res) {
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
        return {};
    }

    return res.json().catch(() => ({}));
}

function createRequestController() {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    return { controller, timeoutId };
}

async function fetchWithTimeout(url, options = {}) {
    const { controller, timeoutId } = createRequestController();
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } finally {
        clearTimeout(timeoutId);
    }
}

const bootstrapData = getBootstrapData();

const formEl = document.getElementById("settingsForm");
const resetStatsBtn = document.getElementById("resetStatsBtn");
const passwordSaveBtn = document.getElementById("passwordSaveBtn");
const adminPasswordEl = document.getElementById("adminPassword");
const adminPasswordConfirmEl = document.getElementById("adminPasswordConfirm");
const passwordErrorEl = document.getElementById("passwordError");
const lalalStatusBadge = document.getElementById("lalalStatusBadge");
const lalalStatusLine = document.getElementById("lalalStatusLine");
const lalalAuthBtnLabel = document.getElementById("lalalAuthBtnLabel");
const lalalDisconnectBtn = document.getElementById("lalalDisconnectBtn");
const lalalAuthModal = document.getElementById("lalalAuthModal");
const lalalAuthAlert = document.getElementById("lalalAuthAlert");
const lalalAuthStep1 = document.getElementById("lalalAuthStep1");
const lalalAuthStep3 = document.getElementById("lalalAuthStep3");
const lalalAuthEmail = document.getElementById("lalalAuthEmail");
const lalalAuthUseKeyBtn = document.getElementById("lalalAuthUseKeyBtn");
const lalalAuthUseKeySpinner = document.getElementById("lalalAuthUseKeySpinner");
const lalalActivationKey = document.getElementById("lalalActivationKey");
const lalalDurationGuard = document.getElementById("lalalDurationGuard");
const mp4PresetEl = document.getElementById("mp4Preset");
const enableAuthenticationEl = document.getElementById("enableAuthentication");

let saveTimeoutId = null;
let isSaving = false;
let pendingSave = false;
let settingsDirty = false;
let isAuthUseKeyBusy = false;
let isResettingStats = false;

function renderAuthUseKeyButton() {
    if (!lalalAuthUseKeyBtn || !lalalAuthUseKeySpinner) return;
    lalalAuthUseKeyBtn.disabled = isAuthUseKeyBusy;
    lalalAuthUseKeySpinner.classList.toggle("d-none", !isAuthUseKeyBusy);
}

function updateLalalAuthButtonLabel(statusText) {
    if (!lalalAuthBtnLabel) return;
    const normalized = String(statusText || "").trim().toLowerCase();
    const shouldReconnect = normalized.startsWith("connected") || normalized === "token invalid";
    lalalAuthBtnLabel.textContent = shouldReconnect ? "Reconnect" : "Click to Connect";
}

function setLalalStatus(text, type = "secondary") {
    if (lalalStatusBadge) {
        lalalStatusBadge.className = `badge bg-${type}`;
        lalalStatusBadge.textContent = text;
    }
    updateLalalAuthButtonLabel(text);
}

function setDisconnectVisible(visible) {
    if (!lalalDisconnectBtn) return;
    lalalDisconnectBtn.classList.toggle("d-none", !visible);
    lalalDisconnectBtn.disabled = !visible;
}

function setLalalStatusLine(text) {
    if (lalalStatusLine) {
        lalalStatusLine.textContent = text;
    }
}

function renderLalalStatusLine({ email, remainingMinutes }) {
    if (!lalalStatusLine) return;

    lalalStatusLine.replaceChildren();

    if (email) {
        lalalStatusLine.append("Logged in as ");

        const code = document.createElement("code");
        code.className = "lalal-status-email";
        code.textContent = email;
        lalalStatusLine.appendChild(code);
    } else {
        lalalStatusLine.append("Session active");
    }

    if (remainingMinutes != null) {
        const minutes = document.createElement("span");
        minutes.className = "lalal-status-minutes";
        minutes.textContent = ` - ${remainingMinutes} min left`;
        lalalStatusLine.appendChild(minutes);
    }
}

function applyInitialLalalState() {
    setLalalStatus(
        bootstrapData.lalal_status,
        bootstrapData.lalal_configured ? "success" : "secondary",
    );

    if (bootstrapData.lalal_configured) {
        if (bootstrapData.lalal_email) {
            renderLalalStatusLine({ email: bootstrapData.lalal_email, remainingMinutes: null });
        } else {
            setLalalStatusLine("Session active");
        }
    } else {
        setLalalStatusLine("Click 'Authenticate' to connect your Lalal.ai account");
    }

    setDisconnectVisible(bootstrapData.lalal_configured);
}

function showAuthStep(step) {
    lalalAuthStep1?.classList.toggle("d-none", step !== 1);
    lalalAuthStep3?.classList.toggle("d-none", step !== 3);
    clearAlert(lalalAuthAlert);
}

function setAuthAlert(type, message) {
    setAlert(lalalAuthAlert, type, message);
}

async function loadLalalStatus() {
    try {
        const res = await fetchWithTimeout("/api/lalal/status?force_refresh=1", { credentials: "same-origin" });
        const payload = await parseResponsePayload(res);

        if (!res.ok) {
            setLalalStatus("Error", "danger");
            setLalalStatusLine(payload.detail || "Unable to check status");
            setDisconnectVisible(false);
            return;
        }

        if (payload.configured && payload.token_valid === false) {
            setLalalStatus("Token invalid", "danger");
            setLalalStatusLine(payload.validation_error || "Stored Lalal token is invalid. Please authenticate again.");
            setDisconnectVisible(true);
            return;
        }

        if (payload.configured) {
            setLalalStatus("Connected", "success");
            renderLalalStatusLine({
                email: payload.email || "",
                remainingMinutes: payload.remaining_minutes,
            });
            setDisconnectVisible(true);
            return;
        }

        setLalalStatus("Not configured", "secondary");
        setLalalStatusLine("Click 'Authenticate' to connect your Lalal.ai account");
        setDisconnectVisible(false);
    } catch (err) {
        setLalalStatus("Error", "danger");
        setLalalStatusLine(err?.message || "Unable to check status");
        setDisconnectVisible(false);
    }
}

function validateSettings() {
    const form = new FormData(formEl);
    const retention = parseInt(String(form.get("retention_days") || ""), 10);
    if (!Number.isFinite(retention) || retention < 1 || retention > 365) {
        return { valid: false, error: "Retention must be between 1 and 365 days" };
    }

    const fragments = parseInt(String(form.get("download_concurrent_fragments") || ""), 10);
    if (!Number.isFinite(fragments) || fragments < 1 || fragments > 16) {
        return { valid: false, error: "Parallel fragments must be between 1 and 16" };
    }

    const shareMaxUses = parseInt(String(form.get("share_link_max_uses") || "0"), 10);
    if (!Number.isFinite(shareMaxUses) || shareMaxUses < 0 || shareMaxUses > 10000) {
        return { valid: false, error: "Max. uses per share link must be between 0 and 10000" };
    }

    return {
        valid: true,
        data: {
            retention_days: retention,
            download_concurrent_fragments: fragments,
            share_link_max_uses: shareMaxUses,
            download_mp4_preset: mp4PresetEl ? mp4PresetEl.checked : true,
            lalalaai_duration_guard: lalalDurationGuard ? lalalDurationGuard.checked : true,
            enable_authentication: enableAuthenticationEl ? enableAuthenticationEl.checked : true,
        },
    };
}

function validatePasswordChange() {
    const nextPassword = adminPasswordEl?.value || "";
    const confirmPassword = adminPasswordConfirmEl?.value || "";

    if (!nextPassword) {
        return { valid: false, error: "New password is required" };
    }
    if (nextPassword.length < 8) {
        return { valid: false, error: "Password must be at least 8 characters" };
    }
    if (nextPassword !== confirmPassword) {
        return { valid: false, error: "Passwords do not match" };
    }

    return {
        valid: true,
        data: {
            admin_password: nextPassword,
        },
    };
}

function setPasswordError(message) {
    if (!adminPasswordEl || !adminPasswordConfirmEl || !passwordErrorEl) {
        return;
    }

    if (message) {
        passwordErrorEl.textContent = message;
        passwordErrorEl.classList.remove("d-none");
        adminPasswordConfirmEl.classList.add("is-invalid");
        adminPasswordEl.classList.add("is-invalid");
        return;
    }

    passwordErrorEl.classList.add("d-none");
    passwordErrorEl.textContent = "";
    adminPasswordConfirmEl.classList.remove("is-invalid");
    adminPasswordEl.classList.remove("is-invalid");

    const pw = adminPasswordEl.value;
    const confirm = adminPasswordConfirmEl.value;
    const isValid = pw.length >= 8 && confirm.length > 0 && pw === confirm;
    adminPasswordEl.classList.toggle("is-valid", isValid);
    adminPasswordConfirmEl.classList.toggle("is-valid", isValid);
}

function validatePasswordFields() {
    const password = adminPasswordEl?.value || "";
    const confirmation = adminPasswordConfirmEl?.value || "";

    if (!password && !confirmation) {
        setPasswordError("");
        return;
    }
    if (password && password.length < 8) {
        setPasswordError("Password must be at least 8 characters");
        return;
    }
    if (confirmation && password !== confirmation) {
        setPasswordError("Passwords do not match");
        return;
    }

    setPasswordError("");
}

function scheduleAutoSave(delay = AUTO_SAVE_DELAY_MS) {
    settingsDirty = true;
    if (saveTimeoutId) {
        clearTimeout(saveTimeoutId);
    }

    saveTimeoutId = window.setTimeout(() => {
        saveTimeoutId = null;
        void saveSettings();
    }, delay);
}

async function saveSettings() {
    if (isSaving) {
        pendingSave = true;
        return;
    }

    const validation = validateSettings();
    if (!validation.valid) {
        showToast(validation.error, "danger");
        return;
    }

    isSaving = true;
    pendingSave = false;

    try {
        const res = await fetchWithTimeout("/api/settings", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": requireCsrfToken(),
            },
            body: JSON.stringify(validation.data),
        });

        const payload = await parseResponsePayload(res);
        if (!res.ok) {
            throw new Error(payload.detail || `HTTP ${res.status}`);
        }

        if (!pendingSave) {
            settingsDirty = false;
            showToast(payload.message || "Settings updated", "success");
        }
    } catch (err) {
        const message = err?.name === "AbortError"
            ? "Request timed out"
            : (err?.message || "Could not save");
        showToast(`Error: ${message}`, "danger");
    } finally {
        isSaving = false;
        if (pendingSave) {
            pendingSave = false;
            scheduleAutoSave(150);
        }
    }
}

async function changePassword() {
    const validation = validatePasswordChange();
    if (!validation.valid) {
        setPasswordError(validation.error);
        showToast(validation.error, "danger");
        return;
    }

    if (passwordSaveBtn) {
        passwordSaveBtn.disabled = true;
    }
    setPasswordError("");

    try {
        const res = await fetchWithTimeout("/api/settings", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": requireCsrfToken(),
            },
            body: JSON.stringify(validation.data),
        });

        const payload = await parseResponsePayload(res);
        if (!res.ok) {
            throw new Error(payload.detail || `HTTP ${res.status}`);
        }

        if (adminPasswordEl) adminPasswordEl.value = "";
        if (adminPasswordConfirmEl) adminPasswordConfirmEl.value = "";
        setPasswordError("");

        if (payload.redirect && isSafeSameOriginRedirect(payload.redirect)) {
            window.location.replace(payload.redirect);
            return;
        }

        showToast(payload.message || "Password updated", "success");
    } catch (err) {
        const message = err?.name === "AbortError" ? "Request timed out" : (err?.message || "Request failed");
        setPasswordError(message);
        showToast(`Error: ${message}`, "danger");
    } finally {
        if (passwordSaveBtn) {
            passwordSaveBtn.disabled = false;
        }
    }
}

async function resetStatistics() {
    if (isResettingStats || !window.confirm("Reset all dashboard statistics to 0?")) {
        return;
    }

    isResettingStats = true;
    if (resetStatsBtn) {
        resetStatsBtn.disabled = true;
    }

    try {
        const res = await fetchWithTimeout("/api/stats/reset", {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": requireCsrfToken(),
            },
        });

        const payload = await parseResponsePayload(res);
        if (!res.ok) {
            throw new Error(payload.detail || `HTTP ${res.status}`);
        }

        showToast(payload.message || "Statistics reset", "success");
    } catch (err) {
        const message = err?.name === "AbortError" ? "Request timed out" : (err?.message || "Request failed");
        showToast(`Error: ${message}`, "danger");
    } finally {
        isResettingStats = false;
        if (resetStatsBtn) {
            resetStatsBtn.disabled = false;
        }
    }
}

function bindPasswordVisibilityToggles() {
    document.querySelectorAll(".password-toggle-btn").forEach((btn) => {
        const input = document.getElementById(btn.dataset.target);
        if (!input) return;

        btn.addEventListener("click", () => {
            const showing = input.type === "text";
            input.type = showing ? "password" : "text";
            btn.setAttribute("aria-pressed", String(!showing));
            btn.setAttribute("aria-label", showing ? "Show password" : "Hide password");
            btn.querySelector(".material-symbols-outlined").textContent = showing
                ? "visibility"
                : "visibility_off";
        });
    });
}

window.addEventListener("beforeunload", (event) => {
    if (!settingsDirty && !isSaving && saveTimeoutId === null) {
        return;
    }
    event.preventDefault();
    event.returnValue = "";
});

function clearSensitiveInputs() {
    if (adminPasswordEl) adminPasswordEl.value = "";
    if (adminPasswordConfirmEl) adminPasswordConfirmEl.value = "";
    if (lalalActivationKey) lalalActivationKey.value = "";
}

function persistPendingSettingsOnPageHide() {
    clearSensitiveInputs();

    if (saveTimeoutId !== null) {
        clearTimeout(saveTimeoutId);
        saveTimeoutId = null;
    }

    // A regular save is already writing the same payload - a second concurrent
    // POST would only add a competing write transaction.
    if (!settingsDirty || isSaving) {
        return;
    }

    const validation = validateSettings();
    const csrfToken = getCsrfToken();
    if (!validation.valid || !csrfToken) {
        return;
    }

    // iOS fires pagehide on every tab switch. Clearing the flag up front keeps
    // a suspended-and-resumed page from re-sending the same payload each time.
    settingsDirty = false;

    // iOS/Safari may suspend the page before the debounced save runs. The
    // request is intentionally small and idempotent so keepalive can finish
    // it during navigation or BFCache entry.
    void fetch("/api/settings", {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(validation.data),
    }).catch(() => {
        // Navigation is already in progress; the normal page state cannot be
        // updated reliably here. Restore the flag so a restored page retries.
        settingsDirty = true;
    });
}

window.addEventListener("pagehide", persistPendingSettingsOnPageHide);

function bindSettingsInputs() {
    formEl?.querySelectorAll("input, select, textarea").forEach((input) => {
        // The password fields and the authentication switch are never
        // auto-saved: they have their own explicit confirmation path.
        if (input === adminPasswordEl
            || input === adminPasswordConfirmEl
            || input === enableAuthenticationEl) {
            return;
        }

        if (input.type === "checkbox") {
            input.addEventListener("change", () => scheduleAutoSave());
            return;
        }

        input.addEventListener("input", () => scheduleAutoSave());
        input.addEventListener("change", () => scheduleAutoSave());
    });

    enableAuthenticationEl?.addEventListener("change", () => {
        // Turning authentication off exposes the whole instance to anyone who
        // can reach it, so a stray click must not be enough to do it.
        if (!enableAuthenticationEl.checked
            && !window.confirm(
                "Disable authentication? Everyone who can reach this server will have full access.",
            )) {
            enableAuthenticationEl.checked = true;
            return;
        }

        scheduleAutoSave(0);
    });
}

function bindLalalEvents() {
    lalalDisconnectBtn?.addEventListener("click", async () => {
        lalalDisconnectBtn.disabled = true;
        clearAlert(lalalAuthAlert);

        try {
            const res = await fetchWithTimeout("/api/lalal/auth/logout", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "X-CSRF-Token": requireCsrfToken(),
                },
            });

            const payload = await parseResponsePayload(res);
            if (!res.ok) {
                throw new Error(payload.detail || `HTTP ${res.status}`);
            }

            setLalalStatus("Not configured", "secondary");
            setLalalStatusLine("Session disconnected. Click 'Authenticate' to reconnect.");
            setDisconnectVisible(false);
            showToast("Lalal.ai session disconnected", "success");
        } catch (err) {
            showToast(err?.message || "Failed to disconnect Lalal.ai session", "danger");
            setDisconnectVisible(true);
        } finally {
            lalalDisconnectBtn.disabled = false;
        }
    });

    lalalAuthUseKeyBtn?.addEventListener("click", async () => {
        if (isAuthUseKeyBusy) {
            return;
        }

        const email = lalalAuthEmail?.value.trim() || "";
        const activationKey = lalalActivationKey?.value.trim() || "";

        if (!email || !email.includes("@")) {
            setAuthAlert("danger", "Please enter a valid email address");
            return;
        }
        if (!activationKey) {
            setAuthAlert("danger", "Please enter a valid activation key");
            return;
        }

        isAuthUseKeyBusy = true;
        renderAuthUseKeyButton();
        clearAlert(lalalAuthAlert);

        try {
            const res = await fetchWithTimeout("/api/lalal/auth/activation-key", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": requireCsrfToken(),
                },
                body: JSON.stringify({
                    email,
                    activation_key: activationKey,
                }),
            });

            const payload = await parseResponsePayload(res);
            if (!res.ok) {
                throw new Error(payload.detail || `HTTP ${res.status}`);
            }

            if (lalalActivationKey) lalalActivationKey.value = "";
            showAuthStep(3);
            void loadLalalStatus();
            window.setTimeout(() => {
                bootstrap.Modal.getInstance(lalalAuthModal)?.hide();
            }, 2000);
        } catch (err) {
            setAuthAlert("danger", err?.message || "Invalid activation key");
        } finally {
            isAuthUseKeyBusy = false;
            renderAuthUseKeyButton();
        }
    });

    lalalAuthModal?.addEventListener("show.bs.modal", () => {
        showAuthStep(1);
        if (lalalAuthEmail) lalalAuthEmail.value = "";
        if (lalalActivationKey) {
            lalalActivationKey.value = "";
            lalalActivationKey.placeholder = "Paste activation key";
        }
        renderAuthUseKeyButton();
    });

    lalalAuthModal?.addEventListener("hidden.bs.modal", clearSensitiveInputs);

    lalalAuthModal?.addEventListener("shown.bs.modal", () => {
        lalalAuthEmail?.focus();
    });

    lalalAuthEmail?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            lalalAuthUseKeyBtn?.click();
        }
    });

    lalalActivationKey?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            lalalAuthUseKeyBtn?.click();
        }
    });
}

const APP_UPDATE_STATES = {
    loading: { icon: "progress_activity", spin: true, text: "Checking for updates…" },
    current: { icon: "check_circle", text: "You're running the latest version" },
    available: { icon: "new_releases", text: "Update available" },
    prerelease: { icon: "science", text: "You're on a pre-release build" },
    unknown: { icon: "cloud_off", text: "Update check unavailable" },
};

function parseVersionParts(value) {
    const match = String(value || "").trim().replace(/^[vn]/i, "").match(/^\d+(?:\.\d+)*/);
    if (!match) return null;
    return match[0].split(".").map((part) => Number.parseInt(part, 10) || 0);
}

function formatVersion(value) {
    const raw = String(value || "").trim();
    return raw ? raw.replace(/^v?/i, "v") : "";
}

function compareVersions(a, b) {
    const pa = parseVersionParts(a);
    const pb = parseVersionParts(b);
    if (!pa || !pb) return 0;
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i += 1) {
        const diff = (pa[i] || 0) - (pb[i] || 0);
        if (diff !== 0) return diff > 0 ? 1 : -1;
    }
    return 0;
}

function setAppUpdateState(box, state, { text, url } = {}) {
    if (!box) return;

    const preset = APP_UPDATE_STATES[state] || APP_UPDATE_STATES.unknown;
    const iconEl = box.querySelector("[data-app-update-icon]");
    const textEl = box.querySelector("[data-app-update-text]");
    const linkEl = box.querySelector("[data-app-update-link]");
    const refreshBtn = document.querySelector("[data-app-update-refresh]");

    box.dataset.appUpdateState = state;
    box.setAttribute("aria-busy", String(state === "loading"));

    if (iconEl) {
        iconEl.textContent = preset.icon;
        iconEl.classList.toggle("settings-app-update-spin", Boolean(preset.spin));
    }
    if (textEl) {
        textEl.textContent = text || preset.text;
    }
    if (refreshBtn) {
        refreshBtn.disabled = state === "loading";
    }
    if (linkEl) {
        if (state === "available" && url) {
            linkEl.href = url;
            linkEl.hidden = false;
        } else {
            linkEl.hidden = true;
            linkEl.removeAttribute("href");
        }
    }
}

function setAppUpdateVersions(box, current, latest) {
    if (!box) return;

    const currentEl = box.querySelector("[data-app-update-current]");
    const latestEl = box.querySelector("[data-app-update-latest]");

    if (currentEl) {
        currentEl.textContent = formatVersion(current) || "–";
    }
    if (latestEl) {
        latestEl.textContent = formatVersion(latest) || "–";
    }
}

function applyAppUpdate(box, info) {
    if (!box) return;

    const current = box.dataset.currentVersion || "";
    const latest = info?.latest || "";
    const latestLabel = formatVersion(latest);
    setAppUpdateVersions(box, current, latest);

    if (!info || !latest) {
        setAppUpdateState(box, "unknown");
        return;
    }

    if (info.update_available) {
        setAppUpdateState(box, "available", {
            text: `Update available — ${latestLabel} is out`,
            url: info.url,
        });
        return;
    }

    if (current && compareVersions(current, latest) > 0) {
        setAppUpdateState(box, "prerelease", {
            text: `Ahead of the latest release (${latestLabel})`,
        });
        return;
    }

    setAppUpdateState(box, "current", {
        text: `You're running the latest version (${latestLabel})`,
    });
}

function applyUpdateBadge(cell, info) {
    const badge = cell.querySelector("[data-update-badge]");
    if (!badge) return;

    if (!info?.update_available || !info.latest) {
        badge.hidden = true;
        return;
    }

    // Informational only — updating means rebuilding the container image.
    const hint = `Version ${info.latest} is available — rebuild the image to update`;
    badge.href = info.url || "#";
    badge.title = hint;
    badge.setAttribute("aria-label", `${info.label || "Component"}: ${hint}`);
    badge.hidden = false;
}

async function loadUpdateStatus() {
    const cells = document.querySelectorAll("[data-update-component]");
    const appBox = document.querySelector("[data-app-update]");
    if (!cells.length && !appBox) {
        return;
    }

    if (appBox) {
        setAppUpdateVersions(appBox, appBox.dataset.currentVersion, "");
        setAppUpdateState(appBox, "loading");
    }

    try {
        const res = await fetchWithTimeout("/api/updates", { credentials: "same-origin" });
        if (!res.ok) {
            setAppUpdateState(appBox, "unknown");
            return;
        }

        const payload = await parseResponsePayload(res);
        const components = payload?.components;
        if (!components || typeof components !== "object") {
            setAppUpdateState(appBox, "unknown");
            return;
        }

        cells.forEach((cell) => {
            applyUpdateBadge(cell, components[cell.dataset.updateComponent]);
        });
        applyAppUpdate(appBox, components.fetchly);
    } catch {
        // Update checks are best-effort — a failed check simply shows no badge.
        setAppUpdateState(appBox, "unknown");
    }
}

function init() {
    if (!formEl) {
        return;
    }

    applyInitialLalalState();
    renderAuthUseKeyButton();
    updateLalalAuthButtonLabel(lalalStatusBadge?.textContent || bootstrapData.lalal_status);
    void loadLalalStatus();
    void loadUpdateStatus();

    document.querySelector("[data-app-update-refresh]")?.addEventListener("click", () => {
        void loadUpdateStatus();
    });

    adminPasswordEl?.addEventListener("input", validatePasswordFields);
    adminPasswordConfirmEl?.addEventListener("input", validatePasswordFields);
    passwordSaveBtn?.addEventListener("click", () => {
        void changePassword();
    });
    resetStatsBtn?.addEventListener("click", () => {
        void resetStatistics();
    });

    bindSettingsInputs();
    bindLalalEvents();
    bindPasswordVisibilityToggles();

    formEl.addEventListener("submit", (event) => {
        event.preventDefault();
    });
}

init();
