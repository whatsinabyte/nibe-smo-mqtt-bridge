import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { loginToHa, readToken } from './support/ha-login';

/**
 * Proves the central claim of the whole design — ARCHITECTURE.md §4.4's
 * "MQTT-first state": retained discovery configs in the broker are the single
 * source of truth for which entities are enabled, so *"the bridge survives
 * restarts without losing user customisations"*.
 *
 * Every other spec here proves entities appear or disappear correctly. None
 * of them proved they come back. That gap mattered: the restart path
 * (scan_mqtt_discovery + restore_from_mqtt) is a completely separate code
 * path from the enable path, and a real bug lived in it undetected — the
 * wanted-points safety net was only ever written when an entity was
 * *enabled*, so after any restart the bridge ran with dozens of live entities
 * and an empty wanted set, and could not re-enable any of them. It took a
 * firmware update on real hardware to surface that.
 *
 * The customisation under test is deliberately a *manual* one: a point
 * enabled through the card that the configured mode (`essential`) would not
 * have enabled on its own. Restoring the mode's own points on restart would
 * pass a weaker test while still losing exactly what users care about.
 */

const REFERENCE_DUMP = path.join(__dirname, '..', '..', '..', 'reference-dumps', 'all_points_en.json');
const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const BRIDGE_CONTAINER = process.env.BRIDGE_CONTAINER || 'nibe-e2e-bridge';

/** Same sentinel/isOk filter as enable-entity.spec.ts's knownGoodPointIds(). */
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

/** Every entity this bridge owns, by the device slug HA derives from the
 * configured `device_name` ("Nibe SMO S40") and the management device —
 * identified by object_id, with the domain prefix stripped.
 *
 * The domain is dropped deliberately. A point's HA domain is derived from its
 * value, and dynamic binary_sensor -> sensor reclassification
 * (binary-sensor-reclassification.spec.ts) legitimately moves a point from one
 * domain to the other, which is a change of entity_id but not a loss of the
 * entity. Comparing raw entity_ids made this spec fail on exactly that:
 * point 247 was `sensor.` before the restart and `binary_sensor.` after,
 * because the earlier spec's mock-control value change had since been rolled
 * back (controller-outage.spec.ts restarts the mock container, which reloads
 * its dump from disk) and the restart re-derived the classification from the
 * restored boolean value. Real behaviour, correct in both directions, and
 * nothing to do with what this spec is about. The object_id still pins
 * identity, so a genuinely lost or silently swapped entity still fails. */
function bridgeEntityIds(states: Array<{ entity_id: string }>): string[] {
  return states
    .map((s) => s.entity_id)
    .filter((id) => /smo_s40|nibe/i.test(id))
    .map((id) => id.split('.', 2)[1])
    .sort();
}

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

/** Restart the bridge and wait for it to report ready again.
 *
 * Matched against logs emitted *after* the restart (`--since`), not the whole
 * log: the previous run's own "Bridge ready" is still in there, and matching
 * that would make this return instantly and the test pass without the bridge
 * having restarted at all — the exact shape of vacuous test this suite has
 * been bitten by before. */
async function restartBridgeAndWaitForReady(): Promise<void> {
  const since = new Date().toISOString();
  execSync(`docker restart ${BRIDGE_CONTAINER}`, { stdio: 'ignore' });
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const logs = execSync(`docker logs --since ${since} ${BRIDGE_CONTAINER} 2>&1 || true`, {
      encoding: 'utf-8',
    });
    if (logs.includes('Bridge ready')) return;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error('bridge did not report ready within 120s of being restarted');
}

test('a bridge restart restores every enabled entity, including manual additions', async ({
  page,
}) => {
  // One enable round trip, a container restart, and a rediscovery pass.
  test.setTimeout(240_000);

  const token = readToken();

  await loginToHa(page);

  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  const searchInput = card.locator('#search-input');

  // 1. Make a manual customisation: enable a point the `essential` mode did
  // not. Candidates are tried in turn rather than trusting one guessed ID,
  // the same resilience pattern the other specs use.
  const before = await fetchStates(token);
  const beforeIds = new Set(before.map((s) => s.entity_id));
  let pointId: string | null = null;
  let manualEntityId: string | null = null;

  for (const candidate of knownGoodPointIds()) {
    const candidateId = String(candidate);
    await searchInput.fill(candidateId);
    const row = card.locator(`tr[data-id="${candidateId}"]`);
    if ((await row.count()) === 0) continue;
    await expect(row).toBeVisible({ timeout: 10_000 });

    const enableButton = row.locator('button[data-action="enable"]');
    if ((await enableButton.count()) === 0) continue; // already enabled by the mode
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
      continue;
    }

    const after = await fetchStates(token);
    manualEntityId = after.find(
      (s) => !beforeIds.has(s.entity_id) && s.state !== 'unavailable'
    )!.entity_id;
    pointId = candidateId;
    break;
  }

  expect(pointId, 'no candidate point produced a new available HA entity').not.toBeNull();
  expect(manualEntityId).not.toBeNull();

  // 2. Snapshot exactly what the user has right now.
  const entitiesBefore = bridgeEntityIds(await fetchStates(token));
  expect(
    entitiesBefore,
    'the manually enabled entity should be in the pre-restart snapshot'
  ).toContain(manualEntityId!.split('.', 2)[1]);
  expect(entitiesBefore.length).toBeGreaterThan(1);

  // 3. Restart the bridge — the ordinary case: same mode, same config, so
  // apply_mode() must not run and nothing may be reconciled away.
  await restartBridgeAndWaitForReady();

  // 4. Everything comes back. Polled rather than asserted once, because
  // republishing the retained discovery configs and having HA process them
  // takes a moment after "Bridge ready".
  await expect
    .poll(async () => bridgeEntityIds(await fetchStates(token)), {
      timeout: 90_000,
      message: 'the bridge lost entities across a restart',
    })
    .toEqual(entitiesBefore);

  // 5. The manual addition specifically: present, and actually live again —
  // restoring a discovery config without resuming state publishes would
  // leave a permanently unavailable entity, which is not "surviving".
  await expect
    .poll(async () => (await fetchState(token, manualEntityId!))?.state ?? 'deleted', {
      timeout: 60_000,
      message: `${manualEntityId} (point ${pointId}) did not become available again after the restart`,
    })
    .toMatch(/^(?!unavailable$|deleted$|unknown$).+/);

  // 6. The safety net is still armed after the restart. Being recorded as
  // wanted is what re-enables a point the controller drops and later serves
  // again, so losing that across a restart would leave every entity
  // unprotected without anything looking wrong.
  //
  // This asserts the invariant, not the route to it: the wanted set is
  // restored from its retained MQTT topic, and restore_from_mqtt() also
  // backfills it from the enabled set. Isolating the backfill alone would
  // mean wiping the broker's retained topic first, which is far heavier than
  // the property being checked — the pytest suite covers that branch
  // directly (test_entity_manager_discovery.py's TestRestoreFromMqtt).
  await expect
    .poll(() => persistedWantedPoints(), {
      timeout: 30_000,
      message: `point ${pointId} was not recorded as wanted after the restart`,
    })
    .toContain(Number(pointId));
});
