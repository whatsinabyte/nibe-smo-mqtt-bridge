import { test, expect } from '@playwright/test';

// A handful of smoke tests for things jsdom genuinely can't verify: real
// CSS media-query-driven responsive layout, real pointer/click behaviour,
// and modal visibility as actually laid out by the browser. The bulk of
// the card's logic is covered by the Vitest suite in tests-js/*.test.js —
// keep this file small.

async function publishAllMetadata(page, entries) {
  const metadata = {};
  entries.forEach((e) => {
    metadata[String(e.id)] = e;
  });
  await page.evaluate(
    ({ metadata, count }) => {
      window.__publish('nibe/browser/all_metadata', JSON.stringify({ metadata, count, last_updated: Date.now() / 1000 }));
    },
    { metadata, count: entries.length }
  );
}

function sampleEntry(overrides = {}) {
  return {
    id: 1,
    title: 'Heating setpoint',
    type: 'number',
    writable: true,
    unit: '°C',
    is_dynamic: false,
    ...overrides,
  };
}

test.beforeEach(async ({ page }) => {
  await page.goto('fixture.html');
  await page.waitForFunction(() => window.__cardReady === true);
});

test('desktop viewport (>600px) shows the table and hides mobile cards', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 800 });
  await publishAllMetadata(page, [sampleEntry()]);

  const card = page.locator('#card');
  const table = card.locator('.table-container');
  const mobileCards = card.locator('.mobile-cards');

  await expect(table).toBeVisible();
  await expect(mobileCards).toBeHidden();
});

test('mobile viewport (<=600px) shows mobile cards and hides the table, per the 600px breakpoint', async ({
  page,
}) => {
  await page.setViewportSize({ width: 400, height: 800 });
  await publishAllMetadata(page, [sampleEntry()]);

  const card = page.locator('#card');
  const table = card.locator('.table-container');
  const mobileBar = card.locator('.mobile-filter-bar');

  await expect(table).toBeHidden();
  await expect(mobileBar).toBeVisible();
});

test('clicking a table row opens the entity details modal', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 800 });
  await publishAllMetadata(page, [sampleEntry({ id: 42, title: 'Click me' })]);

  const card = page.locator('#card');
  await card.locator('tr[data-id="42"]').click();

  const modal = card.locator('#details-modal');
  await expect(modal).toHaveClass(/show/);
  await expect(modal.locator('.modal-body')).toContainText('Click me');

  await card.locator('#close-details').click();
  await expect(modal).not.toHaveClass(/show/);
});

test('the mobile filter panel toggles open and closed on tap', async ({ page }) => {
  await page.setViewportSize({ width: 400, height: 800 });
  await publishAllMetadata(page, [sampleEntry()]);

  const card = page.locator('#card');
  const panel = card.locator('#mobile-filter-panel');
  const toggle = card.locator('#mobile-filter-toggle');

  await expect(panel).toBeHidden();
  await toggle.click();
  await expect(panel).toBeVisible();
  await toggle.click();
  await expect(panel).toBeHidden();
});

test('enabling an entity from the table publishes the documented MQTT command', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 800 });
  await publishAllMetadata(page, [sampleEntry({ id: 7, title: 'Enable me' })]);

  const card = page.locator('#card');
  await card.locator('tr[data-id="7"] [data-action="enable"]').click();

  await page.waitForFunction(() =>
    window.__published.some((p) => p.topic === 'homeassistant/text/nibe_enable_entity/set' && p.payload === '7')
  );
});
