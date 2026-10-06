import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * The anti-legionella stop temperature can't be set below 55 °C.
 *
 * Point 3702 ("Stop temperature HW periodic increase") declares minValue 55
 * with divisor 10 — 5.5 °C, a factor of ten below the real 55 °C minimum
 * (installer menu range 55 – 70 °C). Home Assistant's number entity offered
 * the whole 5.5 – 55 °C range below it.
 */

const POINT_ID = 3702;

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function minAttribute(token: string, entityId: string): Promise<number | undefined> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const min = resp.ok() ? (await resp.json()).attributes?.min : undefined;
  await ctx.dispose();
  return min;
}

test('the anti-legionella stop temperature starts at 55 °C', async () => {
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
    await expect
      .poll(() => minAttribute(token, id!), {
        timeout: 60_000,
        message: `${id} offers settings below the real 55 °C minimum`,
      })
      .toBe(55);
  } finally {
    await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(POINT_ID));
  }
});
