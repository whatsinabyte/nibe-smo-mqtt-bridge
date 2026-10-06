import { test, expect, request as pwRequest } from '@playwright/test';
import { loginToHa, readToken, gotoLoggedIn } from './support/ha-login';
import { HA_URL, readRetained } from './support/stack';

/**
 * A snapshot save the bridge refuses is reported in the card.
 *
 * The bridge used to only log the outcome of a snapshot command, so a refused
 * save — most commonly the limit of 10 snapshots — looked to the user exactly
 * like a success: the card showed "Saving…" for a few seconds and cleared it.
 * The bridge now publishes each outcome on nibe/browser/snapshots/result and
 * the card shows the bridge's own message.
 *
 * Fills the snapshot list to the limit through the bridge's own command
 * topic, then saves one more from the real card.
 */

const CMD_TOPIC = 'nibe/browser/snapshots/cmd';
const LIMIT = 10;

function snapshotNames(): string[] {
  const raw = readRetained('nibe/browser/snapshots');
  return raw ? JSON.parse(raw).map((s: any) => s.name) : [];
}

async function snapshotCmd(token: string, cmd: object): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic: CMD_TOPIC, payload: JSON.stringify(cmd) },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('a refused snapshot save is reported in the card', async ({ page }) => {
  test.setTimeout(240_000);
  const token = readToken();
  const created: string[] = [];

  try {
    for (let i = 0; snapshotNames().length < LIMIT && i < LIMIT; i++) {
      const name = `e2e-fill-${i}`;
      const before = snapshotNames().length;
      await snapshotCmd(token, { action: 'save', name });
      created.push(name);
      await expect.poll(() => snapshotNames().length, { timeout: 15_000 }).toBeGreaterThan(before);
    }
    expect(snapshotNames().length, 'precondition: could not fill the snapshot list').toBe(LIMIT);

    await loginToHa(page);
    await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
    const card = page.locator('nibe-entity-manager-card');
    await expect(card).toBeVisible({ timeout: 30_000 });
    await card.evaluate((el: any) => el.showSnapshots());
    await card.locator('#snapshot-name-input').fill('e2e-overflow');
    await card.evaluate((el: any) => el._handleSnapshotSave());

    const msg = card.locator('#snapshot-save-msg');
    await expect(
      msg,
      'the bridge refused the save (snapshot limit) but the card never said so'
    ).toContainText('Maximum', { timeout: 8_000 }); // before the card's own 10s fallback
  } finally {
    for (const name of created) await snapshotCmd(token, { action: 'delete', name });
  }
});
