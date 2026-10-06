import { defineConfig } from '@playwright/test';
import { STACK, stackEnv } from './tests/support/stacks';
import { orderedSpecFiles } from './tests/support/spec-order';

const TEST_DIR = './tests';

// One worker per harness stack (stacks.sh brings up stacks B, C, …; the
// original stack A is run.sh's). Each worker's TEST_PARALLEL_INDEX selects
// its stack — and since this file is evaluated again inside every worker,
// baseURL below is that worker's own Home Assistant.
const STACKS = Number(process.env.E2E_STACKS || 1);

// Playwright always sorts the files within a project alphabetically but runs
// projects in the order given, so each spec file is its own project, in the
// order spec-order.ts picks (changed specs first, then longest first).
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

export default defineConfig({
  testDir: TEST_DIR,
  timeout: 90_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  retries: 0,
  workers: process.env.E2E_STACK ? 1 : STACKS,
  reporter: [['list'], ['./tests/support/duration-reporter.ts']],
  use: {
    baseURL: stackEnv(STACK).HA_URL,
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
  },
  projects: orderedSpecFiles(TEST_DIR).map((file) => ({
    name: file.replace(/\.spec\.ts$/, ''),
    testMatch: new RegExp(`(^|/)${escape(file)}$`),
  })),
});
