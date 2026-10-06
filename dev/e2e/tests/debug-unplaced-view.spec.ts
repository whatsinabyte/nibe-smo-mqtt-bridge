import { test, expect } from '@playwright/test';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  haNow,
  recreateBridge,
  savedMenuDashboardMtime,
  savedMenuDashboardText,
} from './support/stack';

/**
 * The debug-only "Unplaced settings" tab lists writable registers that
 * declare no range.
 *
 * min == max is this firmware's "no bounds declared" convention, not a dead
 * register — switches 3754/8982 report min=max=0 and work fine. The tab
 * skipped every such point; for read-only status fields that is deliberate
 * (nothing to adjust, nothing to document), but it also hid writable ones,
 * which are exactly what the tab exists to surface. In the reference dump that
 * is point 6016 (ground water pump control signal).
 *
 * Runs the bridge in menus mode with debug_mode on (the tab is only built
 * then) through the fake Supervisor, and reads the saved dashboard from Home
 * Assistant's own storage.
 */

const POINT = 6016;

test('the debug unplaced tab lists a writable register without a declared range', async () => {
  test.setTimeout(900_000);

  try {
    const recreatedAt = haNow();
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-menus-debug.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });
    await expect
      .poll(
        () => savedMenuDashboardMtime() >= recreatedAt && savedMenuDashboardText().includes('menu-unplaced-debug'),
        { timeout: 300_000, message: 'this run never saved a Nibe Menus dashboard with the debug tab' }
      )
      .toBe(true);

    const text = savedMenuDashboardText();
    expect(
      text.includes(`(point ${POINT})`) && text.includes('no declared range'),
      `writable point ${POINT} declares min == max and is missing from the debug unplaced tab`
    ).toBe(true);
  } finally {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
