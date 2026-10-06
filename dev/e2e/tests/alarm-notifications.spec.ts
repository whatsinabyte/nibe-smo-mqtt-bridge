import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  haWs,
  recreateBridge,
  stopBridge,
} from './support/stack';

/**
 * The "Active Alarms" HA notification follows the controller's alarms.
 *
 * Two gaps, both from tracking only "a notification is showing":
 *
 *   - It was sent once when alarms first appeared and never touched again
 *     while any stayed active, so a second alarm — or one replacing another —
 *     only showed on the Active Alarms sensor, never in the notification.
 *   - Whether it was showing lived only in memory. If the alarms cleared while
 *     the bridge was down, the restarted bridge saw no alarms and nothing of
 *     its own to dismiss, and the stale notification stayed in HA.
 *
 * Alarms are driven through the mock's control channel; the bridge reaches
 * Home Assistant's notification service through the fake Supervisor, and the
 * notification is read back from Home Assistant's own API.
 */

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const NOTIFICATION_ID = 'nibe_active_alarms';
const WITH_SUPERVISOR = {
  BRIDGE_OPTIONS: './bridge/options.json',
  BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
};

function alarm(id: number, header: string) {
  return { alarmId: id, header, description: '', severity: 'Warning', time: '2026-10-04T10:00:00', equipName: '' };
}

async function setAlarms(alarms: object[]): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/alarms`, { data: { alarms } });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

/** The bridge's alarm notification as HA holds it, or null. */
async function alarmNotification(token: string): Promise<{ title: string; message: string } | null> {
  const [resp] = await haWs(token, [{ type: 'persistent_notification/get' }]);
  return (resp.result ?? []).find((n: any) => n.notification_id === NOTIFICATION_ID) ?? null;
}

test.describe.configure({ mode: 'serial' });

test('a new alarm while one is active updates the notification', async () => {
  test.setTimeout(300_000);
  const token = readToken();
  try {
    await setAlarms([]);
    await recreateBridge(WITH_SUPERVISOR);

    await setAlarms([alarm(1, 'High pressure')]);
    await expect
      .poll(async () => (await alarmNotification(token))?.message ?? '', {
        timeout: 60_000,
        message: 'precondition: the first alarm never produced a notification',
      })
      .toContain('High pressure');

    await setAlarms([alarm(1, 'High pressure'), alarm(2, 'Low brine flow')]);
    await expect
      .poll(async () => (await alarmNotification(token))?.message ?? '', {
        timeout: 60_000,
        message:
          'a second alarm appeared while one was active, but the notification still only ' +
          'lists the first — the new alarm shows nowhere but the sensor',
      })
      .toContain('Low brine flow');
    expect((await alarmNotification(token))?.title).toContain('2 Active Alarm');

    await setAlarms([]);
    await expect
      .poll(async () => await alarmNotification(token), { timeout: 60_000 })
      .toBeNull();
  } finally {
    await setAlarms([]);
  }
});

test('a notification left from before a restart is cleared when the alarms are gone', async () => {
  test.setTimeout(300_000);
  const token = readToken();
  try {
    await setAlarms([alarm(3, 'Compressor fault')]);
    await recreateBridge(WITH_SUPERVISOR);
    await expect
      .poll(async () => (await alarmNotification(token))?.message ?? '', {
        timeout: 60_000,
        message: 'precondition: the alarm never produced a notification',
      })
      .toContain('Compressor fault');

    // The alarm clears while the bridge is down.
    stopBridge();
    await setAlarms([]);
    await recreateBridge(WITH_SUPERVISOR);

    await expect
      .poll(async () => await alarmNotification(token), {
        timeout: 60_000,
        message:
          'the alarm cleared while the bridge was down, and the restarted bridge left its ' +
          'stale notification in Home Assistant',
      })
      .toBeNull();
  } finally {
    await setAlarms([]);
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
