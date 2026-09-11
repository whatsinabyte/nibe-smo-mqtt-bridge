import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { loginToHa, readToken } from './support/ha-login';

/**
 * Proves, against a real Home Assistant instance, that a point which
 * temporarily vanishes from the controller's bulk response is reported
 * unavailable but *not* destroyed — and that it recovers on its own once the
 * point comes back.
 *
 * This reproduces a real incident. During a NIBE 4.13.12 firmware update the
 * controller restarted and served 1145 of its 1169 points for about a
 * minute, spanning four polls. The bridge treated every missing point as
 * removed by the firmware and disabled it immediately, which clears the
 * retained discovery config — so HA deleted thirteen entities along with
 * their history, breaking every dashboard, automation and template that
 * referenced them (BT25, the outdoor sensor, among them). The points
 * returned a minute later; the entities did not.
 *
 * The fix (_ABSENT_GRACE_S in nibe_entity_manager.py) publishes `offline`
 * straight away — honest, and non-destructive — and only disables after five
 * continuous minutes of absence. This test asserts the non-destructive half,
 * which is the actual regression and the only half a real HA instance can
 * show: that the entity survives an absence spanning several polls and comes
 * back by itself. The disable-after-grace half is pinned by pytest with a
 * patched clock (test_entity_manager_state.py's
 * TestUpdateEntityStateAbsentNoPostWrite); re-proving it here would mean
 * either a five-minute test or a test-only override of the grace period, and
 * neither is worth it for a branch already covered deterministically.
 *
 * The mock API replays one static reference dump and would otherwise serve
 * every point forever, so the absence is driven through its test-only
 * control channel (POST /mock-control/hidden/{id}, added for this scenario —
 * see dev/e2e/mock-api/mock_nibe_api.py). A hidden point is absent from the
 * bulk dict, 404s on the single-point read and is rejected by PATCH, exactly
 * as if the firmware had stopped serving it.
 */

const REFERENCE_DUMP = path.join(__dirname, '..', '..', '..', 'reference-dumps', 'all_points_en.json');
const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const BRIDGE_CONTAINER = process.env.BRIDGE_CONTAINER || 'nibe-e2e-bridge';

/** poll_interval in dev/e2e/bridge/options.json. */
const POLL_INTERVAL_S = 15;
/** Polls the point must stay absent for. Four is what the real controller
 * did during the 4.13.12 update, and it is what makes this test meaningful:
 * one missed poll could be explained away as the entity simply not having
 * been updated yet, but an entity that is still present and still enabled
 * after four consecutive absent polls is being kept deliberately. */
const ABSENT_POLLS = 4;

/** Same sentinel/isOk filter as enable-entity.spec.ts's knownGoodPointIds():
 * this dump's lowest-ID disabled registers cluster around accessories that
 * are legitimately "not connected" on the unit it was captured from, and
 * those never produce an available entity to begin with. */
function knownGoodPointIds(): number[] {
  const dump = JSON.parse(fs.readFileSync(REFERENCE_DUMP, 'utf-8'));
  const sentinels: Record<string, number> = {
    s16: -32768,
    u16: 65535,
    s32: -2147483648,
    u32: 4294967295,
  };
  const ids: number[] = [];
  for (const [idStr, point] of Object.entries<any>(dump)) {
    const size = point?.metadata?.variableSize;
    const rawValue = point?.value?.integerValue;
    if (point?.value?.isOk === false) continue;
    if (size in sentinels && rawValue === sentinels[size]) continue;
    ids.push(Number(idStr));
  }
  ids.sort((a, b) => a - b);
  return ids;
}

async function fetchStates(token: string): Promise<Array<{ entity_id: string; state: string }>> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const states = await resp.json();
  await ctx.dispose();
  return states;
}

async function fetchState(
  token: string,
  entityId: string
): Promise<{ entity_id: string; state: string } | null> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok()) {
    await ctx.dispose();
    return null;
  }
  const body = await resp.json();
  await ctx.dispose();
  return body;
}

/** Withhold a point from / restore it to the mock's responses. */
async function setMockPointHidden(pointId: string, hidden: boolean): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/hidden/${pointId}`, {
    data: { hidden },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

/** The point ids in the bridge's own /data fallback file. Read directly for
 * the same reason disable-entity.spec.ts does: proving it is correct needs no
 * broker wipe and no restart.
 *
 * A missing file reads as an empty set rather than throwing. It is legitimately
 * absent until something needs persisting: the wanted set is restored from its
 * retained MQTT topic on startup, and the file is only written when that set
 * actually changes. Throwing here turned a real assertion failure into an
 * opaque "Command failed: docker exec".
 */
function persistedWantedPoints(): number[] {
  try {
    const raw = execSync(
      `docker exec ${BRIDGE_CONTAINER} cat /data/wanted_points.json 2>/dev/null`,
      { encoding: 'utf-8' }
    );
    return JSON.parse(raw);
  } catch {
    return [];
  }
}

test('a point that vanishes from the controller for several polls goes unavailable but is not deleted', async ({
  page,
}) => {
  // Bounded by real poll intervals, not by anything this test does: one
  // enable round trip, then ABSENT_POLLS absent polls, then a recovery poll.
  test.setTimeout(240_000);

  const token = readToken();

  await loginToHa(page);

  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  const searchInput = card.locator('#search-input');

  // 1. Enable a point and find the real HA entity it produced. Candidates
  // are tried in turn rather than trusting one guessed ID — not every point
  // that looks eligible in the dump reliably surfaces as a fresh available
  // entity through the card's enable flow in this harness, the same
  // resilience pattern enable-entity.spec.ts and
  // binary-sensor-reclassification.spec.ts both use.
  const beforeIds = new Set((await fetchStates(token)).map((s) => s.entity_id));
  let pointId: string | null = null;
  let entityId: string | null = null;

  for (const candidate of knownGoodPointIds()) {
    const candidateId = String(candidate);
    await searchInput.fill(candidateId);
    const row = card.locator(`tr[data-id="${candidateId}"]`);
    if ((await row.count()) === 0) continue;
    await expect(row).toBeVisible({ timeout: 10_000 });

    const enableButton = row.locator('button[data-action="enable"]');
    if ((await enableButton.count()) === 0) continue; // already enabled
    await enableButton.click();
    await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });
    await searchInput.fill('');

    try {
      await expect
        .poll(
          async () => {
            const after = await fetchStates(token);
            const found = after.find(
              (s) => !beforeIds.has(s.entity_id) && s.state !== 'unavailable'
            );
            return found ? found.entity_id : null;
          },
          { timeout: 20_000 }
        )
        .toBeTruthy();
    } catch {
      continue; // this candidate didn't pan out — try the next one
    }

    const after = await fetchStates(token);
    const match = after.find((s) => !beforeIds.has(s.entity_id) && s.state !== 'unavailable')!;
    pointId = candidateId;
    entityId = match.entity_id;
    break;
  }

  expect(pointId, 'no candidate point produced a new available HA entity').not.toBeNull();
  expect(entityId).not.toBeNull();
  // Persisted asynchronously relative to the card's own "enabled" badge, so
  // poll rather than assuming the write has already landed.
  await expect
    .poll(() => persistedWantedPoints(), {
      timeout: 15_000,
      message: `point ${pointId} never reached /data/wanted_points.json after being enabled`,
    })
    .toContain(Number(pointId));

  // 2. The controller stops serving the point — a restart mid-firmware-
  // update, not a removal.
  await setMockPointHidden(pointId!, true);

  // 3. It must be reported unavailable promptly. This part was never the
  // bug: reporting an unreadable point as unavailable is the honest answer.
  //
  // A missing entity is reported as the distinct state 'deleted' rather than
  // as undefined, because that is exactly how the old code failed here — it
  // disabled the point on the first absent poll, HA dropped the entity, and
  // the REST API started 404ing it. Collapsing that into "never went
  // unavailable" would describe a timeout, which is the wrong diagnosis.
  const observedState = async (): Promise<string> => {
    const state = await fetchState(token, entityId!);
    return state === null ? 'deleted' : state.state;
  };
  await expect
    .poll(observedState, {
      timeout: (POLL_INTERVAL_S + 20) * 1000,
      message: `${entityId} did not become unavailable while point ${pointId} was absent ('deleted' means the entity was destroyed instead)`,
    })
    .toBe('unavailable');

  // 4. The actual regression. Across ABSENT_POLLS consecutive absent polls
  // the entity must keep existing — HA's REST API must not 404 it — and the
  // card must keep showing it as enabled, with the point still recorded as
  // wanted. The old code failed here on the very first poll: it disabled the
  // entity, which clears the retained discovery config, so HA deleted it and
  // every reference to it broke.
  const absenceDeadline = Date.now() + ABSENT_POLLS * POLL_INTERVAL_S * 1000;
  while (Date.now() < absenceDeadline) {
    await page.waitForTimeout(POLL_INTERVAL_S * 1000);
    expect(
      await observedState(),
      `${entityId} should have stayed present-but-unavailable while point ${pointId} was only temporarily absent ('deleted' means it was destroyed)`
    ).toBe('unavailable');
    expect(
      persistedWantedPoints(),
      `point ${pointId} was dropped from the wanted set during a temporary absence`
    ).toContain(Number(pointId));
  }

  // The card's own view of it: still enabled, not moved back to the
  // disabled list behind the user's back.
  await searchInput.fill(pointId!);
  const row = card.locator(`tr[data-id="${pointId}"]`);
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 10_000 });
  await searchInput.fill('');

  // 5. The controller finishes restarting and serves the point again. No
  // user action, no re-enable: the entity must come back available and
  // resume updating on its own.
  await setMockPointHidden(pointId!, false);

  await expect
    .poll(observedState, {
      timeout: (POLL_INTERVAL_S + 30) * 1000,
      message: `${entityId} never recovered after point ${pointId} came back`,
    })
    .toMatch(/^(?!unavailable$|deleted$).+/);
});
