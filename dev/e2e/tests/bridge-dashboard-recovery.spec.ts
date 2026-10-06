import { test, expect } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  haWs,
  recreateBridge,
  recreateHa,
} from './support/stack';

/**
 * A Nibe Bridge dashboard that exists but has no saved config gets its card.
 *
 * The bridge creates this dashboard in two WebSocket calls: create it, then
 * save its card config. If the save fails (a dropped socket, or _ws_call's
 * deadline while Home Assistant is busy starting up), the dashboard is left
 * empty, and every later startup used to return early on "already exists" —
 * so it never got the Entity Manager card, the only place to enable entities
 * in "none" mode.
 *
 * This spec puts real Home Assistant into exactly that state — a storage
 * dashboard at /nibe-bridge that HA reports as config_not_found — and
 * requires one bridge start to write the card into it.
 *
 * Two pieces of the harness are swapped for its duration only:
 *   - Home Assistant runs with configuration-storage-bridge-dashboard.yaml,
 *     identical to the usual configuration.yaml except that it doesn't
 *     define nibe-bridge in YAML (which no add-on install does: the bridge
 *     provisions it). A YAML dashboard can never lack a config.
 *   - The bridge gets the fake Supervisor's token, since Lovelace
 *     provisioning only runs behind a Supervisor.
 */

const BRIDGE_SLUG = 'nibe-bridge';
const CARD_TYPE = 'custom:nibe-entity-manager-card';

async function deleteStorageBridgeDashboard(token: string): Promise<void> {
  const [list] = await haWs(token, [{ type: 'lovelace/dashboards/list' }]);
  const existing = (list.result ?? []).find(
    (d: any) => d.url_path === BRIDGE_SLUG && d.mode === 'storage'
  );
  if (existing) {
    await haWs(token, [{ type: 'lovelace/dashboards/delete', dashboard_id: existing.id }]);
  }
}

test('an existing Nibe Bridge dashboard without a config gets its card', async () => {
  // Two Home Assistant recreates and two bridge recreates.
  test.setTimeout(900_000);
  const token = readToken();

  try {
    await recreateHa(token, {
      HA_CONFIGURATION: './ha-seed/configuration-storage-bridge-dashboard.yaml',
    });

    // ── 1. The state a create-then-failed-save leaves behind ───────────────
    await deleteStorageBridgeDashboard(token);
    const [created, config] = await haWs(token, [
      {
        type: 'lovelace/dashboards/create',
        url_path: BRIDGE_SLUG,
        mode: 'storage',
        title: 'Nibe Bridge',
        show_in_sidebar: true,
        require_admin: false,
      },
      { type: 'lovelace/config', url_path: BRIDGE_SLUG },
    ]);
    expect(created.success, JSON.stringify(created)).toBe(true);
    expect(config.error?.code, 'precondition: the new dashboard should have no config').toBe(
      'config_not_found'
    );

    // ── 2. One bridge start must write the card into it ────────────────────
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });
    // Guard against a vacuous pass: the YAML nibe-bridge dashboard of the
    // default configuration carries the card too, so if Home Assistant has
    // been put back on it (a bridge recreate without --no-deps did exactly
    // that) the check below would pass with the bridge never involved.
    const [listAfter] = await haWs(token, [{ type: 'lovelace/dashboards/list' }]);
    expect(
      (listAfter.result ?? []).find((d: any) => d.url_path === BRIDGE_SLUG)?.mode,
      'precondition: /nibe-bridge should still be the storage dashboard this spec created'
    ).toBe('storage');
    await expect
      .poll(
        async () => {
          const [resp] = await haWs(token, [{ type: 'lovelace/config', url_path: BRIDGE_SLUG }]);
          return JSON.stringify(resp.result ?? resp.error ?? null);
        },
        {
          timeout: 90_000,
          message:
            'the bridge started against a Nibe Bridge dashboard with no saved config and left it ' +
            'that way — a dashboard whose config save once failed never gets its card',
        }
      )
      .toContain(CARD_TYPE);
  } finally {
    // The storage dashboard would collide with the YAML one on the default
    // configuration, so it goes before Home Assistant is put back.
    await deleteStorageBridgeDashboard(token);
    await recreateHa(token, {});
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
