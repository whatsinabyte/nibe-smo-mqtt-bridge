import { test, expect, request as pwRequest } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Real end-to-end happy path: log into the real Home Assistant frontend,
 * open the Nibe Bridge dashboard (seeded via YAML-mode Lovelace — see
 * ../ha-seed/), find one currently-disabled entity in the real
 * nibe-entity-manager-card, click its Enable action, and confirm — via the
 * real HA REST API, round-tripping through the real bridge and the real
 * Mosquitto broker — that a new HA entity actually appears and is not
 * "unavailable".
 *
 * This is the one thing app/tests-js's stubbed-hass suite and the pytest
 * suite's MQTT-broker integration tests cannot prove between them: that the
 * real hass.connection/hass.callService implementation in a real HA
 * frontend agrees with the card's assumptions about it.
 */

const SEED_OUT = path.join(__dirname, '..', 'seed-out');
const REFERENCE_DUMP = path.join(__dirname, '..', '..', '..', 'reference-dumps', 'all_points_en.json');
const HA_URL = process.env.HA_URL || 'http://localhost:18123';

function readCredentials(): { username: string; password: string } {
  const raw = fs.readFileSync(path.join(SEED_OUT, 'credentials.json'), 'utf-8');
  return JSON.parse(raw);
}

function readToken(): string {
  return fs.readFileSync(path.join(SEED_OUT, 'token.txt'), 'utf-8').trim();
}

/**
 * Points to try enabling, ordered ascending by ID, filtered to exclude ones
 * that carry the bridge's "sensor not connected" sentinel raw value in this
 * specific real firmware dump (reference-dumps/all_points_en.json, the same
 * file the mock API replays verbatim — see nibe_entity_manager.py's
 * sentinel_values handling for s16/u16/s32/u32).
 *
 * The dashboard's default table sort is ID-ascending, and this dump's
 * lowest-ID disabled registers happen to cluster around collector/EP2x
 * sensors that are legitimately "not connected" on the physical unit this
 * dump was taken from (no ground-source/collector accessory attached) — so
 * blindly taking "the first N disabled rows" reproducibly picks the same
 * always-unavailable cluster every run, not just occasionally. Filtering by
 * the dump's own recorded value up front avoids that regardless of how the
 * bridge/card end up sorting or which points happen to already be enabled
 * by the configured mode.
 */
function knownGoodPointIds(): number[] {
  const dump = JSON.parse(fs.readFileSync(REFERENCE_DUMP, 'utf-8'));
  const sentinels: Record<string, number> = {
    s16: -32768,
    u16: 65535,
    s32: -2147483648,
    u32: 4294967295,
  };
  const ids: number[] = [];
  for (const [idStr, point] of Object.entries<any>(dump)) {
    const size = point?.metadata?.variableSize;
    const rawValue = point?.value?.integerValue;
    const isOk = point?.value?.isOk;
    if (isOk === false) continue;
    if (size in sentinels && rawValue === sentinels[size]) continue;
    ids.push(Number(idStr));
  }
  ids.sort((a, b) => a - b);
  return ids;
}

async function fetchStateIds(token: string): Promise<Set<string>> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const states = await resp.json();
  await ctx.dispose();
  return new Set(states.map((s: { entity_id: string }) => s.entity_id));
}

async function fetchState(
  token: string,
  entityId: string
): Promise<{ entity_id: string; state: string } | null> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok()) {
    await ctx.dispose();
    return null;
  }
  const body = await resp.json();
  await ctx.dispose();
  return body;
}

test('enabling a disabled entity via the card creates a real HA entity', async ({ page }) => {
  const { username, password } = readCredentials();
  const token = readToken();

  // 1. Log into the real HA frontend UI.
  await page.goto('/');
  await page.getByLabel('Username').fill(username);
  await page.getByRole('textbox', { name: 'Password' }).fill(password);
  await page.getByRole('button', { name: /log in/i }).click();
  await expect(page).toHaveURL(/\/lovelace|\/$|\/home/, { timeout: 30_000 });

  // 2. Navigate to the seeded Nibe Bridge dashboard / Entity Manager view.
  await page.goto('/nibe-bridge/entity-manager');

  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });

  // The card is a Shadow DOM web component — pierce it with Playwright's
  // built-in shadow-piercing locators (no manual shadowRoot walking needed).
  // Enable a handful of currently-disabled points, not just the first one —
  // and specifically the first ones known (from the dump itself) not to
  // carry the "sensor not connected" sentinel, rather than whatever happens
  // to sort first in the table (see knownGoodPointIds() above for why that
  // distinction matters for this specific dump).
  const CANDIDATE_COUNT = 5;
  const enabledPointIds: string[] = [];
  const searchInput = card.locator('#search-input');

  // Snapshot HA's entity set before enabling anything.
  const before = await fetchStateIds(token);

  for (const pointId of knownGoodPointIds()) {
    if (enabledPointIds.length >= CANDIDATE_COUNT) break;

    // Filter the table to this exact point so its row is guaranteed to be
    // rendered on the current page regardless of default sort/pagination —
    // the row itself is still located by an exact data-id match afterward,
    // so a substring collision in the search (e.g. "4" matching "40004")
    // can't select the wrong row.
    await searchInput.fill(String(pointId));
    const row = card.locator(`tr[data-id="${pointId}"]`);
    await expect(row).toBeVisible({ timeout: 10_000 });

    const enableButton = row.locator('button[data-action="enable"]');
    if ((await enableButton.count()) === 0) {
      // Already enabled (e.g. by the configured mode at startup) — its
      // action button says "Disable" instead. Skip to the next candidate.
      continue;
    }

    // Click Enable — this calls hass.callService('mqtt', 'publish', ...) in
    // the real frontend, which round-trips over the real broker to the real
    // bridge's ManagementCommandHandler, which enables the point and
    // publishes a real MQTT discovery config for it.
    await enableButton.click();

    // The card itself flips the row to "Enabled" once the bridge publishes
    // nibe/browser/enabled_state back — confirms the browser round trip.
    await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });

    enabledPointIds.push(String(pointId));
  }

  await searchInput.fill('');
  expect(enabledPointIds.length).toBe(CANDIDATE_COUNT);

  // The real proof: poll HA's REST API (real HA, real entity registry, real
  // MQTT discovery listener) until new entity_ids appear for the enabled
  // points, and at least one of them is not "unavailable".
  let newEntityIds: string[] = [];
  await expect
    .poll(
      async () => {
        const after = await fetchStateIds(token);
        newEntityIds = [...after].filter((id) => !before.has(id));
        return newEntityIds.length >= enabledPointIds.length;
      },
      { timeout: 60_000, message: 'not all newly-enabled points produced an HA entity' }
    )
    .toBeTruthy();

  let availableEntityId: string | null = null;
  await expect
    .poll(
      async () => {
        for (const id of newEntityIds) {
          const state = await fetchState(token, id);
          if (state && state.state !== 'unavailable') {
            availableEntityId = id;
            return true;
          }
        }
        return false;
      },
      {
        timeout: 60_000,
        message: `none of the newly-created entities [${newEntityIds.join(', ')}] left 'unavailable'`,
      }
    )
    .toBeTruthy();

  expect(availableEntityId).not.toBeNull();
});
