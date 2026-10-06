import { test, expect } from '@playwright/test';
import { execSync } from 'child_process';
import { readToken } from './support/ha-login';
import {
  bridgeLogsSince,
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  haWs,
  publishRetained,
  readRetained,
  recreateBridge,
  stopBridge,
} from './support/stack';

/**
 * A dynamic entity disabled in Home Assistant while the add-on was stopped is
 * re-enabled — not removed — when the controller is unreachable at startup.
 *
 * The startup registry check (dynamic-disabled-while-stopped.spec.ts) tells
 * a dynamic entity, which it re-enables, from a static one, which it mirrors
 * by removing it. With the controller down, discovery is deferred and the
 * bridge has no point metadata yet, so every entity looked static: the
 * dynamic one was removed from the bridge and stayed disabled in HA. Points
 * it can't classify yet are now left for the same check to handle once
 * deferred discovery completes.
 *
 * Same seeded chain as dynamic-disable-reversed.spec.ts (3846 shows 3933);
 * the controller is taken down by stopping the mock API.
 */

const DYNAMIC_MAP_TOPIC = 'nibe/browser/dynamic_point_map';
const DYNAMIC_POINT = 3933;
const MOCK_API_CONTAINER = process.env.MOCK_API_CONTAINER || 'nibe-e2e-mock-api';
const WITH_SUPERVISOR = {
  BRIDGE_OPTIONS: './bridge/options.json',
  BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
};

async function registryEntry(token: string): Promise<any | undefined> {
  const [resp] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  return (resp.result ?? []).find((e: any) => e.unique_id === `nibe_${DYNAMIC_POINT}`);
}

async function notificationIds(token: string): Promise<string[]> {
  const [resp] = await haWs(token, [{ type: 'persistent_notification/get' }]);
  return (resp.result ?? []).map((n: any) => n.notification_id);
}

function bridgeEnabledPoints(): number[] {
  const raw = readRetained('nibe/browser/enabled_state');
  return raw ? (JSON.parse(raw).enabled_points ?? []) : [];
}

test('a dynamic entity disabled while stopped is re-enabled once a down controller answers', async () => {
  test.setTimeout(700_000);
  const token = readToken();
  stopBridge();
  const originalMap = readRetained(DYNAMIC_MAP_TOPIC);

  try {
    publishRetained(
      DYNAMIC_MAP_TOPIC,
      JSON.stringify({
        3846: {
          point_id: 3846,
          title: 'Point 3846',
          entity_type: 'switch',
          processed_values: [0, 1],
          unprocessed_values: [],
          is_controlling: true,
          dynamic_points_by_value: { '0': [DYNAMIC_POINT], '1': [DYNAMIC_POINT] },
          firmware_removed: false,
        },
      })
    );
    await recreateBridge(WITH_SUPERVISOR);
    let entityId = '';
    await expect
      .poll(async () => (entityId = (await registryEntry(token))?.entity_id ?? ''), {
        timeout: 60_000,
        message: `precondition: dynamic point ${DYNAMIC_POINT} never got an HA entity`,
      })
      .not.toBe('');

    stopBridge();
    const [resp] = await haWs(token, [
      { type: 'config/entity_registry/update', entity_id: entityId, disabled_by: 'user' },
    ]);
    expect(resp.success, JSON.stringify(resp)).toBe(true);

    execSync(`docker stop ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });
    const since = new Date().toISOString();
    await recreateBridge(WITH_SUPERVISOR);
    // Keep the controller down until discovery has actually been deferred
    // ("Started Without Device") and the registry watcher has run its
    // startup check.
    await expect
      .poll(async () => (await notificationIds(token)).includes('nibe_discovery_incomplete'), {
        timeout: 240_000,
        message: 'precondition: the bridge never deferred discovery',
      })
      .toBe(true);
    await expect
      .poll(() => bridgeLogsSince(since).includes('Entity registry watcher started'), {
        timeout: 60_000,
        message: 'precondition: the registry watcher never started',
      })
      .toBe(true);
    await expect
      .poll(() => bridgeLogsSince(since).includes('WebSocket connected and subscribed'), {
        timeout: 60_000,
        message: 'precondition: the registry watcher never connected',
      })
      .toBe(true);
    const reconnectedAt = new Date().toISOString();
    execSync(`docker start ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });

    await expect
      .poll(async () => (await registryEntry(token))?.disabled_by ?? null, {
        timeout: 180_000,
        message:
          `${entityId} is a dynamic entity disabled in HA while the add-on was stopped; with the ` +
          'controller down at startup it was never re-enabled',
      })
      .toBeNull();
    expect(
      bridgeEnabledPoints(),
      'the bridge removed the dynamic entity, treating it as a static one'
    ).toContain(DYNAMIC_POINT);

    // That re-enable runs off the registry watcher's thread, so HA's echo of
    // it can reach the watcher before the bridge has noted it as its own;
    // the notification must still explain the disable once things settle.
    const notifId = `nibe_ha_disable_${entityId.replace('.', '_')}`;
    await expect
      .poll(() => bridgeLogsSince(reconnectedAt).includes(`Registry re-enable of ${entityId}`) ||
        bridgeLogsSince(reconnectedAt).includes(`${entityId} (point ${DYNAMIC_POINT}) re-enabled via HA`), {
        timeout: 150_000,
        message: "precondition: HA's echo of the re-enable never reached the bridge",
      })
      .toBe(true);
    const [notes] = await haWs(token, [{ type: 'persistent_notification/get' }]);
    const note = (notes.result ?? []).find((n: any) => n.notification_id === notifId);
    expect(note?.title, `precondition: no ${notifId} notification`).toBeTruthy();
    expect(note.title, 'the echo of the bridge\'s own re-enable was taken for the user').not.toContain(
      're-enabled'
    );
  } finally {
    execSync(`docker start ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });
    const entry = await registryEntry(token);
    if (entry?.disabled_by) {
      await haWs(token, [
        { type: 'config/entity_registry/update', entity_id: entry.entity_id, disabled_by: null },
      ]);
    }
    stopBridge();
    publishRetained(DYNAMIC_MAP_TOPIC, originalMap ?? '');
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
