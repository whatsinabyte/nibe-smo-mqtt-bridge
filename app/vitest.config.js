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
      thresholds: {
        statements: 97,
        branches: 86,
        functions: 97,
        lines: 97,
      },
    },
  },
});
