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
 * Settings on the Nibe Menus dashboard are tiles with their control, each
 * with an info box above it.
 *
 * Home Assistant refuses a number between a setting's steps, and on a phone
 * the number box has no arrows. 32341 "Desired room temperature for zone 1"
 * declares change 5 at divisor 10: in menu 1.1 it must be a tile named by
 * the menu's label, with -/+ buttons (Home Assistant's numeric-input
 * feature) and the live default_status next to its value, under an info box
 * titled "5.0 – 35.0 °C  ·  in steps of 0.5 °C" with its note — all rendered
 * as such. Selects get a drop-down and switches a toggle the same way.
 */

const POINT = 32341;
const NAME = 'Desired room temperature, zone 1';
const INFO_TITLE = '5.0 – 35.0 °C  ·  in steps of 0.5 °C';

test('a stepped setting shows its step and -/+ buttons on the Nibe Menus dashboard', async ({
  page,
}) => {
  // A mode change into menus (a few hundred entities) and its dashboard build.
  test.setTimeout(600_000);
  const recreatedAt = haNow();
  try {
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-menus.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });
    const token = readToken();
    let entityId: string | undefined;
    await expect
      .poll(
        async () =>
          savedMenuDashboardMtime() >= recreatedAt &&
          Boolean((entityId = await pointEntityId(token, POINT))) &&
          savedMenuDashboardText().includes(`"entity": "${entityId}"`),
        {
          timeout: 300_000,
          message: `precondition: this run never saved a Nibe Menus dashboard with point ${POINT}`,
        }
      )
      .toBe(true);
    const saved = savedMenuDashboardText();
    expect(saved.includes(`title=\\"${INFO_TITLE}\\"`), `no info box titled "${INFO_TITLE}"`).toBe(
      true
    );

    const config = JSON.parse(saved);
    const tiles: any[] = [];
    const walk = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(walk);
      else if (node && typeof node === 'object') {
        const obj = node as Record<string, any>;
        if (obj.type === 'tile' && obj.entity === entityId) tiles.push(obj);
        Object.values(obj).forEach(walk);
      }
    };
    walk(config);
    // Menus 1.1 and 1.3.4 both list it.
    expect(tiles.length, `${entityId} is not a tile on the menu dashboard`).toBeGreaterThan(0);
    for (const tile of tiles) {
      expect(tile.features).toEqual([{ type: 'numeric-input', style: 'buttons' }]);
      expect(tile.state_content).toEqual(['state', 'default_status']);
    }
    expect(tiles.some((t) => t.name === NAME), `no ${entityId} tile named "${NAME}"`).toBe(true);
    const features = new Set<string>();
    const collect = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(collect);
      else if (node && typeof node === 'object') {
        const obj = node as Record<string, any>;
        if (obj.type === 'tile') for (const f of obj.features ?? []) features.add(f.type);
        Object.values(obj).forEach(collect);
      }
    };
    collect(config);
    for (const kind of ['select-options', 'toggle']) {
      expect(features.has(kind), `no tile with the ${kind} feature on the menu dashboard`).toBe(true);
    }

    // No dead references (what Spook reports): every tile that shows
    // default_status belongs to an entity that has that attribute — once
    // it is available: an unavailable entity (an unconfigured zone's
    // setpoint, 0 against its 5–35 °C range) has no attributes at all.
    const asking = new Set<string>();
    const collectAsking = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(collectAsking);
      else if (node && typeof node === 'object') {
        const obj = node as Record<string, any>;
        if (obj.type === 'tile' && (obj.state_content ?? []).includes('default_status')) asking.add(obj.entity);
        Object.values(obj).forEach(collectAsking);
      }
    };
    collectAsking(config);
    expect(asking.size, 'no tile shows default_status at all').toBeGreaterThan(0);
    let missing: string[] = [];
    await expect
      .poll(
        async () => {
          const ctx = await pwRequest.newContext();
          const resp = await ctx.get(`${HA_URL}/api/states`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          const states: any[] = resp.ok() ? await resp.json() : [];
          await ctx.dispose();
          const lacking = new Set(
            states
              .filter((st) => st.state !== 'unavailable' && st.attributes?.default_status === undefined)
              .map((st) => st.entity_id)
          );
          missing = [...asking].filter((id) => lacking.has(id));
          return missing.length;
        },
        { timeout: 120_000, message: 'a tile shows default_status its (available) entity lacks' }
      )
      .toBe(0);
    expect(missing, 'tiles showing default_status their entity lacks').toEqual([]);

    // And Home Assistant renders it with its -/+ buttons.
    await loginToHa(page);
    await gotoLoggedIn(page, '/nibe-menus/menu-1-1');
    await expect(page.locator('hui-tile-card ha-control-number-buttons').first()).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('ha-alert', { hasText: 'in steps of 0.5 °C' }).first()).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('hui-tile-card', { hasText: NAME }).first()).toBeVisible({
      timeout: 30_000,
    });
    // Next to the value, the live default_status: the mock serves 21.0 °C
    // against a 20 °C default.
    await expect(
      page.locator('hui-tile-card', { hasText: '✏️ default: 20 °C' }).first()
    ).toBeVisible({ timeout: 30_000 });
  } finally {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
