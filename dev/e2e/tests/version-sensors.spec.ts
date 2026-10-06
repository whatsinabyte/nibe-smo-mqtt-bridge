import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId, setMockValue } from './support/stack';

/**
 * Version sensors show the same version myUplink does.
 *
 * The controller reports versions as plain integers. 2509 (SMO S40) and 2453
 * (S2125) pack the version into bit fields; 14987 (inverter) is just the
 * number. The mock serves the reference installation's raw values, read back
 * from Home Assistant's own state.
 */

const EXPECTED: Record<number, { raw: number; state: string }> = {
  2509: { raw: 1037, state: '4.13' },
  2453: { raw: 12481, state: '3.3.1' },
  14987: { raw: 61, state: '61' },
};

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

test('version sensors match myUplink', async () => {
  test.setTimeout(240_000);
  const token = readToken();
  const enabledHere: string[] = [];
  try {
    for (const [pointId, want] of Object.entries(EXPECTED)) {
      await setMockValue(pointId, want.raw);
      if (!(await pointEntityId(token, pointId))) {
        await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', pointId);
        enabledHere.push(pointId);
      }
    }
    for (const [pointId, want] of Object.entries(EXPECTED)) {
      let id: string | undefined;
      await expect
        .poll(async () => (id = await pointEntityId(token, pointId)) ?? '', {
          timeout: 60_000,
          message: `precondition: point ${pointId} never got an HA entity`,
        })
        .not.toBe('');
      await expect
        .poll(async () => haState(token, id!), {
          timeout: 60_000,
          message: `${id} (raw ${want.raw}) should show ${want.state}`,
        })
        .toBe(want.state);
    }
  } finally {
    for (const pointId of enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', pointId);
    }
  }
});
