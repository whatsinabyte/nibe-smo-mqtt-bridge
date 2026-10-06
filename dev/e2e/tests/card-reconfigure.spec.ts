import { test, expect } from '@playwright/test';
import { loginToHa, gotoLoggedIn } from './support/ha-login';

/**
 * The Entity Manager card survives Lovelace re-applying its config.
 *
 * Lovelace calls setConfig() again on a live card element when it re-applies
 * the card's config, and the card's setConfig() rebuilt its whole shadow DOM.
 * The card's state survived, but the screen didn't: the table came back
 * empty until some later MQTT message happened to trigger a redraw, an open
 * changelog or snapshots modal vanished mid-read, and the search box and
 * filter dropdowns went blank while the old search and filters were still
 * being applied. This is the likeliest cause of the intermittent
 * sg-ready-dynamic-discovery.spec.ts failure, where the open changelog modal
 * read back as empty.
 *
 * What triggers the re-apply inside Home Assistant varies, so this calls
 * setConfig() on the real card element in the real frontend — the same call
 * Lovelace makes — rather than trying to provoke Home Assistant into it.
 */

test('re-applying the card config keeps the table, filters and an open modal', async ({ page }) => {
  await loginToHa(page);
  await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  const rows = card.locator('tbody tr[data-id]');
  await expect(rows.first()).toBeVisible({ timeout: 60_000 });

  await card.locator('#type-filter').selectOption('sensor');
  await expect(rows.first()).toBeVisible();
  const rowsBefore = await rows.count();
  await card.locator('#show-changelog').click();
  await expect(card.locator('#changelog-modal')).toBeVisible({ timeout: 10_000 });

  await card.evaluate((el: any) => el.setConfig({ ...el.config }));

  await expect(
    rows.first(),
    're-applying the config left the entity table empty'
  ).toBeVisible({ timeout: 5_000 });
  expect(await rows.count()).toBe(rowsBefore);
  await expect(
    card.locator('#type-filter'),
    'the type filter dropdown no longer shows the filter that is still applied'
  ).toHaveValue('sensor');
  await expect(
    card.locator('#changelog-modal'),
    're-applying the config closed the open changelog modal'
  ).toBeVisible();
  await expect(card.locator('#changelog-content')).not.toHaveText('');
});

test('re-applying the card config keeps an open details modal on its point', async ({ page }) => {
  await loginToHa(page);
  await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  const firstRow = card.locator('tbody tr[data-id]').first();
  await expect(firstRow).toBeVisible({ timeout: 60_000 });
  const pointId = await firstRow.getAttribute('data-id');

  await firstRow.locator('button[data-action="details"]').click();
  const details = card.locator('#details-modal');
  await expect(details).toBeVisible({ timeout: 10_000 });

  await card.evaluate((el: any) => el.setConfig({ ...el.config }));

  await expect(
    details,
    're-applying the config closed the open details modal'
  ).toBeVisible({ timeout: 5_000 });
  await expect(details.locator('.modal-body')).toContainText(pointId!);
});

test('re-applying the card config keeps an open mobile filter panel working', async ({ page }) => {
  await loginToHa(page);
  await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  // The mobile controls are hidden at desktop width; drive them by script.
  const panelDisplay = () =>
    card.evaluate((el: any) => el.shadowRoot.getElementById('mobile-filter-panel').style.display);
  const tapToggle = () =>
    card.evaluate((el: any) => el.shadowRoot.getElementById('mobile-filter-toggle').click());

  await tapToggle();
  expect(await panelDisplay()).toBe('block');
  await card.evaluate((el: any) => el.setConfig({ ...el.config }));
  expect(await panelDisplay(), 're-applying the config closed the open mobile filter panel').toBe(
    'block'
  );
  await tapToggle();
  expect(await panelDisplay(), 'one tap on the toggle no longer closes the panel').toBe('none');
});
