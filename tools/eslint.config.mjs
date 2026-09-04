//
// tools/eslint.config.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import js from "@eslint/js";
import globals from "globals";

export default [
    {
        // Vendored (minified, not ours) and build output. app/static/vendor is
        // checked in for the script-src 'self' CSP but left unlinted.
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
            // Repo rule: no native dialogs (use confirmModal()/showToast()).
            "no-alert": "error",
            "no-restricted-globals": [
                "error",
                { name: "confirm", message: "Use confirmModal() from app/static/js/confirm.js." },
                { name: "alert", message: "Use showToast() from app/static/js/toast.js." },
                { name: "prompt", message: "Use a modal; see app/static/js/confirm.js." },
            ],
            // A stray console.log ships to users and trips the ui-lint audit.
            "no-console": ["error", { allow: ["warn", "error"] }],
            "no-var": "error",
            "prefer-const": "error",
            eqeqeq: ["error", "smart"],
            "no-unused-vars": ["error", { argsIgnorePattern: "^_", caughtErrors: "none" }],
            "no-implicit-globals": "error",
            "no-eval": "error",
            "no-implied-eval": "error",
            "no-new-func": "error",
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
