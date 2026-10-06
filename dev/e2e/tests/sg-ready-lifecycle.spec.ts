import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import { readToken } from './support/ha-login';

/**
 * The full lifecycle of a set of dynamic points after they have been
 * learned: disappearing and reappearing at runtime, then going away while
 * the bridge is down. Uses the same three SG Ready points as
 * sg-ready-dynamic-discovery.spec.ts, and is named to run right after it so
 * it normally picks up the points that spec already activated (it activates
 * them itself if run on its own).
 *
 * Two regressions are pinned here:
 *
 *   - **Ghost entity after a restart.** _reconcile_dynamic_points runs inside
 *     discover_points(), before the MQTT scan has filled
 *     mqtt_enabled_points, and used to clear a removed dynamic point's
 *     retained discovery config only when the point was in that (always
 *     empty at startup) set. A dynamic point whose controlling switch was
 *     changed on the controller itself while the bridge was down therefore
 *     kept its HA entity forever. Step 4 reproduces exactly that: stop the
 *     bridge, flip 10613 back to 0 and withdraw the three points in the
 *     mock (what the real controller does), start the bridge, and require
 *     the entities to be gone from HA.
 *
 *   - **Entity stats drift.** The entity stats sensor's per-type counts must
 *     always add up to its enabled count. A dynamic disappearance used to
 *     deindex the point before disabling it, so the stats decrement saw no
 *     metadata and every appear/disappear cycle left the counts one higher;
 *     points re-activated at startup were also counted twice. Checked after
 *     every step.
 *
 * The mock has no notion of points depending on 10613's value, so the spec
 * hides/unhides them through its control channel alongside each write —
 * the same thing the real firmware does on its own.
 */

const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const BRIDGE_CONTAINER = process.env.BRIDGE_CONTAINER || 'nibe-e2e-bridge';

const ACTIVATION_POINT_ID = '10613';
// All three SG Ready points are hidden/unhidden together, but only 4694 and
// 10614 are tracked in HA. 3260 is reclassified binary_sensor -> sensor on
// its first poll, and the reclassified entity currently reaches HA without
// its attributes (point_id included), so it can't be found the way the
// others are — a separate bug, not what this spec is about.
const DYNAMIC_POINT_IDS = ['3260', '4694', '10614'];
const TRACKED_POINT_IDS = ['4694', '10614'];

/** Same real point definitions as sg-ready-dynamic-discovery.spec.ts (see
 * that file for where they come from) — duplicated rather than imported so
 * each spec stays readable on its own. */
function sgReadyPoint(
  id: number,
  title: string,
  size: string,
  registerType: string,
  writable: boolean,
  max: number,
  registerId: number,
  value: number
): unknown {
  return {
    title,
    description: '',
    metadata: {
      variableId: id,
      variableType: 'integer',
      variableSize: size,
      unit: '',
      modbusRegisterType: registerType,
      shortUnit: '',
      isWritable: writable,
      divisor: 1,
      decimal: 0,
      modbusRegisterID: registerId,
      minValue: 0,
      maxValue: max,
      intDefaultValue: writable ? 1 : 0,
      change: 1,
      stringDefaultValue: '',
    },
    value: { variableId: id, integerValue: value, stringValue: '', isOk: true },
  };
}

const SG_READY_DYNAMIC_POINTS: Record<string, unknown> = {
  '3260': sgReadyPoint(3260, 'Operating mode (SG Ready)', 'u8', 'MODBUS_INPUT_REGISTER', false, 0, 1911, 10),
  '4694': sgReadyPoint(4694, 'Cooling (SG Ready)', 'u8', 'MODBUS_HOLDING_REGISTER', true, 1, 761, 0),
  '10614': sgReadyPoint(10614, 'Req. op. mode (SG Ready)', 'u8', 'MODBUS_HOLDING_REGISTER', true, 3, 6008, 1),
};

interface HaState {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
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

async function stateExists(token: string, entityId: string): Promise<boolean> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const exists = resp.ok();
  await ctx.dispose();
  return exists;
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

async function mockControl(path: string, data: unknown): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/${path}`, { data });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function setDynamicPointsHidden(hidden: boolean): Promise<void> {
  for (const id of DYNAMIC_POINT_IDS) {
    await mockControl(`hidden/${id}`, { hidden });
  }
}

/** entity_id of the live HA entity for a point, found by the point_id
 * attribute every bridge entity carries — HA derives entity_ids from the
 * (translated) discovery name, not the point id. */
function entityForPoint(states: HaState[], pointId: string): HaState | undefined {
  return states.find((s) => s.attributes?.point_id === pointId);
}

async function waitForPointEntities(token: string, pointIds: string[]): Promise<Record<string, string>> {
  const found: Record<string, string> = {};
  await expect
    .poll(
      async () => {
        const states = await fetchStates(token);
        for (const id of pointIds) {
          const s = entityForPoint(states, id);
          if (s && s.state !== 'unavailable') found[id] = s.entity_id;
        }
        return pointIds.every((id) => id in found);
      },
      { timeout: 150_000, message: `entities for points ${pointIds.join(', ')} never all became available` }
    )
    .toBeTruthy();
  return found;
}

async function waitForEntitiesGone(token: string, entityIds: string[], message: string): Promise<void> {
  await expect
    .poll(
      async () => {
        for (const id of entityIds) {
          if (await stateExists(token, id)) return false;
        }
        return true;
      },
      { timeout: 120_000, message }
    )
    .toBeTruthy();
}

/** The stats sensor's by_type counts must add up to its enabled count. The
 * bridge republishes stats every poll cycle, so poll until they agree
 * rather than reading once mid-update. */
async function expectStatsConsistent(token: string, when: string): Promise<void> {
  let last = '';
  await expect
    .poll(
      async () => {
        const stats = (await fetchStates(token)).find(
          (s) => s.attributes && typeof s.attributes.by_type === 'object' && 'mqtt_enabled' in s.attributes
        );
        if (!stats) return false;
        const byType = stats.attributes.by_type as Record<string, number>;
        const sum = Object.values(byType).reduce((a, b) => a + b, 0);
        last = `by_type sums to ${sum}, mqtt_enabled is ${String(stats.attributes.mqtt_enabled)}`;
        return sum === stats.attributes.mqtt_enabled;
      },
      { timeout: 60_000, message: `entity stats inconsistent ${when}` }
    )
    .toBeTruthy();
  expect(last).not.toBe('');
}

async function waitForBridgeReady(since: string): Promise<void> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const logs = execSync(`docker logs --since ${since} ${BRIDGE_CONTAINER} 2>&1 || true`, {
      encoding: 'utf-8',
    });
    if (logs.includes('Bridge ready')) return;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error('bridge did not report ready within 120s of being started');
}

test('dynamic points disappear, reappear and are cleaned up after going away during a restart', async () => {
  // Up to three post-write scan windows plus a full bridge restart.
  test.setTimeout(600_000);

  const token = readToken();

  // ── 1. Make sure the SG Ready points are active ────────────────────────
  // Normally already true (sg-ready-dynamic-discovery ran just before).
  // Inject before unhiding: the mock forgets injected points whenever it is
  // restarted (controller-outage.spec.ts does that), and refuses to unhide a
  // point it doesn't know.
  for (const [id, definition] of Object.entries(SG_READY_DYNAMIC_POINTS)) {
    await mockControl(`points/${id}`, definition);
  }
  await setDynamicPointsHidden(false);
  await callService(token, 'mqtt', 'publish', {
    topic: 'homeassistant/text/nibe_enable_entity/set',
    payload: ACTIVATION_POINT_ID,
  });
  const switchId = (await waitForPointEntities(token, [ACTIVATION_POINT_ID]))[ACTIVATION_POINT_ID];
  const switchState = (await fetchStates(token)).find((s) => s.entity_id === switchId)?.state;
  if (switchState !== 'on') {
    await callService(token, 'switch', 'turn_on', { entity_id: switchId });
  }
  let dynamicIds = await waitForPointEntities(token, TRACKED_POINT_IDS);
  await expectStatsConsistent(token, 'with the SG Ready points active');

  // ── 2. Runtime disappearance ───────────────────────────────────────────
  // Turning 10613 off is a write to a learned controlling switch, which
  // opens a scan window; withdrawing the points in the mock is what the
  // real controller does in response.
  await callService(token, 'switch', 'turn_off', { entity_id: switchId });
  await setDynamicPointsHidden(true);
  await waitForEntitiesGone(
    token,
    Object.values(dynamicIds),
    'SG Ready entities were not removed after 10613 was turned off'
  );
  await expectStatsConsistent(token, 'after the dynamic points disappeared at runtime');

  // ── 3. Runtime reappearance ────────────────────────────────────────────
  await setDynamicPointsHidden(false);
  await callService(token, 'switch', 'turn_on', { entity_id: switchId });
  dynamicIds = await waitForPointEntities(token, TRACKED_POINT_IDS);
  await expectStatsConsistent(token, 'after the dynamic points reappeared');

  // ── 4. The points go away while the bridge is down ─────────────────────
  // Someone switches SG Ready off on the controller's own display while
  // the bridge isn't running.
  execSync(`docker stop ${BRIDGE_CONTAINER}`, { stdio: 'ignore' });
  await mockControl(`points/${ACTIVATION_POINT_ID}`, { integerValue: 0 });
  await setDynamicPointsHidden(true);
  const since = new Date().toISOString();
  execSync(`docker start ${BRIDGE_CONTAINER}`, { stdio: 'ignore' });
  await waitForBridgeReady(since);

  await waitForEntitiesGone(
    token,
    Object.values(dynamicIds),
    'SG Ready entities survived a restart after their controlling switch was turned off while ' +
      'the bridge was down — their retained discovery configs were never cleared (ghost entities)'
  );
  await expectStatsConsistent(token, 'after the restart');

  // Left as found by later specs: 10613 off, SG Ready points withdrawn.
});
