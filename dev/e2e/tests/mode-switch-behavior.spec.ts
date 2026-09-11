import { test, expect } from '@playwright/test';
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Three separate promises, from ARCHITECTURE.md §4.4 ("Mode reconciliation")
 * and DOCS.md's "Entity Modes", none of which had any end-to-end coverage:
 *
 * 1. `apply_mode()` runs only on a fresh install or a mode change detected
 *    across a restart — **never** on an ordinary same-mode restart, so
 *    entities a user added by hand in the Entity Manager survive restarts.
 * 2. `mode_switch_behavior: merge` never disables anything; it only adds the
 *    new mode's points.
 * 3. `mode_switch_behavior: replace` (the default) disables the points
 *    outside the new mode's set.
 *
 * Getting these wrong is silent and expensive: a bridge that re-applied its
 * mode on every restart would quietly delete every manual addition a user had
 * ever made, and they would only notice when a dashboard went blank.
 *
 * The enabled set is read from the bridge's own retained `enabled_state`
 * topic rather than scraped from the card, because it needs to be exact and
 * complete — the card paginates, and HA entity ids cannot be mapped back to
 * point ids without the attributes round trip.
 *
 * The mode is changed by recreating the container against a different
 * options file (`BRIDGE_OPTIONS`, see the bridge service's `volumes:` in
 * docker-compose.yml) rather than by rewriting `bridge/options.json` — a test
 * that edits the repo to do its job leaves the repo dirty when it fails.
 *
 * It has to be the options file, not the `NIBE_MODE` environment variable:
 * the add-on's own `run.sh` reads options.json and passes `--mode` on the
 * command line, and CLI flags sit *above* environment variables in
 * `load_config`'s documented priority order, so NIBE_MODE is silently
 * outranked. (Tried that first; the bridge simply came up in `essential`
 * again and the spec failed with "merge mode enabled nothing new".)
 *
 * The default options file is restored at the end, since the shared stack
 * outlives this spec and a later `docker restart` would otherwise inherit
 * whatever mode was last set.
 */

const REFERENCE_DUMP = path.join(__dirname, '..', '..', '..', 'reference-dumps', 'all_points_en.json');
const BRIDGE_CONTAINER = process.env.BRIDGE_CONTAINER || 'nibe-e2e-bridge';
const MQTT_CONTAINER = process.env.MQTT_CONTAINER || 'nibe-e2e-mosquitto-1';
const COMPOSE_DIR = path.join(__dirname, '..');

/** The bridge's own view of which points are enabled, exact and complete. */
function enabledPoints(): number[] {
  const raw = execSync(
    `docker exec ${MQTT_CONTAINER} mosquitto_sub -h localhost ` +
      `-t 'nibe/browser/enabled_state' -C 1 -W 10`,
    { encoding: 'utf-8' }
  );
  return JSON.parse(raw).enabled_points;
}

function publish(topic: string, payload: string): void {
  execSync(
    `docker exec ${MQTT_CONTAINER} mosquitto_pub -h localhost -t '${topic}' -m '${payload}'`,
    { stdio: 'ignore' }
  );
}

/** A point that exists in this firmware dump, reports a usable value, and is
 * not currently enabled — so enabling it is a real, observable customisation
 * rather than a no-op. */
function aDisabledPoint(currentlyEnabled: number[]): number {
  const dump = JSON.parse(fs.readFileSync(REFERENCE_DUMP, 'utf-8'));
  const sentinels: Record<string, number> = {
    s16: -32768,
    u16: 65535,
    s32: -2147483648,
    u32: 4294967295,
  };
  const enabled = new Set(currentlyEnabled);
  for (const [idStr, point] of Object.entries<any>(dump)) {
    const id = Number(idStr);
    if (enabled.has(id)) continue;
    const size = point?.metadata?.variableSize;
    if (point?.value?.isOk === false) continue;
    if (size in sentinels && point?.value?.integerValue === sentinels[size]) continue;
    return id;
  }
  throw new Error('every usable point in the dump is already enabled');
}

async function waitForBridgeReady(since: string): Promise<void> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const logs = execSync(`docker logs --since ${since} ${BRIDGE_CONTAINER} 2>&1 || true`, {
      encoding: 'utf-8',
    });
    if (logs.includes('Bridge ready')) return;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error('bridge did not report ready within 120s');
}

/** Bring the bridge up again reading the given options file. Passing nothing
 * restores the harness default (bridge/options.json). */
async function recreateBridge(optionsFile?: string): Promise<void> {
  const since = new Date().toISOString();
  const prefix = optionsFile ? `BRIDGE_OPTIONS=${optionsFile} ` : '';
  execSync(`${prefix}docker compose up -d --force-recreate bridge`, {
    cwd: COMPOSE_DIR,
    stdio: 'ignore',
  });
  await waitForBridgeReady(since);
}

async function restartBridge(): Promise<void> {
  const since = new Date().toISOString();
  execSync(`docker restart ${BRIDGE_CONTAINER}`, { stdio: 'ignore' });
  await waitForBridgeReady(since);
}

test('mode reconciliation respects same-mode restarts, merge, and replace', async () => {
  // Four bridge startups, each waiting for real discovery.
  test.setTimeout(600_000);

  try {
    // ── 1. A manual customisation, then an ordinary same-mode restart ──
    const baseline = enabledPoints();
    expect(baseline.length, 'no points enabled to start from').toBeGreaterThan(0);

    const manualPoint = aDisabledPoint(baseline);
    publish('homeassistant/text/nibe_enable_entity/set', String(manualPoint));
    await expect
      .poll(() => enabledPoints(), {
        timeout: 30_000,
        message: `point ${manualPoint} was never enabled`,
      })
      .toContain(manualPoint);

    await restartBridge();

    // The promise: a same-mode restart does not reconcile. If apply_mode ran
    // here, `replace` would disable this point for being outside `essential`
    // and the user's addition would be gone.
    await expect
      .poll(() => enabledPoints(), {
        timeout: 60_000,
        message:
          `manual addition ${manualPoint} was disabled by a same-mode restart — ` +
          `apply_mode must not run unless the mode actually changed`,
      })
      .toContain(manualPoint);

    const afterRestart = enabledPoints();

    // ── 2. merge: adds, never removes ──
    await recreateBridge('./bridge/options-monitoring-merge.json');

    await expect
      .poll(() => enabledPoints().length, {
        timeout: 90_000,
        message: 'switching to monitoring in merge mode enabled nothing new',
      })
      .toBeGreaterThan(afterRestart.length);

    const afterMerge = enabledPoints();
    const droppedByMerge = afterRestart.filter((p) => !afterMerge.includes(p));
    expect(
      droppedByMerge,
      'merge disabled points that were already enabled — it must only ever add'
    ).toEqual([]);
    expect(afterMerge, 'merge disabled the manual addition').toContain(manualPoint);

    // ── 3. replace: disables what falls outside the new mode ──
    const monitoringOnly = afterMerge.filter((p) => !afterRestart.includes(p));
    expect(monitoringOnly.length, 'no monitoring-only points to test replace with').toBeGreaterThan(
      0
    );

    // Back to the harness default: essential mode, replace behaviour.
    await recreateBridge();

    await expect
      .poll(() => enabledPoints().length, {
        timeout: 90_000,
        message: 'switching back to essential in replace mode disabled nothing',
      })
      .toBeLessThan(afterMerge.length);

    const afterReplace = enabledPoints();
    const survivors = monitoringOnly.filter((p) => afterReplace.includes(p));
    expect(
      survivors,
      'replace left points enabled that fall outside the new mode'
    ).toEqual([]);

    // Documented consequence, and the reason `merge` exists as an option:
    // replace is an intentional override, so it disables manual additions
    // outside the new mode too.
    expect(
      afterReplace.includes(manualPoint),
      `replace should have disabled the manual addition ${manualPoint} as well`
    ).toBe(false);
  } finally {
    // Hand the shared stack back in its configured state, whatever happened
    // above — otherwise every later spec, and any later `docker restart`,
    // silently inherits the last mode this test set.
    await recreateBridge();
  }

  expect(enabledPoints().length).toBeGreaterThan(0);
});
