import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests-js/support/setup.js'],
    include: ['tests-js/**/*.test.js'],
    exclude: ['tests-js/e2e/**', 'node_modules/**'],
    globals: false,
    restoreMocks: true,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      include: ['nibe-entity-manager-card.js'],
      // Recalibrated for vitest 4's v8 provider, which counts more finely
      // than 3.x did. Identical code and identical tests measure differently
      // across that boundary: statements 99.38 -> 96.79, branches
      // 88.60 -> 83.71, functions 100 -> 97 (lines barely moved, 99.38 ->
      // 98.88). No coverage was actually lost — 3.x simply did not count
      // inline arrow callbacks passed to .catch() and setTimeout() as
      // separate functions, nor some short-circuit/default-value branches.
      //
      // Keeping the old numbers would not be holding a standard, it would be
      // comparing readings from a different instrument. These sit ~1 point
      // under the current actuals so the ratchet still catches a real
      // regression without tripping on trivial edits. Raise them when
      // coverage genuinely improves; do not lower them to make a dependency
      // bump pass.
      thresholds: {
        statements: 96,
        branches: 83,
        functions: 96,
        lines: 98,
      },
    },
  },
});
