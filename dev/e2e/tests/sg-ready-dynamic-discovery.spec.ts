import { test, expect, request as pwRequest } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Reproduces, against a real Home Assistant instance, the exact real-world
 * scenario a user's own production log captured (GitHub issues #35/#40):
 * writing to point 10613 ("Activate SG Ready via API") makes three new
 * dynamic points appear — 3260 ("Operating mode SG Ready"), 4694
 * ("Cooling SG Ready"), and 10614 ("Req. op. mode SG Ready") — and:
 *
 *   - 3260 is a u8 INPUT register with no firmware description, the exact
 *     shape that auto-detects as binary_sensor (see
 *     _is_auto_binary_sensor) despite reporting 10 (not 0/1) — proving the
 *     dynamic reclassification safety net (see
 *     binary-sensor-reclassification.spec.ts for the general mechanism)
 *     also fires correctly for this specific, real point, not just a
 *     synthetic one.
 *   - 10614 is this session's actual motivating case for both the SG
 *     Ready VALUE_MAPPINGS entry (0=Cut off/1=Standard/2=Encouraged/
 *     3=Ordered — see GitHub issue #40) and the translation feature
 *     (issue #39) — translation.spec.ts already covers the translation
 *     mechanism generically via a different, always-present point (3751,
 *     since 10614 doesn't exist in the static reference dump at all); this
 *     test is the one place the actual point this whole investigation was
 *     about gets exercised end to end, for real: its default state, its
 *     full published options list (all four translated labels, in the
 *     firmware's own order), and a real write-back through
 *     select.select_option using a translated option that doesn't exist
 *     anywhere in the untranslated English VALUE_MAPPINGS.
 *
 * The write to 10613 goes through a real HA switch.turn_on service call
 * (10613 auto-detects as a plain switch — u8, HOLDING, isWritable, 0/1),
 * not the mock API's test-only control channel: the bridge's dynamic-point
 * "learning detection" (_learning_detection_scan) only starts after an
 * actual write through its own write executor, not an externally-mutated
 * raw value — see that function's own docstring. The scan window is a
 * real 90s (_POST_WRITE_SCAN_S in nibe_entity_manager.py), so this test
 * needs a longer-than-default timeout.
 */

const SEED_OUT = path.join(__dirname, '..', 'seed-out');
const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';

/** Real data from a real installation (GitHub issue #40), captured after
 * activating point 10613 — reference-dumps/all_points_en.json has no
 * entry for any of these at all, since SG Ready was never enabled on the
 * install that dump was taken from. Injected via the mock's test-only
 * "new point" control channel (see mock_nibe_api.py's do_POST) rather
 * than modifying the static dump file itself, since these three points
 * are conditionally present depending on controller state, not a fixed
 * part of any one firmware snapshot. */
const SG_READY_DYNAMIC_POINTS: Record<string, unknown> = {
  '3260': {
    title: 'Operating mode (SG Ready)',
    description: '',
    metadata: {
      variableId: 3260,
      variableType: 'integer',
      variableSize: 'u8',
      unit: '',
      modbusRegisterType: 'MODBUS_INPUT_REGISTER',
      shortUnit: '',
      isWritable: false,
      divisor: 1,
      decimal: 0,
      modbusRegisterID: 1911,
      minValue: 0,
      maxValue: 0,
      intDefaultValue: 0,
      change: 1,
      stringDefaultValue: '',
    },
    value: { variableId: 3260, integerValue: 10, stringValue: '', isOk: true },
  },
  '4694': {
    title: 'Cooling (SG Ready)',
    description: '',
    metadata: {
      variableId: 4694,
      variableType: 'integer',
      variableSize: 'u8',
      unit: '',
      modbusRegisterType: 'MODBUS_HOLDING_REGISTER',
      shortUnit: '',
      isWritable: true,
      divisor: 1,
      decimal: 0,
      modbusRegisterID: 761,
      minValue: 0,
      maxValue: 1,
      intDefaultValue: 1,
      change: 1,
      stringDefaultValue: '',
    },
    value: { variableId: 4694, integerValue: 0, stringValue: '', isOk: true },
  },
  '10614': {
    title: 'Req. op. mode (SG Ready)',
    description: '',
    metadata: {
      variableId: 10614,
      variableType: 'integer',
      variableSize: 'u8',
      unit: '',
      modbusRegisterType: 'MODBUS_HOLDING_REGISTER',
      shortUnit: '',
      isWritable: true,
      divisor: 1,
      decimal: 0,
      modbusRegisterID: 6008,
      minValue: 0,
      maxValue: 3,
      intDefaultValue: 1,
      change: 1,
      stringDefaultValue: '',
    },
    value: { variableId: 10614, integerValue: 1, stringValue: '', isOk: true },
  },
};

async function injectMockPoint(pointId: string, definition: unknown): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/points/${pointId}`, {
    data: definition,
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

function readCredentials(): { username: string; password: string } {
  const raw = fs.readFileSync(path.join(SEED_OUT, 'credentials.json'), 'utf-8');
  return JSON.parse(raw);
}

function readToken(): string {
  return fs.readFileSync(path.join(SEED_OUT, 'token.txt'), 'utf-8').trim();
}

interface HaState {
  entity_id: string;
  state: string;
  attributes?: { options?: string[] };
}

async function fetchStates(token: string): Promise<HaState[]> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const states = await resp.json();
  await ctx.dispose();
  return states;
}

async function fetchState(token: string, entityId: string): Promise<HaState | null> {
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

async function callService(
  token: string,
  domain: string,
  service: string,
  data: Record<string, unknown>
): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/${domain}/${service}`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data,
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('writing to the SG Ready API-activation switch surfaces 3260/10614 correctly classified and translated', async ({
  page,
}) => {
  // Real-world evidence (GitHub issue #40) shows the firmware's ~1 minute
  // internal cache refresh, plus this bridge's own 90s post-write scan
  // window, plus login/enable/UI overhead — comfortably exceeds
  // playwright.config.ts's global 90s default.
  test.setTimeout(180_000);

  const { username, password } = readCredentials();
  const token = readToken();

  await page.goto('/');
  await page.getByLabel('Username').fill(username);
  await page.getByRole('textbox', { name: 'Password' }).fill(password);
  await page.getByRole('button', { name: /log in/i }).click();
  await expect(page).toHaveURL(/\/lovelace|\/$|\/home/, { timeout: 30_000 });

  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });

  const searchInput = card.locator('#search-input');
  const beforeActivation = await fetchStates(token);
  const beforeActivationIds = new Set(beforeActivation.map((s) => s.entity_id));

  // 1. Enable point 10613 ("Activate SG Ready via API") through the real
  // card, same round trip every other spec in this harness uses.
  const ACTIVATION_POINT_ID = '10613';
  await searchInput.fill(ACTIVATION_POINT_ID);
  const activationRow = card.locator(`tr[data-id="${ACTIVATION_POINT_ID}"]`);
  await expect(activationRow).toBeVisible({ timeout: 10_000 });
  const enableActivationButton = activationRow.locator('button[data-action="enable"]');
  if ((await enableActivationButton.count()) > 0) {
    await enableActivationButton.click();
    await expect(activationRow.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });
  }
  await searchInput.fill('');

  let activationEntityId: string | null = null;
  await expect
    .poll(
      async () => {
        const after = await fetchStates(token);
        const candidate = after.find(
          (s) => !beforeActivationIds.has(s.entity_id) && s.entity_id.startsWith('switch.')
        );
        if (candidate && candidate.state !== 'unavailable') {
          activationEntityId = candidate.entity_id;
          return true;
        }
        return false;
      },
      { timeout: 30_000, message: 'no new available switch.* entity appeared for point 10613' }
    )
    .toBeTruthy();
  expect(activationEntityId).not.toBeNull();

  // 2. Make the mock aware these three points exist at all — the static
  // dump has no data for them (see SG_READY_DYNAMIC_POINTS's own comment).
  // Injected before the write below so they're already present in the
  // mock's bulk response by the time the bridge's post-write scan looks.
  for (const [pointId, definition] of Object.entries(SG_READY_DYNAMIC_POINTS)) {
    await injectMockPoint(pointId, definition);
  }

  // 3. The real trigger: a genuine write through HA's own switch service —
  // this is what actually starts the bridge's learning-detection scan,
  // not just mutating the mock's stored value directly.
  const beforeWrite = await fetchStates(token);
  const beforeWriteIds = new Set(beforeWrite.map((s) => s.entity_id));
  await callService(token, 'switch', 'turn_on', { entity_id: activationEntityId });

  // 4. Wait out the real ~90s scan window for the three dynamic points to
  // appear. Point 10614 is the one this test cares about most (this
  // session's actual motivating case); 3260 and 4694 arrive alongside it
  // in the same learning-detection event.
  let point10614EntityId: string | null = null;
  await expect
    .poll(
      async () => {
        const after = await fetchStates(token);
        const newIds = after.filter((s) => !beforeWriteIds.has(s.entity_id));
        // 10614 publishes as a select (see nibe_discovery_config's option
        // translation) — identify it by having one of the known translated
        // SG Ready option labels as its state, not by entity_id shape
        // (HA derives entity_id from the discovery name, not the point
        // ID — see translation.spec.ts's own note on this).
        const candidate = newIds.find(
          (s) =>
            s.entity_id.startsWith('select.') &&
            ['Afgesloten', 'Standaard', 'Aangemoedigd', 'Opgedragen'].includes(s.state)
        );
        if (candidate) {
          point10614EntityId = candidate.entity_id;
          return true;
        }
        return false;
      },
      {
        timeout: 150_000,
        message:
          'no new select.* entity with a translated SG Ready option label appeared after activating SG Ready',
      }
    )
    .toBeTruthy();
  expect(point10614EntityId).not.toBeNull();

  // 5. Point 10614's default raw value in this dump is 1 ("Standard"),
  // translated to Dutch "Standaard" — confirm the exact expected label,
  // not just "some select appeared".
  const state10614 = (await fetchStates(token)).find((s) => s.entity_id === point10614EntityId);
  expect(state10614?.state).toBe('Standaard');

  // 5a. The full published options list must be every translated SG Ready
  // label, in the right order — not just that the current state happens
  // to be one of them. This is the actual point translation.spec.ts's
  // select test exercises via a substitute (3751); this is the same
  // check for the real point this feature was motivated by.
  const attrs10614 = await fetchState(token, point10614EntityId!);
  expect(attrs10614?.attributes?.options).toEqual([
    'Afgesloten',
    'Standaard',
    'Aangemoedigd',
    'Opgedragen',
  ]);

  // 5b. Write back through 10614 itself (translation.spec.ts's select test
  // already proves this mechanism generically via point 3751 — this
  // confirms it for the actual point, not just a stand-in). "Aangemoedigd"
  // is not in the untranslated English VALUE_MAPPINGS at all, so this only
  // succeeds if the translated_reverse_map path in _parse_command_payload
  // is genuinely being reached for this point.
  await callService(token, 'select', 'select_option', {
    entity_id: point10614EntityId,
    option: 'Aangemoedigd',
  });
  await expect
    .poll(
      async () => {
        const state = await fetchState(token, point10614EntityId!);
        return state?.state === 'Aangemoedigd';
      },
      {
        timeout: 30_000,
        message: `${point10614EntityId} never reached state "Aangemoedigd" after selecting it`,
      }
    )
    .toBeTruthy();

  // 6. Point 3260 must show up as sensor.*, not binary_sensor.* — u8 INPUT
  // register, no description, reports 10 (not 0/1), the exact shape the
  // dynamic reclassification safety net exists for (see
  // binary-sensor-reclassification.spec.ts). No translation is expected
  // here (3260 has no confirmed mapping beyond the "10" case — see GitHub
  // issue #40) — the raw value is the correct, honest thing to publish.
  const afterDiscovery = await fetchStates(token);
  const point3260Sensor = afterDiscovery.find(
    (s) => !beforeWriteIds.has(s.entity_id) && s.entity_id.startsWith('sensor.') && s.state === '10'
  );
  expect(
    point3260Sensor,
    'point 3260 should have appeared as an available sensor.* entity with raw state "10"'
  ).toBeTruthy();

  const point3260BinarySensor = afterDiscovery.find(
    (s) => !beforeWriteIds.has(s.entity_id) && s.entity_id.startsWith('binary_sensor.')
  );
  expect(
    point3260BinarySensor,
    'no new binary_sensor.* entity should exist after SG Ready discovery — 3260 must not stay misclassified'
  ).toBeUndefined();

  // 7. The discovery just performed is exactly what the changelog exists to
  // record — dynamic points appearing is its primary event source, not
  // ordinary user enables. Checking it here rather than in a spec of its
  // own reuses this scenario instead of paying for a second real detection
  // window, and it covers the whole path in one go: the bridge appending
  // the entry, gzip-compressing it onto a retained MQTT topic, and the card
  // subscribing, decompressing and rendering it.
  //
  // Worth knowing when reading this: the changelog is the one piece of
  // persisted state with no /data file fallback — retained MQTT is its only
  // home — so it is the only thing here that a broker wipe loses outright.
  await card.locator('#show-changelog').click();
  const changelogModal = card.locator('#changelog-modal');
  await expect(changelogModal).toBeVisible({ timeout: 10_000 });

  const changelogText = card.locator('#changelog-content');
  await expect(changelogText).toBeVisible({ timeout: 10_000 });
  await expect(changelogText).not.toContainText('No changes recorded yet', { timeout: 30_000 });

  // The entry must name the points that actually appeared, not just exist.
  await expect(changelogText).toContainText('10614', { timeout: 30_000 });
});
