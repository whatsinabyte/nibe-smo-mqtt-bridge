import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId, setMockValue } from './support/stack';

/**
 * "Operating mode PV panels" (1021) uses the SG Ready four-state encoding.
 *
 * It used to be mapped as 10 = Off and 40 = On only (both seen on hardware,
 * GitHub issue #29), so 20 and 30 showed as bare numbers. It now carries the
 * labels yozik04/nibe PR #325 gives the register for an SMO S40 on 4.13.12.
 * None of them has a translation yet, so they stay English in the Dutch
 * harness.
 */

const POINT = 1021;
const DUMP_VALUE = 0;
const EXPECTED: [number, string][] = [
  [10, 'Normal'],
  [20, 'Block'],
  [30, 'Low price'],
  [40, 'Boost'],
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

test('PV panels operating mode (1021) shows Normal / Block / Low price / Boost', async () => {
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
