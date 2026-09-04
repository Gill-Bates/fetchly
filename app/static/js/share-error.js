//
// app/static/js/share-error.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Pointer parallax for the public "Lost in Space" share-error page.
 *
 * Layers declare depth via data-parallax. Offsets go through element.style.*
 * (CSSOM), which the style-src 'self' CSP allows (unlike style.cssText).
 */

const REDUCED_MOTION = window.matchMedia?.("(prefers-reduced-motion: reduce)");
// Fraction of the remaining distance covered per frame - eases the layers
// toward the pointer instead of snapping to it.
const EASING = 0.08;
const SETTLE_THRESHOLD = 0.05;

/** @type {{ el: HTMLElement, depth: number, x: number, y: number }[]} */
const layers = [];
let targetX = 0;
let targetY = 0;
let rafId = 0;

function collectLayers() {
    for (const el of document.querySelectorAll("[data-parallax]")) {
        if (!(el instanceof HTMLElement)) continue;
        const depth = Number.parseFloat(el.dataset.parallax || "0");
        if (!Number.isFinite(depth) || depth === 0) continue;
        layers.push({ el, depth, x: 0, y: 0 });
    }
}

function step() {
    let moving = false;

    for (const layer of layers) {
        const destX = targetX * layer.depth;
        const destY = targetY * layer.depth;
        layer.x += (destX - layer.x) * EASING;
        layer.y += (destY - layer.y) * EASING;

        if (Math.abs(destX - layer.x) > SETTLE_THRESHOLD || Math.abs(destY - layer.y) > SETTLE_THRESHOLD) {
            moving = true;
        }
        layer.el.style.transform = `translate3d(${layer.x.toFixed(2)}px, ${layer.y.toFixed(2)}px, 0)`;
    }

    // Stop the loop once everything has settled; a pointer move restarts it.
    rafId = moving ? window.requestAnimationFrame(step) : 0;
}

function schedule() {
    if (rafId === 0) {
        rafId = window.requestAnimationFrame(step);
    }
}

function onPointerMove(event) {
    // Normalize to -0.5 .. 0.5 around the viewport centre.
    targetX = (event.clientX / window.innerWidth) - 0.5;
    targetY = (event.clientY / window.innerHeight) - 0.5;
    schedule();
}

function onPointerLeave() {
    targetX = 0;
    targetY = 0;
    schedule();
}

function enable() {
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    document.addEventListener("pointerleave", onPointerLeave, { passive: true });
}

function disable() {
    window.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerleave", onPointerLeave);
    if (rafId !== 0) {
        window.cancelAnimationFrame(rafId);
        rafId = 0;
    }
    for (const layer of layers) {
        layer.x = 0;
        layer.y = 0;
        layer.el.style.transform = "";
    }
}

function sync() {
    if (REDUCED_MOTION?.matches) {
        disable();
    } else {
        enable();
    }
}

collectLayers();
if (layers.length > 0) {
    sync();
    // Honour a mid-visit change to the OS motion preference.
    REDUCED_MOTION?.addEventListener?.("change", sync);
}
