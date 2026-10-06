import { test, expect } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
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
 * re-enabled when it starts.
 *
 * Dynamic entities are firmware-controlled, so the bridge reverses a disable
 * done in HA. While the add-on runs it sees the registry event; for a disable
 * done while it was stopped, the startup registry check is the only chance —
 * and that check skipped dynamic entities, so this one stayed disabled for
 * good. Same seeded chain as dynamic-disable-reversed.spec.ts (3846 shows
 * 3933); the registry check needs the fake Supervisor.
 */

const DYNAMIC_MAP_TOPIC = 'nibe/browser/dynamic_point_map';
const DYNAMIC_POINT = 3933;
const WITH_SUPERVISOR = {
  BRIDGE_OPTIONS: './bridge/options.json',
  BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
};

async function registryEntry(token: string): Promise<any | undefined> {
  const [resp] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  return (resp.result ?? []).find((e: any) => e.unique_id === `nibe_${DYNAMIC_POINT}`);
}

test('a dynamic entity disabled in HA while the add-on was stopped is re-enabled at startup', async () => {
  test.setTimeout(400_000);
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
    await recreateBridge(WITH_SUPERVISOR);

    await expect
      .poll(async () => (await registryEntry(token))?.disabled_by ?? null, {
        timeout: 90_000,
        message:
          `${entityId} is a dynamic entity disabled in HA while the add-on was stopped, but the ` +
          'bridge never re-enabled it at startup',
      })
      .toBeNull();
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
