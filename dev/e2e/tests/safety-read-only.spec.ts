import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * Registers the bridge must never write are read-only in Home Assistant.
 *
 * DOCS.md "Intentionally Unexposed Registers": 55749 "Block new
 * compressor" and 55884 "Set point value power" (a compressor power request
 * with no timeout). The first rested on the firmware's isWritable flag, no
 * longer relied on; the second was a writable number all along. Both must
 * come out as sensors.
 */

const POINTS = [55749, 55884];

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('intentionally unexposed registers are read-only sensors', async () => {
  test.setTimeout(180_000);
  const token = readToken();
  try {
    for (const pointId of POINTS) {
      await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(pointId));
    }
    for (const pointId of POINTS) {
      let id: string | undefined;
      await expect
        .poll(async () => (id = await pointEntityId(token, pointId)) ?? '', {
          timeout: 60_000,
          message: `precondition: point ${pointId} never got an HA entity`,
        })
        .not.toBe('');
      expect(id!.split('.')[0], `${id} must not be controllable from Home Assistant`).toBe('sensor');
    }
  } finally {
    for (const pointId of POINTS) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(pointId));
    }
  }
});
