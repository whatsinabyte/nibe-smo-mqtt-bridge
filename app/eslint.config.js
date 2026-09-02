import js from '@eslint/js';
import globals from 'globals';

export default [
  js.configs.recommended,

  // nibe-entity-manager-card.js — the Lovelace custom card. Runs in the
  // browser inside Home Assistant's frontend: browser globals only, no
  // Node.js globals (process, require, etc. must never appear here).
  {
    files: ['nibe-entity-manager-card.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'script',
      globals: {
        ...globals.browser,
        // Loaded lazily from a CDN <script> tag at runtime — see _loadFuse().
        Fuse: 'readonly',
        // Set by mid-era Home Assistant frontend builds — see
        // _formatDateTimeHA()'s documented fallback chain.
        hassUtil: 'readonly',
      },
    },
    rules: {
      'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },

  // Vitest suite + shared test support helpers — Node.js test runner
  // environment (Vitest) driving a jsdom browser environment, so both
  // global sets apply.
  {
    files: ['tests-js/**/*.js'],
    ignores: ['tests-js/e2e/**'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.node,
        ...globals.browser,
      },
    },
    rules: {
      'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },

  // Playwright smoke suite: the top-level test file runs under Node, but
  // callbacks passed to page.evaluate()/page.locator() etc. are serialised
  // and executed inside the browser page, so `window`/`document` appear
  // inline in this file too — both global sets apply.
  {
    files: ['tests-js/e2e/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.node,
        ...globals.browser,
      },
    },
  },

  // Playwright config — Node.js only.
  {
    files: ['playwright.config.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.node,
      },
    },
  },

  {
    files: ['vitest.config.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.node,
      },
    },
  },

  {
    ignores: ['node_modules/**', 'coverage/**', 'playwright-report/**', 'test-results/**'],
  },
];
