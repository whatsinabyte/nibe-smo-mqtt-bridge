import { test, expect } from '@playwright/test';
import { execSync } from 'child_process';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  HA_CONTAINER,
  haNow,
  haWs,
  recreateBridge,
  savedMenuDashboardMtime,
  stopBridge,
} from './support/stack';

/**
 * `remove_frontend: true` removes the Nibe Menus dashboard at shutdown.
 *
 * DOCS.md's uninstall instructions list the Nibe Menus dashboard among what
 * this option cleans up, but the shutdown teardown only removed the Nibe
 * Bridge dashboard — so uninstalling in menus mode left a dashboard of rows
 * for entities whose discovery configs the very same shutdown had cleared.
 *
 * The bridge runs in menus mode for this — in any other mode it removes the
 * menu dashboard at startup anyway, and the check would pass vacuously —
 * with the fake Supervisor's token, since Lovelace provisioning and teardown
 * only run behind a Supervisor. It is stopped the way the Supervisor stops
 * it, and Home Assistant's own dashboard list is checked afterwards.
 *
 * The teardown also clears every retained MQTT topic the bridge owns, so the
 * bridge is put back in its default (essential) mode afterwards, which
 * rebuilds its entities from scratch for the specs that follow.
 */

const MENUS_SLUG = 'nibe-menus';

async function menuDashboardListed(token: string): Promise<boolean> {
  const [list] = await haWs(token, [{ type: 'lovelace/dashboards/list' }]);
  expect(list.success, JSON.stringify(list)).toBe(true);
  return (list.result ?? []).some((d: any) => d.url_path === MENUS_SLUG);
}

test('remove_frontend removes the Nibe Menus dashboard at shutdown', async () => {
  // A mode change into menus (a few hundred entities), its dashboard build,
  // a full shutdown, and putting the bridge back afterwards.
  test.setTimeout(900_000);
  const token = readToken();

  try {
    const recreatedAt = haNow();
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-menus-remove-frontend.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });

    // ── 1. This run's menu dashboard is built and saved ────────────────────
    // Waiting for the save (not just the dashboard's existence) also keeps
    // an in-flight build from racing the teardown below.
    await expect
      .poll(() => savedMenuDashboardMtime() >= recreatedAt, {
        timeout: 300_000,
        message: 'this run never saved a Nibe Menus dashboard',
      })
      .toBe(true);
    expect(await menuDashboardListed(token)).toBe(true);

    // The debug "Run Test Suite" report sits next to the card in
    // /config/www; placed here directly rather than by running the suite.
    execSync(
      `docker exec ${HA_CONTAINER} sh -c 'mkdir -p /config/www && echo report > /config/www/nibe_test_report.html'`
    );

    // ── 2. A clean stop must take the menu dashboard with it ───────────────
    stopBridge();
    expect(
      await menuDashboardListed(token),
      'the bridge shut down with remove_frontend: true but left the Nibe Menus dashboard ' +
        'in place — its entities were just cleared, so it shows nothing but unavailable rows'
    ).toBe(false);
    const reportLeft = execSync(
      `docker exec ${HA_CONTAINER} sh -c 'test -e /config/www/nibe_test_report.html && echo yes || echo no'`,
      { encoding: 'utf-8' }
    ).trim();
    expect(
      reportLeft,
      'remove_frontend left the debug test report in /config/www (served at /local/)'
    ).toBe('no');
  } finally {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
