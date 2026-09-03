//
// tests/js/ui-lint-devices.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// The ui-lint runner separates form factor from touch input. Collapsing the
// two back into one flag is exactly the regression that left the iPad
// untested: it either got the 32px desktop hit-area minimum, or was asked for
// phone-only DOM it never renders.
//

import assert from "node:assert/strict";
import test from "node:test";

const {
    DEVICE_PROFILES,
    VIEW_DEFS,
    COMPACT_LAYOUT_MAX_WIDTH,
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

test("the tablet band around the CSS breakpoint is covered on both sides", () => {
    const tabletWidths = Object.entries(DEVICE_PROFILES)
        .filter(([, profile]) => profile.formFactor === "tablet")
        .map(([device]) => widthOf(device));

    assert.ok(
        tabletWidths.some((w) => w <= COMPACT_LAYOUT_MAX_WIDTH),
        "no tablet profile below the compact breakpoint",
    );
    assert.ok(
        tabletWidths.some((w) => w > COMPACT_LAYOUT_MAX_WIDTH),
        "no tablet profile above the compact breakpoint - the desktop-table-on-touch case is untested",
    );
    assert.ok(
        tabletWidths.includes(COMPACT_LAYOUT_MAX_WIDTH),
        "no tablet profile sits exactly on the breakpoint, where off-by-one mistakes show",
    );
});

test("touch and compact layout are independent axes", () => {
    // The whole point: a device that is touch but renders the desktop layout.
    const touchAndWide = Object.keys(DEVICE_PROFILES)
        .filter((device) => profileHasTouch(device) && !profileIsCompactLayout(device));
    assert.ok(touchAndWide.length > 0, "no touch profile outside the compact band");

    // ... and the desktop context must stay non-touch.
    assert.equal(profileHasTouch("desktop"), false);
    assert.equal(profileIsCompactLayout("desktop"), false);
});

test("compact layout follows the viewport, not the device name", () => {
    for (const device of Object.keys(DEVICE_PROFILES)) {
        assert.equal(
            profileIsCompactLayout(device),
            widthOf(device) <= COMPACT_LAYOUT_MAX_WIDTH,
            `${device} disagrees with its own viewport width`,
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
