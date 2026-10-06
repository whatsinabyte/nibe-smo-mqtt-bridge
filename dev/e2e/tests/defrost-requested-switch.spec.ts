import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * "Defrost requested (EB101)" is an on/off switch.
 *
 * Point 8060 is a 0–1 holding register — an on/off flag in the installer
 * menus — but s8, and the switch auto-detection only accepts u8, so it
 * showed as a 0–1 number.
 */

const POINT_ID = 8060;

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('defrost requested (EB101) is a switch', async () => {
  test.setTimeout(180_000);
  const token = readToken();
  try {
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(POINT_ID));
    let id: string | undefined;
    await expect
      .poll(async () => (id = await pointEntityId(token, POINT_ID)) ?? '', {
        timeout: 60_000,
        message: `precondition: point ${POINT_ID} never got an HA entity`,
      })
      .not.toBe('');
    expect(id!.split('.')[0], `${id} is an on/off flag shown as a 0–1 number`).toBe('switch');
  } finally {
    await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(POINT_ID));
  }
});
