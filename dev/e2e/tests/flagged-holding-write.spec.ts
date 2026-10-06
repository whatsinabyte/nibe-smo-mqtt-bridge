import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { spawn } from 'child_process';
import { bridgeLogsSince, HA_URL, haWs, MQTT_CONTAINER } from './support/stack';

/**
 * A write to a holding register is sent to the controller even when the
 * firmware flags the point isWritable false.
 *
 * The isWritable flag follows the register type everywhere except 17 holding
 * registers flagged false, 3478 "Reset alarm" among them — a button nobody
 * could press: the bridge refused every write locally ("Point 3478 is not
 * writable") without asking the controller. Writability now goes by register
 * type, and the controller's own per-point verdict decides. This harness's
 * mock answers flagged points with "error: read only value"; what's checked
 * here is that the write reaches it at all — and that the resulting
 * "Write Failed" message names the setting: it read the title from a key
 * entity_info never has, so it always said "point 3478 (point 3478)".
 */

const POINT_ID = 3478;

async function haPost(token: string, path: string, data: object): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}${path}`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data,
  });
  expect(resp.ok(), `${path}: ${resp.status()}`).toBeTruthy();
  await ctx.dispose();
}

async function entityId(token: string): Promise<string> {
  const [resp] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  return (resp.result ?? []).find((e: any) => e.unique_id === `nibe_${POINT_ID}`)?.entity_id ?? '';
}

test('pressing a holding-register button flagged isWritable false asks the controller', async () => {
  test.setTimeout(180_000);
  const token = readToken();
  try {
    await haPost(token, '/api/services/mqtt/publish', {
      topic: 'homeassistant/text/nibe_enable_entity/set',
      payload: String(POINT_ID),
    });
    let id = '';
    await expect
      .poll(async () => (id = await entityId(token)), {
        timeout: 60_000,
        message: `precondition: point ${POINT_ID} never got an HA entity`,
      })
      .not.toBe('');
    expect(id.split('.')[0], 'precondition: not a button').toBe('button');
    // A press before the bridge has published the button available (and
    // subscribed to its command topic) is lost.
    await expect
      .poll(
        async () => {
          const ctx = await pwRequest.newContext();
          const r = await ctx.get(`${HA_URL}/api/states/${id}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          const state = r.ok() ? (await r.json()).state : 'missing';
          await ctx.dispose();
          return state;
        },
        { timeout: 60_000, message: `precondition: ${id} never became available` }
      )
      .not.toMatch(/^(unavailable|missing)$/);

    // The bridge alert carries the same text as the "Write Failed" notification.
    const sub = spawn('docker', [
      'exec', MQTT_CONTAINER, 'mosquitto_sub', '-t', 'nibe/browser/bridge/alert', '-C', '1', '-W', '30',
    ]);
    let alertRaw = '';
    sub.stdout.on('data', (d) => (alertRaw += d));
    const alertDone = new Promise<void>((resolve) => sub.on('close', () => resolve()));
    await new Promise((r) => setTimeout(r, 1500)); // subscribed before the press

    const since = new Date().toISOString();
    await haPost(token, '/api/services/button/press', { entity_id: id });
    await expect
      .poll(
        () => {
          const logs = bridgeLogsSince(since);
          if (logs.includes(`Point ${POINT_ID} is not writable`)) return 'refused locally';
          if (logs.includes(`Write rejected for point ${POINT_ID}`)) return 'asked the controller';
          return 'nothing yet';
        },
        {
          timeout: 30_000,
          message:
            `the bridge refused the press of ${id} itself, from the firmware's isWritable flag, ` +
            'without asking the controller',
        }
      )
      .toBe('asked the controller');

    await alertDone;
    const alert = alertRaw.trim() ? JSON.parse(alertRaw.trim()) : null;
    expect(alert, 'precondition: no bridge alert for the failed write').not.toBeNull();
    expect(alert.message, 'the "Write Failed" message does not name the setting').toContain(
      'Reset alarm'
    );
  } finally {
    await haPost(token, '/api/services/mqtt/publish', {
      topic: 'homeassistant/text/nibe_disable_entity/set',
      payload: String(POINT_ID),
    });
  }
});
