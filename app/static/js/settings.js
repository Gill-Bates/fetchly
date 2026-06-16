//
// app/static/js/settings.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { showToast } from "./toast.js";
import { getCookie } from "./utils.js";

const AUTO_SAVE_DELAY_MS = 800;
const REQUEST_TIMEOUT_MS = 10_000;
const CSRF_COOKIE_NAME = "tubeyou_csrf";

const AUTO_SAVE_STATE = Object.freeze({
    HIDDEN: "hidden",
    SAVING: "saving",
    SUCCESS: "success",
    ERROR: "error",
});

const TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
});

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
    const token = getCookie(CSRF_COOKIE_NAME);
    if (!token) {
        throw new Error("CSRF token missing — please reload the page");
    }
    return token;
}

function isSafeLocalRedirect(url) {
    if (typeof url !== "string" || !url) return false;
    try {
        return new URL(url, window.location.origin).origin === window.location.origin;
    } catch {
        return false;
    }
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

function randomHexPlaceholder() {
    if (!window.crypto?.getRandomValues) {
        return "";
    }

    return Array.from(window.crypto.getRandomValues(new Uint8Array(8)))
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
}

const bootstrapData = getBootstrapData();

const formEl = document.getElementById("settingsForm");
const alertEl = document.getElementById("settingsAlert");
const settingsSaveBtn = document.getElementById("settingsSaveBtn");
const autoSaveIndicator = document.getElementById("autoSaveIndicator");
const autoSaveIndicatorText = document.getElementById("autoSaveIndicatorText");
const autoSaveSpinner = document.getElementById("autoSaveSpinner");
const passwordSaveBtn = document.getElementById("passwordSaveBtn");
const currentPasswordEl = document.getElementById("currentPassword");
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

let saveTimeoutId = null;
let isSaving = false;
let pendingSave = false;
let isAuthUseKeyBusy = false;

function setAutoSaveState(state, message = "") {
    if (!autoSaveIndicator || !autoSaveIndicatorText || !autoSaveSpinner) {
        return;
    }

    autoSaveIndicator.classList.remove(
        "auto-save-indicator--visible",
        "auto-save-indicator--success",
        "auto-save-indicator--danger",
    );
    autoSaveSpinner.classList.add("d-none");

    switch (state) {
        case AUTO_SAVE_STATE.SAVING:
            autoSaveIndicator.classList.add("auto-save-indicator--visible");
            autoSaveSpinner.classList.remove("d-none");
            autoSaveIndicatorText.textContent = message || "Saving...";
            break;
        case AUTO_SAVE_STATE.SUCCESS:
            autoSaveIndicator.classList.add("auto-save-indicator--visible", "auto-save-indicator--success");
            autoSaveIndicatorText.textContent = message || `Saved ${TIME_FORMATTER.format(new Date())}`;
            break;
        case AUTO_SAVE_STATE.ERROR:
            autoSaveIndicator.classList.add("auto-save-indicator--visible", "auto-save-indicator--danger");
            autoSaveIndicatorText.textContent = message || "Could not save";
            break;
        default:
            autoSaveIndicatorText.textContent = "";
            break;
    }
}

function renderAuthUseKeyButton() {
    if (!lalalAuthUseKeyBtn || !lalalAuthUseKeySpinner) return;
    lalalAuthUseKeyBtn.disabled = isAuthUseKeyBusy;
    lalalAuthUseKeySpinner.classList.toggle("d-none", !isAuthUseKeyBusy);
}

function updateLalalAuthButtonLabel(statusText) {
    if (!lalalAuthBtnLabel) return;
    const normalized = String(statusText || "").trim().toLowerCase();
    const shouldReconnect = normalized.startsWith("connected") || normalized === "token invalid";
    lalalAuthBtnLabel.textContent = shouldReconnect ? "Reconnect" : "Authenticate";
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

    return {
        valid: true,
        data: {
            retention_days: retention,
            lalalaai_duration_guard: lalalDurationGuard ? lalalDurationGuard.checked : true,
        },
    };
}

function validatePasswordChange() {
    const currentPassword = currentPasswordEl?.value || "";
    const nextPassword = adminPasswordEl?.value || "";
    const confirmPassword = adminPasswordConfirmEl?.value || "";

    if (!currentPassword) {
        return { valid: false, error: "Current password is required" };
    }
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
            current_password: currentPassword,
            admin_password: nextPassword,
        },
    };
}

function setPasswordError(message) {
    if (!adminPasswordEl || !adminPasswordConfirmEl || !currentPasswordEl || !passwordErrorEl) {
        return;
    }

    if (message) {
        passwordErrorEl.textContent = message;
        passwordErrorEl.classList.remove("d-none");
        adminPasswordConfirmEl.classList.add("is-invalid");
        adminPasswordEl.classList.add("is-invalid");
        currentPasswordEl.classList.toggle("is-invalid", message.toLowerCase().includes("current password"));
        return;
    }

    passwordErrorEl.classList.add("d-none");
    passwordErrorEl.textContent = "";
    adminPasswordConfirmEl.classList.remove("is-invalid");
    adminPasswordEl.classList.remove("is-invalid");
    currentPasswordEl.classList.remove("is-invalid");

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
        setAutoSaveState(AUTO_SAVE_STATE.ERROR, validation.error);
        return;
    }

    isSaving = true;
    pendingSave = false;
    setAutoSaveState(AUTO_SAVE_STATE.SAVING);
    clearAlert(alertEl);

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

        setAutoSaveState(AUTO_SAVE_STATE.SUCCESS, `Saved ${TIME_FORMATTER.format(new Date())}`);
    } catch (err) {
        const message = err?.name === "AbortError"
            ? "Request timed out"
            : (err?.message || "Could not save");
        setAutoSaveState(AUTO_SAVE_STATE.ERROR, message);
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

        if (currentPasswordEl) currentPasswordEl.value = "";
        if (adminPasswordEl) adminPasswordEl.value = "";
        if (adminPasswordConfirmEl) adminPasswordConfirmEl.value = "";
        setPasswordError("");

        if (payload.redirect && isSafeLocalRedirect(payload.redirect)) {
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

function bindSettingsInputs() {
    formEl?.querySelectorAll("input, select, textarea").forEach((input) => {
        if (input === currentPasswordEl || input === adminPasswordEl || input === adminPasswordConfirmEl) {
            return;
        }

        if (input.type === "checkbox") {
            input.addEventListener("change", () => scheduleAutoSave());
            return;
        }

        input.addEventListener("input", () => scheduleAutoSave());
        input.addEventListener("change", () => scheduleAutoSave());
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

function init() {
    if (!formEl) {
        return;
    }

    applyInitialLalalState();
    setAutoSaveState(AUTO_SAVE_STATE.HIDDEN);
    renderAuthUseKeyButton();
    updateLalalAuthButtonLabel(lalalStatusBadge?.textContent || bootstrapData.lalal_status);
    void loadLalalStatus();

    adminPasswordEl?.addEventListener("input", validatePasswordFields);
    adminPasswordConfirmEl?.addEventListener("input", validatePasswordFields);
    currentPasswordEl?.addEventListener("input", () => {
        currentPasswordEl.classList.remove("is-invalid");
    });
    passwordSaveBtn?.addEventListener("click", () => {
        void changePassword();
    });

    bindSettingsInputs();
    bindLalalEvents();

    settingsSaveBtn?.addEventListener("click", () => {
        if (saveTimeoutId) {
            clearTimeout(saveTimeoutId);
            saveTimeoutId = null;
        }
        void saveSettings();
    });

    formEl.addEventListener("submit", (event) => {
        event.preventDefault();
    });
}

init();