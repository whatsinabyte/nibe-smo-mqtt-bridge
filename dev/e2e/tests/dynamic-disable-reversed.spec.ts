import { test, expect } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
  bridgeLogsSince,
  DEFAULT_BRIDGE_ENV,
  registryWatcherHasMapped,
  FAKE_SUPERVISOR_TOKEN,
  haWs,
  publishRetained,
  readRetained,
  recreateBridge,
  stopBridge,
} from './support/stack';

/**
 * Disabling a dynamic entity in Home Assistant is reversed by the bridge.
 *
 * DOCS.md: dynamic points are firmware-controlled, so "the bridge re-enables
 * any dynamic entity disabled via HA". It reacted by republishing the
 * entity's discovery config — but disabled_by is entity-registry state that a
 * discovery publish doesn't touch, so the entity simply stayed disabled in HA
 * while the bridge treated it as live. The bridge now clears disabled_by
 * through HA's registry API.
 *
 * A dynamic point is seeded through the retained dynamic-map topic (3846
 * shows 3933 for any value); the registry watcher needs the fake Supervisor.
 */

const DYNAMIC_MAP_TOPIC = 'nibe/browser/dynamic_point_map';
const DYNAMIC_POINT = 3933;

async function registryEntry(token: string): Promise<any | undefined> {
  const [resp] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  return (resp.result ?? []).find((e: any) => e.unique_id === `nibe_${DYNAMIC_POINT}`);
}

test('a dynamic entity disabled in HA is re-enabled by the bridge', async () => {
  test.setTimeout(500_000);
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
    const startedAt = new Date().toISOString();
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });

    let entityId = '';
    await expect
      .poll(async () => (entityId = (await registryEntry(token))?.entity_id ?? ''), {
        timeout: 60_000,
        message: `precondition: dynamic point ${DYNAMIC_POINT} never got an HA entity`,
      })
      .not.toBe('');

    // Let the registry watcher map the entity first (see
    // registryWatcherHasMapped). A disable it can't resolve to a point yet
    // is ignored — not a case real use hits, nobody disables an entity
    // seconds after it appears.
    await expect
      .poll(() => registryWatcherHasMapped(startedAt, entityId), {
        timeout: 60_000,
        message: 'precondition: the registry watcher never mapped the entity',
      })
      .toBe(true);
    const disabledAt = new Date().toISOString();
    const [resp] = await haWs(token, [
      { type: 'config/entity_registry/update', entity_id: entityId, disabled_by: 'user' },
    ]);
    expect(resp.success, JSON.stringify(resp)).toBe(true);

    await expect
      .poll(async () => (await registryEntry(token))?.disabled_by ?? null, {
        timeout: 60_000,
        message:
          `${entityId} is a dynamic entity disabled in HA, but the bridge never re-enabled it — ` +
          'republishing its discovery config leaves disabled_by untouched',
      })
      .toBeNull();

    // HA reports that re-enable back as a registry event (up to a minute
    // later). Read as the user re-enabling the entity, it replaced the
    // "dynamic entity disabled" notification with a false "re-enabled via the
    // HA entity settings" one. Wait for the bridge to have seen that echo.
    const notifId = `nibe_ha_disable_${entityId.replace('.', '_')}`;
    await expect
      .poll(() => bridgeLogsSince(disabledAt).includes(`Registry re-enable of ${entityId}`) ||
        bridgeLogsSince(disabledAt).includes(`${entityId} (point ${DYNAMIC_POINT}) re-enabled via HA`), {
        timeout: 150_000,
        message: "precondition: HA's echo of the re-enable never reached the bridge",
      })
      .toBe(true);
    const [notes] = await haWs(token, [{ type: 'persistent_notification/get' }]);
    const note = (notes.result ?? []).find((n: any) => n.notification_id === notifId);
    expect(note?.title, `precondition: no ${notifId} notification`).toBeTruthy();
    expect(
      note.title,
      'the echo of the bridge\'s own re-enable was treated as the user re-enabling the entity'
    ).not.toContain('re-enabled');
  } finally {
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
