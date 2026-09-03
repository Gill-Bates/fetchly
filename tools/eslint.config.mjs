//
// tools/eslint.config.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import js from "@eslint/js";
import globals from "globals";

export default [
    {
        // Vendored libraries ship minified and are not ours to fix; node_modules
        // and build output are noise. app/static/vendor is what keeps the
        // reverse-proxy CSP at script-src 'self', so it stays checked in but
        // unlinted.
        ignores: [
            "**/node_modules/**",
            "app/static/vendor/**",
            "data/**",
            "site/**",
            ".mkdocs-cache/**",
            ".venv/**",
        ],
    },

    js.configs.recommended,

    {
        // Browser bundle: ES modules loaded by the templates.
        files: ["app/static/js/**/*.js"],
        languageOptions: {
            ecmaVersion: 2024,
            sourceType: "module",
            globals: {
                ...globals.browser,
                // Vendored, loaded via <script> before the module graph.
                bootstrap: "readonly",
            },
        },
        rules: {
            // The repo rule: no native dialogs, use confirmModal() from
            // app/static/js/confirm.js. Previously only documented in
            // docs/development/contributing.md, so nothing enforced it.
            "no-alert": "error",
            "no-restricted-globals": [
                "error",
                { name: "confirm", message: "Use confirmModal() from app/static/js/confirm.js." },
                { name: "alert", message: "Use showToast() from app/static/js/toast.js." },
                { name: "prompt", message: "Use a modal; see app/static/js/confirm.js." },
            ],
            // A stray console.log in the browser bundle is shipped to users and
            // shows up as noise in the ui-lint console audit.
            "no-console": ["error", { allow: ["warn", "error"] }],
            "no-var": "error",
            "prefer-const": "error",
            eqeqeq: ["error", "smart"],
            "no-unused-vars": ["error", { argsIgnorePattern: "^_", caughtErrors: "none" }],
            "no-implicit-globals": "error",
            "no-eval": "error",
            "no-implied-eval": "error",
            "no-new-func": "error",
            // innerHTML with interpolated data is how XSS gets in; the codebase
            // builds nodes with textContent instead.
            "no-script-url": "error",
        },
    },

    {
        // Node-side tooling: the front-end contract tests and the ui-lint runner.
        files: ["tests/js/**/*.mjs", "tools/**/*.mjs"],
        languageOptions: {
            ecmaVersion: 2024,
            sourceType: "module",
            globals: {
                ...globals.node,
                // tests/js installs a fake DOM on globalThis before importing
                // the browser module under test.
                document: "writable",
                window: "writable",
            },
        },
        rules: {
            "no-unused-vars": ["error", { argsIgnorePattern: "^_", caughtErrors: "none" }],
            "no-var": "error",
            "prefer-const": "error",
            eqeqeq: ["error", "smart"],
        },
    },

    {
        // Code the ui-lint runner injects into the page via page.evaluate():
        // it executes in the browser, not in Node.
        files: ["tools/ui-lint/**/*.mjs"],
        languageOptions: {
            globals: {
                ...globals.node,
                ...globals.browser,
            },
        },
    },
];
