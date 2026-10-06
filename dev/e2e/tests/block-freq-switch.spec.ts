import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * "Block freq 1 active (EB101)" is an on/off switch, like its EB102–EB108
 * counterparts.
 *
 * blockFreq 1/2 (EB101) are 0–1 flags — the installer menus call them
 * "Block freq N active", separate from the Hz band settings — but were
 * overridden to number on a "frequency value" claim the firmware's own 0–1
 * range rules out. The identical flags for the other heat pumps were already
 * switches, so only EB101's showed as 0–1 sliders.
 */

const POINT_ID = 4970;

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('block freq 1 active (EB101) is a switch', async () => {
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
