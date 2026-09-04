//
// tests/js/limited-playback.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// With "Universally playable output" off, a download keeps the source codec and
// container. The job list has to say so: a .webm the user cannot open on their
// phone must look like a choice, not like a broken file.

import assert from "node:assert/strict";
import test from "node:test";

// jobs.js pulls in config.js and utils.js, which read bootstrap data off the
// document at import time.
globalThis.document = {
    cookie: "",
    documentElement: { dataset: { lalalMaxDurationSeconds: "600" } },
    querySelector() {
        return null;
    },
    getElementById() {
        return null;
    },
};
globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { hasLimitedPlayback } = await import("../../app/static/js/jobs.js");

test("H.264 in MP4 plays everywhere", () => {
    assert.equal(
        hasLimitedPlayback({ type: "video", codec: "h264", filename: "/data/j/Clip (maxQuality).mp4" }),
        false,
    );
});

test("AV1 and VP9 are flagged whatever the container says", () => {
    assert.equal(
        hasLimitedPlayback({ type: "video", codec: "av1", filename: "/data/j/Clip.mkv" }),
        true,
    );
    assert.equal(
        hasLimitedPlayback({ type: "video", codec: "vp9", filename: "/data/j/Clip.webm" }),
        true,
    );
});

test("an H.264 stream in a Matroska container is still flagged", () => {
    // Safari refuses the container, not the codec.
    assert.equal(
        hasLimitedPlayback({ type: "video", codec: "h264", filename: "/data/j/Clip.mkv" }),
        true,
    );
});

test("audio jobs are never flagged", () => {
    assert.equal(
        hasLimitedPlayback({ type: "audio", codec: "opus", filename: "/data/j/Track.source.webm" }),
        false,
    );
});

test("a job without a finished file yet is not flagged", () => {
    assert.equal(hasLimitedPlayback({ type: "video", codec: "", filename: "" }), false);
    assert.equal(hasLimitedPlayback({ type: "video" }), false);
});
