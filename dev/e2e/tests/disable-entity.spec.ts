import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

/**
 * The removal half of the entity lifecycle. enable-entity.spec.ts covers
 * creating an entity through the real card; nothing covered taking one away
 * again, which is a different path end to end — the bridge clears the
 * retained discovery config and publishes offline availability, and Home
 * Assistant has to act on both.
 *
 * Asserting "the entity is gone" is deliberately written as "gone *or*
 * unavailable". Home Assistant does not always drop an MQTT entity from
 * /api/states the instant its discovery config is cleared; what the user
 * actually cares about, and what the bridge is actually responsible for, is
 * that it stops being usable. Requiring full removal would make this test
 * assert an HA implementation detail rather than the bridge's behaviour.
 */

const SEED_OUT = path.join(__dirname, '..', 'seed-out');
const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const BRIDGE_CONTAINER = 'nibe-e2e-bridge';

// An ordinary, always-present writable switch from the real reference dump.
const POINT_ID = '3870'; // "Climate system 2"

function readCredentials(): { username: string; password: string } {
  return JSON.parse(fs.readFileSync(path.join(SEED_OUT, 'credentials.json'), 'utf-8'));
}

function readToken(): string {
  return fs.readFileSync(path.join(SEED_OUT, 'token.txt'), 'utf-8').trim();
}

interface HaState {
  entity_id: string;
  state: string;
}

async function fetchStates(token: string): Promise<HaState[]> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const states = await resp.json();
  await ctx.dispose();
  return states;
}

/**
 * The point ids recorded in the bridge's own /data fallback file.
 *
 * Everything the user explicitly enables is persisted twice: to a retained
 * MQTT topic and to `/data/wanted_points.json`. The retained copy is what
 * normally restores state after a restart, so the file is only ever read
 * when the broker has lost its retained messages — and it is read back with
 * its parse failure swallowed as "no data", so a bad file is silently
 * indistinguishable from no file. Nothing else in this suite looks at it,
 * which means a regression that stopped writing it entirely would go
 * unnoticed until the one situation it exists for.
 *
 * Reading it directly is deliberate: proving the file is correct needs no
 * broker wipe and no restart, both of which would be far heavier than the
 * property being checked.
 */
function persistedWantedPoints(): number[] {
  const raw = execSync(`docker exec ${BRIDGE_CONTAINER} cat /data/wanted_points.json`, {
    encoding: 'utf-8',
  });
  return JSON.parse(raw);
}

test('disabling an entity via the card removes it from Home Assistant', async ({ page }) => {
  test.setTimeout(120_000);

  const { username, password } = readCredentials();
  const token = readToken();

  await page.goto('/');
  await page.getByLabel('Username').fill(username);
  await page.getByRole('textbox', { name: 'Password' }).fill(password);
  await page.getByRole('button', { name: /log in/i }).click();
  await expect(page).toHaveURL(/\/lovelace|\/$|\/home/, { timeout: 30_000 });

  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  const searchInput = card.locator('#search-input');

  // 1. Enable the point first, so this test owns the entity it later
  // removes rather than depending on whatever the configured mode happens
  // to have enabled.
  const before = await fetchStates(token);
  const beforeIds = new Set(before.map((s) => s.entity_id));

  await searchInput.fill(POINT_ID);
  const row = card.locator(`tr[data-id="${POINT_ID}"]`);
  await expect(row).toBeVisible({ timeout: 10_000 });

  const enableButton = row.locator('button[data-action="enable"]');
  if ((await enableButton.count()) > 0) {
    await enableButton.click();
  }
  await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });

  let entityId: string | null = null;
  await expect
    .poll(
      async () => {
        const after = await fetchStates(token);
        const candidate = after.find(
          (s) => !beforeIds.has(s.entity_id) && s.entity_id.startsWith('switch.')
        );
        if (candidate && candidate.state !== 'unavailable') {
          entityId = candidate.entity_id;
          return true;
        }
        return false;
      },
      { timeout: 30_000, message: `no new available entity appeared for point ${POINT_ID}` }
    )
    .toBeTruthy();
  expect(entityId).not.toBeNull();
  const target = entityId as unknown as string;

  // 1b. The enable must also reach the /data fallback file — see
  // persistedWantedPoints() for why that file matters and why it is checked
  // here rather than by restarting anything.
  await expect
    .poll(() => persistedWantedPoints().includes(Number(POINT_ID)), {
      timeout: 30_000,
      intervals: [1_000],
      message: `point ${POINT_ID} never reached /data/wanted_points.json after being enabled`,
    })
    .toBeTruthy();

  // 2. Now disable it through the same card, and confirm the card itself
  // reflects the new state — the round trip back from the bridge, not just
  // an optimistic local update.
  const disableButton = row.locator('button[data-action="disable"]');
  await expect(disableButton).toBeVisible({ timeout: 10_000 });
  await disableButton.click();
  await expect(row.locator('.badge-disabled')).toBeVisible({ timeout: 30_000 });

  // 3. And confirm Home Assistant stops offering it. See the file header
  // for why "unavailable" counts as success alongside outright removal.
  await expect
    .poll(
      async () => {
        const states = await fetchStates(token);
        const found = states.find((s) => s.entity_id === target);
        return !found || found.state === 'unavailable';
      },
      {
        timeout: 60_000,
        intervals: [2_000],
        message: `${target} was still live in HA after being disabled via the card`,
      }
    )
    .toBeTruthy();

  // 4. An explicit user-initiated disable also un-marks the point as
  // wanted, so it must leave the fallback file too. If it didn't, the
  // reconciliation that runs after every bulk fetch would treat the point
  // as still wanted and re-enable it behind the user's back.
  await expect
    .poll(() => persistedWantedPoints().includes(Number(POINT_ID)), {
      timeout: 30_000,
      intervals: [1_000],
      message: `point ${POINT_ID} was still in /data/wanted_points.json after being disabled`,
    })
    .toBeFalsy();
});
