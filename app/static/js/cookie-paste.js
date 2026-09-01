//
// app/static/js/cookie-paste.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module cookie-paste
 *
 * The "Paste cookies" dialog behind each platform tile in Settings. It walks
 * the user through copying the cookie request header out of the browser's dev
 * tools and hands the text to a caller-supplied submit function.
 *
 * The dialog stays open until that submit succeeds: the server rejects a
 * paste that carries no login cookie (see app/routes/cookies.py), and closing
 * on failure would throw away a paste the user cannot easily reproduce, so
 * the reason is shown in place with the text still in the box.
 */

/** Where the user has to be signed in for the copied header to be worth anything. */
const PLATFORM_SITES = {
    youtube: "https://www.youtube.com",
    tiktok: "https://www.tiktok.com",
    instagram: "https://www.instagram.com",
    facebook: "https://www.facebook.com",
};

const PLATFORM_PLACEHOLDERS = {
    youtube: "curl 'https://www.youtube.com/' -H 'cookie: SID=…; __Secure-3PSID=…'",
    tiktok: "curl 'https://www.tiktok.com/' -H 'cookie: sessionid=…; tt_csrf_token=…'",
    instagram: "curl 'https://www.instagram.com/' -H 'cookie: sessionid=…; csrftoken=…'",
    facebook: "curl 'https://www.facebook.com/' -H 'cookie: c_user=…; xs=…'",
};

/**
 * @typedef {{
 *   root: HTMLDivElement,
 *   title: HTMLElement,
 *   subtitle: HTMLElement,
 *   platformBadge: HTMLElement,
 *   platformIcon: HTMLElement,
 *   steps: HTMLOListElement,
 *   textarea: HTMLTextAreaElement,
 *   error: HTMLElement,
 *   saveBtn: HTMLButtonElement,
 *   saveIcon: HTMLElement,
 *   saveLabel: HTMLElement,
 *   cancelBtn: HTMLButtonElement,
 * }} PasteElements
 */

/** @type {PasteElements | null} */
let els = null;
/** @type {{ show: () => void, hide: () => void } | null} */
let modalInstance = null;
/** @type {((saved: boolean) => void) | null} */
let pendingResolve = null;
/** @type {((text: string) => Promise<void>) | null} */
let submitHandler = null;
/** @type {HTMLElement | null} */
let lastFocused = null;
let busy = false;
// Bumped on every open. The dialog node and its state are shared, so an
// in-flight submit has to prove it still belongs to what is on screen before
// it resolves anything: dismissing a dialog mid-request (Escape and the
// backdrop stay live while the buttons are disabled) and opening the next one
// would otherwise let the first response close the second dialog.
let openGeneration = 0;

function settle(saved) {
    const resolve = pendingResolve;
    pendingResolve = null;
    if (resolve) {
        resolve(saved);
    }
}

function restoreFocus() {
    const target = lastFocused;
    lastFocused = null;
    if (target && document.body.contains(target)) {
        target.focus();
    }
}

function setBusy(next) {
    busy = next;
    if (!els) return;
    els.saveBtn.disabled = next || !els.textarea.value.trim();
    els.cancelBtn.disabled = next;
    els.textarea.readOnly = next;
    els.saveIcon.textContent = next ? "progress_activity" : "check";
    els.saveIcon.classList.toggle("cookie-connect-save-icon--busy", next);
    els.saveLabel.textContent = next ? "Checking…" : "Save cookies";
}

function showError(message) {
    if (!els) return;
    els.error.textContent = message || "";
    els.error.classList.toggle("d-none", !message);
}

/** @returns {PasteElements} */
function buildModal() {
    const root = document.createElement("div");
    root.className = "modal fade cookie-connect-modal";
    root.id = "cookiePasteModal";
    root.tabIndex = -1;
    root.setAttribute("aria-labelledby", "cookiePasteModalLabel");
    root.setAttribute("aria-describedby", "cookiePasteModalDescription");
    root.setAttribute("aria-hidden", "true");

    const dialog = document.createElement("div");
    dialog.className = "modal-dialog modal-dialog-centered modal-lg modal-dialog-scrollable";

    const content = document.createElement("div");
    content.className = "modal-content cookie-connect-content";

    const header = document.createElement("div");
    header.className = "modal-header cookie-connect-header";

    const identity = document.createElement("div");
    identity.className = "cookie-connect-identity";

    const platformBadge = document.createElement("span");
    platformBadge.className = "platform-pill cookie-connect-platform";
    platformBadge.setAttribute("aria-hidden", "true");

    const platformIcon = document.createElement("span");
    platformIcon.className = "platform-pill__icon";
    platformIcon.setAttribute("aria-hidden", "true");
    platformBadge.appendChild(platformIcon);

    const heading = document.createElement("div");
    heading.className = "cookie-connect-heading";

    const eyebrow = document.createElement("span");
    eyebrow.className = "cookie-connect-eyebrow";
    eyebrow.textContent = "Browser session";

    const title = document.createElement("h5");
    title.className = "modal-title cookie-connect-title";
    title.id = "cookiePasteModalLabel";

    const subtitle = document.createElement("p");
    subtitle.className = "cookie-connect-subtitle";
    subtitle.id = "cookiePasteModalDescription";

    heading.append(eyebrow, title, subtitle);
    identity.append(platformBadge, heading);

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "btn-close cookie-connect-close";
    closeBtn.setAttribute("data-bs-dismiss", "modal");
    closeBtn.setAttribute("aria-label", "Close dialog");

    header.append(identity, closeBtn);

    const body = document.createElement("div");
    body.className = "modal-body cookie-connect-body";

    const layout = document.createElement("div");
    layout.className = "cookie-connect-layout";

    const guide = document.createElement("section");
    guide.className = "cookie-connect-panel cookie-connect-guide";

    const guideHeader = document.createElement("div");
    guideHeader.className = "cookie-connect-panel-header";

    const guideIcon = document.createElement("span");
    guideIcon.className = "material-symbols-outlined cookie-connect-panel-icon";
    guideIcon.setAttribute("aria-hidden", "true");
    guideIcon.textContent = "travel_explore";

    const guideHeading = document.createElement("div");

    const guideTitle = document.createElement("h6");
    guideTitle.className = "cookie-connect-panel-title";
    guideTitle.textContent = "Copy from your browser";

    const guideMeta = document.createElement("p");
    guideMeta.className = "cookie-connect-panel-meta";
    guideMeta.textContent = "Five quick steps · about a minute";

    guideHeading.append(guideTitle, guideMeta);
    guideHeader.append(guideIcon, guideHeading);

    const steps = document.createElement("ol");
    steps.className = "cookie-paste-steps";

    guide.append(guideHeader, steps);

    const pastePanel = document.createElement("section");
    pastePanel.className = "cookie-connect-panel cookie-connect-paste-panel";

    const pasteHeader = document.createElement("div");
    pasteHeader.className = "cookie-connect-panel-header";

    const pasteIcon = document.createElement("span");
    pasteIcon.className = "material-symbols-outlined cookie-connect-panel-icon";
    pasteIcon.setAttribute("aria-hidden", "true");
    pasteIcon.textContent = "content_paste";

    const pasteHeading = document.createElement("div");

    const label = document.createElement("label");
    label.className = "form-label cookie-connect-panel-title";
    label.textContent = "Paste the copied request";
    label.htmlFor = "cookiePasteInput";

    const pasteMeta = document.createElement("p");
    pasteMeta.className = "cookie-connect-panel-meta";
    pasteMeta.textContent = "The cookies are extracted automatically";

    pasteHeading.append(label, pasteMeta);
    pasteHeader.append(pasteIcon, pasteHeading);

    const textarea = document.createElement("textarea");
    textarea.className = "form-control cookie-paste-input";
    textarea.id = "cookiePasteInput";
    textarea.rows = 5;
    textarea.spellcheck = false;
    textarea.autocapitalize = "off";
    textarea.setAttribute("autocorrect", "off");
    textarea.placeholder = "Paste a copied request or cookie header here";

    const formatList = document.createElement("div");
    formatList.className = "cookie-connect-formats";
    formatList.setAttribute("aria-label", "Accepted paste formats");
    for (const format of ["cURL", "fetch", "Cookie header", "JSON"]) {
        const chip = document.createElement("span");
        chip.className = "cookie-connect-format";
        chip.textContent = format;
        formatList.appendChild(chip);
    }

    const formats = document.createElement("p");
    formats.className = "cookie-paste-hint";
    formats.textContent =
        "Avoid the page request at the top of the list - it usually contains no cookies. " +
        "Use one of the Fetch/XHR requests instead.";

    const error = document.createElement("p");
    error.className = "cookie-paste-error d-none";
    error.setAttribute("role", "alert");

    pastePanel.append(pasteHeader, textarea, formatList, formats, error);
    layout.append(guide, pastePanel);
    body.appendChild(layout);

    const footer = document.createElement("div");
    footer.className = "modal-footer cookie-connect-footer";

    const privacy = document.createElement("div");
    privacy.className = "cookie-connect-privacy";

    const privacyIcon = document.createElement("span");
    privacyIcon.className = "material-symbols-outlined";
    privacyIcon.setAttribute("aria-hidden", "true");
    privacyIcon.textContent = "shield_lock";

    const privacyText = document.createElement("span");
    privacyText.textContent = "Stored only on this fetchly server";

    privacy.append(privacyIcon, privacyText);

    const actions = document.createElement("div");
    actions.className = "cookie-connect-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-outline-secondary";
    cancelBtn.textContent = "Cancel";
    cancelBtn.setAttribute("data-bs-dismiss", "modal");

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "btn btn-primary cookie-connect-save";

    const saveIcon = document.createElement("span");
    saveIcon.className = "material-symbols-outlined cookie-connect-save-icon";
    saveIcon.setAttribute("aria-hidden", "true");
    saveIcon.textContent = "check";

    const saveLabel = document.createElement("span");
    saveLabel.textContent = "Save cookies";

    saveBtn.append(saveIcon, saveLabel);

    actions.append(cancelBtn, saveBtn);
    footer.append(privacy, actions);

    content.append(header, body, footer);
    dialog.appendChild(content);
    root.appendChild(dialog);
    document.body.appendChild(root);

    textarea.addEventListener("input", () => {
        saveBtn.disabled = busy || !textarea.value.trim();
        showError("");
    });

    saveBtn.addEventListener("click", () => {
        void submit();
    });

    root.addEventListener("shown.bs.modal", () => {
        textarea.focus();
    });

    // Escape, backdrop click, the close icon and Cancel all land here. A
    // dismissal mid-request would leave the caller's promise dangling, so it
    // resolves as "not saved" either way.
    root.addEventListener("hidden.bs.modal", () => {
        settle(false);
        restoreFocus();
    });

    return {
        root,
        title,
        subtitle,
        platformBadge,
        platformIcon,
        steps,
        textarea,
        error,
        saveBtn,
        saveIcon,
        saveLabel,
        cancelBtn,
    };
}

async function submit() {
    if (!els || busy) return;

    const text = els.textarea.value.trim();
    if (!text) return;

    const handler = submitHandler;
    if (!handler) return;

    const generation = openGeneration;
    setBusy(true);
    showError("");
    try {
        await handler(text);
        if (generation !== openGeneration) return;
        settle(true);
        modalInstance?.hide();
    } catch (err) {
        if (generation !== openGeneration) return;
        showError(err?.message || "Could not save these cookies");
    } finally {
        if (generation === openGeneration) {
            setBusy(false);
        }
    }
}

function renderSteps(list, label, siteUrl) {
    list.textContent = "";
    const site = siteUrl.replace(/^https:\/\//, "");
    // The page request itself is the wrong target: these sites answer it from
    // a service worker or the cache, and the dev tools then show only
    // "provisional headers" with no cookie among them. A Fetch/XHR request
    // always crosses the network, so its headers are the real ones.
    const steps = [
        `Open ${site} in a tab where you are signed in.`,
        "Press F12, open the Network tab and click the Fetch/XHR filter.",
        "Reload the page and wait for the list to fill.",
        `Right-click any entry from ${site} and choose Copy \u203a Copy as cURL.`,
        "Paste it below - the cookies are taken out of it.",
    ];
    for (const step of steps) {
        const item = document.createElement("li");
        item.textContent = step;
        list.appendChild(item);
    }
    list.setAttribute("aria-label", `How to copy ${label} cookies`);
}

/**
 * Open the paste dialog for one platform.
 *
 * @param {{
 *   platform: string,
 *   label: string,
 *   onSubmit: (text: string) => Promise<void>,
 * }} options - The platform, its display name, and the save handler. The
 *   handler should reject with a readable Error message; the dialog then
 *   stays open and shows it.
 * @returns {Promise<boolean>} Resolves true once the handler succeeded.
 */
export function openCookiePasteDialog({ platform, label, onSubmit }) {
    // Without Bootstrap there is no modal to show, and a native prompt() is
    // no substitute for a multi-line paste box. The caller surfaces this as a
    // toast rather than silently doing nothing.
    if (typeof bootstrap === "undefined" || !bootstrap?.Modal) {
        return Promise.reject(new Error("Dialog unavailable - reload the page and try again"));
    }

    if (!els) {
        els = buildModal();
        modalInstance = bootstrap.Modal.getOrCreateInstance(els.root);
    }

    // A prior dialog still open: treat it as dismissed before reusing the node.
    settle(false);
    openGeneration += 1;

    submitHandler = onSubmit;
    els.root.setAttribute("data-cookie-platform", platform);
    els.platformBadge.className = `platform-pill platform-pill--${platform} cookie-connect-platform`;
    els.platformIcon.className =
        `platform-pill__icon platform-pill__icon--${platform}`;
    els.title.textContent = `Connect ${label}`;
    els.subtitle.textContent =
        `Import your signed-in ${label} session for restricted or private downloads.`;
    els.textarea.value = "";
    els.textarea.placeholder = PLATFORM_PLACEHOLDERS[platform]
        || `curl 'https://${platform}.com/' -H 'cookie: session=…'`;
    renderSteps(els.steps, label, PLATFORM_SITES[platform] || `https://${platform}.com`);
    showError("");
    setBusy(false);

    lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    return new Promise((resolve) => {
        pendingResolve = resolve;
        modalInstance.show();
    });
}
