import { test, expect } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  haNow,
  haWs,
  recreateBridge,
  savedMenuDashboardEntities,
  savedMenuDashboardMtime,
} from './support/stack';

/**
 * Renaming an entity in Home Assistant updates the Nibe Menus dashboard.
 *
 * HA's entity-registry update event never carries unique_id — only the new
 * entity_id and old_entity_id — and a rename doesn't change which points are
 * enabled, which is what regenerates the menu dashboard. So the saved
 * dashboard kept the old entity_id, a row reading "entity not available",
 * until the bridge restarted or some unrelated enable/disable rebuilt it.
 *
 * Renames one of the dashboard's entities through HA's own registry API, the
 * same call the entity settings dialog makes, and requires the saved
 * dashboard to follow. Renames it back afterwards.
 */

test('renaming an entity in HA updates the menu dashboard', async () => {
  test.setTimeout(900_000);
  const token = readToken();
  let renamed: { from: string; to: string } | null = null;

  try {
    const recreatedAt = haNow();
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-menus.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });
    let onDashboard: Set<string> = new Set();
    await expect
      .poll(
        () => {
          if (savedMenuDashboardMtime() < recreatedAt) return 0;
          onDashboard = savedMenuDashboardEntities() ?? new Set();
          return onDashboard.size;
        },
        { timeout: 300_000, message: 'this run never saved a Nibe Menus dashboard with entities' }
      )
      .toBeGreaterThan(20);

    const [registry] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
    const target = (registry.result ?? []).find(
      (e: any) => (e.unique_id ?? '').startsWith('nibe_') && onDashboard.has(e.entity_id)
    );
    expect(target, 'no bridge entity on the menu dashboard found in the registry').toBeTruthy();
    const from: string = target.entity_id;
    const to = `${from}_renamed_e2e`;

    const [resp] = await haWs(token, [
      { type: 'config/entity_registry/update', entity_id: from, new_entity_id: to },
    ]);
    expect(resp.success, JSON.stringify(resp)).toBe(true);
    renamed = { from, to };

    await expect
      .poll(
        () => {
          const ids = savedMenuDashboardEntities();
          return Boolean(ids?.has(to) && !ids?.has(from));
        },
        {
          timeout: 90_000,
          message:
            `${from} was renamed to ${to} in Home Assistant, but the saved menu dashboard still ` +
            'references the old entity_id — an "entity not available" row until a restart',
        }
      )
      .toBe(true);
  } finally {
    if (renamed) {
      await haWs(token, [
        { type: 'config/entity_registry/update', entity_id: renamed.to, new_entity_id: renamed.from },
      ]);
    }
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
