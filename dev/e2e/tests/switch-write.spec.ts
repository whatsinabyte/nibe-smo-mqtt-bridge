import { test, expect, request as pwRequest } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

/**
 * A real switch write, end to end: HA's switch.turn_on/turn_off service →
 * MQTT command topic → the bridge's write executor → a real PATCH to the
 * (mock) controller → the new value coming back through the bulk poll and
 * releasing the pending-write guard.
 *
 * translation.spec.ts already round-trips a *select* write, but that goes
 * through a different payload path: a select command carries a translated
 * option label that has to be mapped back to an integer, whereas a switch
 * command carries a plain ON/OFF that becomes 1/0. Only the select path was
 * covered.
 *
 * The guard is the interesting part. Writable entities are published with
 * "optimistic": false, so HA does not flip its own state on the service
 * call — it waits for the bridge to report the value back. Until the
 * controller confirms, the bridge suppresses state publishes for that point
 * (pending_writes) so a stale bulk value can't flip the entity back in the
 * UI. This test therefore asserts the *settled* state after the round trip,
 * which is the thing a user actually sees.
 */

const SEED_OUT = path.join(__dirname, '..', 'seed-out');
const HA_URL = process.env.HA_URL || 'http://localhost:18123';

// "Floor drying" — an ordinary writable 0/1 switch that reads 0 in the
// reference dump, so turning it on is a real change of value.
const POINT_ID = '3846';

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

async function fetchState(token: string, entityId: string): Promise<string | null> {
  const states = await fetchStates(token);
  return states.find((s) => s.entity_id === entityId)?.state ?? null;
}

async function callService(
  token: string,
  domain: string,
  service: string,
  data: Record<string, unknown>
): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/${domain}/${service}`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data,
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('a switch round-trips a real HA turn_on through the bridge to the controller', async ({
  page,
}) => {
  // A first-ever write to a switch opens the bridge's dynamic-point
  // learning window, which holds its single write worker for up to 90s —
  // the turn_off below queues behind it.
  test.setTimeout(240_000);

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

  const before = await fetchStates(token);
  const beforeIds = new Set(before.map((s) => s.entity_id));

  const searchInput = card.locator('#search-input');
  await searchInput.fill(POINT_ID);
  const row = card.locator(`tr[data-id="${POINT_ID}"]`);
  await expect(row).toBeVisible({ timeout: 10_000 });
  const enableButton = row.locator('button[data-action="enable"]');
  if ((await enableButton.count()) > 0) {
    await enableButton.click();
    await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });
  }
  await searchInput.fill('');

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
      { timeout: 30_000, message: `no new available switch entity appeared for point ${POINT_ID}` }
    )
    .toBeTruthy();
  expect(entityId).not.toBeNull();
  const target = entityId as unknown as string;

  // The dump has this point at 0, so it should surface as off.
  expect(await fetchState(token, target)).toBe('off');

  // 1. Turn it on through HA's own service call — not the card, and not the
  // mock's control channel. This is the full command path.
  await callService(token, 'switch', 'turn_on', { entity_id: target });

  // Because discovery sets "optimistic": false, HA only reports 'on' once
  // the bridge has written the value and read it back from the controller.
  await expect
    .poll(() => fetchState(token, target), {
      timeout: 120_000,
      intervals: [2_000],
      message: `${target} never settled to 'on' after switch.turn_on`,
    })
    .toBe('on');

  // 2. And back again, proving the reverse payload maps correctly too
  // rather than the entity simply being stuck on.
  await callService(token, 'switch', 'turn_off', { entity_id: target });
  await expect
    .poll(() => fetchState(token, target), {
      timeout: 120_000,
      intervals: [2_000],
      message: `${target} never settled back to 'off' after switch.turn_off`,
    })
    .toBe('off');
});
