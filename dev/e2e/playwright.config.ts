import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 90_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.HA_URL || 'http://localhost:18123',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
  },
});
