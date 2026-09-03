//
// app/static/js/settings.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { showToast } from "./toast.js";
import { confirmModal } from "./confirm.js";
import { openCookiePasteDialog } from "./cookie-paste.js?v=20260901a";
import { fetchStats, toErrorMessage } from "./api.js";
import { formatLalalMinutes, getCsrfToken, humanSize, isSafeSameOriginRedirect } from "./utils.js";

const AUTO_SAVE_DELAY_MS = 800;
const REQUEST_TIMEOUT_MS = 10_000;
const PANEL_SLIDE_DURATION_MS = 220;
const reduceMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
const slideStates = new WeakMap();

const BOOTSTRAP_FALLBACK = Object.freeze({
    lalal_configured: false,
    lalal_status: "Not configured",
    lalal_email: "",
    lalal_minutes_left: null,
    has_admin_credentials: false,
});

function getBootstrapData() {
    const node = document.getElementById("settingsBootstrapData");
    if (!node) {
        return { ...BOOTSTRAP_FALLBACK };
    }

    try {
        const payload = JSON.parse(node.textContent || "{}");
        const minutesLeft = Number(payload?.lalal_minutes_left);
        return {
            lalal_configured: Boolean(payload?.lalal_configured),
            lalal_status: String(payload?.lalal_status || "Not configured"),
            lalal_email: String(payload?.lalal_email || ""),
            lalal_minutes_left: Number.isFinite(minutesLeft) ? minutesLeft : null,
            has_admin_credentials: Boolean(payload?.has_admin_credentials),
        };
    } catch {
        return { ...BOOTSTRAP_FALLBACK };
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

/** Animate a Bootstrap d-none section while preserving its final visibility. */
function setSlideVisible(el, visible, { animate = true, focusTarget = null } = {}) {
    if (!el) return;

    const state = slideStates.get(el) || { animation: null, hiding: false };
    slideStates.set(el, state);
    const isVisible = !el.classList.contains("d-none") && !state.hiding;
    if (isVisible === visible) return;

    const resetStyles = () => {
        el.style.height = "";
        el.style.overflow = "";
    };
    const cancelAnimation = () => {
        state.animation?.cancel();
        state.animation = null;
    };
    const showImmediately = () => {
        cancelAnimation();
        state.hiding = false;
        el.classList.toggle("d-none", !visible);
        resetStyles();
        if (!visible && el.contains(document.activeElement)) {
            focusTarget?.focus();
        }
    };

    if (!animate || reduceMotionQuery.matches || typeof el.animate !== "function") {
        showImmediately();
        return;
    }

    cancelAnimation();
    if (visible) {
        const startHeight = el.classList.contains("d-none") ? 0 : el.getBoundingClientRect().height;
        state.hiding = false;
        el.classList.remove("d-none");
        el.style.height = `${startHeight}px`;
        el.style.overflow = "hidden";
        const endHeight = el.scrollHeight;
        const animation = el.animate(
            { height: [`${startHeight}px`, `${endHeight}px`] },
            { duration: PANEL_SLIDE_DURATION_MS, easing: "cubic-bezier(0.22, 1, 0.36, 1)" },
        );
        state.animation = animation;
        animation.onfinish = () => {
            if (state.animation !== animation) return;
            state.animation = null;
            resetStyles();
        };
        animation.oncancel = () => {
            if (state.animation === animation) state.animation = null;
        };
        return;
    }

    state.hiding = true;
    if (el.contains(document.activeElement)) {
        focusTarget?.focus();
    }
    const startHeight = el.getBoundingClientRect().height;
    el.style.height = `${startHeight}px`;
    el.style.overflow = "hidden";
    const animation = el.animate(
        { height: [`${startHeight}px`, "0px"] },
        { duration: PANEL_SLIDE_DURATION_MS, easing: "cubic-bezier(0.4, 0, 1, 1)" },
    );
    state.animation = animation;
    animation.onfinish = () => {
        if (state.animation !== animation) return;
        state.animation = null;
        state.hiding = false;
        el.classList.add("d-none");
        resetStyles();
    };
    animation.oncancel = () => {
        if (state.animation === animation) state.animation = null;
    };
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
const removeAllJobsBtn = document.getElementById("removeAllJobsBtn");
const logoutFormEl = document.getElementById("logoutBtn")?.closest("form");
const passwordSaveBtn = document.getElementById("passwordSaveBtn");
const adminPasswordEl = document.getElementById("adminPassword");
const adminPasswordConfirmEl = document.getElementById("adminPasswordConfirm");
const passwordErrorEl = document.getElementById("passwordError");
const lalalStatusBadge = document.getElementById("lalalStatusBadge");
const lalalStatusLine = document.getElementById("lalalStatusLine");
const lalalAuthBtnLabel = document.getElementById("lalalAuthBtnLabel");
const lalalDisconnectBtn = document.getElementById("lalalDisconnectBtn");
const lalalAnalysisLimits = document.getElementById("lalalAnalysisLimits");
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
const videoWatermarkEl = document.getElementById("videoWatermark");
const enableAuthenticationEl = document.getElementById("enableAuthentication");
const retentionDaysEl = document.getElementById("retentionDays");
const retentionDaysValueEl = document.getElementById("retentionDaysValue");
const retentionDaysInputEl = document.getElementById("retentionDaysInput");
const shareLinkMaxUsesEl = document.getElementById("shareLinkMaxUses");
const shareLinkMaxUsesValueEl = document.getElementById("shareLinkMaxUsesValue");
const shareLinkMaxUsesInputEl = document.getElementById("shareLinkMaxUsesInput");
const publicHostnameEl = document.getElementById("publicHostname");
const publicHostnameDetectBtn = document.getElementById("publicHostnameDetectBtn");
const adminUsernameEl = document.getElementById("adminUsername");
const passwordSaveBtnLabel = document.getElementById("passwordSaveBtnLabel");
const authCredentialsRequiredEl = document.getElementById("authCredentialsRequired");
const credentialsSectionEl = document.getElementById("credentialsSection");

const RETENTION_DAY_OPTIONS = [0, 7, 14, 30, 90, 180, 365];
const SHARE_LINK_MAX_USE_OPTIONS = [0, 1, 10, 100, 1_000, 5_000, 10_000];
const RUNTIME_LIMITS = [
    ["download_worker_count", "Download workers", 0, 8],
    ["download_timeout_minutes", "Download timeout", 1, 240],
    ["transcode_timeout_minutes", "Transcode timeout", 1, 480],
    ["download_max_filesize_gib", "Maximum input size", 1, 100],
    ["audio_analysis_max_minutes", "BPM analysis track limit", 0, 240],
    ["audio_analysis_timeout_minutes", "BPM analysis timeout", 1, 60],
    ["lalal_max_download_gib", "Lalal result limit", 1, 100],
    ["session_idle_minutes", "Session idle timeout", 1, 1440],
];

let saveTimeoutId = null;
let isSaving = false;
let activeSettingsSave = null;
let isPagehideSaving = false;
let activePagehideSave = null;
let pendingSave = false;
let pendingSettingsSuccessToast = null;
let settingsDirty = false;
let isSavingAuthentication = false;
let isAuthUseKeyBusy = false;
let isResettingStats = false;
// Whether an admin account exists in the database. Authentication cannot be
// switched on without one, so the toggle stays "pending" until the credentials
// form beside it has been saved.
let hasAdminCredentials = bootstrapData.has_admin_credentials;
let authEnablePending = false;
let isRemovingAllJobs = false;

function setLogoutVisible(visible) {
    logoutFormEl?.classList.toggle("d-none", !visible);
}

function updateRetentionDaysPreview() {
    if (!retentionDaysEl) return;

    const parsed = Number.parseInt(retentionDaysEl.value, 10);
    const index = Number.isFinite(parsed)
        ? Math.min(RETENTION_DAY_OPTIONS.length - 1, Math.max(0, parsed))
        : 0;
    const days = RETENTION_DAY_OPTIONS[index];
    const label = days === 0 ? "Unlimited" : (days === 365 ? "1 year" : `${days} days`);

    retentionDaysEl.value = String(index);
    retentionDaysEl.dataset.activeIndex = String(index);
    retentionDaysEl.setAttribute("aria-valuetext", label);
    if (retentionDaysInputEl) {
        retentionDaysInputEl.value = String(days);
    }
    if (retentionDaysValueEl) {
        retentionDaysValueEl.textContent = label;
    }
}

function closestOptionIndex(options, value) {
    return options.reduce((bestIndex, option, index) => (
        Math.abs(option - value) < Math.abs(options[bestIndex] - value)
            ? index
            : bestIndex
    ), 0);
}

function updateShareLinkMaxUsesPreview() {
    if (!shareLinkMaxUsesEl) return;

    const parsed = Number.parseInt(shareLinkMaxUsesEl.value, 10);
    const index = Number.isFinite(parsed)
        ? Math.min(SHARE_LINK_MAX_USE_OPTIONS.length - 1, Math.max(0, parsed))
        : 0;
    const uses = SHARE_LINK_MAX_USE_OPTIONS[index];
    const label = uses === 0 ? "Unlimited" : `${uses.toLocaleString()} use${uses === 1 ? "" : "s"}`;

    shareLinkMaxUsesEl.value = String(index);
    shareLinkMaxUsesEl.dataset.activeIndex = String(index);
    shareLinkMaxUsesEl.setAttribute("aria-valuetext", label);
    if (shareLinkMaxUsesInputEl) {
        shareLinkMaxUsesInputEl.value = String(uses);
    }
    if (shareLinkMaxUsesValueEl) {
        shareLinkMaxUsesValueEl.textContent = label;
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
    lalalAuthBtnLabel.textContent = shouldReconnect ? "Reconnect" : "Click to Connect";
}

// badgeClass is the complete class list. A real status uses Bootstrap's
// .badge with a bg-* utility; the unconfigured state is not a status and uses
// .ui-badge, the app's shared non-status badge. Mixing the two does not work:
// the bg-* utilities are !important. Same split as COOKIE_STATUS_BADGE_CLASSES.
function setLalalStatus(text, badgeClass = "ui-badge") {
    if (lalalStatusBadge) {
        lalalStatusBadge.className = badgeClass;
        lalalStatusBadge.textContent = text;
    }
    updateLalalAuthButtonLabel(text);
}

function setDisconnectVisible(visible) {
    if (!lalalDisconnectBtn) return;
    lalalDisconnectBtn.classList.toggle("d-none", !visible);
    lalalDisconnectBtn.disabled = !visible;
}

// The analysis/stem limits only take effect for Lalal-backed jobs, so the
// column is hidden while no session is connected. The inputs stay in the form
// (d-none, not removed), so saving settings keeps their stored values.
function setAnalysisLimitsVisible(visible) {
    lalalAnalysisLimits?.classList.toggle("d-none", !visible);
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

    const balance = formatLalalMinutes(remainingMinutes);
    if (balance) {
        const minutes = document.createElement("span");
        minutes.className = "lalal-status-minutes";
        minutes.textContent = ` - ${balance}`;
        minutes.title = "Processing minutes left on this Lalal.ai account";
        lalalStatusLine.appendChild(minutes);
    }
}

function applyInitialLalalState() {
    setLalalStatus(
        bootstrapData.lalal_status,
        bootstrapData.lalal_configured ? "badge bg-success" : "ui-badge",
    );

    if (bootstrapData.lalal_configured) {
        if (bootstrapData.lalal_email) {
            renderLalalStatusLine({
                email: bootstrapData.lalal_email,
                // Last known balance, so the tile is not blank while
                // loadLalalStatus() is still in flight.
                remainingMinutes: bootstrapData.lalal_minutes_left,
            });
        } else {
            setLalalStatusLine("Session active");
        }
    } else {
        setLalalStatusLine("Click 'Authenticate' to connect your Lalal.ai account");
    }

    setDisconnectVisible(bootstrapData.lalal_configured);
    setAnalysisLimitsVisible(bootstrapData.lalal_configured);
}

function showAuthStep(step) {
    lalalAuthStep1?.classList.toggle("d-none", step !== 1);
    lalalAuthStep3?.classList.toggle("d-none", step !== 3);
    clearAlert(lalalAuthAlert);
}

function setAuthAlert(type, message) {
    setAlert(lalalAuthAlert, type, message);
}

async function loadLalalStatus({ forceRefresh = false } = {}) {
    const url = forceRefresh ? "/api/lalal/status?force_refresh=1" : "/api/lalal/status";
    try {
        const res = await fetchWithTimeout(url, { credentials: "same-origin" });
        const payload = await parseResponsePayload(res);

        if (!res.ok) {
            setLalalStatus("Error", "badge bg-danger");
            setLalalStatusLine(toErrorMessage(payload.detail) || "Unable to check status");
            setDisconnectVisible(false);
            setAnalysisLimitsVisible(false);
            return;
        }

        if (payload.configured && payload.token_valid === false) {
            setLalalStatus("Token invalid", "badge bg-danger");
            setLalalStatusLine(payload.validation_error || "Stored Lalal token is invalid. Please authenticate again.");
            setDisconnectVisible(true);
            setAnalysisLimitsVisible(true);
            return;
        }

        if (payload.configured) {
            setLalalStatus("Connected", "badge bg-success");
            renderLalalStatusLine({
                email: payload.email || "",
                remainingMinutes: payload.remaining_minutes,
            });
            setDisconnectVisible(true);
            setAnalysisLimitsVisible(true);
            return;
        }

        setLalalStatus("Not configured");
        setLalalStatusLine("Click 'Authenticate' to connect your Lalal.ai account");
        setDisconnectVisible(false);
        setAnalysisLimitsVisible(false);
    } catch (err) {
        setLalalStatus("Error", "badge bg-danger");
        setLalalStatusLine(err?.message || "Unable to check status");
        setDisconnectVisible(false);
        setAnalysisLimitsVisible(false);
    }
}

function validateSettings() {
    const form = new FormData(formEl);
    const retention = parseInt(String(form.get("retention_days") || ""), 10);
    if (!Number.isFinite(retention) || retention < 0 || retention > 365) {
        return { valid: false, error: "Retention must be unlimited or between 1 and 365 days" };
    }

    // 0 is the "Automatic" option: the host's CPU quota and free memory decide.
    const fragments = parseInt(String(form.get("download_concurrent_fragments") || ""), 10);
    if (!Number.isFinite(fragments) || fragments < 0 || fragments > 16) {
        return { valid: false, error: "Parallel fragments must be automatic or between 1 and 16" };
    }

    const shareMaxUses = parseInt(String(form.get("share_link_max_uses") || "0"), 10);
    if (!Number.isFinite(shareMaxUses) || shareMaxUses < 0 || shareMaxUses > 10000) {
        return { valid: false, error: "Max. uses per share link must be between 0 and 10000" };
    }

    const runtimeLimits = {};
    for (const [key, label, minimum, maximum] of RUNTIME_LIMITS) {
        const value = parseInt(String(form.get(key) || ""), 10);
        if (!Number.isFinite(value) || value < minimum || value > maximum) {
            return { valid: false, error: `${label} must be between ${minimum} and ${maximum}` };
        }
        runtimeLimits[key] = value;
    }

    const publicHostname = String(form.get("public_hostname") || "").trim();
    // Cheap sanity check only; app/utils/public_url.py does the real validation.
    if (publicHostname && (publicHostname.length > 253 || !/^[A-Za-z0-9.:-]+$/.test(publicHostname))) {
        return { valid: false, error: "Public hostname must be a plain host or IP — no scheme, port or path" };
    }

    return {
        valid: true,
        data: {
            retention_days: retention,
            download_concurrent_fragments: fragments,
            share_link_max_uses: shareMaxUses,
            public_hostname: publicHostname,
            download_mp4_preset: mp4PresetEl ? mp4PresetEl.checked : true,
            video_watermark: videoWatermarkEl ? videoWatermarkEl.checked : true,
            lalalaai_duration_guard: lalalDurationGuard ? lalalDurationGuard.checked : true,
            ...runtimeLimits,
        },
    };
}

/** Mirrors normalize_admin_username() in app/utils/credentials.py. */
const USERNAME_RE = /^[A-Za-z_-]+$/;

function validateCredentials() {
    const username = adminUsernameEl?.value.trim() || "";
    const nextPassword = adminPasswordEl?.value || "";
    const confirmPassword = adminPasswordConfirmEl?.value || "";

    if (!username) {
        return { valid: false, error: "Username is required" };
    }
    if (username.length > 64) {
        return { valid: false, error: "Username must be at most 64 characters" };
    }
    if (!USERNAME_RE.test(username)) {
        return {
            valid: false,
            error: "Username may only contain letters, hyphens and underscores",
        };
    }
    if (!nextPassword) {
        return { valid: false, error: "Password is required" };
    }
    if (nextPassword.length < 8) {
        return { valid: false, error: "Password must be at least 8 characters" };
    }
    if (nextPassword !== confirmPassword) {
        return { valid: false, error: "Passwords do not match" };
    }

    const data = {
        admin_username: username,
        admin_password: nextPassword,
    };
    // Flip the toggle in the same request so the account and the enabled flag
    // are never persisted apart.
    if (authEnablePending) {
        data.enable_authentication = true;
    }
    return { valid: true, data };
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

/** Reflect the current credential/toggle state in the Security tab. */
function renderAuthState({ animate = true } = {}) {
    const focusTarget = enableAuthenticationEl;
    setSlideVisible(authCredentialsRequiredEl, authEnablePending, { animate, focusTarget });
    setSlideVisible(credentialsSectionEl, Boolean(enableAuthenticationEl?.checked), { animate, focusTarget });
    if (passwordSaveBtnLabel) {
        passwordSaveBtnLabel.textContent = authEnablePending
            ? "Create account & enable"
            : (hasAdminCredentials ? "Update credentials" : "Create admin account");
    }
}

function scheduleAutoSave(delay = AUTO_SAVE_DELAY_MS, successToast = null) {
    settingsDirty = true;
    if (successToast) {
        pendingSettingsSuccessToast = successToast;
    }
    if (isSaving || isPagehideSaving || isSavingAuthentication) {
        pendingSave = true;
        return;
    }
    if (saveTimeoutId !== null) {
        clearTimeout(saveTimeoutId);
    }

    saveTimeoutId = window.setTimeout(() => {
        saveTimeoutId = null;
        void saveSettings();
    }, delay);
}

function saveSettings() {
    if (isSaving) {
        pendingSave = true;
        return activeSettingsSave;
    }

    const validation = validateSettings();
    if (!validation.valid) {
        pendingSettingsSuccessToast = null;
        showToast(validation.error, "danger");
        return Promise.resolve();
    }

    const successToast = pendingSettingsSuccessToast;
    pendingSettingsSuccessToast = null;
    isSaving = true;
    pendingSave = false;
    activeSettingsSave = performSettingsSave(validation.data, successToast);
    return activeSettingsSave;
}

async function performSettingsSave(data, successToast = null) {
    try {
        const res = await fetchWithTimeout("/api/settings", {
            method: "POST",
            keepalive: true,
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": requireCsrfToken(),
            },
            body: JSON.stringify(data),
        });

        const payload = await parseResponsePayload(res);
        if (!res.ok) {
            throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
        }

        if (!pendingSave) {
            settingsDirty = false;
        }
        if (successToast) {
            showToast(payload.message || successToast, "success");
        }
    } catch (err) {
        const message = err?.name === "AbortError"
            ? "Request timed out"
            : (err?.message || "Could not save");
        showToast(`Error: ${message}`, "danger");
    } finally {
        isSaving = false;
        activeSettingsSave = null;
        if (pendingSave) {
            pendingSave = false;
            scheduleAutoSave(150);
        }
    }
}

async function waitForPendingSettingsWrites() {
    if (activeSettingsSave) {
        await activeSettingsSave;
    }

    const pagehideSave = activePagehideSave;
    if (!pagehideSave) {
        return true;
    }

    let timeoutId = null;
    const completed = await Promise.race([
        pagehideSave.then(() => true),
        new Promise((resolve) => {
            timeoutId = window.setTimeout(() => resolve(false), REQUEST_TIMEOUT_MS);
        }),
    ]);
    if (timeoutId !== null) {
        clearTimeout(timeoutId);
    }
    return completed;
}

function setSecuritySaveBusy(busy) {
    isSavingAuthentication = busy;
    if (enableAuthenticationEl) enableAuthenticationEl.disabled = busy;
    if (passwordSaveBtn) passwordSaveBtn.disabled = busy;
    if (formEl) formEl.inert = busy;
}

async function saveAuthenticationFlag(enabled) {
    if (!enableAuthenticationEl || isSavingAuthentication) {
        return;
    }

    setSecuritySaveBusy(true);
    if (saveTimeoutId !== null) {
        clearTimeout(saveTimeoutId);
        saveTimeoutId = null;
    }

    if (!await waitForPendingSettingsWrites()) {
        showToast("Could not confirm the pending settings save. Reloading…", "warning");
        setSecuritySaveBusy(false);
        window.location.reload();
        return;
    }

    const validation = validateSettings();
    if (!validation.valid) {
        enableAuthenticationEl.checked = !enabled;
        renderAuthState();
        showToast(validation.error, "danger");
        setSecuritySaveBusy(false);
        return;
    }

    pendingSave = false;
    let responseReceived = false;

    try {
        const res = await fetchWithTimeout("/api/settings", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": requireCsrfToken(),
            },
            body: JSON.stringify({
                ...validation.data,
                enable_authentication: enabled,
            }),
        });
        responseReceived = true;
        const payload = await parseResponsePayload(res);
        if (!res.ok) {
            throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
        }

        settingsDirty = false;
        if (payload.redirect && isSafeSameOriginRedirect(payload.redirect)) {
            window.location.replace(payload.redirect);
            return;
        }

        setLogoutVisible(enabled);
        showToast(payload.message || "Authentication setting updated", "success");
    } catch (err) {
        const message = err?.name === "AbortError"
            ? "Request timed out"
            : (err?.message || "Could not update authentication");
        if (!responseReceived) {
            showToast(`Authentication state is uncertain: ${message}. Reloading…`, "warning");
            window.location.reload();
            return;
        }

        enableAuthenticationEl.checked = !enabled;
        authEnablePending = false;
        renderAuthState();
        showToast(`Error: ${message}`, "danger");
    } finally {
        setSecuritySaveBusy(false);
        if (pendingSave) {
            pendingSave = false;
            scheduleAutoSave(150);
        }
    }
}

async function saveCredentials() {
    const credentialsValidation = validateCredentials();
    if (!credentialsValidation.valid) {
        setPasswordError(credentialsValidation.error);
        showToast(credentialsValidation.error, "danger");
        return;
    }
    if (isSavingAuthentication) {
        return;
    }

    setSecuritySaveBusy(true);
    setPasswordError("");
    if (saveTimeoutId !== null) {
        clearTimeout(saveTimeoutId);
        saveTimeoutId = null;
    }

    if (!await waitForPendingSettingsWrites()) {
        showToast("Could not confirm the pending settings save. Reloading…", "warning");
        setSecuritySaveBusy(false);
        window.location.reload();
        return;
    }

    const settingsValidation = validateSettings();
    if (!settingsValidation.valid) {
        showToast(settingsValidation.error, "danger");
        setSecuritySaveBusy(false);
        return;
    }

    pendingSave = false;
    let responseReceived = false;

    try {
        const res = await fetchWithTimeout("/api/settings", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": requireCsrfToken(),
            },
            body: JSON.stringify({
                ...settingsValidation.data,
                ...credentialsValidation.data,
            }),
        });
        responseReceived = true;
        const payload = await parseResponsePayload(res);
        if (!res.ok) {
            throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
        }

        settingsDirty = false;
        if (adminPasswordEl) adminPasswordEl.value = "";
        if (adminPasswordConfirmEl) adminPasswordConfirmEl.value = "";
        setPasswordError("");
        hasAdminCredentials = true;
        authEnablePending = false;
        renderAuthState();

        if (payload.redirect && isSafeSameOriginRedirect(payload.redirect)) {
            window.location.replace(payload.redirect);
            return;
        }

        showToast(payload.message || "Credentials updated", "success");
    } catch (err) {
        const message = err?.name === "AbortError" ? "Request timed out" : (err?.message || "Request failed");
        if (!responseReceived) {
            showToast(`Credential state is uncertain: ${message}. Reloading…`, "warning");
            window.location.reload();
            return;
        }

        setPasswordError(message);
        showToast(`Error: ${message}`, "danger");
    } finally {
        setSecuritySaveBusy(false);
        if (pendingSave) {
            pendingSave = false;
            scheduleAutoSave(150);
        }
    }
}

async function resetStatistics() {
    if (isResettingStats) {
        return;
    }

    const confirmed = await confirmModal({
        title: "Reset statistics",
        message: "Reset all dashboard statistics to 0?",
        confirmText: "Reset",
        variant: "danger",
    });
    if (!confirmed) {
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
            throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
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

async function removeAllJobs() {
    if (isRemovingAllJobs) {
        return;
    }

    const confirmed = await confirmModal({
        title: "Remove every job",
        message: "Remove every job? This permanently deletes all job files and invalidates every shared link.",
        confirmText: "Remove all",
        variant: "danger",
    });
    if (!confirmed) {
        return;
    }

    isRemovingAllJobs = true;
    removeAllJobsBtn?.setAttribute("aria-busy", "true");
    if (removeAllJobsBtn) {
        removeAllJobsBtn.disabled = true;
    }

    try {
        const res = await fetchWithTimeout("/api/jobs/remove-all", {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": requireCsrfToken(),
            },
        });

        const payload = await parseResponsePayload(res);
        if (!res.ok) {
            throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
        }

        showToast(payload.message || "All jobs removed", "success");
    } catch (err) {
        const message = err?.name === "AbortError" ? "Request timed out" : (err?.message || "Request failed");
        showToast(`Error: ${message}`, "danger");
    } finally {
        isRemovingAllJobs = false;
        removeAllJobsBtn?.removeAttribute("aria-busy");
        if (removeAllJobsBtn) {
            removeAllJobsBtn.disabled = false;
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
            btn.querySelector(".material-symbols-outlined")?.replaceChildren(
                showing ? "visibility" : "visibility_off",
            );
        });
    });
}

function clearSensitiveInputs() {
    if (adminPasswordEl) adminPasswordEl.value = "";
    if (adminPasswordConfirmEl) adminPasswordConfirmEl.value = "";
    if (lalalActivationKey) lalalActivationKey.value = "";
}

function retryPendingSettingsWhenVisible() {
    settingsDirty = true;
    if (document.visibilityState === "visible") {
        scheduleAutoSave(0);
    }
}

function persistPendingSettingsOnPageHide() {
    clearSensitiveInputs();

    if (saveTimeoutId !== null) {
        clearTimeout(saveTimeoutId);
        saveTimeoutId = null;
    }

    // Normal autosaves use keepalive as well, so an in-flight request can
    // finish after pagehide without racing a second write of the same fields.
    if (!settingsDirty || isSaving || isSavingAuthentication) {
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
    isPagehideSaving = true;
    activePagehideSave = fetchWithTimeout("/api/settings", {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(validation.data),
    }).then((res) => {
        if (!res.ok) {
            retryPendingSettingsWhenVisible();
        }
    }).catch(retryPendingSettingsWhenVisible).finally(() => {
        isPagehideSaving = false;
        activePagehideSave = null;
        if (pendingSave && document.visibilityState === "visible") {
            pendingSave = false;
            scheduleAutoSave(0);
        }
    });
}

window.addEventListener("pagehide", persistPendingSettingsOnPageHide);
window.addEventListener("pageshow", (event) => {
    if (event.persisted && settingsDirty) {
        scheduleAutoSave(0);
    }
});

const AUTO_SAVE_INPUT_SELECTOR = [
    '[name="download_concurrent_fragments"]',
    '[name="download_mp4_preset"]',
    '[name="video_watermark"]',
    '[name="lalalaai_duration_guard"]',
    '[name="download_worker_count"]',
    '[name="download_timeout_minutes"]',
    '[name="transcode_timeout_minutes"]',
    '[name="download_max_filesize_gib"]',
    '[name="audio_analysis_max_minutes"]',
    '[name="audio_analysis_timeout_minutes"]',
    '[name="lalal_max_download_gib"]',
    '[name="session_idle_minutes"]',
].join(", ");

function bindSettingsInputs() {
    formEl?.querySelectorAll(AUTO_SAVE_INPUT_SELECTOR).forEach((input) => {
        if (input.type === "checkbox") {
            input.addEventListener("change", () => scheduleAutoSave());
            return;
        }

        input.addEventListener("input", () => scheduleAutoSave());
        input.addEventListener("change", () => scheduleAutoSave());
    });

    enableAuthenticationEl?.addEventListener("change", async () => {
        if (enableAuthenticationEl.checked) {
            // Without an admin account there is nothing to log in with, so the
            // flag is held back until the credentials form has been saved -
            // enabling it now would lock everyone out of the instance.
            if (!hasAdminCredentials) {
                authEnablePending = true;
                renderAuthState();
                adminUsernameEl?.focus();
                showToast("Set a username and password to enable authentication", "info", 3600);
                return;
            }
            authEnablePending = false;
            renderAuthState();
            await saveAuthenticationFlag(true);
            return;
        }

        // Turning authentication off exposes the whole instance to anyone who
        // can reach it, so a stray click must not be enough to do it.
        if (!authEnablePending) {
            const confirmed = await confirmModal({
                title: "Disable authentication",
                message: "Disable authentication? Everyone who can reach this server will have full access.",
                confirmText: "Disable authentication",
                cancelText: "Keep it on",
                variant: "danger",
            });
            if (!confirmed) {
                enableAuthenticationEl.checked = true;
                return;
            }
        }

        // Unchecking a pending toggle just abandons the intent; nothing was
        // ever persisted, so there is nothing to save.
        const wasPending = authEnablePending;
        authEnablePending = false;
        renderAuthState();
        if (!wasPending) {
            await saveAuthenticationFlag(false);
        }
    });
}

function bindRetentionSlider() {
    const savedDays = Number.parseInt(retentionDaysEl?.dataset.retentionDays || "", 10);
    const exactIndex = RETENTION_DAY_OPTIONS.indexOf(savedDays);
    const closestIndex = closestOptionIndex(RETENTION_DAY_OPTIONS, savedDays);

    if (retentionDaysEl) {
        retentionDaysEl.value = String(exactIndex >= 0 ? exactIndex : closestIndex);
    }
    retentionDaysEl?.addEventListener("input", updateRetentionDaysPreview);
    retentionDaysEl?.addEventListener("change", () => {
        scheduleAutoSave(0, "Retention updated");
    });
    updateRetentionDaysPreview();
}

function bindShareLinkMaxUsesSlider() {
    const savedUses = Number.parseInt(shareLinkMaxUsesEl?.dataset.shareLinkMaxUses || "", 10);
    const exactIndex = SHARE_LINK_MAX_USE_OPTIONS.indexOf(savedUses);
    const closestIndex = closestOptionIndex(SHARE_LINK_MAX_USE_OPTIONS, savedUses);

    if (shareLinkMaxUsesEl) {
        shareLinkMaxUsesEl.value = String(exactIndex >= 0 ? exactIndex : closestIndex);
    }
    shareLinkMaxUsesEl?.addEventListener("input", updateShareLinkMaxUsesPreview);
    shareLinkMaxUsesEl?.addEventListener("change", () => {
        scheduleAutoSave(0, "Share-link limit updated");
    });
    updateShareLinkMaxUsesPreview();
}

/**
 * Best guess at the public hostname: the address this page is loaded from,
 * with any IPv6 brackets stripped ([::1] -> ::1). Ported from the FQDN
 * auto-detect in the wirebuddy sister project.
 * @returns {string}
 */
function detectPublicHostname() {
    return String(window.location.hostname || "").replace(/^\[|\]$/g, "");
}

function bindPublicHostnameDetect() {
    // A plain "input" save without confirmation leaves it unclear whether
    // leaving the field actually persisted anything, so this one gets its
    // own toast on "change" (fires on blur), like the sliders below.
    publicHostnameEl?.addEventListener("change", () => {
        scheduleAutoSave(0, "Public hostname updated");
    });

    publicHostnameDetectBtn?.addEventListener("click", () => {
        const detected = detectPublicHostname();
        if (!detected || !publicHostnameEl) {
            showToast("Could not detect a hostname from the current address", "warning");
            return;
        }
        if (publicHostnameEl.value.trim() === detected) {
            return;
        }
        publicHostnameEl.value = detected;
        scheduleAutoSave(0);
        showToast(`Detected: ${detected}`, "success", 2200);
    });
}

/**
 * Changelog card (Settings → System): keep expanding the "Previous versions"
 * disclosure from scrolling the reader away from the toggle they just clicked.
 * Ported from initChangelogDetailsScroll() in the wirebuddy sister project.
 *
 * The card's height is capped purely in CSS (see .changelog-content): the
 * scroll container is what gets the max-height, so the tile stays put and the
 * extra entries land in its own scrollbar.
 */
function bindChangelog() {
    const container = document.querySelector("[data-changelog]");
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const animationDuration = 220;

    container?.querySelectorAll("details").forEach((details) => {
        const summary = details.querySelector("summary");
        if (!summary) {
            return;
        }

        const content = document.createElement("div");
        content.className = "changelog-details-content";
        let node = summary.nextSibling;
        while (node) {
            const nextNode = node.nextSibling;
            content.append(node);
            node = nextNode;
        }
        details.append(content);

        const state = { animation: null, closing: false };
        const resetContentStyles = () => {
            content.style.height = "";
            content.style.overflow = "";
        };
        const cancelAnimation = () => {
            state.animation?.cancel();
            state.animation = null;
        };
        const card = details.closest(".settings-changelog-card");
        const scrollSummaryIntoView = () => {
            summary.scrollIntoView({ block: "nearest" });
        };
        // The card grows downward until it hits its CSS cap, which can leave
        // its bottom edge below the fold even though the toggle itself is
        // still visible. Nudge the page just enough to show the whole tile.
        const scrollCardIntoView = () => {
            card?.scrollIntoView({ block: "nearest" });
        };
        const animateOpen = () => {
            cancelAnimation();
            state.closing = false;

            const startHeight = content.getBoundingClientRect().height;
            details.open = true;
            content.style.height = `${startHeight}px`;
            content.style.overflow = "hidden";
            const endHeight = content.scrollHeight;

            const animation = content.animate(
                { height: [`${startHeight}px`, `${endHeight}px`] },
                { duration: animationDuration, easing: "cubic-bezier(0.22, 1, 0.36, 1)" },
            );
            state.animation = animation;
            animation.onfinish = () => {
                if (state.animation !== animation) {
                    return;
                }
                state.animation = null;
                resetContentStyles();
                scrollCardIntoView();
            };
            animation.oncancel = () => {
                if (state.animation === animation) {
                    state.animation = null;
                }
            };
            scrollSummaryIntoView();
        };
        const animateClose = () => {
            cancelAnimation();
            state.closing = true;

            const startHeight = content.getBoundingClientRect().height;
            content.style.height = `${startHeight}px`;
            content.style.overflow = "hidden";
            const animation = content.animate(
                { height: [`${startHeight}px`, "0px"] },
                { duration: animationDuration, easing: "cubic-bezier(0.4, 0, 1, 1)" },
            );
            state.animation = animation;
            animation.onfinish = () => {
                if (state.animation !== animation) {
                    return;
                }
                state.animation = null;
                state.closing = false;
                details.open = false;
                resetContentStyles();
            };
            animation.oncancel = () => {
                if (state.animation === animation) {
                    state.animation = null;
                }
            };
        };

        summary.addEventListener("click", (event) => {
            event.preventDefault();
            const isOpen = details.open && !state.closing;

            if (reduceMotion.matches) {
                cancelAnimation();
                state.closing = false;
                details.open = !isOpen;
                resetContentStyles();
                if (details.open) {
                    scrollSummaryIntoView();
                    scrollCardIntoView();
                }
                return;
            }

            if (isOpen) {
                animateClose();
            } else {
                animateOpen();
            }
        });
    });
}

const COOKIE_AGE_FORMATTER = new Intl.RelativeTimeFormat("en", { numeric: "always" });

function isCookieAuthenticated(status) {
    if (typeof status?.authenticated === "boolean") {
        return status.authenticated;
    }

    return String(status?.status || "missing").toLowerCase() === "valid"
        && Array.isArray(status?.missing_login_cookies)
        && status.missing_login_cookies.length === 0;
}

function cookieTileElements(platform) {
    const section = document.querySelector(`[data-cookie-platform="${platform}"]`);
    if (!section) return null;
    return {
        section,
        badge: section.querySelector("[data-cookie-status-badge]"),
        summary: section.querySelector("[data-cookie-summary]"),
        pasteBtn: section.querySelector("[data-cookie-paste-btn]"),
        pasteLabel: section.querySelector("[data-cookie-paste-label]"),
        removeBtn: section.querySelector("[data-cookie-remove-btn]"),
    };
}

/** "3 minutes ago" for a unix timestamp, in the viewer's locale. */
function describeAge(unixSeconds) {
    const seconds = Math.round((Date.now() - unixSeconds * 1000) / 1000);
    if (seconds < 60) return "just now";
    for (const [unit, size] of [["minute", 60], ["hour", 3600], ["day", 86_400]]) {
        const next = size * (unit === "minute" ? 60 : unit === "hour" ? 24 : Infinity);
        if (seconds < next) {
            return COOKIE_AGE_FORMATTER.format(-Math.round(seconds / size), unit);
        }
    }
    return COOKIE_AGE_FORMATTER.format(-Math.round(seconds / 86_400), "day");
}

function describeCookieState(status) {
    const state = String(status?.status || "missing").toLowerCase();
    if (state === "missing") {
        return {
            badgeClass: "ui-badge",
            label: "Not set up",
            summary: "Add cookies only when this site requires you to sign in.",
        };
    }

    if (state !== "valid" || !isCookieAuthenticated(status)) {
        return {
            badgeClass: "badge bg-warning text-dark",
            label: "Needs update",
            summary: state === "expired"
                ? "These cookies have expired. Replace them while signed in."
                : "Replace these cookies while signed in to enable account-only downloads.",
        };
    }

    const updatedAt = Number(status?.updated_at) || 0;
    const updated = updatedAt ? ` Last updated ${describeAge(updatedAt)}.` : "";
    return {
        badgeClass: "badge bg-success",
        label: "Ready",
        summary: `Signed-in downloads are enabled.${updated}`,
    };
}

function renderCookieStatus(platform, status) {
    const els = cookieTileElements(platform);
    if (!els) return;

    const normalized = String(status?.status || "missing").toLowerCase();
    const presentation = describeCookieState(status);

    if (els.badge) {
        els.badge.className = `${presentation.badgeClass} cookie-status-badge`;
        els.badge.dataset.status = normalized;
        els.badge.dataset.cookieAuthenticated = String(isCookieAuthenticated(status));
        els.badge.textContent = presentation.label;
    }
    if (els.summary) {
        els.summary.textContent = presentation.summary;
        els.summary.classList.toggle("cookie-tile-summary--warning", presentation.label === "Needs update");
    }
    if (els.removeBtn) {
        els.removeBtn.classList.toggle("d-none", !status?.present);
    }
    if (els.pasteLabel) {
        els.pasteLabel.textContent = status?.present ? "Replace cookies" : "Add cookies";
    }
}

async function loadCookieStatuses() {
    try {
        const res = await fetchWithTimeout("/api/cookies", { credentials: "same-origin" });
        if (!res.ok) return;
        const payload = await parseResponsePayload(res);
        if (!Array.isArray(payload)) return;
        payload.forEach((status) => renderCookieStatus(status.platform, status));
    } catch {
        // Best-effort: leave the server-rendered initial state on screen.
    }
}

function setCookieTileBusy(els, isBusy) {
    for (const btn of [els.pasteBtn, els.removeBtn]) {
        if (btn) btn.disabled = isBusy;
    }
}

function cookieTileLabel(els, platform) {
    return els.section.querySelector(".cookie-tile-name")?.textContent?.trim() || platform;
}

async function pasteCookies(platform) {
    const els = cookieTileElements(platform);
    if (!els) return;

    const label = cookieTileLabel(els, platform);
    setCookieTileBusy(els, true);
    try {
        await openCookiePasteDialog({
            platform,
            label,
            // Rejecting here keeps the dialog open with the paste intact, so
            // the user can fix it rather than start over.
            onSubmit: async (text) => {
                const res = await fetchWithTimeout(
                    `/api/cookies/${encodeURIComponent(platform)}/paste`,
                    {
                        method: "POST",
                        credentials: "same-origin",
                        headers: {
                            "Content-Type": "application/json",
                            "X-CSRF-Token": requireCsrfToken(),
                        },
                        body: JSON.stringify({ text }),
                    },
                );

                const payload = await parseResponsePayload(res);
                if (!res.ok) {
                    throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
                }

                renderCookieStatus(platform, payload);
                showToast(`${label} cookies stored`, "success");
            },
        });
    } catch (err) {
        const message = err?.name === "AbortError" ? "Request timed out" : err?.message;
        showToast(`Error: ${message || "Could not store these cookies"}`, "danger");
    } finally {
        setCookieTileBusy(els, false);
    }
}

async function removeCookieFile(platform) {
    const els = cookieTileElements(platform);
    if (!els) return;

    const label = cookieTileLabel(els, platform);
    const confirmed = await confirmModal({
        title: "Remove cookies",
        message: `Remove the stored cookies for ${label}? Downloads that need a login for this platform will run signed out until new cookies are stored.`,
        confirmText: "Remove",
        variant: "danger",
    });
    if (!confirmed) return;

    setCookieTileBusy(els, true);

    try {
        const res = await fetchWithTimeout(`/api/cookies/${encodeURIComponent(platform)}`, {
            method: "DELETE",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": requireCsrfToken(),
            },
        });

        const payload = await parseResponsePayload(res);
        if (!res.ok) {
            throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
        }

        renderCookieStatus(platform, { platform, status: "missing", present: false });
        showToast(`${label} cookies removed`, "success");
    } catch (err) {
        const message = err?.name === "AbortError" ? "Request timed out" : (err?.message || "Request failed");
        showToast(`Error: ${message}`, "danger");
    } finally {
        setCookieTileBusy(els, false);
    }
}

function bindCookieEvents() {
    document.querySelectorAll("[data-cookie-paste-btn]").forEach((btn) => {
        btn.addEventListener("click", () => {
            void pasteCookies(btn.dataset.platform);
        });
    });

    document.querySelectorAll("[data-cookie-remove-btn]").forEach((btn) => {
        btn.addEventListener("click", () => {
            void removeCookieFile(btn.dataset.platform);
        });
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
                throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
            }

            setLalalStatus("Not configured");
            setLalalStatusLine("Session disconnected. Click 'Authenticate' to reconnect.");
            setDisconnectVisible(false);
            setAnalysisLimitsVisible(false);
            showToast("Lalal.ai session disconnected", "success");
        } catch (err) {
            showToast(err?.message || "Failed to disconnect Lalal.ai session", "danger");
            setDisconnectVisible(true);
            setAnalysisLimitsVisible(true);
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
                throw new Error(toErrorMessage(payload.detail) || `HTTP ${res.status}`);
            }

            if (lalalActivationKey) lalalActivationKey.value = "";
            showAuthStep(3);
            void loadLalalStatus();
            window.setTimeout(() => {
                if (lalalAuthModal) {
                    globalThis.bootstrap?.Modal?.getInstance(lalalAuthModal)?.hide();
                }
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

const HOST_STATS_POLL_MS = 5_000;

let hostStatsTimer = null;
let hostStatsInFlight = false;

function setHostMetric(metric, { value, unit = "", title = null }) {
    const card = document.querySelector(`[data-host-metric="${metric}"]`);
    if (!card) return;

    const valueNode = card.querySelector("[data-stat-value]");
    const unitNode = card.querySelector("[data-stat-unit]");
    if (valueNode) valueNode.textContent = value;
    if (unitNode) {
        unitNode.textContent = unit;
        unitNode.hidden = !unit;
    }
    card.toggleAttribute("title", Boolean(title));
    if (title) card.setAttribute("title", title);
}

function splitSize(bytes) {
    const rendered = humanSize(bytes);
    const match = /^(.+?)\s+(\S+)$/.exec(rendered);
    return match ? { value: match[1], unit: match[2] } : { value: rendered, unit: "" };
}

function renderHostStats(payload) {
    const storage = payload?.storage;
    setHostMetric("storage", storage && Number.isFinite(Number(storage.free))
        ? {
            ...splitSize(storage.free),
            title: `${humanSize(storage.free)} free of ${humanSize(storage.total)} · ${storage.percent}% used`,
        }
        : { value: "–" });

    const cpu = payload?.cpu;
    setHostMetric("cpu", cpu && Number.isFinite(Number(cpu.percent))
        ? { value: String(Math.round(cpu.percent)), unit: "%", title: cpu.cores ? `${cpu.cores} logical cores` : null }
        : { value: "–" });

    const memory = payload?.memory;
    setHostMetric("memory", memory && Number.isFinite(Number(memory.percent))
        ? {
            value: String(Math.round(memory.percent)),
            unit: "%",
            title: `${humanSize(memory.used)} used of ${humanSize(memory.total)}`,
        }
        : { value: "–" });

    setHostMetric("uptime", { value: payload?.uptime?.text || "–" });
}

async function loadHostStats() {
    if (hostStatsInFlight) return;
    hostStatsInFlight = true;
    try {
        const res = await fetchWithTimeout("/api/system/host", { credentials: "same-origin" });
        if (res.ok) renderHostStats(await parseResponsePayload(res));
    } catch {
        // Best-effort: unavailable host metrics leave their placeholders intact.
    } finally {
        hostStatsInFlight = false;
    }
}

const JOB_STATS_MOBILE_QUERY = "(max-width: 767.98px)";

/**
 * Render one dashboard stat value into its tile. Mirrors renderStatParts() in
 * main.js -- the same four keys, formatted the same way.
 * @param {string} statKey
 * @param {unknown} value
 */
function renderJobStatParts(statKey, value) {
    switch (statKey) {
        case "total_bytes":
            return splitSize(value);
        case "total_jobs":
            return { value: String(Math.max(0, Math.trunc(Number(value) || 0))), unit: "" };
        case "total_minutes":
        case "total_lalal_minutes":
            return { value: String(Math.round(Math.max(0, Number(value) || 0) * 10) / 10), unit: "" };
        default:
            return { value: String(value ?? 0), unit: "" };
    }
}

/**
 * Refresh the dashboard stat tiles that mobile moves onto the System tab.
 * The row is server-rendered, so this only has to catch what changed while
 * the page stayed open -- a statistics reset, or jobs removed.
 */
async function refreshJobStats() {
    const row = document.querySelector(".settings-job-stats");
    if (!row || !window.matchMedia(JOB_STATS_MOBILE_QUERY).matches) return;

    let stats;
    try {
        stats = await fetchStats();
    } catch {
        // Best-effort: the server-rendered numbers stay on screen.
        return;
    }

    row.querySelectorAll(".stat-card[data-stat-key]").forEach((card) => {
        const rendered = renderJobStatParts(card.dataset.statKey, stats?.[card.dataset.statKey]);
        const valueNode = card.querySelector("[data-stat-value]");
        const unitNode = card.querySelector("[data-stat-unit]");
        if (valueNode) valueNode.textContent = rendered.value;
        if (unitNode) {
            unitNode.textContent = rendered.unit;
            unitNode.hidden = !rendered.unit;
        }
    });
}

function bindHostStats() {
    const panel = document.getElementById("settingsSystemPanel");
    const tab = document.getElementById("settingsSystemTab");
    if (!panel || !tab) return;

    const start = () => {
        void refreshJobStats();
        if (hostStatsTimer !== null) return;
        void loadHostStats();
        hostStatsTimer = window.setInterval(() => {
            if (document.visibilityState === "visible") void loadHostStats();
        }, HOST_STATS_POLL_MS);
    };
    const stop = () => {
        if (hostStatsTimer === null) return;
        window.clearInterval(hostStatsTimer);
        hostStatsTimer = null;
    };

    tab.addEventListener("shown.bs.tab", start);
    tab.addEventListener("hidden.bs.tab", stop);
    if (panel.classList.contains("active")) start();
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

    renderAuthState({ animate: false });
    adminPasswordEl?.addEventListener("input", validatePasswordFields);
    adminPasswordConfirmEl?.addEventListener("input", validatePasswordFields);
    passwordSaveBtn?.addEventListener("click", () => {
        void saveCredentials();
    });
    resetStatsBtn?.addEventListener("click", () => {
        void resetStatistics();
    });
    removeAllJobsBtn?.addEventListener("click", () => {
        void removeAllJobs();
    });

    bindRetentionSlider();
    bindShareLinkMaxUsesSlider();
    bindSettingsInputs();
    bindPublicHostnameDetect();
    bindChangelog();
    bindLalalEvents();
    bindCookieEvents();
    void loadCookieStatuses();
    bindPasswordVisibilityToggles();
    bindHostStats();
    formEl.addEventListener("submit", (event) => {
        event.preventDefault();
    });
}

init();
