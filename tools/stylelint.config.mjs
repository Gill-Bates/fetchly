//
// tools/stylelint.config.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

export default {
    extends: ["stylelint-config-standard"],

    // Vendored Bootstrap and the WaveSurfer bundle are not ours to reformat.
    ignoreFiles: [
        "**/node_modules/**",
        "app/static/vendor/**",
        "data/**",
        "site/**",
        ".venv/**",
    ],

    rules: {
        // The stylesheet is hand-written and heavily commented; these are
        // cosmetic rules that would churn thousands of lines without changing
        // a single rendered pixel.
        "comment-empty-line-before": null,
        "declaration-empty-line-before": null,
        "rule-empty-line-before": null,
        "custom-property-empty-line-before": null,
        "no-descending-specificity": null,
        "alpha-value-notation": null,
        "color-function-notation": null,
        "value-keyword-case": null,
        "font-family-name-quotes": null,
        "selector-not-notation": null,
        "keyframes-name-pattern": null,

        // rgba() with an alpha channel is the notation used throughout; the
        // rgb()-with-slash form buys nothing and would churn 140 declarations.
        "color-function-alias-notation": null,

        // -webkit-* prefixes here are load-bearing, not legacy: Safari and
        // iOS/iPadOS still need -webkit-backdrop-filter, -webkit-line-clamp and
        // -webkit-overflow-scrolling. Stripping them would break the very
        // browsers the mobile audit targets.
        "property-no-vendor-prefix": null,

        // Range notation - (width <= 767.98px) - only landed in Safari 16.4.
        // The prefix form works on every iOS version fetchly still supports.
        "media-feature-range-notation": null,

        // BEM: block, block__element, block--modifier.
        "selector-class-pattern": [
            "^[a-z][a-z0-9]*(-[a-z0-9]+)*(__[a-z0-9]+(-[a-z0-9]+)*)?(--[a-z0-9]+(-[a-z0-9]+)*)?$",
            { message: "Expected class selector to be BEM kebab-case (block__element--modifier)" },
        ],

        // IDs are camelCase because they are the handles the JS looks up by
        // name (#jobsCard, #settingsForm); renaming them would desync the two.
        "selector-id-pattern": [
            "^[a-z][a-zA-Z0-9]*(-[a-z0-9]+)*$",
            { message: "Expected id selector to be camelCase or kebab-case, matching the JS lookups" },
        ],

        // style.css is organised in component sections, so a selector may be
        // declared once for layout and again in a later theme section. That is
        // deliberate here, not an accidental override.
        "no-duplicate-selectors": null,

        // These catch real defects rather than formatting drift.
        "block-no-empty": true,
        "color-no-invalid-hex": true,
        "declaration-block-no-duplicate-properties": [
            true,
            {
                // A repeated declaration is the standard progressive-enhancement
                // fallback pattern here (min-height: 100vh then 100dvh).
                ignore: ["consecutive-duplicates-with-different-values"],
            },
        ],
        "declaration-block-no-shorthand-property-overrides": true,
        "font-family-no-duplicate-names": true,
        "font-family-no-missing-generic-family-keyword": true,
        "function-calc-no-unspaced-operator": true,
        "keyframe-declaration-no-important": true,
        "media-feature-name-no-unknown": true,
        "named-grid-areas-no-invalid": true,
        "no-duplicate-at-import-rules": true,
        "no-invalid-position-at-import-rule": true,
        "property-no-unknown": true,
        "selector-pseudo-class-no-unknown": true,
        "selector-pseudo-element-no-unknown": true,
        "selector-type-no-unknown": true,
        "string-no-newline": true,
        "unit-no-unknown": true,

        // iOS/iPad safety net: `100vh` does not account for Safari's collapsing
        // toolbar, so a full-height shell overflows on iPhone and in iPad
        // Split View. Pair it with a 100dvh (or svh) declaration right after -
        // the duplicate-property ignore above permits exactly that.
        "declaration-property-value-disallowed-list": [
            {
                "/^(min-|max-)?height$/": ["/^100vh$/"],
            },
            {
                message:
                    "100vh breaks under Safari's collapsing toolbar. Declare 100vh then 100dvh " +
                    "as a progressive-enhancement pair, and mark the fallback with " +
                    "/* stylelint-disable-next-line declaration-property-value-disallowed-list */.",
                severity: "warning",
            },
        ],
    },
};
