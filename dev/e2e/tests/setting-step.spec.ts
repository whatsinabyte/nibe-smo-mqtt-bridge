import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * A setting's step in Home Assistant is the firmware's own `change` metadata.
 *
 * 32341 "Desired room temperature for zone 1" declares change 5 at divisor
 * 10, so it moves by 0.5 °C, as on the controller and in myUplink. It used
 * to move by one raw unit, 0.1 °C. 3869 "Delta T for DOT" declares change 1
 * and keeps 0.1.
 */

const EXPECTED: [number, number][] = [
  [32341, 0.5],
  [3869, 0.1],
];

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function step(token: string, entityId: string): Promise<string> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = resp.ok() ? await resp.json() : null;
  await ctx.dispose();
  if (!body) return 'not loaded';
  return String(body.attributes?.step ?? 'none');
}

test('a setting moves by the firmware step', async () => {
  test.setTimeout(180_000);
  const token = readToken();
  const enabledHere: number[] = [];
  try {
    for (const [pointId] of EXPECTED) {
      if (!(await pointEntityId(token, pointId))) {
        await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(pointId));
        enabledHere.push(pointId);
      }
    }
    for (const [pointId, want] of EXPECTED) {
      let id: string | undefined;
      await expect
        .poll(async () => (id = await pointEntityId(token, pointId)) ?? '', {
          timeout: 60_000,
          message: `precondition: point ${pointId} never got an HA entity`,
        })
        .not.toBe('');
      await expect
        .poll(() => step(token, id!), {
          timeout: 60_000,
          message: `point ${pointId} (${id}) has the wrong step`,
        })
        .toBe(String(want));
    }
  } finally {
    for (const pointId of enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(pointId));
    }
  }
});
