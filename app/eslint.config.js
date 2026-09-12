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

      // Style rules that lock in what this file already does, rather than
      // imposing anything new: both pass clean as written. js.configs.
      // recommended carries no stylistic rules at all, so until now nothing
      // checked either of these and they held only by hand.
      //
      // Double quotes stay allowed inside template literals because that is
      // where the card's HTML lives, and HTML attributes take double quotes:
      // `<button class="button-fixed" data-id="${id}">`. avoidEscape keeps a
      // string containing an apostrophe from having to escape it.
      quotes: ['error', 'single', { allowTemplateLiterals: true, avoidEscape: true }],
      semi: ['error', 'always'],

      // `indent` is deliberately NOT enabled. The card is consistently two-
      // space indented, but it also aligns the branches of multi-line ternary
      // expressions under each other for readability:
      //
      //   ? this._lastKnownEnabledPoints.has(pointId)
      //   : false,
      //
      // ESLint's arithmetic wants those pushed to a computed depth instead,
      // which reports 56 findings across 16 such expressions and no other
      // problem anywhere in the file. `--fix` would reformat every one of
      // them into something harder to read. The .editorconfig entry
      // (indent_size = 2 for *.js) still describes the file correctly; it is
      // the continuation lines inside ternaries and multi-line template
      // literals that a flat rule cannot judge.
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
