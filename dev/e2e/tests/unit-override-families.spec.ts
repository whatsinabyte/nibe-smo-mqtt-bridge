import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * Every member of a unit-override family gets its unit.
 *
 * The firmware reports no unit for these registers; UNIT_OVERRIDES supplied
 * one for only part of two families. Charge pumps 1–4 showed their blank
 * time in seconds, pumps 5–8 as bare numbers; the read-only DM values
 * showed DM, the "DM start" settings that set them didn't.
 */

const EXPECTED: Record<number, string> = { 821: 's', 5298: 'DM' };

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function unitOf(token: string, entityId: string): Promise<string | undefined> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const unit = resp.ok() ? (await resp.json()).attributes?.unit_of_measurement : undefined;
  await ctx.dispose();
  return unit;
}

test('charge pump 5 blank time and the DM start settings show their units', async () => {
  test.setTimeout(180_000);
  const token = readToken();
  try {
    for (const pointId of Object.keys(EXPECTED)) {
      await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', pointId);
    }
    for (const [pointId, unit] of Object.entries(EXPECTED)) {
      let id: string | undefined;
      await expect
        .poll(async () => (id = await pointEntityId(token, pointId)) ?? '', {
          timeout: 60_000,
          message: `precondition: point ${pointId} never got an HA entity`,
        })
        .not.toBe('');
      await expect
        .poll(() => unitOf(token, id!), {
          timeout: 60_000,
          message: `${id} shows no unit, while the rest of its family shows ${unit}`,
        })
        .toBe(unit);
    }
  } finally {
    for (const pointId of Object.keys(EXPECTED)) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', pointId);
    }
  }
});
