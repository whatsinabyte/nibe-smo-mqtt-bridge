import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, haWs, readRetained } from './support/stack';

/**
 * A snapshot restored with "Replace current selection" also drops points the
 * absence grace period disabled.
 *
 * A point absent for _ABSENT_GRACE_S (5 min) is disabled but stays wanted, so
 * that its return re-enables it. A replacing restore only un-wanted the
 * points it disabled itself — enabled ones — so a point excluded by the
 * snapshot that had been disabled this way came back when it returned,
 * undoing the restored selection. (Replace-mode switches had the same gap.)
 *
 * Point 4 (outdoor temperature, essential mode) is withheld through the
 * mock's control channel until the grace period disables it.
 */

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const POINT_ID = 4;
const SNAPSHOT = 'e2e-flush-absent';
const ABSENT_GRACE_S = 300;
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

async function setHidden(hidden: boolean): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/hidden/${POINT_ID}`, { data: { hidden } });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

function enabledPoints(): number[] {
  const raw = readRetained('nibe/browser/enabled_state');
  return raw ? (JSON.parse(raw).enabled_points ?? []) : [];
}

function snapshotNames(): string[] {
  const raw = readRetained('nibe/browser/snapshots');
  return raw ? JSON.parse(raw).map((s: any) => s.name) : [];
}

// @slow: waits out the real 5-minute grace period.
test('a replacing snapshot restore drops a point the absence grace disabled', { tag: '@slow' }, async () => {
  test.setTimeout(900_000);
  const token = readToken();
  const [registry] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  expect(
    (registry.result ?? []).some((e: any) => e.unique_id === `nibe_${POINT_ID}`),
    `precondition: point ${POINT_ID} has no HA entity`
  ).toBe(true);
  expect(enabledPoints()).toContain(POINT_ID);

  try {
    // ── 1. Absent past the grace period: disabled, but still wanted ────────
    await setHidden(true);
    await expect
      .poll(() => enabledPoints().includes(POINT_ID), {
        timeout: (ABSENT_GRACE_S + 4 * POLL_INTERVAL_S) * 1000,
        intervals: [10_000],
        message: `precondition: point ${POINT_ID} was never disabled by the absence grace`,
      })
      .toBe(false);

    // ── 2. A snapshot without it, restored replacing the selection ─────────
    await mqttPublish(token, 'nibe/browser/snapshots/cmd', JSON.stringify({ action: 'save', name: SNAPSHOT }));
    await expect.poll(snapshotNames, { timeout: 30_000 }).toContain(SNAPSHOT);
    await mqttPublish(
      token,
      'nibe/browser/snapshots/cmd',
      JSON.stringify({ action: 'restore', name: SNAPSHOT, mode: 'flush' })
    );
    await new Promise((r) => setTimeout(r, 5_000));

    // ── 3. It returns: it must stay disabled ───────────────────────────────
    await setHidden(false);
    await new Promise((r) => setTimeout(r, 3 * POLL_INTERVAL_S * 1000));
    expect(
      enabledPoints(),
      `point ${POINT_ID} was excluded by the restored snapshot, but came back enabled when it ` +
        'returned — the restore left it marked as wanted'
    ).not.toContain(POINT_ID);
  } finally {
    await setHidden(false);
    await mqttPublish(token, 'nibe/browser/snapshots/cmd', JSON.stringify({ action: 'delete', name: SNAPSHOT }));
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(POINT_ID));
  }
});
