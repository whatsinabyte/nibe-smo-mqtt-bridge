import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, haWs } from './support/stack';

/**
 * A select whose value is not one of its options shows as unavailable.
 *
 * The bridge published the raw number as the select's state. Home Assistant
 * rejects a state that isn't one of the options — logging a warning on every
 * poll — and keeps showing the last valid one, so the entity silently claimed
 * a setting the controller no longer had. It is now published unavailable, as
 * an out-of-range number already was, until a valid value returns.
 *
 * Point 3751 (Operating mode: Auto / Manual / Additional heat only) is
 * enabled for the test, and the mock's control channel makes the controller
 * report a value outside those options. Labels are taken from the entity's
 * own options, since the bridge translates them (the harness runs in Dutch).
 */

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const POINT_ID = 3751;

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function setMockValue(value: number): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/points/${POINT_ID}`, {
    data: { integerValue: value },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function entityId(token: string): Promise<string> {
  const [resp] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  return (resp.result ?? []).find((e: any) => e.unique_id === `nibe_${POINT_ID}`)?.entity_id ?? '';
}

async function haEntity(token: string, id: string): Promise<any | undefined> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${id}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const entity = resp.ok() ? await resp.json() : undefined;
  await ctx.dispose();
  return entity;
}

async function haState(token: string, id: string): Promise<string | undefined> {
  return (await haEntity(token, id))?.state;
}

test('a select value outside its options shows as unavailable', async () => {
  test.setTimeout(300_000);
  const token = readToken();

  try {
    await setMockValue(0);
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(POINT_ID));
    let id = '';
    await expect
      .poll(async () => (id = await entityId(token)), {
        timeout: 60_000,
        message: `precondition: point ${POINT_ID} never got an HA entity`,
      })
      .not.toBe('');
    // Options in value order: 0, 1, 2.
    let options: string[] = [];
    await expect
      .poll(async () => (options = (await haEntity(token, id))?.attributes?.options ?? []).length, {
        timeout: 90_000,
        message: `precondition: ${id} has no options`,
      })
      .toBe(3);
    await expect
      .poll(() => haState(token, id), {
        timeout: 90_000,
        message: `precondition: ${id} never showed ${options[0]}`,
      })
      .toBe(options[0]);

    await setMockValue(7);
    await expect
      .poll(() => haState(token, id), {
        timeout: 90_000,
        message:
          `the controller reports 7 for ${id}, which is none of its options, but HA still shows ` +
          'the last valid one',
      })
      .toBe('unavailable');

    // The next valid value brings it back.
    await setMockValue(1);
    await expect.poll(() => haState(token, id), { timeout: 90_000 }).toBe(options[1]);
  } finally {
    await setMockValue(0);
    await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(POINT_ID));
  }
});
