import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  HA_URL,
  haNow,
  publishRetained,
  readRetained,
  recreateBridge,
  savedMenuDashboardEntities,
  savedMenuDashboardMtime,
  savedMenuDashboardText,
  stopBridge,
} from './support/stack';

/**
 * A dynamic point shown by another dynamic point appears on the Nibe Menus
 * dashboard.
 *
 * The menu dashboard never renders a dynamic point as a row of its own: it
 * injects it below its controller's row. A controller that is itself dynamic
 * has no row either, so a point it shows used to appear nowhere — enabled in
 * Home Assistant, absent from the dashboard. Every writable switch/select is
 * tracked as a possible controller, dynamic ones included, so such a chain is
 * an ordinary learned outcome.
 *
 * The chain is seeded rather than learned, through the same retained
 * dynamic-map topic the bridge restores from at startup, using real points
 * the mock API serves:
 *
 *   3846 (a switch placed in menu_structure.yaml)
 *     └ shows 3933 (a select)
 *         └ shows 248 (a sensor placed in no menu)
 *
 * Both are recorded for every value, so the chain is active whatever the
 * mock reports. 248 is in no menu, so the nested injection is the only way
 * it can reach the dashboard. The map is put back afterwards; the next
 * start then retires 3933/248 as stale dynamic points by itself.
 */

const DYNAMIC_MAP_TOPIC = 'nibe/browser/dynamic_point_map';
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

/** Every "↳ …" label on the saved menu dashboard, read from its JSON. */
function injectedLabels(): string[] {
  const labels: string[] = [];
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) node.forEach(walk);
    else if (node && typeof node === 'object') {
      const obj = node as Record<string, unknown>;
      if (typeof obj.label === 'string' && obj.label.startsWith('↳')) labels.push(obj.label);
      Object.values(obj).forEach(walk);
    }
  };
  const text = savedMenuDashboardText();
  if (text.trim()) walk(JSON.parse(text));
  return labels;
}

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function entityIdForPoint(token: string, pointId: number): Promise<string | undefined> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const states: { entity_id: string; attributes: Record<string, unknown> }[] = resp.ok()
    ? await resp.json()
    : [];
  await ctx.dispose();
  return states.find((s) => s.attributes?.point_id === String(pointId))?.entity_id;
}

test('a point shown by a dynamic select appears on the menu dashboard', async () => {
  // A mode change into menus (a few hundred entities), its dashboard build,
  // and putting the bridge back afterwards.
  test.setTimeout(900_000);
  const token = readToken();

  // Stopped first, so its own shutdown can't overwrite the seeded map.
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

    const recreatedAt = haNow();
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-menus.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });

    // Both dynamic points must have live entities. This is a regression check
    // of its own: the startup used to judge the map's entries against the
    // static baseline, which leaves dynamic points out, so 3933's entry was
    // marked firmware_removed although the controller served it — and 248,
    // shown by it, was never expected, never enabled, and if it had been
    // active before the restart, deleted as stale.
    let selectEntity: string | undefined;
    let nestedEntity: string | undefined;
    await expect
      .poll(
        async () => {
          selectEntity = await entityIdForPoint(token, DYNAMIC_SELECT);
          nestedEntity = await entityIdForPoint(token, NESTED);
          return Boolean(selectEntity && nestedEntity);
        },
        {
          timeout: 120_000,
          message:
            `points ${DYNAMIC_SELECT} and ${NESTED} never both got HA entities — the startup ` +
            `marked dynamic select ${DYNAMIC_SELECT}'s map entry firmware_removed, so the ` +
            `point it shows was never expected`,
        }
      )
      .toBe(true);

    await expect
      .poll(
        () =>
          savedMenuDashboardMtime() >= recreatedAt &&
          Boolean(savedMenuDashboardEntities()?.has(selectEntity!)),
        {
          timeout: 300_000,
          message:
            `this run never saved a Nibe Menus dashboard showing ${selectEntity} under ` +
            `point ${CONTROLLER} (precondition — single-level injection)`,
        }
      )
      .toBe(true);

    await expect
      .poll(() => savedMenuDashboardEntities()?.has(nestedEntity!), {
        timeout: 60_000,
        message:
          `point ${NESTED} (${nestedEntity}) is live and shown by dynamic select ` +
          `${DYNAMIC_SELECT}, but the saved menu dashboard doesn't include it — a point shown ` +
          'by a dynamic controller appears nowhere on the dashboard',
      })
      .toBe(true);

    // 248 declares min == max (0–0 %), this firmware's "no bounds declared"
    // convention shared by about half of all points; its injected row used to
    // claim a "0 – 0 %" range.
    const fakeRanges = injectedLabels().filter((l) => / 0 – 0\b/.test(l));
    expect(
      fakeRanges,
      'an injected dynamic row shows a "0 – 0" range for a point that declares no bounds'
    ).toEqual([]);

    // ── Disabling the controller must not take its dynamic points with it ──
    // Disabling 3846 in HA changes nothing on the device: its setting stays,
    // so 3933 and 248 stay live entities with current values. The dashboard
    // used to inject them only below an enabled controller, so they vanished
    // with it.
    const controllerEntity = await entityIdForPoint(token, CONTROLLER);
    expect(controllerEntity, `precondition: controller ${CONTROLLER} has no HA entity`).toBeTruthy();
    await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(CONTROLLER));
    try {
      // Wait for the regen that reflects the disable, whatever it renders.
      await expect
        .poll(() => savedMenuDashboardEntities()?.has(controllerEntity!), {
          timeout: 90_000,
          message: `precondition: the menu dashboard never reflected disabling ${controllerEntity}`,
        })
        .toBe(false);
      const entities = savedMenuDashboardEntities();
      expect(
        Boolean(entities?.has(selectEntity!) && entities?.has(nestedEntity!)),
        `controller ${CONTROLLER} was disabled in HA, and the menu dashboard dropped the dynamic ` +
          `points it shows (${selectEntity}, ${nestedEntity}) although they are still live entities`
      ).toBe(true);
      const text = savedMenuDashboardText();
      expect(
        text.includes('not enabled in HA') && text.includes('current value: '),
        'the disabled controller\'s row should name its current value on the device'
      ).toBe(true);
    } finally {
      await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(CONTROLLER));
    }
  } finally {
    stopBridge();
    publishRetained(DYNAMIC_MAP_TOPIC, originalMap ?? '');
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
