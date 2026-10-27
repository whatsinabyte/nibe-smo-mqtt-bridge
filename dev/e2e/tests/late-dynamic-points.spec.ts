import { test, expect, request as pwRequest } from '@playwright/test';
import * as zlib from 'zlib';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  HA_URL,
  bridgeLogsSince,
  pointEntityId,
  publishRetained,
  readRetained,
  recreateBridge,
  setMockValue,
  stopBridge,
} from './support/stack';

/**
 * Points a switch shows that arrive after its scan window are still learned
 * as its dynamic points.
 *
 * An HA write to a switch not learned yet watches 90 s for the points it
 * shows. When they came later, the window recorded "shows nothing" for the
 * written value and — a switch having two values — for the other one too.
 * The switch was then taken for one that shows nothing: never scanned again,
 * and its points indexed as static whenever they came. Now the window leaves
 * a late-arrival period in which new points are still linked to it.
 *
 * Enables 4537 ("Pool 2 accessory", a plain 0/1 holding switch no other spec
 * writes), turns it on through Home Assistant, and serves a point the dump
 * doesn't have only after the window has ended. The learned link is read
 * from the retained dynamic-map topic.
 */

const DYNAMIC_MAP_TOPIC = 'nibe/browser/dynamic_point_map';
const ACTIVE_DYNAMIC_TOPIC = 'nibe/browser/active_dynamic_points';
const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const SWITCH = 4537;
const SHOWN = 99538;

const SHOWN_DEFINITION = {
  title: 'Pool 2 late point (e2e)',
  description: '',
  metadata: {
    variableId: SHOWN,
    variableType: 'integer',
    variableSize: 's16',
    unit: '°C',
    modbusRegisterType: 'MODBUS_INPUT_REGISTER',
    shortUnit: '°C',
    isWritable: false,
    divisor: 10,
    decimal: 1,
    modbusRegisterID: 9538,
    minValue: 0,
    maxValue: 0,
    intDefaultValue: 0,
    change: 1,
    stringDefaultValue: '',
  },
  value: { variableId: SHOWN, integerValue: 251, stringValue: '', isOk: true },
};

async function mockControl(path: string, data: unknown): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/${path}`, { data });
  await ctx.dispose();
  if (!resp.ok()) throw new Error(`mock-control/${path}: ${resp.status()}`);
}

async function haPost(token: string, path: string, data: unknown): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/${path}`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data,
  });
  await ctx.dispose();
  if (!resp.ok()) throw new Error(`${path}: ${resp.status()}`);
}

async function haState(token: string, entityId: string): Promise<string> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const state = resp.ok() ? (await resp.json()).state : 'deleted';
  await ctx.dispose();
  return state;
}

function decode(raw: string | null): any {
  if (!raw) return null;
  const json = raw.startsWith('gzip1:')
    ? zlib.gunzipSync(Buffer.from(raw.slice('gzip1:'.length), 'base64')).toString('utf-8')
    : raw;
  return JSON.parse(json);
}

function mapEntry(): any {
  return decode(readRetained(DYNAMIC_MAP_TOPIC))?.[String(SWITCH)];
}

test('points arriving after the scan window of an HA write are learned as the switch’s', async () => {
  // A stop and two starts, the 90s scan window, and a few 15s polls after it.
  test.setTimeout(480_000);
  const token = readToken();

  stopBridge();
  const originalMap = readRetained(DYNAMIC_MAP_TOPIC);
  const originalActive = readRetained(ACTIVE_DYNAMIC_TOPIC);

  try {
    // Nothing learned for the switch yet, the switch off and the point it
    // shows not served.
    const map = decode(originalMap) ?? {};
    delete map[String(SWITCH)];
    publishRetained(DYNAMIC_MAP_TOPIC, JSON.stringify(map));
    await setMockValue(SWITCH, 0);
    await mockControl(`points/${SHOWN}`, SHOWN_DEFINITION);
    await mockControl(`hidden/${SHOWN}`, { hidden: true });
    await recreateBridge(DEFAULT_BRIDGE_ENV);

    await haPost(token, 'services/mqtt/publish', {
      topic: 'homeassistant/text/nibe_enable_entity/set',
      payload: String(SWITCH),
    });
    let switchId = '';
    await expect
      .poll(
        async () => {
          switchId = (await pointEntityId(token, SWITCH)) ?? '';
          return switchId ? await haState(token, switchId) : 'deleted';
        },
        { timeout: 60_000, message: `precondition: point ${SWITCH} has no available switch` }
      )
      .toBe('off');

    // ── 1. Turned on through HA; the window ends before the point comes ────
    const since = new Date().toISOString();
    await haPost(token, 'services/switch/turn_on', { entity_id: switchId });
    await expect
      .poll(() => bridgeLogsSince(since).includes(`Post-write scan window ended for point ${SWITCH}`), {
        timeout: 150_000,
        message: `the scan window for point ${SWITCH} never ended`,
      })
      .toBe(true);
    // Recorded when the learning detection's own 90s run out, a poll apart.
    await expect
      .poll(() => mapEntry()?.dynamic_points_by_value?.['1'], {
        timeout: 30_000,
        message: 'precondition: the window recorded no outcome',
      })
      .toEqual([]);
    expect(mapEntry()?.is_controlling, 'precondition: taken for a switch showing nothing').toBe(
      false
    );

    // ── 2. The point arrives late: still linked to the switch ──────────────
    await mockControl(`hidden/${SHOWN}`, { hidden: false });
    await expect
      .poll(() => mapEntry()?.dynamic_points_by_value?.['1'], {
        timeout: 60_000,
        message: `point ${SHOWN}, served after the window, was not learned as shown by ${SWITCH} = 1`,
      })
      .toEqual([SHOWN]);
    expect(mapEntry()?.is_controlling).toBe(true);
    expect(bridgeLogsSince(since)).not.toContain(`New permanent point ${SHOWN}`);
    await expect
      .poll(async () => (await pointEntityId(token, SHOWN)) ?? '', {
        timeout: 30_000,
        message: `point ${SHOWN} has no Home Assistant entity`,
      })
      .toMatch(/^sensor\./);

    // ── 3. Turned off through HA: the point goes with it ───────────────────
    await haPost(token, 'services/switch/turn_off', { entity_id: switchId });
    await mockControl(`hidden/${SHOWN}`, { hidden: true });
    await expect
      .poll(async () => (await pointEntityId(token, SHOWN)) ?? null, {
        timeout: 60_000,
        message: `point ${SHOWN}'s entity stayed after ${SWITCH} was turned off`,
      })
      .toBeNull();
  } finally {
    await haPost(token, 'services/mqtt/publish', {
      topic: 'homeassistant/text/nibe_disable_entity/set',
      payload: String(SWITCH),
    }).catch(() => undefined);
    await setMockValue(SWITCH, 0);
    await mockControl(`hidden/${SHOWN}`, { hidden: true });
    stopBridge();
    publishRetained(DYNAMIC_MAP_TOPIC, originalMap ?? '');
    publishRetained(ACTIVE_DYNAMIC_TOPIC, originalActive ?? '');
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
