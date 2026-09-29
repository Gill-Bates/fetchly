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

// Mirrors tests/test_platform_urls.py; _validate_facebook_url() and
// validate_youtube_url() in app/utils/ are the authority for both.
test("accepts Facebook story links", () => {
    const shared = "https://www.facebook.com/stories/122108714109304856/"
        + "UzpfSVNDOjQ3MzkzMzkwNDk3MjQ0NDk=/?view_single=1&source=shared_permalink";
    assert.equal(detectPlatform(shared), PLATFORM.FACEBOOK);
    assert.equal(isValidMediaUrl(shared), true);
    assert.equal(isValidMediaUrl("https://www.facebook.com/stories/122108714109304856"), true);
    assert.equal(
        isValidMediaUrl("https://www.facebook.com/stories/122108714109304856/UzpfSVNDOjQ3MzkzMzkwNDk3MjQ0NDk%3D/"),
        true,
    );
});

test("rejects Facebook story links without a numeric set id", () => {
    assert.equal(isValidMediaUrl("https://www.facebook.com/stories"), false);
    assert.equal(isValidMediaUrl("https://www.facebook.com/stories/not-a-set-id"), false);
});

test("accepts YouTube shorts links", () => {
    assert.equal(isValidMediaUrl("https://www.youtube.com/shorts/dQw4w9WgXcQ"), true);
    assert.equal(isValidMediaUrl("https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share"), true);
});

test("rejects YouTube shorts links without a video id", () => {
    assert.equal(isValidMediaUrl("https://www.youtube.com/shorts"), false);
    assert.equal(isValidMediaUrl("https://www.youtube.com/shorts/tooshort"), false);
});
