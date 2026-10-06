import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId, setMockValue } from './support/stack';

/**
 * "All sub units operating prio" (56150) shows what the system is doing.
 *
 * A live controller reports 0, 1 and 2 here (idle, heating, cooling), in step
 * with its other status entities. The bridge used to map 10/20/30/40/60, so
 * Home Assistant showed a bare number. The harness bridge runs in Dutch
 * (bridge/options.json), so the labels are translations/nl.yaml's.
 */

const POINT = 56150;
const DUMP_VALUE = 2;
const EXPECTED: [number, string][] = [
  [0, 'In rust'], // Idle
  [1, 'Verwarmen'], // Heating
  [2, 'Koelen'], // Cooling
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

test('operating prio (56150) shows idle / heating / cooling', async () => {
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
