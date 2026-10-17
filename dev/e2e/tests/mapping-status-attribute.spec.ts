import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * Entities whose value labels come from the bridge say how far those labels
 * are confirmed.
 *
 * 56150 ("All sub units operating prio") maps the 0/1/2 a live controller
 * reports; hot water and pool aren't known yet, so it is `partial`. The
 * attribute is static, so Home Assistant's recorder stores it once. A point
 * without bridge-made labels (4, outdoor temperature) gets no attribute.
 */

const CASES: [number, string | undefined][] = [
  [56150, 'partial'],
  [4, undefined],
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

async function attributes(token: string, entityId: string): Promise<Record<string, any>> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const attrs = resp.ok() ? (await resp.json()).attributes ?? {} : {};
  await ctx.dispose();
  return attrs;
}

test('value label status reaches Home Assistant as the mapping attribute', async () => {
  test.setTimeout(240_000);
  const token = readToken();
  const enabledHere: number[] = [];
  try {
    for (const [pointId] of CASES) {
      if (!(await pointEntityId(token, pointId))) {
        await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(pointId));
        enabledHere.push(pointId);
      }
    }
    for (const [pointId, want] of CASES) {
      let id = '';
      await expect
        .poll(async () => (id = (await pointEntityId(token, pointId)) ?? ''), {
          timeout: 60_000,
          message: `precondition: point ${pointId} never got an HA entity`,
        })
        .not.toBe('');
      // point_id arrives with the same attributes payload; wait for it so an
      // absent `mapping` means "not published", not "not arrived yet".
      await expect
        .poll(async () => (await attributes(token, id)).point_id, {
          timeout: 60_000,
          message: `${id} never received its attributes`,
        })
        .toBe(String(pointId));
      expect((await attributes(token, id)).mapping, `${id} mapping attribute`).toBe(want);
    }
  } finally {
    for (const pointId of enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(pointId));
    }
  }
});
