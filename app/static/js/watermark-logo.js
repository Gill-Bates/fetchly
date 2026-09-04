//
// app/static/js/watermark-logo.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Prepares a dropped logo for upload. What leaves this file is always a PNG
// with an alpha channel - the only thing the server stores (see
// app/utils/watermark_logo.py).
//
// A PNG is passed through untouched: it is already flat pixels, so there is
// nothing to convert and nothing to re-encode.
//
// An SVG is rasterized here, in the browser, for two reasons the server cannot
// solve on its own: the static ffmpeg builds the Docker image ships have no SVG
// decoder, and an SVG kept on the server would be a script-carrying document we
// would eventually serve back. Drawing it through an <img> puts it in the
// browser's secure static mode - no scripts, no external references - and what
// comes out is flat pixels too.
//
// Everything below is convenience, not a security boundary: the server
// re-derives every property from the uploaded bytes and refuses the file if
// they do not hold.

/** Matches MAX_LOGO_BYTES in app/utils/watermark_logo.py. */
export const MAX_LOGO_BYTES = 2 * 1024 * 1024;

// Matches MIN/MAX_LOGO_ASPECT in app/utils/watermark_logo.py.
const MIN_ASPECT = 0.1;
const MAX_ASPECT = 20;

// Matches MIN/MAX_LOGO_PIXELS in app/utils/watermark_logo.py.
const MIN_LOGO_PIXELS = 32;
const MAX_LOGO_PIXELS = 4096;

// The badge is never drawn wider than 960px (watermark.py::_MAX_LOGO_WIDTH), so
// twice that leaves room for a clean lanczos downscale and nothing is gained
// above it. Capped at the server's MAX_LOGO_PIXELS on either side.
const RASTER_TARGET_WIDTH = 1920;

// An SVG that carries any of these is refused rather than quietly flattened:
// the browser would ignore them, but a logo that only looks right when its
// script runs is not the logo the user thinks they uploaded.
const FORBIDDEN_ELEMENTS = ["script", "foreignObject"];

/**
 * Structural check of an SVG's source text.
 *
 * @param {string} text
 * @returns {{ok: boolean, error?: string, width?: number, height?: number, aspect?: number}}
 */
export function inspectSvg(text) {
    const source = String(text || "").trim();
    if (!source) {
        return { ok: false, error: "That file is empty." };
    }

    let doc;
    try {
        doc = new DOMParser().parseFromString(source, "image/svg+xml");
    } catch {
        return { ok: false, error: "That file is not valid SVG." };
    }
    if (doc.querySelector("parsererror") || doc.documentElement?.localName !== "svg") {
        return { ok: false, error: "That file is not valid SVG." };
    }

    const root = doc.documentElement;

    for (const name of FORBIDDEN_ELEMENTS) {
        if (root.getElementsByTagName(name).length > 0) {
            return { ok: false, error: `That SVG contains <${name}>, which cannot be part of a watermark.` };
        }
    }

    for (const element of [root, ...root.querySelectorAll("*")]) {
        for (const attribute of Array.from(element.attributes || [])) {
            if (attribute.name.toLowerCase().startsWith("on")) {
                return { ok: false, error: "That SVG contains scripted event handlers." };
            }
            if (attribute.localName !== "href") {
                continue;
            }
            // Internal references (#gradient, #clip) are how normal artwork is
            // built; anything pointing outside the file would not load anyway.
            const value = String(attribute.value || "").trim();
            if (!value.startsWith("#")) {
                return { ok: false, error: "That SVG references external files, which will not render." };
            }
        }
    }

    const size = intrinsicSize(root);
    if (!size) {
        return { ok: false, error: "That SVG has no width/height or viewBox, so its size is unknown." };
    }
    const aspect = size.width / size.height;
    if (!(aspect >= MIN_ASPECT && aspect <= MAX_ASPECT)) {
        return { ok: false, error: "That SVG is too elongated to sit in a video corner." };
    }

    return { ok: true, width: size.width, height: size.height, aspect };
}

/** Intrinsic size from viewBox, falling back to width/height. */
function intrinsicSize(root) {
    const viewBox = String(root.getAttribute("viewBox") || "").trim();
    if (viewBox) {
        const parts = viewBox.split(/[\s,]+/).map(Number);
        if (parts.length === 4 && parts.every(Number.isFinite) && parts[2] > 0 && parts[3] > 0) {
            return { width: parts[2], height: parts[3] };
        }
    }

    // Only unitless or px values: "10em" depends on a font this document has no
    // context for, and guessing would silently distort the logo.
    const width = parseLength(root.getAttribute("width"));
    const height = parseLength(root.getAttribute("height"));
    return width && height ? { width, height } : null;
}

function parseLength(raw) {
    const match = /^\s*([0-9]*\.?[0-9]+)\s*(px)?\s*$/i.exec(String(raw || ""));
    if (!match) {
        return 0;
    }
    const value = Number(match[1]);
    return Number.isFinite(value) && value > 0 ? value : 0;
}

/** Raster dimensions for an aspect ratio, inside the server's pixel bounds. */
export function rasterSize(aspect) {
    let width = RASTER_TARGET_WIDTH;
    let height = Math.round(width / aspect);
    if (height > MAX_LOGO_PIXELS) {
        height = MAX_LOGO_PIXELS;
        width = Math.round(height * aspect);
    }
    if (width > MAX_LOGO_PIXELS) {
        width = MAX_LOGO_PIXELS;
        height = Math.round(width / aspect);
    }
    return { width: Math.max(1, width), height: Math.max(1, height) };
}

function loadImage(source, failure) {
    return new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error(failure));
        image.src = source;
    });
}

/**
 * Render validated SVG text to a transparent PNG blob.
 *
 * @param {string} text
 * @param {number} aspect
 * @returns {Promise<Blob>}
 */
export async function rasterizeSvg(text, aspect) {
    const { width, height } = rasterSize(aspect);
    const url = URL.createObjectURL(new Blob([text], { type: "image/svg+xml" }));

    try {
        const image = await loadImage(url, "That SVG could not be rendered.");
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;

        const context = canvas.getContext("2d");
        if (!context) {
            throw new Error("This browser cannot render the logo.");
        }
        context.drawImage(image, 0, 0, width, height);

        assertUsableAlpha(context.getImageData(0, 0, width, height).data);

        const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
        if (!blob) {
            throw new Error("This browser could not encode the logo.");
        }
        return blob;
    } finally {
        URL.revokeObjectURL(url);
    }
}

/**
 * Which of the two accepted kinds a picked file claims to be, "" for neither.
 *
 * Kept in step with the drop zone's accept attribute in settings.html: a file
 * dragged onto the zone never passes through that filter.
 */
export function logoFileKind(file) {
    const name = String(file?.name || "");
    if (file?.type === "image/svg+xml" || /\.svg$/i.test(name)) {
        return "svg";
    }
    if (file?.type === "image/png" || /\.png$/i.test(name)) {
        return "png";
    }
    return "";
}

/**
 * The server's size and shape rules, stated for already-known dimensions.
 *
 * @returns {string} the reason the artwork is unusable, or "" if it is fine
 */
export function pixelSizeProblem(width, height) {
    if (!(width > 0 && height > 0)) {
        return "That file is not a readable image.";
    }
    if (width < MIN_LOGO_PIXELS || height < MIN_LOGO_PIXELS) {
        return `That image is smaller than ${MIN_LOGO_PIXELS}x${MIN_LOGO_PIXELS} pixels.`;
    }
    if (width > MAX_LOGO_PIXELS || height > MAX_LOGO_PIXELS) {
        return `That image is larger than ${MAX_LOGO_PIXELS} pixels on a side.`;
    }
    const aspect = width / height;
    if (!(aspect >= MIN_ASPECT && aspect <= MAX_ASPECT)) {
        return "That image is too elongated to sit in a video corner.";
    }
    return "";
}

/**
 * Check a dropped PNG's dimensions, decoding it only far enough to read them.
 *
 * The transparency check that the SVG path does on its own render is left to
 * the server here: it reads the real alpha plane with ffmpeg at full
 * resolution, where a browser would have to pull a multi-megabyte pixel buffer
 * back out of a canvas to reach the same answer less reliably.
 *
 * @param {File} file
 * @returns {Promise<{width: number, height: number}>}
 */
export async function inspectPng(file) {
    const url = URL.createObjectURL(file);
    try {
        const image = await loadImage(url, "That PNG could not be read.");
        const width = image.naturalWidth;
        const height = image.naturalHeight;
        const problem = pixelSizeProblem(width, height);
        if (problem) {
            throw new Error(problem);
        }
        return { width, height };
    } finally {
        URL.revokeObjectURL(url);
    }
}

/**
 * Turn a dropped file into the PNG blob the upload sends.
 *
 * @param {File} file
 * @returns {Promise<Blob>}
 */
export async function prepareLogoUpload(file) {
    if (!file) {
        throw new Error("No file to upload.");
    }
    if (file.size > MAX_LOGO_BYTES) {
        throw new Error(`That file is larger than ${MAX_LOGO_BYTES / (1024 * 1024)} MB.`);
    }

    const kind = logoFileKind(file);
    if (kind === "png") {
        // Uploaded byte for byte: re-encoding a PNG through a canvas would only
        // cost quality and size for a file that is already what we store.
        await inspectPng(file);
        return file;
    }
    if (kind === "svg") {
        const text = await file.text();
        const inspected = inspectSvg(text);
        if (!inspected.ok) {
            throw new Error(inspected.error);
        }
        return rasterizeSvg(text, inspected.aspect);
    }
    throw new Error("Please choose an SVG or a PNG file.");
}

/**
 * A watermark has to be artwork on the video, not a hole and not a box.
 *
 * @param {Uint8ClampedArray} pixels RGBA, as returned by getImageData
 */
export function assertUsableAlpha(pixels) {
    let hasInk = false;
    let hasTransparency = false;

    for (let index = 3; index < pixels.length; index += 4) {
        const alpha = pixels[index];
        if (alpha > 8) {
            hasInk = true;
        }
        if (alpha < 250) {
            hasTransparency = true;
        }
        if (hasInk && hasTransparency) {
            return;
        }
    }

    if (!hasInk) {
        throw new Error("That SVG renders as an empty image.");
    }
    throw new Error("That SVG has no transparency; it would cover the video with a solid box.");
}
