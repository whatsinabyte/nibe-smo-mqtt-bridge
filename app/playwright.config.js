import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests-js/e2e',
  fullyParallel: true,
  reporter: 'list',
  webServer: {
    // Serves the whole app/ directory (not just tests-js/e2e) so the
    // fixture page's `import '../../nibe-entity-manager-card.js'` resolves
    // to the real card file one level up from tests-js/e2e.
    command: 'npx http-server . -p 4173 -s',
    port: 4173,
    reuseExistingServer: !process.env.CI,
  },
  use: {
    baseURL: 'http://127.0.0.1:4173/tests-js/e2e/',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
