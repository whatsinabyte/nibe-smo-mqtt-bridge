import { test, expect, request as pwRequest } from '@playwright/test';
import * as zlib from 'zlib';
import {
  DEFAULT_BRIDGE_ENV,
  publishRetained,
  readRetained,
  recreateBridge,
  stopBridge,
} from './support/stack';

/**
 * A dynamic select that was hidden when the bridge started is taken back into
 * use once it reappears.
 *
 * At startup, discover_points marks a dynamic-map entry firmware_removed when
 * its point is absent from the bulk response. A switch/select that is itself
 * dynamic is absent whenever whatever shows it is off — so it gets marked, and
 * the startup was the only place that ever cleared the mark. Once switched
 * back on, its learned outcomes were ignored on writes, it was never probed,
 * and the menu dashboard didn't inject the points it shows, all until a later
 * restart happened to catch it present.
 *
 * Uses the same seeded chain as menus-nested-dynamic.spec.ts (3846 shows
 * 3933, which shows 248) and the mock's control channel to withhold 3933 for
 * the start, then return it. The map entry's state is read from the retained
 * dynamic-map topic the bridge persists it to.
 */

const DYNAMIC_MAP_TOPIC = 'nibe/browser/dynamic_point_map';
const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const CONTROLLER = 3846;
const DYNAMIC_SELECT = 3933;
const NESTED = 248;

function entry(pointId: number, entityType: string, shows: number, values: number[]) {
  return {
    point_id: pointId,
    title: `Point ${pointId}`,
    entity_type: entityType,
    processed_values: values,
    unprocessed_values: [],
    is_controlling: true,
    dynamic_points_by_value: Object.fromEntries(values.map((v) => [String(v), [shows]])),
    firmware_removed: false,
  };
}

async function setHidden(pointId: number, hidden: boolean): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}/mock-control/hidden/${pointId}`, {
    data: { hidden },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

/** firmware_removed of one entry in the retained dynamic map, or undefined. */
function firmwareRemoved(pointId: number): boolean | undefined {
  const raw = readRetained(DYNAMIC_MAP_TOPIC);
  if (!raw) return undefined;
  const json = raw.startsWith('gzip1:')
    ? zlib.gunzipSync(Buffer.from(raw.slice('gzip1:'.length), 'base64')).toString('utf-8')
    : raw;
  return JSON.parse(json)[String(pointId)]?.firmware_removed;
}

test('a dynamic select hidden at startup is restored when it reappears', async () => {
  // A stop, two starts, and a few bulk polls (15s interval).
  test.setTimeout(420_000);

  stopBridge();
  const originalMap = readRetained(DYNAMIC_MAP_TOPIC);

  try {
    publishRetained(
      DYNAMIC_MAP_TOPIC,
      JSON.stringify({
        [CONTROLLER]: entry(CONTROLLER, 'switch', DYNAMIC_SELECT, [0, 1]),
        [DYNAMIC_SELECT]: entry(DYNAMIC_SELECT, 'select', NESTED, [0, 1, 2, 3]),
      })
    );
    await setHidden(DYNAMIC_SELECT, true);
    await recreateBridge(DEFAULT_BRIDGE_ENV);

    // ── 1. Absent at startup: marked firmware_removed (that part is right) ─
    await expect
      .poll(() => firmwareRemoved(DYNAMIC_SELECT), {
        timeout: 30_000,
        message: `precondition: point ${DYNAMIC_SELECT} was hidden at startup but not marked`,
      })
      .toBe(true);

    // ── 2. Back in the bulk response: the mark must be cleared ─────────────
    await setHidden(DYNAMIC_SELECT, false);
    await expect
      .poll(() => firmwareRemoved(DYNAMIC_SELECT), {
        timeout: 90_000,
        message:
          `point ${DYNAMIC_SELECT} is back in the bulk response but its dynamic-map entry is ` +
          'still firmware_removed — its learned outcomes stay ignored until the next restart',
      })
      .toBe(false);
  } finally {
    await setHidden(DYNAMIC_SELECT, false);
    stopBridge();
    publishRetained(DYNAMIC_MAP_TOPIC, originalMap ?? '');
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
