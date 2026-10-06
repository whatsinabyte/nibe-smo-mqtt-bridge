import { test, expect, request as pwRequest } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { loginToHa, readToken, gotoLoggedIn } from './support/ha-login';
import { pointEntityId } from './support/stack';

/**
 * Proves, against a real Home Assistant instance, that the bridge's dynamic
 * binary_sensor -> sensor reclassification (nibe_entity_manager.py's
 * _reclassify_binary_sensor, triggered from _process_and_publish_state when
 * a point classified as binary_sensor is polled with a raw value outside
 * {0, 1}) is genuinely visible in HA's own entity registry/frontend, not
 * just correct at the MQTT-message level.
 *
 * This is the one thing neither the mocked pytest suite nor the
 * real-broker-only integration tests can show: that a real HA instance's
 * own entity registry actually drops the old binary_sensor entity and picks
 * up a new sensor entity in its place — round-tripping through the real
 * card, the real broker, and the real bridge, exactly like
 * enable-entity.spec.ts's happy path, but carrying the point through a
 * second, later poll that changes its raw value.
 *
 * Candidate points are u8 INPUT registers with minValue=0/maxValue<=1/no
 * unit/isWritable=false and not in nibe_entity_detection.py's
 * _BINARY_SENSOR_EXCLUSIONS, so they auto-detect as binary_sensor (see
 * _is_auto_binary_sensor) — genuinely different from the points this
 * session's static-exclusion fix already covers (3292, 242-245, 998,
 * 632-638, 2804, 24961 etc.), all of which are now *excluded* from binary_
 * sensor auto-detection and so can never reach the dynamic reclassification
 * path this test targets.
 *
 * A single hardcoded point (originally 247, "Relay status (ERS 1)") turned
 * out not to be reliable: like enable-entity.spec.ts's own knownGoodPointIds()
 * helper and its comment about not every filtered candidate producing a
 * fresh HA entity, not every point that passes the Python-side eligibility
 * shape reliably surfaces as a new entity through the card's enable flow in
 * this harness (essential/dynamic-point interactions we haven't fully
 * traced). So, mirroring enable-entity.spec.ts's resilience pattern instead
 * of trusting one guessed ID, this test tries several eligible candidates
 * and only needs one to actually work.
 *
 * The mock API replays a static reference dump and has no built-in way to
 * change a point's value across polls — this test drives that via the
 * mock's own test-only control channel (POST /mock-control/points/{id}, see
 * dev/e2e/mock-api/mock_nibe_api.py), added specifically to make this
 * scenario exercisable.
 */

const REFERENCE_DUMP = path.join(__dirname, '..', '..', '..', 'reference-dumps', 'all_points_en.json');
const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';

const RECLASSIFY_VALUE = 30; // any value outside {0, 1}

/** Point IDs whose firmware shape auto-detects as binary_sensor per
 * nibe_entity_detection.py's _is_auto_binary_sensor (u8, minValue=0,
 * maxValue<=1, no unit, not writable, INPUT register), filtered to ones
 * with a real (non-sentinel, isOk) value — same sentinel/isOk filter as
 * enable-entity.spec.ts's knownGoodPointIds(), so results are directly
 * comparable. Does not attempt to reproduce _BINARY_SENSOR_EXCLUSIONS'
 * point-ID denylist or the VALUE_MAPPINGS-state-count / description-pairs
 * checks — those only ever narrow this candidate set further (never widen
 * it), so a candidate that isn't actually eligible just fails to enable /
 * produce a binary_sensor entity and this test moves on to the next one,
 * same as enable-entity.spec.ts already does for its own candidates. */
function binarySensorCandidateIds(): number[] {
  const dump = JSON.parse(fs.readFileSync(REFERENCE_DUMP, 'utf-8'));
  const sentinels: Record<string, number> = {
    s16: -32768,
    u16: 65535,
    s32: -2147483648,
    u32: 4294967295,
  };
  const ids: number[] = [];
  for (const [idStr, point] of Object.entries<any>(dump)) {
    const meta = point?.metadata;
    if (!meta) continue;
    if (meta.modbusRegisterType !== 'MODBUS_INPUT_REGISTER') continue;
    if (meta.variableSize !== 'u8') continue;
    if (meta.minValue !== 0 || (meta.maxValue ?? 99) > 1) continue;
    if (meta.unit) continue;
    if (meta.isWritable !== false) continue;
    const rawValue = point?.value?.integerValue;
    const isOk = point?.value?.isOk;
    if (isOk === false) continue;
    if (meta.variableSize in sentinels && rawValue === sentinels[meta.variableSize]) continue;
    ids.push(Number(idStr));
  }
  ids.sort((a, b) => a - b);
  return ids;
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

/** Drive the mock API's test-only control channel to change a point's raw
 * value, simulating the physical device reporting something new on a later
 * poll (see mock-api/mock_nibe_api.py's do_POST/module docstring). */
async function setMockPointValue(pointId: string, integerValue: number): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/points/${pointId}`, {
    data: { integerValue },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('a binary_sensor that starts reporting a non-boolean value is reclassified to sensor in real HA', async ({
  page,
}) => {
  // Reclassification only happens once the poll loop observes the offending
  // value, so this test's duration is bounded by real bulk-poll intervals
  // rather than by anything it does itself — in practice right around 90s.
  // Without this it inherits playwright.config.ts's global 90s timeout and
  // sits exactly on that boundary, passing or failing on luck; it was the
  // only long-running spec here not setting its own budget.
  test.setTimeout(240_000);

  const token = readToken();

  // 1. Log into the real HA frontend UI.
  await loginToHa(page);

  // 2. Navigate to the seeded Nibe Bridge dashboard / Entity Manager view.
  await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });

  const searchInput = card.locator('#search-input');

  // Wait until the card has actually received the bridge's retained
  // enabled-state before deciding which points are disabled.
  //
  // The table renders as soon as the point list arrives, and until the
  // separate enabled-state message lands *every* row shows an "Enable"
  // button. Acting on that reads already-enabled points as candidates, and
  // clicking one is unrecoverable rather than merely wrong: the button is
  // replaced with "Disable" the moment the real state arrives, so the click
  // can never complete and Playwright retries it until the test budget runs
  // out — failing with a bare `locator.click: Test timeout exceeded` that
  // points at the selector instead of the race. Seen against point 242,
  // which `essential` mode enables, so its row should never have offered an
  // Enable button at all.
  await expect(card.locator('.badge-enabled').first()).toBeVisible({ timeout: 60_000 });

  // 3-4. Enable candidates one at a time (same round trip as
  // enable-entity.spec.ts: hass.callService -> real broker -> real bridge)
  // until one produces a genuinely new, available binary_sensor.* entity —
  // not every candidate that passes the Python-side shape check reliably
  // does so in this harness (see the module docstring), so this doesn't
  // trust a single guessed point the way the original version of this test
  // did.
  let pointId: string | null = null;
  let binaryEntityId: string | null = null;
  for (const candidate of binarySensorCandidateIds()) {
    const candidateId = String(candidate);
    await searchInput.fill(candidateId);
    const row = card.locator(`tr[data-id="${candidateId}"]`);
    // The table re-renders on a debounce after the search box changes, so the
    // row may not exist yet. A single immediate row.count() used to skip real
    // candidates that simply hadn't been drawn — failing the spec whenever the
    // first candidate happened to be enabled already by an earlier spec.
    try {
      await expect(row).toBeVisible({ timeout: 5_000 });
    } catch {
      continue; // genuinely not in the card
    }

    // Only a point this test enables itself: one already enabled may have
    // been reclassified by an earlier run on this stack (reclassification is
    // persistent), or enabled by a mode another spec applied.
    const enableButton = row.locator('button[data-action="enable"]');
    if ((await enableButton.count()) === 0) {
      await searchInput.fill('');
      continue;
    }
    await enableButton.click();
    await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });
    await searchInput.fill('');

    // Found by its own unique_id — "any new binary_sensor" also matched
    // entities a mode change on this stack happened to create meanwhile.
    let entityId: string | null = null;
    try {
      await expect
        .poll(
          async () => {
            const id = await pointEntityId(token, candidateId);
            const current = id ? await fetchState(token, id) : null;
            entityId =
              id?.startsWith('binary_sensor.') && current !== null && current.state !== 'unavailable'
                ? id
                : null;
            return entityId;
          },
          { timeout: 15_000 }
        )
        .toBeTruthy();
    } catch {
      continue; // this candidate didn't pan out — try the next one
    }
    pointId = candidateId;
    binaryEntityId = entityId;
    break;
  }
  expect(
    pointId,
    'no candidate point produced a new available binary_sensor.* entity'
  ).not.toBeNull();
  expect(binaryEntityId).not.toBeNull();

  // 5. Simulate the device reporting a non-boolean value on a later poll —
  // the mock API has no built-in way to do this on its own (it replays one
  // static dump forever), so drive it through the test-only control
  // channel added for this scenario.
  await setMockPointValue(pointId!, RECLASSIFY_VALUE);

  // 6. The real proof: poll HA's own REST API (real entity registry, real
  // MQTT discovery listener) until the old binary_sensor entity is gone
  // (the bridge's _reclassify_binary_sensor republishes an empty retained
  // discovery payload on the stale domain, which HA's MQTT integration
  // treats as entity removal — see nibe_mqtt_publisher.py's stale_domains
  // handling) and a new sensor.* entity has appeared and is available. Not
  // asserting a specific naming scheme for the new entity_id (HA derives it
  // from the discovery config's "name", not the point ID) — same
  // before/after-diff approach as enable-entity.spec.ts, just domain-scoped
  // to "sensor." since that's the only domain _reclassify_binary_sensor
  // ever republishes under.
  let sensorEntityId: string | null = null;
  await expect
    .poll(
      async () => {
        const oldState = await fetchState(token, binaryEntityId!);
        if (oldState !== null) return false; // old entity still present — not reclassified yet

        // The same point's entity, now in the sensor domain.
        const id = await pointEntityId(token, pointId!);
        const current = id ? await fetchState(token, id) : null;
        if (id?.startsWith('sensor.') && current !== null && current.state !== 'unavailable') {
          sensorEntityId = id;
          return true;
        }
        return false;
      },
      {
        timeout: 60_000,
        message: `old entity ${binaryEntityId} never disappeared / no new available sensor.* entity appeared`,
      }
    )
    .toBeTruthy();

  expect(sensorEntityId).not.toBeNull();

  // Belt-and-braces: the old entity_id is genuinely gone (404 from HA's own
  // REST API), not just carrying a stale "unavailable" state.
  const oldStateAfter = await fetchState(token, binaryEntityId!);
  expect(oldStateAfter).toBeNull();

  // 7. The Entity Manager card must list it as a sensor too. Its bulk
  // all_metadata message is only published at discovery, so the bridge has
  // to republish the point's own metadata topic; without that, a freshly
  // loaded card kept showing it as a binary sensor until the bridge
  // restarted. Reloading makes the card rebuild from retained state alone.
  await page.reload();
  await expect(card).toBeVisible({ timeout: 30_000 });
  await searchInput.fill(pointId!);
  await expect(
    card.locator(`tr[data-id="${pointId}"] .badge-sensor`),
    `the card still lists reclassified point ${pointId} as a binary sensor after a reload`
  ).toBeVisible({ timeout: 30_000 });
});
