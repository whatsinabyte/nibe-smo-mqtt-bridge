import { test, expect } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
  DOT,
  DOT_POINTS,
  SETPOINT,
  SG_POINTS,
  SG_READY,
  endScenario,
  entityFor,
  haCall,
  haState,
  learned,
  mock,
  mqttPublish,
  refreshList,
  refreshListFromSnapshot,
  snapshotList,
  startScenario,
  waitForLog,
  WITH_SUPERVISOR,
} from './support/dynamic-scenarios';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  bridgeLogsSince,
  haWs,
  pointEntityId,
  readRetained,
  recreateBridge,
  setMockValue,
} from './support/stack';

/**
 * How the bridge learns which settings a switch shows, in the orderings a
 * real controller produces. The REST API's point list lags its values by up
 * to a minute (docs/known-firmware-quirks.md), so whether the list catches up
 * before or after the next switch action decides what can be learned. The
 * mock's lagging list (support/dynamic-scenarios.ts) lets each scenario fix
 * that order instead of hoping for it.
 *
 * Switches: SG Ready activation (10613, shows 3260/4694/10614) and "Power at
 * DOT, manual value" (6984, shows 6983), both unlearned at the start of each
 * scenario.
 */

let token = '';

test.beforeEach(async () => {
  token = readToken();
  await dismissDashboardNotices();
});

test.afterEach(async () => {
  await endScenario();
  await dismissDashboardNotices();
});

const on = (id: string) => haCall(token, 'switch', 'turn_on', { entity_id: id });
const off = (id: string) => haCall(token, 'switch', 'turn_off', { entity_id: id });

async function dashboardNotices(): Promise<string[]> {
  const [res] = await haWs(token, [{ type: 'persistent_notification/get' }]);
  return (res.result ?? [])
    .map((n: { notification_id: string }) => n.notification_id)
    .filter((id: string) => id.startsWith('nibe_dashboard_updated'))
    .sort();
}

async function dismissDashboardNotices(): Promise<void> {
  for (const id of await dashboardNotices()) {
    await haCall(token, 'persistent_notification', 'dismiss', { notification_id: id });
  }
}

async function expectLearned(controller: number, value: number, points: number[]): Promise<void> {
  await expect
    .poll(() => learned(controller)?.byValue[String(value)], {
      timeout: 60_000,
      message: `${controller}=${value} should be learned as showing ${points}`,
    })
    .toEqual(points);
}

async function expectGone(pointId: number): Promise<void> {
  await expect
    .poll(async () => (await pointEntityId(token, pointId)) ?? 'gone', {
      timeout: 60_000,
      message: `point ${pointId} should no longer have an entity`,
    })
    .toBe('gone');
}

test('A: an unlearned switch is learned once the point list catches up', async () => {
  test.setTimeout(240_000);
  const { sg } = await startScenario(token);
  const since = new Date().toISOString();
  await on(sg);
  await waitForLog(since, `Point ${SG_READY} value=1 is unprocessed`);
  await refreshList();
  await expectLearned(SG_READY, 1, SG_POINTS);
  await expectLearned(SG_READY, 0, []);
  expect(await pointEntityId(token, 10614), 'its select is shown').toBeTruthy();
});

test('B: switched on and straight off before the list catches up, nothing is learned and the off is not held back', async () => {
  test.setTimeout(240_000);
  const { sg } = await startScenario(token);
  const since = new Date().toISOString();
  await on(sg);
  await waitForLog(since, `Write successful for point ${SG_READY}`);
  await snapshotList(); // the list will catch up to "on"…
  const offAt = new Date().toISOString();
  await off(sg);
  await waitForLog(offAt, `Writing 0 to point ${SG_READY}`, 5_000);
  expect(bridgeLogsSince(offAt)).not.toContain('waits for the learning window');
  await refreshListFromSnapshot(); // …while the switch already reads off
  await waitForLog(offAt, `but aren't recorded`);
  expect(learned(SG_READY)?.unprocessed, 'neither value learned').toEqual([0, 1]);
  await refreshList(); // the list catches up to "off"
  for (const pid of SG_POINTS) await expectGone(pid);
});

test('C: a learned switch switched on and straight off does not teach "off" the settings "on" shows', async () => {
  test.setTimeout(300_000);
  const { sg } = await startScenario(token);
  let since = new Date().toISOString();
  await on(sg);
  await refreshList();
  await expectLearned(SG_READY, 1, SG_POINTS);
  await off(sg);
  await refreshList();
  for (const pid of SG_POINTS) await expectGone(pid);

  since = new Date().toISOString();
  await on(sg);
  await waitForLog(since, `Write successful for point ${SG_READY}`);
  await snapshotList();
  await off(sg);
  await waitForLog(since, /Write successful for point 10613[\s\S]*Write successful for point 10613/);
  await refreshListFromSnapshot();
  await waitForLog(since, `but aren't recorded`);
  await expectLearned(SG_READY, 0, []);
  await expectLearned(SG_READY, 1, SG_POINTS);
  await refreshList();
  for (const pid of SG_POINTS) await expectGone(pid);
});

test('D: two unlearned switches in quick succession each learn their own settings and get their own notice', async () => {
  test.setTimeout(300_000);
  const { sg, dot } = await startScenario(token, WITH_SUPERVISOR);
  try {
    await twoSwitches(sg, dot);
  } finally {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});

async function twoSwitches(sg: string, dot: string): Promise<void> {
  const since = new Date().toISOString();
  await on(dot);
  await waitForLog(since, `Point ${DOT} value=1 is unprocessed`);
  await on(sg);
  await waitForLog(since, `Write for point ${SG_READY} waits for the learning window of point ${DOT}`);
  await refreshList(); // 6984's setting arrives; 10613 is still off
  await expectLearned(DOT, 1, DOT_POINTS);
  await waitForLog(since, `Writing 1 to point ${SG_READY}`);
  await refreshList();
  await expectLearned(SG_READY, 1, SG_POINTS);
  await expectLearned(DOT, 1, DOT_POINTS);
  await expect
    .poll(dashboardNotices, { timeout: 30_000 })
    .toEqual([`nibe_dashboard_updated_${SG_READY}`, `nibe_dashboard_updated_${DOT}`].sort());
}

test('E: a setpoint is not held back while a switch is being learned', async () => {
  test.setTimeout(240_000);
  const enabledBefore = (
    JSON.parse(readRetained('nibe/browser/enabled_state') ?? '{}').enabled_points ?? []
  ).includes(SETPOINT);
  const { sg } = await startScenario(token);
  const setpoint = await entityFor(token, SETPOINT);
  try {
    const since = new Date().toISOString();
    await on(sg);
    await waitForLog(since, `Point ${SG_READY} value=1 is unprocessed`);
    const setAt = new Date().toISOString();
    await haCall(token, 'number', 'set_value', { entity_id: setpoint, value: 21.5 });
    await waitForLog(setAt, `to point ${SETPOINT}`, 5_000);
    expect(bridgeLogsSince(setAt)).not.toContain(`Write for point ${SETPOINT} waits`);
  } finally {
    await setMockValue(SETPOINT, 210);
    if (!enabledBefore) mqttPublish('homeassistant/text/nibe_disable_entity/set', String(SETPOINT));
  }
});

test('F: a switch changed on the controller while another is being learned is not credited to it', async () => {
  test.setTimeout(240_000);
  const { dot } = await startScenario(token);
  const since = new Date().toISOString();
  await on(dot);
  await waitForLog(since, `Point ${DOT} value=1 is unprocessed`);
  await setMockValue(SG_READY, 1); // on the controller's own display
  await waitForLog(since, `Point(s) [${SG_READY}] changed on the controller during the scan window of point ${DOT}`);
  await refreshList(); // both switches' settings arrive in 6984's window
  await expect.poll(() => pointEntityId(token, 10614), { timeout: 60_000 }).toBeTruthy();
  await waitForLog(since, `but aren't recorded`);
  // What appeared can't be told apart: neither switch is credited with it,
  // and 6984 is learned on a later, calmer change.
  expect(learned(DOT)?.byValue['1'], '6984=1 not credited with 10613\'s settings').toBeUndefined();
  expect(learned(DOT)?.unprocessed).toContain(1);
});

test('G: an API failure during learning teaches nothing', async () => {
  test.setTimeout(240_000);
  const { sg } = await startScenario(token);
  const since = new Date().toISOString();
  await on(sg);
  await waitForLog(since, `Point ${SG_READY} value=1 is unprocessed`);
  await mock('/mock-control/fail', { count: 2 });
  // The API client answers an HTTP 500 with no response at all.
  await waitForLog(since, 'Bulk fetch returned an unexpected response type');
  await waitForLog(since, `Post-write scan window ended for point ${SG_READY}`, 150_000);
  await waitForLog(since, 'ended without a usable outcome');
  expect(learned(SG_READY)?.unprocessed, 'still to be learned').toContain(1);
});

test('H: settings that arrive after the 90 s window are still linked to their switch', async () => {
  test.setTimeout(300_000);
  const { sg } = await startScenario(token);
  const since = new Date().toISOString();
  await on(sg);
  await waitForLog(since, `Post-write scan window ended for point ${SG_READY}`, 150_000);
  await refreshList(); // the list catches up late
  await expectLearned(SG_READY, 1, SG_POINTS);
});

test('I: a switch shows its new state at once and does not bounce', async () => {
  test.setTimeout(240_000);
  const { sg } = await startScenario(token);
  for (const [act, want, other] of [
    [on, 'on', 'off'],
    [off, 'off', 'on'],
  ] as [(id: string) => Promise<void>, string, string][]) {
    const t0 = Date.now();
    await act(sg);
    await expect.poll(() => haState(token, sg), { timeout: 1_500, intervals: [100] }).toBe(want);
    expect(Date.now() - t0, `${want} within 1.5 s`).toBeLessThan(1_500);
    for (let i = 0; i < 20; i++) {
      expect(await haState(token, sg), `${want} must not flip back`).not.toBe(other);
      await new Promise((r) => setTimeout(r, 250));
    }
  }
});

test('J: an entity whose removal Home Assistant missed is removed when it comes back online and at a bridge restart', async () => {
  test.setTimeout(360_000);
  const { sg } = await startScenario(token);
  await on(sg);
  await refreshList();
  await expectLearned(SG_READY, 1, SG_POINTS);
  await off(sg);
  await refreshList();
  for (const pid of SG_POINTS) await expectGone(pid);

  // What HA is left with when it reconnects just as the bridge removes an
  // entity: the entity, while the broker holds no config for it. A config
  // sent without retain gives HA exactly that.
  const leftBehind = async () => {
    mqttPublish(
      'homeassistant/sensor/nibe_4694/config',
      JSON.stringify({ name: 'Left behind', unique_id: 'nibe_4694', state_topic: 'nibe/e2e/left_behind' })
    );
    await expect.poll(() => pointEntityId(token, 4694), { timeout: 30_000 }).toBeTruthy();
  };

  await leftBehind();
  const since = new Date().toISOString();
  mqttPublish('homeassistant/status', 'online'); // HA's birth message
  await expectGone(4694);
  await waitForLog(since, 'Resent the removal of');

  await leftBehind();
  await recreateBridge(DEFAULT_BRIDGE_ENV);
  await expectGone(4694);
});

test('K: in menus mode, switching a switch saves the menu dashboard once', async () => {
  test.setTimeout(420_000);
  const menus = { BRIDGE_OPTIONS: './bridge/options-menus.json', BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN };
  const saved = (since: string) => bridgeLogsSince(since).split('Nibe Menus dashboard provisioned').length - 1;
  const builds = (since: string) =>
    bridgeLogsSince(since).split('Menu dashboard regen starting').length - 1;
  // Until no build has started or finished for 15 s.
  const settled = async (since: string) => {
    let last = '';
    let stableSince = Date.now();
    await expect
      .poll(
        () => {
          const now = `${builds(since)}/${saved(since)}/${bridgeLogsSince(since).split('unchanged — not saved again').length}`;
          if (now !== last) {
            last = now;
            stableSince = Date.now();
          }
          return Date.now() - stableSince;
        },
        { timeout: 180_000, intervals: [1_000] }
      )
      .toBeGreaterThan(15_000);
  };
  const startedAt = new Date().toISOString();
  const { sg } = await startScenario(token, menus);
  try {
    await waitForLog(startedAt, 'Nibe Menus dashboard provisioned', 180_000);
    await settled(startedAt);

    const since = new Date().toISOString();
    await on(sg);
    await refreshList();
    await expectLearned(SG_READY, 1, SG_POINTS);
    await waitForLog(since, 'Registry refresh: updated', 60_000);
    await settled(since);
    // The build the change scheduled, and the one the registry refresh
    // scheduled when it mapped the new entities: one save between them.
    expect(builds(since), 'both builds ran').toBeGreaterThanOrEqual(2);
    expect(saved(since), 'saved once').toBe(1);
    expect(bridgeLogsSince(since)).toContain('Nibe Menus dashboard unchanged — not saved again');
  } finally {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
