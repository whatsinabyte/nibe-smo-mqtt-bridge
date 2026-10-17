import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId, setMockValue } from './support/stack';

/**
 * "Priority" (55000) shows the labels of the myUplink cloud's own option list.
 *
 * It used to have no labels at all, so Home Assistant showed a bare number.
 * The cloud API, which uses the same point ids, gives eight values; three of
 * them (25, 41, 50) were never seen locally. The harness runs in Dutch, so
 * this also checks the translations, NIBE's own wording from the same source.
 */

const POINT = 55000;
const DUMP_VALUE = 60;
const EXPECTED: [number, string][] = [
  [25, 'Ext. warmtapwater'],
  [41, 'Zwembad 2'],
  [50, 'Overdracht'],
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

async function haState(token: string, entityId: string): Promise<string> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const state = resp.ok() ? (await resp.json()).state : '';
  await ctx.dispose();
  return state;
}

test('Priority (55000) shows the cloud labels, translated', async () => {
  test.setTimeout(300_000);
  const token = readToken();
  const enabledHere = !(await pointEntityId(token, POINT));
  try {
    if (enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(POINT));
    }
    let id = '';
    await expect
      .poll(async () => (id = (await pointEntityId(token, POINT)) ?? ''), {
        timeout: 60_000,
        message: `precondition: point ${POINT} never got an HA entity`,
      })
      .not.toBe('');
    for (const [raw, label] of EXPECTED) {
      await setMockValue(POINT, raw);
      await expect
        .poll(async () => haState(token, id), {
          timeout: 60_000,
          message: `${id} with raw ${raw} should show ${label}`,
        })
        .toBe(label);
    }
  } finally {
    await setMockValue(POINT, DUMP_VALUE);
    if (enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(POINT));
    }
  }
});
