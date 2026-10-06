import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, haWs } from './support/stack';

/**
 * A static entity briefly missing just after a write keeps its grace period.
 *
 * For 90 seconds after any write the bridge watches for points the write
 * shows or hides. A point missing in that window was taken for a dynamic
 * point the write had hidden, and disabled on the spot — Home Assistant then
 * deleted the entity with its history — even a static sensor gone for one
 * poll during a controller restart or a partial response. Outside the window
 * the same absence makes it unavailable, with five minutes' grace. Now it
 * does inside the window too, unless the point is a known dynamic one.
 *
 * Writes 3751 (Operating mode, enabled for the test), then withholds point 4
 * (outdoor temperature) through the mock's control channel.
 */

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const WRITTEN = 3751;
const STATIC_POINT = 4;
const POLL_INTERVAL_S = 15;

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function mockControl(path: string, data: object): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/${path}`, { data });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function entityIdOf(token: string, pointId: number): Promise<string> {
  const [resp] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  return (resp.result ?? []).find((e: any) => e.unique_id === `nibe_${pointId}`)?.entity_id ?? '';
}

async function stateOf(token: string, entityId: string): Promise<string> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const state = resp.ok() ? (await resp.json()).state : 'deleted';
  await ctx.dispose();
  return state;
}

test('a static entity missing just after a write goes unavailable, not deleted', async () => {
  test.setTimeout(300_000);
  const token = readToken();
  const staticId = await entityIdOf(token, STATIC_POINT);
  expect(staticId, `precondition: point ${STATIC_POINT} has no HA entity`).toBeTruthy();
  await expect
    .poll(() => stateOf(token, staticId), { timeout: 60_000, message: `precondition: ${staticId} not available` })
    .not.toMatch(/^(deleted|unavailable|unknown)$/);

  try {
    await mockControl(`points/${WRITTEN}`, { integerValue: 0 });
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(WRITTEN));
    let writtenId = '';
    await expect
      .poll(async () => (writtenId = await entityIdOf(token, WRITTEN)), { timeout: 60_000 })
      .not.toBe('');
    const options: string[] = await (async () => {
      let opts: string[] = [];
      await expect
        .poll(async () => {
          const ctx = await pwRequest.newContext();
          const r = await ctx.get(`${HA_URL}/api/states/${writtenId}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          opts = r.ok() ? ((await r.json()).attributes?.options ?? []) : [];
          await ctx.dispose();
          return opts.length;
        }, { timeout: 60_000 })
        .toBe(3);
      return opts;
    })();

    // The bridge skips a write of the value it already sees, and holds the
    // state of a write the controller hasn't confirmed — so write option 2
    // ("Additional heat only"): no other spec writes it, so it is always a
    // real write, whatever an earlier spec on this stack left behind.
    await expect.poll(() => stateOf(token, writtenId), { timeout: 60_000 }).not.toBe(options[2]);

    // The write opens the bridge's 90s post-write window…
    await mqttPublish(token, `homeassistant/select/nibe_${WRITTEN}/set`, options[2]);
    // …and the static point goes missing inside it.
    await mockControl(`hidden/${STATIC_POINT}`, { hidden: true });
    await expect
      .poll(async () => ['unavailable', 'deleted'].includes(await stateOf(token, staticId)), {
        timeout: (2 * POLL_INTERVAL_S + 10) * 1000,
        message: `precondition: ${staticId} never noticed missing`,
      })
      .toBe(true);

    await new Promise((r) => setTimeout(r, 2 * POLL_INTERVAL_S * 1000));
    expect(
      await stateOf(token, staticId),
      `${staticId} went missing within 90s of a write and was deleted on the spot — taken for a ` +
        'dynamic point the write had hidden, with no grace period'
    ).toBe('unavailable');
  } finally {
    await mockControl(`hidden/${STATIC_POINT}`, { hidden: false });
    await mockControl(`points/${WRITTEN}`, { integerValue: 0 });
    await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(WRITTEN));
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(STATIC_POINT));
  }
});
