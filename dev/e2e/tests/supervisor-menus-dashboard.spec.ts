import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import * as path from 'path';
import { readToken } from './support/ha-login';

/**
 * The menu dashboard against a real entity registry. Everything here runs
 * through the fake Supervisor (fake-supervisor/), which lets the bridge's
 * registry watcher and Lovelace provisioning reach this harness's real Home
 * Assistant — both are Supervisor-only and otherwise never run in the
 * harness at all.
 *
 * The bridge is brought up in `menus` mode with a SUPERVISOR_TOKEN for the
 * duration of this spec only, then put back as it was, so every other spec
 * keeps running exactly as before.
 *
 * Two regressions are pinned, both races between the menu dashboard regen
 * (2s debounce plus a registry-stability wait) and the registry watcher's
 * own debounced refresh, which run independently off the same event:
 *
 *   - **Disable: stale entity_id baked into the dashboard.** Home Assistant's
 *     registry `remove` event carries no unique_id, so the watcher could not
 *     drop the mapping directly, and its refresh only ever added entries —
 *     so a disabled point kept resolving to its deleted entity_id and the
 *     regen saved it into the dashboard. Step 2 requires the row to stop
 *     referencing that entity.
 *
 *   - **Re-enable: the entity missing from the dashboard.** The `create`
 *     event carries no unique_id either, so a re-enabled point's entity_id
 *     only arrives with the watcher's refresh, 5s after the event — usually
 *     after the regen has already read it and saved the row as
 *     "not enabled", with no retry for non-dynamic points. Step 3 requires
 *     the entity to be back in the saved dashboard.
 *
 * The saved dashboard is read straight from Home Assistant's own storage,
 * which is what the frontend renders.
 */

const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const BRIDGE_CONTAINER = process.env.BRIDGE_CONTAINER || 'nibe-e2e-bridge';
const HA_CONTAINER = process.env.HA_CONTAINER || 'nibe-e2e-homeassistant';
const E2E_DIR = path.join(__dirname, '..');
const FAKE_SUPERVISOR_TOKEN = 'e2e-fake-supervisor-token';

interface HaState {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
}

async function fetchStates(token: string): Promise<HaState[]> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const states = await resp.json();
  await ctx.dispose();
  return states;
}

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

/** Recreate the bridge container with the given environment, and wait for
 * this start's own "Bridge ready" (not an earlier run's, still in the log). */
async function recreateBridge(env: Record<string, string>): Promise<void> {
  const since = new Date().toISOString();
  execSync('docker compose up -d --no-deps --force-recreate bridge', {
    cwd: E2E_DIR,
    env: { ...process.env, ...env },
    stdio: 'ignore',
  });
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    const logs = execSync(`docker logs --since ${since} ${BRIDGE_CONTAINER} 2>&1 || true`, {
      encoding: 'utf-8',
    });
    if (logs.includes('Bridge ready')) return;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error('bridge did not report ready within 180s of being recreated');
}

/** Seconds since the epoch on HA's own clock (not the host's — the Docker VM
 * can drift), and the saved menu dashboard's mtime on that same clock. */
function haNow(): number {
  return Number(execSync(`docker exec ${HA_CONTAINER} date +%s`, { encoding: 'utf-8' }).trim());
}

function savedMenuDashboardMtime(): number {
  try {
    const out = execSync(
      `docker exec ${HA_CONTAINER} sh -c 'stat -c %Y /config/.storage/lovelace.*menus* 2>/dev/null'`,
      { encoding: 'utf-8' }
    );
    return Number(out.trim().split('\n')[0]) || 0;
  } catch {
    return 0;
  }
}

/** All entity_ids referenced by the saved Nibe Menus dashboard, or null if
 * it hasn't been saved yet. */
function savedMenuDashboardEntities(): Set<string> | null {
  let raw: string;
  try {
    raw = execSync(
      `docker exec ${HA_CONTAINER} sh -c 'cat /config/.storage/lovelace.*menus* 2>/dev/null'`,
      { encoding: 'utf-8' }
    );
  } catch {
    return null;
  }
  if (!raw.trim()) return null;
  const entities = new Set<string>();
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(walk);
    } else if (node && typeof node === 'object') {
      const obj = node as Record<string, unknown>;
      if (typeof obj.entity === 'string') entities.add(obj.entity);
      Object.values(obj).forEach(walk);
    }
  };
  walk(JSON.parse(raw));
  return entities;
}

test('the menu dashboard tracks a point being disabled and re-enabled', async () => {
  // A mode change into `menus` (a few hundred entities), the initial
  // dashboard build, two regens, and putting the bridge back afterwards.
  test.setTimeout(900_000);

  const token = readToken();

  try {
    const recreatedAt = haNow();
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-menus.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });

    // ── 1. Wait for this run's initial dashboard and pick a point on it ───
    // Only a dashboard saved after the recreate counts: an earlier run's is
    // still in HA's storage and would otherwise satisfy this immediately.
    let initial: Set<string> = new Set();
    await expect
      .poll(
        () => {
          if (savedMenuDashboardMtime() < recreatedAt) return 0;
          initial = savedMenuDashboardEntities() ?? new Set();
          return initial.size;
        },
        { timeout: 300_000, message: 'this run never saved a Nibe Menus dashboard with entities' }
      )
      .toBeGreaterThan(20);

    // Any non-dynamic bridge entity rendered on the dashboard will do.
    const states = await fetchStates(token);
    const target = states.find(
      (s) => initial.has(s.entity_id) && typeof s.attributes?.point_id === 'string'
    );
    expect(target, 'no dashboard entity carries a point_id attribute').toBeTruthy();
    const pointId = target!.attributes.point_id as string;
    const entityId = target!.entity_id;

    // ── 2. Disable: the dashboard must stop referencing the deleted entity ─
    await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', pointId);
    await expect
      .poll(() => savedMenuDashboardEntities()?.has(entityId), {
        timeout: 90_000,
        message:
          `point ${pointId} was disabled but the saved menu dashboard still references its ` +
          `deleted entity ${entityId} — a stale registry mapping was baked into the dashboard`,
      })
      .toBe(false);

    // Let the disable's own registry refresh (scheduled by its `remove`
    // event, debounced 5s plus the fake Supervisor's fetch latency) finish
    // first. Re-enabling while it is still pending lets that refresh fetch
    // the registry after the entity already exists again and resolve it by
    // coincidence, which would hide exactly the race step 3 is here to catch.
    await new Promise((resolve) => setTimeout(resolve, 15_000));

    // ── 3. Re-enable: the entity must come back into the dashboard ────────
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', pointId);
    let reEnabledId: string | undefined;
    await expect
      .poll(
        async () => {
          const s = (await fetchStates(token)).find((st) => st.attributes?.point_id === pointId);
          reEnabledId = s?.entity_id;
          return reEnabledId !== undefined;
        },
        { timeout: 60_000, message: `point ${pointId} never got an HA entity back after re-enabling` }
      )
      .toBeTruthy();
    await expect
      .poll(() => savedMenuDashboardEntities()?.has(reEnabledId!), {
        timeout: 90_000,
        message:
          `point ${pointId} was re-enabled (entity ${reEnabledId}) but the saved menu dashboard ` +
          `never picked it up — the regen read the registry before the watcher's refresh landed`,
      })
      .toBe(true);
  } finally {
    // Back to the harness default (essential mode, no Supervisor) for the
    // specs that follow.
    await recreateBridge({ BRIDGE_OPTIONS: './bridge/options.json', BRIDGE_SUPERVISOR_TOKEN: '' });
  }
});
