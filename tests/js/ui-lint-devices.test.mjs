//
// tests/js/ui-lint-devices.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The ui-lint runner separates form factor from touch input. Compact layout
// keys off the viewport width AND the touch axis (touch iPads render the feed
// up to 1366px via `(pointer: coarse)`), while hit-area sizing keys off touch
// alone - collapsing the two back into one flag is the regression that leaves
// some iPad viewport either over-sized for the desktop minimum or asked for
// DOM it never renders.
//

import assert from "node:assert/strict";
import test from "node:test";

const {
    DEVICE_PROFILES,
    VIEW_DEFS,
    COMPACT_LAYOUT_MAX_WIDTH,
    COMPACT_LAYOUT_TOUCH_MAX_WIDTH,
    createContextOptions,
    profileHasTouch,
    profileIsCompactLayout,
} = await import("../../tools/ui-lint/run-ui-lint.mjs");

const widthOf = (device) => createContextOptions(device).viewport.width;

test("covers phone, tablet and desktop form factors", () => {
    const factors = new Set(Object.values(DEVICE_PROFILES).map((p) => p.formFactor));
    assert.deepEqual([...factors].sort(), ["desktop", "phone", "tablet"]);
});

test("every profile resolves to a real viewport", () => {
    for (const device of Object.keys(DEVICE_PROFILES)) {
        const { viewport } = createContextOptions(device);
        assert.ok(Number.isFinite(viewport?.width) && viewport.width > 0, `${device} has no viewport width`);
    }
});

test("the tablet band around the CSS width breakpoint is covered on both sides", () => {
    const tabletWidths = Object.entries(DEVICE_PROFILES)
        .filter(([, profile]) => profile.formFactor === "tablet")
        .map(([device]) => widthOf(device));

    assert.ok(
        tabletWidths.some((w) => w <= COMPACT_LAYOUT_MAX_WIDTH),
        "no tablet profile below the 1024px width breakpoint",
    );
    assert.ok(
        tabletWidths.some((w) => w > COMPACT_LAYOUT_MAX_WIDTH),
        "no tablet profile past 1024px - the coarse-pointer feed extension is untested",
    );
    assert.ok(
        tabletWidths.includes(COMPACT_LAYOUT_MAX_WIDTH),
        "no tablet profile sits exactly on the breakpoint, where off-by-one mistakes show",
    );
    assert.ok(
        tabletWidths.every((w) => w <= COMPACT_LAYOUT_TOUCH_MAX_WIDTH),
        "a tablet profile is wider than the 1366px coarse-pointer bound and would render the desktop table",
    );
});

test("touch and compact layout stay separate axes", () => {
    // The desktop context is neither.
    assert.equal(profileHasTouch("desktop"), false);
    assert.equal(profileIsCompactLayout("desktop"), false);

    // Every touch profile within the 1366px coarse-pointer bound renders the
    // compact feed - the contract this extension adds.
    for (const device of Object.keys(DEVICE_PROFILES)) {
        if (profileHasTouch(device) && widthOf(device) <= COMPACT_LAYOUT_TOUCH_MAX_WIDTH) {
            assert.ok(
                profileIsCompactLayout(device),
                `${device} is a touch screen within 1366px but does not render the feed`,
            );
        }
    }

    // The axes are still distinct: compact layout keys off touch above 1024px,
    // so a wide viewport is compact only when it is also a touch screen.
    for (const device of Object.keys(DEVICE_PROFILES)) {
        if (widthOf(device) > COMPACT_LAYOUT_MAX_WIDTH && !profileHasTouch(device)) {
            assert.equal(
                profileIsCompactLayout(device),
                false,
                `${device} is a wide non-touch viewport but renders the compact feed`,
            );
        }
    }
});

test("compact layout follows the viewport and touch axis, not the device name", () => {
    for (const device of Object.keys(DEVICE_PROFILES)) {
        const width = widthOf(device);
        const expected = width <= COMPACT_LAYOUT_MAX_WIDTH
            || (width <= COMPACT_LAYOUT_TOUCH_MAX_WIDTH && profileHasTouch(device));
        assert.equal(
            profileIsCompactLayout(device),
            expected,
            `${device} disagrees with its own viewport width and touch axis`,
        );
    }
});

test("every device profile is exercised by at least one view", () => {
    const used = new Set(VIEW_DEFS.map((view) => view.device));
    for (const device of Object.keys(DEVICE_PROFILES)) {
        assert.ok(used.has(device), `no view runs on the '${device}' profile`);
    }
});

test("every view names a known device and a unique result name", () => {
    const names = new Set();
    for (const view of VIEW_DEFS) {
        assert.ok(DEVICE_PROFILES[view.device], `${view.name} names unknown device '${view.device}'`);
        assert.ok(!names.has(view.name), `duplicate view name '${view.name}'`);
        names.add(view.name);
    }
});

test("compact dashboards require the feed, wide ones the table", () => {
    for (const view of VIEW_DEFS.filter((v) => v.url === "/")) {
        const selectors = view.requiredSelectors;
        if (profileIsCompactLayout(view.device)) {
            assert.ok(
                selectors.includes("#jobsMobileList"),
                `${view.name} is inside the compact band but does not require the feed`,
            );
        } else {
            assert.ok(
                selectors.includes("#jobsTable"),
                `${view.name} is past the breakpoint but does not require the desktop table`,
            );
        }
    }
});

test("the invalid-login check has a login view per device it runs on", () => {
    for (const device of ["desktop", "mobile", "tablet"]) {
        assert.ok(
            VIEW_DEFS.some((view) => view.url === "/login" && view.device === device),
            `no /login view for '${device}'`,
        );
    }
});
