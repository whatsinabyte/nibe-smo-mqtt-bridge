import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId, setMockValue } from './support/stack';

/**
 * A lifetime counter that drops to 0 for a moment is shown unavailable, not 0.
 *
 * A restarting controller can serve 0 for a lifetime energy counter before
 * the real value returns (GitHub issue #132). Home Assistant takes any
 * decrease of a total_increasing sensor for a counter reset, so the return
 * counted the whole lifetime total again in its statistics and Energy
 * dashboard. Now a running total below its last value is published
 * unavailable until it is back.
 *
 * Uses 28393 ("Tot. consumption", kWh) and, as the control, 2501
 * ("Compressor, time to start"), a countdown whose lower values must pass.
 */

const COUNTER = 28393;
const COUNTDOWN = 2501;

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
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

async function enabledEntity(token: string, pointId: number): Promise<string> {
  await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(pointId));
  let entityId = '';
  await expect
    .poll(
      async () => {
        entityId = (await pointEntityId(token, pointId)) ?? '';
        return entityId ? await stateOf(token, entityId) : 'deleted';
      },
      { timeout: 60_000, message: `precondition: point ${pointId} has no available entity` }
    )
    .not.toMatch(/^(deleted|unavailable|unknown)$/);
  return entityId;
}

test('a running total that drops to 0 is unavailable, a countdown is not', async () => {
  test.setTimeout(240_000);
  const token = readToken();
  const counterId = await enabledEntity(token, COUNTER);
  const countdownId = await enabledEntity(token, COUNTDOWN);
  const counterBefore = await stateOf(token, counterId);
  const countdownBefore = await stateOf(token, countdownId);
  const counterRaw = Math.round(Number(counterBefore) * 10);

  try {
    // ── 1. The counter drops to 0: unavailable, never 0 ────────────────────
    await setMockValue(COUNTER, 0);
    await expect
      .poll(() => stateOf(token, counterId), {
        timeout: 60_000,
        message: `${counterId} did not go unavailable when its total dropped to 0`,
      })
      .toBe('unavailable');

    // ── 2. Back above its last value: published again ──────────────────────
    await setMockValue(COUNTER, counterRaw + 5);
    await expect
      .poll(() => stateOf(token, counterId), {
        timeout: 60_000,
        message: `${counterId} did not come back when its total returned`,
      })
      .toBe((counterRaw / 10 + 0.5).toFixed(1));

    // ── 3. A countdown counting down: every value passes ───────────────────
    await setMockValue(COUNTDOWN, 3);
    await expect
      .poll(() => stateOf(token, countdownId), {
        timeout: 60_000,
        message: `${countdownId} did not show its lower value`,
      })
      .toBe('3');
    await setMockValue(COUNTDOWN, 1);
    await expect
      .poll(() => stateOf(token, countdownId), {
        timeout: 60_000,
        message: `${countdownId} did not show its lower value`,
      })
      .toBe('1');
  } finally {
    // Not back to its old value: that would be a drop too, leaving it
    // unavailable for 15 minutes for whatever runs next on this stack.
    await setMockValue(COUNTER, counterRaw + 5);
    await setMockValue(COUNTDOWN, Number(countdownBefore) || 0);
  }
});
