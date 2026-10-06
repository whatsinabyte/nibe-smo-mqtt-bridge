import { test, expect } from '@playwright/test';
import { loginToHa, gotoLoggedIn } from './support/ha-login';

/**
 * A search in the Entity Manager card still honours the type filter — and so
 * does "Select All".
 *
 * The card's filter returned straight from a search match (by ID or Modbus
 * prefix, by unit, or by Fuse's fuzzy title match), skipping the
 * type/status/writable/dynamic filters, which only applied on the plain
 * substring path. With a search active the dropdowns were silently ignored,
 * and "Select All" (which selects the whole filtered list, not just the
 * visible page) picked up entities the filters had hidden — a bulk Disable
 * then deleted entities of types the user had filtered out.
 *
 * Driven through the real card's own controls against the real point set:
 * an ID prefix is chosen at runtime so that it matches both switches and
 * other types.
 */

test('search plus type filter shows and selects only that type', async ({ page }) => {
  await loginToHa(page);
  await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect
    .poll(() => card.evaluate((el: any) => el.entities.size), { timeout: 60_000 })
    .toBeGreaterThan(100);

  // A two-digit ID prefix shared by switches and by at least one other type.
  const prefix: string | null = await card.evaluate((el: any) => {
    const byPrefix = new Map<string, Set<string>>();
    for (const e of el.entities.values()) {
      const p = String(e.id).slice(0, 2);
      if (!byPrefix.has(p)) byPrefix.set(p, new Set());
      byPrefix.get(p)!.add(e.type);
    }
    for (const [p, types] of byPrefix) {
      if (types.has('switch') && types.size > 1) return p;
    }
    return null;
  });
  expect(prefix, 'no ID prefix is shared by switches and another type').not.toBeNull();

  await card.locator('#type-filter').selectOption('switch');
  await card.locator('#search-input').fill(prefix!);

  // Every rendered row must be a switch.
  await expect
    .poll(
      async () =>
        card.evaluate((el: any) =>
          el.filteredEntities.filter((e: any) => e.type !== 'switch').map((e: any) => e.id)
        ),
      {
        timeout: 15_000,
        message:
          `searching "${prefix}" with the type filter on Switch still lists entities of other ` +
          'types — a search match skips the dropdown filters',
      }
    )
    .toEqual([]);
  // And what the table actually renders agrees.
  const rows = card.locator('tbody tr[data-id]');
  await expect(rows.first()).toBeVisible();
  await expect(rows.filter({ hasNot: page.locator('.badge-switch') })).toHaveCount(0);

  // Select All selects the whole filtered list — it must hold only switches.
  await card.locator('#select-all').click();
  const selectedTypes: string[] = await card.evaluate((el: any) =>
    [...el.selectedIds].map((id: number) => el.entities.get(id)?.type)
  );
  expect(selectedTypes.length).toBeGreaterThan(0);
  expect(
    [...new Set(selectedTypes)],
    'Select All picked up entities the type filter had hidden'
  ).toEqual(['switch']);

  // Leave nothing selected for later specs sharing this dashboard.
  await card.evaluate((el: any) => el.clearSelection());
});
