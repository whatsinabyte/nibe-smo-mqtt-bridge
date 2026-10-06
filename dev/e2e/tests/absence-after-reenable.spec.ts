import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, haWs } from './support/stack';

/**
 * An entity disabled while its point was absent gets a full grace period
 * again once re-enabled.
 *
 * A point missing from the bulk response only makes its entity unavailable;
 * it is disabled (and deleted in HA) after _ABSENT_GRACE_S (5 min) of
 * continuous absence, measured from the first miss. Disabling the entity
 * stopped it being polled, so when its point came back nothing cleared that
 * first-miss record. Re-enabled later while the point happened to be absent
 * again (a controller reboot), the absence was measured from the stale
 * record, already more than five minutes old, and the entity was deleted on
 * the next poll: the 4.13.12 incident the grace period exists to prevent.
 * (Re-enabling while the point is present clears the record — the enable
 * publishes the current state — so the window is narrow, but real.)
 *
 * Runs past the real grace period (about seven minutes), driving absence
 * through the mock's control channel and enable/disable through the
 * bridge's own command topics.
 */

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const ABSENT_GRACE_S = 300;
const POLL_INTERVAL_S = 15;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** The entity's state by entity_id, or 'deleted' if HA no longer has it.
 * Looked up by entity_id, not by the point_id attribute: HA drops an
 * entity's extra attributes while it is unavailable. */
async function stateOf(token: string, entityId: string): Promise<string> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const state = resp.ok() ? (await resp.json()).state : 'deleted';
  await ctx.dispose();
  return state;
}

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function setHidden(pointId: string, hidden: boolean): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/hidden/${pointId}`, { data: { hidden } });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

// @slow: waits out the real 5-minute grace period.
test('an entity re-enabled after being disabled while absent gets a full grace period', { tag: '@slow' }, async () => {
  test.setTimeout(900_000);
  const token = readToken();

  // A static, always-present sensor: point 4, the outdoor temperature (BT1),
  // which essential mode enables. "Any available sensor" could land on a
  // dynamic point, whose absence is a removal rather than an absence.
  const pointId = '4';
  // Resolved through the entity registry by the bridge's unique_id, not the
  // point_id state attribute: HA drops attributes while an entity is
  // unavailable, which it briefly is whenever the bridge has just restarted.
  const [registry] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  const entityId = (registry.result ?? []).find((e: any) => e.unique_id === `nibe_${pointId}`)
    ?.entity_id as string;
  expect(entityId, `precondition: point ${pointId} has no HA entity`).toBeTruthy();
  await expect
    .poll(() => stateOf(token, entityId), {
      timeout: 60_000,
      message: `precondition: ${entityId} never became available`,
    })
    .not.toMatch(/^(deleted|unavailable|unknown)$/);

  try {
    // ── 1. Absent: unavailable, grace clock starts ─────────────────────────
    const firstMiss = Date.now();
    await setHidden(pointId, true);
    await expect
      .poll(() => stateOf(token, entityId), {
        timeout: 60_000,
        message: `precondition: point ${pointId} hidden but ${entityId} never went unavailable`,
      })
      .toBe('unavailable');

    // ── 2. Disabled by the user while absent ───────────────────────────────
    await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', pointId);
    await expect.poll(() => stateOf(token, entityId), { timeout: 60_000 }).toBe('deleted');

    // ── 3. The point comes back while the entity is disabled, then goes
    //      absent again. Nothing polls a disabled entity, so the first-miss
    //      record from step 1 survives all of this. ─────────────────────────
    await setHidden(pointId, false);
    await sleep(2 * POLL_INTERVAL_S * 1000);
    await setHidden(pointId, true);

    // ── 4. Re-enabled while absent, once that first miss is past the grace ─
    // Re-enabling while the point is present would clear the record (the
    // enable publishes the current state), so it has to be absent here.
    const wait = firstMiss + (ABSENT_GRACE_S + 30) * 1000 - Date.now();
    if (wait > 0) await sleep(wait);
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', pointId);
    await expect
      .poll(() => stateOf(token, entityId), {
        timeout: 60_000,
        message: `precondition: ${entityId} never came back after re-enabling`,
      })
      .not.toBe('deleted');

    // ── 5. It must get a full grace period, not be deleted on the next poll ─
    await sleep(3 * POLL_INTERVAL_S * 1000);
    expect(
      await stateOf(token, entityId),
      `${entityId} was deleted within ${3 * POLL_INTERVAL_S}s of being re-enabled while absent — ` +
        'its grace period was measured from a stale first miss recorded before it was disabled'
    ).toBe('unavailable');
  } finally {
    await setHidden(pointId, false);
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', pointId);
  }
});
