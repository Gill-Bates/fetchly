//
// tests/js/watermark-logo.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The SVG structure checks need a DOM (DOMParser) that Node does not ship, and
// reading a PNG's dimensions needs an image decoder; both are exercised in the
// browser. What is covered here are the pure decisions the drop zone makes
// before any of that - which route a dropped file takes, the bounds its pixels
// have to be inside, the raster size handed to the canvas, and whether a
// rendered result is usable as a watermark at all.
// The security-relevant half of the validation lives on the server anyway
// (tests/test_watermark_logo.py), because that is where the bytes are stored.

import assert from "node:assert/strict";
import test from "node:test";

const { rasterSize, assertUsableAlpha, logoFileKind, pixelSizeProblem, MAX_LOGO_BYTES } = await import(
    "../../app/static/js/watermark-logo.js"
);

// Kept in step with MAX_LOGO_BYTES in app/utils/watermark_logo.py: a client
// that allowed more would only produce uploads the server rejects.
test("the client size cap matches the server's", () => {
    assert.equal(MAX_LOGO_BYTES, 2 * 1024 * 1024);
});

// The two accepted kinds take different routes: a PNG is uploaded untouched, an
// SVG is rasterized first. Anything else never reaches either.
test("both accepted kinds are recognized by type and by extension", () => {
    assert.equal(logoFileKind({ name: "logo.svg", type: "" }), "svg");
    assert.equal(logoFileKind({ name: "logo", type: "image/svg+xml" }), "svg");
    assert.equal(logoFileKind({ name: "LOGO.PNG", type: "" }), "png");
    assert.equal(logoFileKind({ name: "logo", type: "image/png" }), "png");
    assert.equal(logoFileKind({ name: "logo.jpg", type: "image/jpeg" }), "");
    assert.equal(logoFileKind(null), "");
});

// The two signals can disagree - a file manager that types .svg as image/png,
// or a .png that is really an SVG. The routes differ (SVG is parsed and
// rasterized, PNG is uploaded as-is), so which signal wins has to be decided
// rather than left to selector order. It is "SVG if either signal says SVG",
// and that is the safe direction: the SVG route runs the structure checks in
// inspectSvg() (forbidden elements, on* handlers) and rasterizes, whereas the
// PNG route would hand the bytes straight to the upload. Mislabelling only
// costs a rejection; the reverse would skip the checks. The server re-derives
// the kind from the bytes either way (app/utils/watermark_logo.py), so this is
// a UX contract, not the security boundary.
test("a MIME type and an extension that disagree resolve to SVG", () => {
    assert.equal(logoFileKind({ name: "logo.svg", type: "image/png" }), "svg");
    assert.equal(logoFileKind({ name: "logo.png", type: "image/svg+xml" }), "svg");

    // An unaccepted MIME type does not veto an accepted extension, and vice
    // versa: either signal on its own is enough to pick a route.
    assert.equal(logoFileKind({ name: "logo.svg", type: "application/octet-stream" }), "svg");
    assert.equal(logoFileKind({ name: "logo.png", type: "application/octet-stream" }), "png");
    assert.equal(logoFileKind({ name: "logo.bin", type: "image/png" }), "png");

    // A name that only contains the extension mid-string is not a match.
    assert.equal(logoFileKind({ name: "logo.svg.txt", type: "" }), "");
    assert.equal(logoFileKind({ name: "png", type: "" }), "");
});

// Mirrors the server's size and shape rules, so a PNG that cannot be stored is
// refused before it is uploaded (app/utils/watermark_logo.py).
test("a PNG's dimensions are held to the server's bounds", () => {
    assert.equal(pixelSizeProblem(400, 120), "");
    assert.match(pixelSizeProblem(0, 0), /not a readable image/);
    assert.match(pixelSizeProblem(31, 120), /smaller than 32x32/);
    assert.match(pixelSizeProblem(4097, 120), /larger than 4096/);
    assert.match(pixelSizeProblem(4000, 100), /too elongated/);
});

test("a wide logo is rendered at twice the widest badge", () => {
    // The badge is never drawn wider than 960px, so 1920 leaves headroom for a
    // clean downscale without wasting pixels.
    assert.deepEqual(rasterSize(400 / 120), { width: 1920, height: 576 });
});

test("a tall logo is bounded by the server's pixel limit instead", () => {
    assert.deepEqual(rasterSize(1 / 4), { width: 1024, height: 4096 });
});

test("the pixel and aspect bounds are inclusive on both edges", () => {
    // Matches MIN/MAX_LOGO_PIXELS and MIN/MAX_LOGO_ASPECT in
    // app/utils/watermark_logo.py, which use <= on every side.
    assert.equal(pixelSizeProblem(32, 32), "");
    assert.equal(pixelSizeProblem(4096, 4096), "");
    assert.equal(pixelSizeProblem(4000, 200), "");               // aspect exactly 20
    assert.equal(pixelSizeProblem(400, 4000), "");               // aspect exactly 0.1
    assert.match(pixelSizeProblem(200, 4000), /too elongated/);  // aspect 0.05
});

function pixels(alphas) {
    return Uint8ClampedArray.from(alphas.flatMap((alpha) => [255, 255, 255, alpha]));
}

test("artwork with ink and transparency is accepted", () => {
    assert.doesNotThrow(() => assertUsableAlpha(pixels([0, 255, 0, 128])));
});

test("a fully opaque render is refused as a box over the video", () => {
    assert.throws(() => assertUsableAlpha(pixels([255, 255, 255])), /no transparency/);
});

test("an empty render is refused", () => {
    assert.throws(() => assertUsableAlpha(pixels([0, 0, 0, 4])), /empty image/);
});
