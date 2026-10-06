import { test, expect, request as pwRequest } from '@playwright/test';
import { loginToHa, readToken, gotoLoggedIn } from './support/ha-login';
import { HA_URL, readRetained } from './support/stack';

/**
 * An open snapshots modal follows a mode change.
 *
 * The modal warns that restoring is disabled in `menus` and `all` mode, and
 * disables its Restore buttons there. Both were only drawn when the modal
 * opened, so a mode switch made while it was open left them stale — Restore
 * still offered in menus mode, or still disabled after leaving it.
 *
 * A real mode switch means restarting the bridge, and in this harness
 * restarting any container reloads the page (Colima re-syncs its port
 * forwards, the frontend's connection drops and it reloads), which closes
 * the modal before the new mode can arrive. So the mode change is delivered
 * the way the card receives it in production — the retained
 * `nibe/browser/applied_mode` message the bridge publishes at the end of a
 * mode switch — published through Home Assistant's own MQTT service, and
 * the original value is put back afterwards.
 */

const MODE_TOPIC = 'nibe/browser/applied_mode';

async function publishRetained(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload, retain: true },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('an open snapshots modal reflects a mode change', async ({ page }) => {
  const token = readToken();
  const originalMode = readRetained(MODE_TOPIC);
  expect(originalMode, 'precondition: no retained applied_mode').toBeTruthy();
  expect(originalMode).not.toBe('menus');

  await loginToHa(page);
  await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect
    .poll(() => card.evaluate((el: any) => el.appliedMode), { timeout: 30_000 })
    .toBe(originalMode);

  // A snapshot to show Restore buttons for.
  await card.evaluate((el: any) => el.showSnapshots());
  const list = card.locator('#snapshots-list');
  let created = false;
  if ((await card.evaluate((el: any) => el.snapshots.length)) === 0) {
    created = true;
    await card.locator('#snapshot-name-input').fill('e2e mode-change');
    await card.evaluate((el: any) => el._handleSnapshotSave());
  }
  await expect(list.locator('.snapshot-restore-btn').first()).toBeEnabled({ timeout: 30_000 });

  try {
    await publishRetained(token, MODE_TOPIC, 'menus');
    await expect
      .poll(() => card.evaluate((el: any) => el.appliedMode), { timeout: 30_000 })
      .toBe('menus');
    await expect(
      list,
      'the mode switched to menus while the snapshots modal was open, but it still offers Restore'
    ).toContainText('Restore is disabled', { timeout: 10_000 });
    await expect(list.locator('.snapshot-restore-btn').first()).toBeDisabled();

    // And back: leaving menus mode must re-enable Restore in the open modal.
    await publishRetained(token, MODE_TOPIC, originalMode!);
    await expect(list.locator('.snapshot-restore-btn').first()).toBeEnabled({ timeout: 10_000 });
    await expect(list).not.toContainText('Restore is disabled');
  } finally {
    await publishRetained(token, MODE_TOPIC, originalMode!);
    await card.evaluate((el: any) => el.hideModal('snapshots-modal')).catch(() => {});
    if (created) {
      await card
        .evaluate((el: any) => el._sendSnapshotCmd({ action: 'delete', name: 'e2e mode-change' }))
        .catch(() => {});
    }
  }
});
