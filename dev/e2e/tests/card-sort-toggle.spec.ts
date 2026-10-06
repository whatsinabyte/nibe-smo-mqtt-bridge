import { test, expect } from '@playwright/test';
import { loginToHa, gotoLoggedIn } from './support/ha-login';

/**
 * Each click on the Entity Manager card's controls is handled once.
 *
 * Lovelace detaches and re-inserts card elements while it lays out a view,
 * and the card re-attached its click listeners on every reconnect — to the
 * same, still-present controls. Every control ended up with two handlers: a
 * column header sorted descending on its first click and never flipped
 * back, the mobile filter toggle opened and closed in a single tap, and
 * "Enable selected" / "Disable selected" sent each command twice.
 */

test('a column header click flips the sort direction', async ({ page }) => {
  await loginToHa(page);
  await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect(card.locator('tbody tr[data-id]').first()).toBeVisible({ timeout: 60_000 });
  const sortAscending = () => card.evaluate((el: any) => el.sortAscending);

  const header = card.locator('th[data-sort="title"]');
  await header.click();
  expect(await sortAscending(), 'a first click on a column header should sort ascending').toBe(true);
  await header.click();
  expect(await sortAscending(), 'a second click on the same header should flip to descending').toBe(
    false
  );
});
