import { test, expect } from '@playwright/test';
import { execSync } from 'child_process';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  haWs,
  recreateBridge,
  stopBridge,
} from './support/stack';

/**
 * Notifications that promise to clear once the controller answers do, even
 * across a restart.
 *
 * "Started Without Device" and "API Unreachable" are dismissed when contact
 * is restored — but only by the process that raised them, which is the only
 * one that knew they were showing. Restarting the add-on while the controller
 * is unreachable is an ordinary troubleshooting step, and afterwards both
 * stayed in Home Assistant for good, the second one still saying it would
 * clear automatically.
 *
 * The controller is taken down by stopping the mock API; the bridge reaches
 * Home Assistant's notification service through the fake Supervisor, and the
 * notifications are read back from Home Assistant's own API.
 */

const MOCK_API_CONTAINER = process.env.MOCK_API_CONTAINER || 'nibe-e2e-mock-api';
const WITH_SUPERVISOR = {
  BRIDGE_OPTIONS: './bridge/options.json',
  BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
};
const STALE_IDS = ['nibe_discovery_incomplete', 'nibe_api_unreachable'];

async function notificationIds(token: string): Promise<string[]> {
  const [resp] = await haWs(token, [{ type: 'persistent_notification/get' }]);
  return (resp.result ?? []).map((n: any) => n.notification_id);
}

test('notifications about an unreachable controller clear after a restart once it answers', async () => {
  test.setTimeout(600_000);
  const token = readToken();

  try {
    // ── 1. Controller down at startup: both notifications raised ───────────
    execSync(`docker stop ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });
    await recreateBridge(WITH_SUPERVISOR);
    await expect
      .poll(async () => (await notificationIds(token)).filter((id) => STALE_IDS.includes(id)).sort(), {
        timeout: 180_000,
        message: 'precondition: the unreachable controller never raised both notifications',
      })
      .toEqual([...STALE_IDS].sort());

    // ── 2. Restarted while down; the controller comes back ─────────────────
    stopBridge();
    execSync(`docker start ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });
    await recreateBridge(WITH_SUPERVISOR);

    await expect
      .poll(async () => (await notificationIds(token)).filter((id) => STALE_IDS.includes(id)), {
        timeout: 90_000,
        message:
          'the controller answers again, but notifications raised before the restart are still ' +
          'in Home Assistant — only the process that raised them could ever dismiss them',
      })
      .toEqual([]);
  } finally {
    execSync(`docker start ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
