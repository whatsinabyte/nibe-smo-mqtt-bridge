import { test, expect, request as pwRequest } from '@playwright/test';
import { gotoLoggedIn, loginToHa, readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  HA_URL,
  haNow,
  pointEntityId,
  recreateBridge,
  savedMenuDashboardMtime,
  savedMenuDashboardText,
} from './support/stack';

/**
 * A ground-source S1155, emulated from its owner's own bulk fetch (GitHub
 * issue #82), gets its water/water menus and the registers mapped from it.
 *
 * Runs only on a stack whose mock serves that dump as an "S1155" — the
 * reference dump is air/water, where these menus are hidden:
 *
 *   MOCK_DUMP=../../reference-dumps/all_points_en_s1155_issue82.json \
 *   MOCK_PRODUCT_NAME=S1155 ./stacks.sh up-one 4
 *   E2E_STACK=4 MOCK_PRODUCT_NAME=S1155 npx playwright test tests/ground-source-s1155.spec.ts
 *   ./stacks.sh down-one 4
 *
 * In Home Assistant: the GP1 "auto" settings (declared 0–1 with unit %) are
 * switches, heat pump type 55 reads "S1155", compressor starts count up,
 * and an external-control compressor count is a sensor. On the Nibe Menus
 * dashboard, menus 7.1.2.2, 7.1.2.7 and 7.1.2.8 carry the twelve settings
 * mapped from the dump as tiles with their control, and the browser renders
 * menu 7.1.2.2's toggles and -/+ buttons.
 */

const MAPPED: [number, string][] = [
  [4799, 'toggle'], // 7.1.2.2 Heating, auto
  [3961, 'numeric-input'], // Manual speed
  [8033, 'numeric-input'], // Minimum permitted speed
  [3963, 'numeric-input'], // Speed in wait mode
  [4798, 'toggle'], // Hot water, auto
  [3960, 'numeric-input'], // Hot water, manual speed
  [4800, 'toggle'], // Pool, auto
  [3962, 'numeric-input'], // Pool, manual speed
  [5481, 'numeric-input'], // 7.1.2.7 Delta-T, fixed delta
  [3928, 'toggle'], // 7.1.2.8 Automatic reset
  [3929, 'numeric-input'], // Alarm temperature
  [3931, 'numeric-input'], // Max brine in
];

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function haState(token: string, entityId: string): Promise<any> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = resp.ok() ? await resp.json() : null;
  await ctx.dispose();
  return body;
}

test('an S1155 gets its ground-source menus and registers', async ({ page }) => {
  test.skip(process.env.MOCK_PRODUCT_NAME !== 'S1155', 'needs a stack serving the S1155 dump');
  test.setTimeout(600_000);
  const token = readToken();
  const recreatedAt = haNow();
  try {
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-menus.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });

    // Every mapped setting has an entity, and the dashboard saved by this
    // run shows each of them as a tile.
    const ids = new Map<number, string>();
    await expect
      .poll(
        async () => {
          for (const [pid] of MAPPED) {
            const id = await pointEntityId(token, pid);
            if (id) ids.set(pid, id);
          }
          const text = savedMenuDashboardMtime() >= recreatedAt ? savedMenuDashboardText() : '';
          return MAPPED.filter(([pid]) => ids.has(pid) && text.includes(`"${ids.get(pid)}"`)).length;
        },
        { timeout: 360_000, intervals: [10_000], message: 'not every mapped setting reached the dashboard' }
      )
      .toBe(MAPPED.length);

    const tiles = new Map<string, any>();
    const walk = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(walk);
      else if (node && typeof node === 'object') {
        const obj = node as Record<string, any>;
        if (obj.type === 'tile') tiles.set(obj.entity, obj);
        Object.values(obj).forEach(walk);
      }
    };
    walk(JSON.parse(savedMenuDashboardText()));
    for (const [pid, feature] of MAPPED) {
      const tile = tiles.get(ids.get(pid)!);
      expect(tile, `point ${pid} (${ids.get(pid)}) is not a tile`).toBeTruthy();
      expect(tile.features[0].type, `point ${pid}'s tile control`).toBe(feature);
    }
    for (const pid of [4798, 4799, 4800, 3928]) {
      expect(ids.get(pid), `point ${pid} should be a switch`).toMatch(/^switch\./);
    }

    // Read-only points the dump brought to light, enabled here if the
    // menus don't already.
    const readOnly: [number, (s: any) => boolean][] = [
      [2527, (s) => s?.state === 'S1155'],
      [1959, (s) => s?.attributes?.state_class === 'total_increasing'],
      [671, (s) => String(s?.entity_id).startsWith('sensor.')],
    ];
    for (const [pid] of readOnly) {
      if (!(await pointEntityId(token, pid))) {
        await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(pid));
      }
    }
    for (const [pid, check] of readOnly) {
      await expect
        .poll(
          async () => {
            const id = await pointEntityId(token, pid);
            return id ? check(await haState(token, id)) : false;
          },
          { timeout: 90_000, message: `point ${pid} has the wrong type, state or state class` }
        )
        .toBe(true);
    }

    // Rendered: the view holding 7.1.2.2's Heating, auto shows its toggle
    // and the -/+ buttons of its speeds.
    const config = JSON.parse(savedMenuDashboardText());
    const view = (config.data?.config ?? config).views.find((v: any) =>
      JSON.stringify(v).includes(`"${ids.get(4799)}"`)
    );
    expect(view, 'no view holds menu 7.1.2.2').toBeTruthy();
    await loginToHa(page);
    await gotoLoggedIn(page, `/nibe-menus/${view.path}`);
    await expect(page.locator('hui-tile-card', { hasText: 'Heating, auto' }).first()).toBeVisible({
      timeout: 30_000,
    });
    await expect(
      page.locator('hui-tile-card', { hasText: 'Manual speed' }).locator('ha-control-number-buttons').first()
    ).toBeVisible({ timeout: 30_000 });
  } finally {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
