import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * Settings whose options the firmware doesn't describe are selects.
 *
 * Registers like 7022 ("Blocking actions (ERS 3)") and 4692 ("Charging
 * method") carry no description, so no value labels: they showed as a bare
 * 0–2 number and as a switch. Their option lists — verified on a live
 * controller — are now hard-coded. Counted rather than compared by text: the
 * harness runs in Dutch, and some labels have translations.
 */

const EXPECTED: Record<number, number> = { 7022: 3, 4692: 2 };

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function options(token: string, entityId: string): Promise<string[]> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const opts = resp.ok() ? (await resp.json()).attributes?.options ?? [] : [];
  await ctx.dispose();
  return opts;
}

test('settings with controller-verified option lists are selects', async () => {
  test.setTimeout(180_000);
  const token = readToken();
  try {
    for (const pointId of Object.keys(EXPECTED)) {
      await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', pointId);
    }
    for (const [pointId, count] of Object.entries(EXPECTED)) {
      let id: string | undefined;
      await expect
        .poll(async () => (id = await pointEntityId(token, pointId)) ?? '', {
          timeout: 60_000,
          message: `precondition: point ${pointId} never got an HA entity`,
        })
        .not.toBe('');
      expect(id!.split('.')[0], `${id} should be a select with ${count} options`).toBe('select');
      await expect.poll(async () => (await options(token, id!)).length, { timeout: 30_000 }).toBe(count);
    }
  } finally {
    for (const pointId of Object.keys(EXPECTED)) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', pointId);
    }
  }
});
