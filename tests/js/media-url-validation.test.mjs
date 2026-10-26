//
// tests/js/media-url-validation.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

globalThis.document = {
    cookie: "",
    documentElement: { dataset: {} },
    querySelector() {
        return null;
    },
};

const { isValidMediaUrl, detectPlatform, PLATFORM } = await import("../../app/static/js/utils.js");

// isValidMediaUrl() is a cheap client-side preflight ONLY. The authority for
// which URLs are accepted is validate_media_url() in app/utils/platform.py,
// pinned by tests/test_media_url_contract.py. The frontend no longer
// reproduces the per-platform grammar, so these tests cover the reduced
// responsibility: a known host over HTTPS clears the preflight, obvious junk
// (no HTTPS, unknown host, credentials, custom port) is refused early, and the
// server has the final word on everything in between.

test("accepts a known host over HTTPS for every supported platform", () => {
    for (const url of [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.tiktok.com/@someuser/video/1234567890123456789",
        "https://vm.tiktok.com/ZMabc123/",
        "https://www.instagram.com/reel/Cabcdef1234/",
        "https://www.facebook.com/watch?v=1234567890",
        "https://fb.watch/abcDEF123/",
    ]) {
        assert.equal(isValidMediaUrl(url), true, url);
    }
});

test("does not reject a URL the server would accept but the old grammar did not model", () => {
    // These clear the preflight (known host + HTTPS) and are left to the
    // server to judge; the frontend must not block them.
    assert.equal(isValidMediaUrl("https://www.facebook.com/somepage/videos/some-slug/1234567890"), true);
    assert.equal(isValidMediaUrl("https://www.instagram.com/p/Cabcdef1234/"), true);
});

test("rejects a non-HTTPS URL", () => {
    assert.equal(isValidMediaUrl("http://www.youtube.com/watch?v=dQw4w9WgXcQ"), false);
});

test("rejects an unknown host", () => {
    assert.equal(isValidMediaUrl("https://example.com/watch?v=dQw4w9WgXcQ"), false);
    assert.equal(isValidMediaUrl("https://evil.test/@someuser/video/1234567890123456789"), false);
});

test("rejects embedded credentials and a custom port", () => {
    assert.equal(isValidMediaUrl("https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ"), false);
    assert.equal(isValidMediaUrl("https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ"), false);
});

test("rejects empty and non-string input", () => {
    assert.equal(isValidMediaUrl(""), false);
    assert.equal(isValidMediaUrl(null), false);
    assert.equal(isValidMediaUrl(undefined), false);
    assert.equal(isValidMediaUrl(42), false);
});

test("detectPlatform stays a host-based UI helper for pills and icons", () => {
    assert.equal(detectPlatform("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), PLATFORM.YOUTUBE);
    assert.equal(detectPlatform("https://www.tiktok.com/@someuser/video/1234567890123456789"), PLATFORM.TIKTOK);
    assert.equal(detectPlatform("https://www.instagram.com/reel/Cabcdef1234/"), PLATFORM.INSTAGRAM);
    assert.equal(detectPlatform("https://fb.watch/abcDEF123/"), PLATFORM.FACEBOOK);
    assert.equal(detectPlatform("https://example.com/whatever"), null);
});
