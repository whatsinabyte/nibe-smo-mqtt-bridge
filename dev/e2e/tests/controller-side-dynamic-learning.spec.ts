import { test, expect, request as pwRequest } from '@playwright/test';
import * as zlib from 'zlib';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  bridgeLogsSince,
  pointEntityId,
  publishRetained,
  readRetained,
  recreateBridge,
  setMockValue,
  stopBridge,
} from './support/stack';

/**
 * A switch changed on the controller's own display, not through Home
 * Assistant, still has the points it shows learned as its dynamic points.
 *
 * Only an HA write used to open the post-write scan window that links new
 * points to a switch. Flipped on the display, nothing opened one: the points
 * the switch shows were indexed as new static points ("New permanent point …
 * indexing as static"), as after a firmware update, and were not linked to
 * the switch. Now a change of one candidate switch between two polls, with no
 * HA write pending, opens the same window — bookkeeping only, the bridge
 * writes nothing.
 *
 * Uses 4537 ("Pool 2 accessory", a plain 0/1 holding switch no other spec
 * touches) and a point the reference dump doesn't have, injected through the
 * mock's control channel a poll after the switch changes, the way the
 * firmware serves a switch's points a while after it changes. The learned link
 * is read from the retained dynamic-map topic, the changelog entry from the
 * retained changelog history.
 */

const DYNAMIC_MAP_TOPIC = 'nibe/browser/dynamic_point_map';
const ACTIVE_DYNAMIC_TOPIC = 'nibe/browser/active_dynamic_points';
const HISTORY_TOPIC = 'nibe/browser/changelog/history';
const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const SWITCH = 4537;
const SHOWN = 99537;

const SHOWN_DEFINITION = {
  title: 'Pool 2 temperature (e2e)',
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
    modbusRegisterID: 9537,
    minValue: 0,
    maxValue: 0,
    intDefaultValue: 0,
    change: 1,
    stringDefaultValue: '',
  },
  value: { variableId: SHOWN, integerValue: 245, stringValue: '', isOk: true },
};

async function mockControl(path: string, data: unknown): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/${path}`, { data });
  await ctx.dispose();
  if (!resp.ok()) throw new Error(`mock-control/${path}: ${resp.status()}`);
}

function decode(raw: string | null): any {
  if (!raw) return null;
  const json = raw.startsWith('gzip1:')
    ? zlib.gunzipSync(Buffer.from(raw.slice('gzip1:'.length), 'base64')).toString('utf-8')
    : raw;
  return JSON.parse(json);
}

/** The points the retained dynamic map has learned for one value of SWITCH. */
function learned(value: number): number[] | undefined {
  return decode(readRetained(DYNAMIC_MAP_TOPIC))?.[String(SWITCH)]?.dynamic_points_by_value?.[
    String(value)
  ];
}

test('points a switch shows when changed on the controller are learned as its dynamic points', async () => {
  // A stop and two starts, a few 15s polls, and the 5s polls of a scan window.
  test.setTimeout(420_000);
  const token = readToken();

  stopBridge();
  const originalMap = readRetained(DYNAMIC_MAP_TOPIC);
  const originalActive = readRetained(ACTIVE_DYNAMIC_TOPIC);

  try {
    // Start with nothing learned for the switch (an earlier run may have),
    // the switch off and the point it shows not served.
    const map = decode(originalMap) ?? {};
    delete map[String(SWITCH)];
    publishRetained(DYNAMIC_MAP_TOPIC, JSON.stringify(map));
    await setMockValue(SWITCH, 0);
    await mockControl(`points/${SHOWN}`, SHOWN_DEFINITION);
    await mockControl(`hidden/${SHOWN}`, { hidden: true });
    await recreateBridge(DEFAULT_BRIDGE_ENV);
    expect(learned(1), 'precondition: the switch already has learned points').toBeUndefined();

    // ── 1. The switch is turned on at the controller: a scan window opens ──
    const since = new Date().toISOString();
    await setMockValue(SWITCH, 1);
    await expect
      .poll(() => bridgeLogsSince(since).includes(`Point ${SWITCH} changed on the controller`), {
        timeout: 60_000,
        message: `no scan window opened for point ${SWITCH} changed on the controller`,
      })
      .toBe(true);

    // ── 2. The point it shows arrives a poll later: linked to the switch ───
    await mockControl(`hidden/${SHOWN}`, { hidden: false });
    await expect
      .poll(() => learned(1), {
        timeout: 60_000,
        message: `point ${SHOWN} was not learned as shown by ${SWITCH} = 1`,
      })
      .toEqual([SHOWN]);
    expect(bridgeLogsSince(since)).not.toContain(`New permanent point ${SHOWN}`);

    // Enabled in Home Assistant, as a point an HA write uncovers is.
    await expect
      .poll(async () => (await pointEntityId(token, SHOWN)) ?? '', {
        timeout: 30_000,
        message: `point ${SHOWN} has no Home Assistant entity`,
      })
      .toMatch(/^sensor\./);

    // The changelog says the switch was set on the controller.
    const entry = (decode(readRetained(HISTORY_TOPIC))?.history ?? []).find((e: any) =>
      (e.added ?? []).some((a: any) => a.id === SHOWN)
    );
    expect(entry?.triggered_by?.changed_on, 'changelog entry for the appearance').toBe(
      'controller'
    );

    // ── 3. Off again at the controller: the point goes with it ─────────────
    await setMockValue(SWITCH, 0);
    await mockControl(`hidden/${SHOWN}`, { hidden: true });
    await expect
      .poll(async () => (await pointEntityId(token, SHOWN)) ?? null, {
        timeout: 60_000,
        message: `point ${SHOWN}'s entity stayed after ${SWITCH} was turned off`,
      })
      .toBeNull();
    expect(learned(0)).toEqual([]);
  } finally {
    await setMockValue(SWITCH, 0);
    await mockControl(`hidden/${SHOWN}`, { hidden: true });
    stopBridge();
    publishRetained(DYNAMIC_MAP_TOPIC, originalMap ?? '');
    publishRetained(ACTIVE_DYNAMIC_TOPIC, originalActive ?? '');
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
