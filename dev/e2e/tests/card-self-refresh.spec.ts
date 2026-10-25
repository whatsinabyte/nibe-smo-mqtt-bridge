import { test, expect } from '@playwright/test';
import { loginToHa, gotoLoggedIn, readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  haWs,
  publishRetained,
  readRetained,
  recreateBridge,
  recreateHa,
} from './support/stack';

/**
 * An outdated copy of the Entity Manager card refreshes itself.
 *
 * The Home Assistant Companion app can keep running an old copy of the card
 * after an update, since a page left open never re-reads the resource list.
 * The bridge therefore publishes the cache key of the card Lovelace serves
 * (retained nibe/browser/card_version, the resource URL's ?v=), and a card
 * whose own ?v= differs reloads the page once; if it is still outdated after
 * that, it shows a notice instead of reloading again.
 *
 * Runs the bridge through the fake Supervisor, and Home Assistant with
 * Lovelace resources in storage mode (ha-seed/configuration-storage-
 * resources.yaml), since only then does the bridge register the card
 * resource with its ?v= itself, as in production. Publishing a different
 * card version stands in for an update the page hasn't picked up: the card
 * must reload by itself, and come back with the notice, because its own
 * version still doesn't match.
 */

const TOPIC = 'nibe/browser/card_version';
const NOTICE = '#stale-card-notice';

test('a card older than the one Lovelace serves reloads once, then shows the notice', async ({
  page,
}) => {
  test.setTimeout(300_000);
  const token = readToken();
  let current: string | null = null;
  try {
    await recreateHa(token, {
      HA_CONFIGURATION: './ha-seed/configuration-storage-resources.yaml',
    });
    await recreateBridge({ ...DEFAULT_BRIDGE_ENV, BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN });
    // The bridge publishes the version Lovelace serves.
    await expect
      .poll(() => (current = readRetained(TOPIC)), {
        timeout: 60_000,
        message: 'the bridge never published nibe/browser/card_version',
      })
      .toMatch(/^[0-9a-f]{12}$/);
    const [resources] = await haWs(token, [{ type: 'lovelace/resources' }]);
    const urls: string[] = (resources?.result ?? []).map((r: { url: string }) => r.url);
    expect(urls, 'Lovelace serves the card under that version').toContain(
      `/local/nibe-entity-manager-card.js?v=${current}`
    );

    // A current copy neither reloads nor warns.
    await loginToHa(page);
    await gotoLoggedIn(page, '/nibe-bridge/entity-manager');
    const card = page.locator('nibe-entity-manager-card');
    await expect(card).toBeVisible({ timeout: 30_000 });
    await expect(card.locator(NOTICE)).toBeHidden();

    // A newer version than this copy: one reload, then the notice. A marker
    // on the window shows the reload: only a new page drops it (waiting for
    // a 'load' event instead caught the first navigation's own, late one).
    // Home Assistant's frontend can reload once by itself on the first visit
    // after a restart; wait until the page keeps the marker for 15 s.
    const marked = () =>
      page.evaluate(() => (window as any).__beforeStaleVersion === true).catch(() => false);
    for (let i = 0; i < 4; i++) {
      await page.waitForLoadState('load');
      await page.evaluate(() => ((window as any).__beforeStaleVersion = true)).catch(() => {});
      await page.waitForTimeout(15_000);
      if (await marked()) break;
    }
    expect(await marked(), 'precondition: the page settles without reloading').toBe(true);
    await expect(page.locator('nibe-entity-manager-card').locator(NOTICE)).toBeHidden();
    publishRetained(TOPIC, '000000000000');
    await expect
      .poll(() => page.evaluate(() => (window as any).__beforeStaleVersion === true).catch(() => true), {
        timeout: 60_000,
        message: 'the page never reloaded',
      })
      .toBe(false);
    await expect(page.locator('nibe-entity-manager-card').locator(NOTICE)).toBeVisible({
      timeout: 60_000,
    });
  } finally {
    // Put the real version back, so later specs' cards don't reload.
    publishRetained(TOPIC, current ?? '');
    await recreateHa(token, {});
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
