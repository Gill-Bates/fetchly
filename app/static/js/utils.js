//
// app/static/js/utils.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

export const YOUTUBE_URL_REGEX = /^https?:\/\/(?:www\.|m\.)?(?:youtube\.com\/(?:watch\?.*v=|embed\/|v\/|shorts\/)|youtu\.be\/)[\w-]{11}(?:[?&].*)?$/i;

export function escHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

export function humanSize(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return "-";
    if (value >= 1073741824) return `${(value / 1073741824).toFixed(2)} GB`;
    if (value >= 1048576) return `${(value / 1048576).toFixed(1)} MB`;
    if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${Math.round(value)} B`;
}

export function formatDuration(sec) {
    if (sec === null || sec === undefined) return "–";
    const value = Number(sec);
    if (!Number.isFinite(value) || value < 0) return "–";
    const minutes = Math.floor(value / 60);
    const seconds = Math.floor(value % 60);
    return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

export function getCookie(name) {
    const prefix = `${name}=`;
    const found = document.cookie
        .split(";")
        .map(v => v.trim())
        .find(v => v.startsWith(prefix));
    return found ? decodeURIComponent(found.slice(prefix.length)) : "";
}

export function isValidYouTubeUrl(url) {
    if (!url || typeof url !== "string") return false;
    const value = url.trim();
    if (value.length > 2048) return false;
    return YOUTUBE_URL_REGEX.test(value);
}

export function extractYouTubeVideoId(url) {
    if (!isValidYouTubeUrl(url)) return "";

    try {
        const parsed = new URL(url.trim());
        if (parsed.hostname.includes("youtu.be")) {
            return parsed.pathname.split("/").filter(Boolean).pop() || "";
        }

        const directId = parsed.searchParams.get("v");
        if (directId) return directId;

        const segments = parsed.pathname.split("/").filter(Boolean);
        return segments.pop() || "";
    } catch {
        return "";
    }
}