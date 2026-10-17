import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * A unitless quantity is graphed, a running total counts up, and a unitless
 * code keeps its state bars.
 *
 * Home Assistant draws a sensor as a graph only when it has a unit or a state
 * class. 2472 "Compressor size" has no unit, so it must reach HA with state
 * class `measurement`. 2505 "Compressor, number of starts" (no unit) and 2506
 * "Compressor, oper. time, total" (hours) only go up: `total_increasing`, so
 * statistics show the increase per period. 2502 "Relay status" has no unit
 * either, but is a code: it must have no state class.
 */

const EXPECTED: [number, string][] = [
  [2472, 'measurement'],
  [2505, 'total_increasing'],
  [2506, 'total_increasing'],
  [2502, 'none'],
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

async function stateClass(token: string, entityId: string): Promise<string> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = resp.ok() ? await resp.json() : null;
  await ctx.dispose();
  if (!body || body.state === 'unavailable') return 'not loaded';
  return body.attributes?.state_class ?? 'none';
}

test('unitless quantities and running totals get the right state class', async () => {
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
        .poll(() => stateClass(token, id!), {
          timeout: 60_000,
          message: `point ${pointId} (${id}) has the wrong state class`,
        })
        .toBe(want);
    }
  } finally {
    for (const pointId of enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(pointId));
    }
  }
});
