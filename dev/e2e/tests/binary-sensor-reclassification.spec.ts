import { test, expect, request as pwRequest } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

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

const SEED_OUT = path.join(__dirname, '..', 'seed-out');
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

function readCredentials(): { username: string; password: string } {
  const raw = fs.readFileSync(path.join(SEED_OUT, 'credentials.json'), 'utf-8');
  return JSON.parse(raw);
}

function readToken(): string {
  return fs.readFileSync(path.join(SEED_OUT, 'token.txt'), 'utf-8').trim();
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
  const { username, password } = readCredentials();
  const token = readToken();

  // 1. Log into the real HA frontend UI.
  await page.goto('/');
  await page.getByLabel('Username').fill(username);
  await page.getByRole('textbox', { name: 'Password' }).fill(password);
  await page.getByRole('button', { name: /log in/i }).click();
  await expect(page).toHaveURL(/\/lovelace|\/$|\/home/, { timeout: 30_000 });

  // 2. Navigate to the seeded Nibe Bridge dashboard / Entity Manager view.
  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });

  const searchInput = card.locator('#search-input');
  const before = await fetchStates(token);
  const beforeIds = new Set(before.map((s) => s.entity_id));

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
    if ((await row.count()) === 0) continue;
    await expect(row).toBeVisible({ timeout: 10_000 });

    const enableButton = row.locator('button[data-action="enable"]');
    if ((await enableButton.count()) > 0) {
      await enableButton.click();
      await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });
    }
    await searchInput.fill('');

    try {
      await expect
        .poll(
          async () => {
            const after = await fetchStates(token);
            const match = after.find(
              (s) => !beforeIds.has(s.entity_id) && s.entity_id.startsWith('binary_sensor.')
            );
            return match && match.state !== 'unavailable' ? match.entity_id : null;
          },
          { timeout: 15_000 }
        )
        .toBeTruthy();
    } catch {
      continue; // this candidate didn't pan out — try the next one
    }
    const after = await fetchStates(token);
    const match = after.find(
      (s) => !beforeIds.has(s.entity_id) && s.entity_id.startsWith('binary_sensor.')
    );
    pointId = candidateId;
    binaryEntityId = match!.entity_id;
    break;
  }
  expect(
    pointId,
    'no candidate point produced a new available binary_sensor.* entity'
  ).not.toBeNull();
  expect(binaryEntityId).not.toBeNull();

  const afterEnableIds = new Set((await fetchStates(token)).map((s) => s.entity_id));

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

        const after = await fetchStates(token);
        const candidate = after.find(
          (s) => !afterEnableIds.has(s.entity_id) && s.entity_id.startsWith('sensor.')
        );
        if (candidate && candidate.state !== 'unavailable') {
          sensorEntityId = candidate.entity_id;
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
});
