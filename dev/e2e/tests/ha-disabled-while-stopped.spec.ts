import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  HA_URL,
  haWs,
  readRetained,
  recreateBridge,
  stopBridge,
} from './support/stack';

/**
 * An entity disabled in Home Assistant while the add-on was stopped is
 * mirrored when it starts.
 *
 * Disabling one of the bridge's entities in HA's entity settings is mirrored
 * by the bridge from the registry event — it removes the entity. Done while
 * the add-on was stopped there is no event to see: the bridge republished
 * the entity at startup, HA kept it disabled, and the Entity Manager card
 * listed it as enabled. The registry watcher now checks the registry on
 * every connect. Runs behind the fake Supervisor, which the watcher needs.
 */

const POINT_ID = 4;

async function registryEntry(token: string): Promise<any | undefined> {
  const [resp] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  return (resp.result ?? []).find((e: any) => e.unique_id === `nibe_${POINT_ID}`);
}

function bridgeEnabledPoints(): number[] {
  const raw = readRetained('nibe/browser/enabled_state');
  return raw ? (JSON.parse(raw).enabled_points ?? []) : [];
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

test('an entity disabled in HA while the add-on was stopped is mirrored at startup', async () => {
  test.setTimeout(400_000);
  const token = readToken();
  const entry = await registryEntry(token);
  expect(entry, `precondition: point ${POINT_ID} has no registry entry`).toBeTruthy();
  expect(bridgeEnabledPoints()).toContain(POINT_ID);

  try {
    stopBridge();
    const [resp] = await haWs(token, [
      { type: 'config/entity_registry/update', entity_id: entry.entity_id, disabled_by: 'user' },
    ]);
    expect(resp.success, JSON.stringify(resp)).toBe(true);

    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });

    await expect
      .poll(() => bridgeEnabledPoints().includes(POINT_ID), {
        timeout: 90_000,
        message:
          `${entry.entity_id} was disabled in Home Assistant while the add-on was stopped, but ` +
          'the bridge still has it enabled — the card lists it as enabled while HA has it disabled',
      })
      .toBe(false);
  } finally {
    // Back as it was: re-enabled in the bridge, and not disabled in HA.
    const current = await registryEntry(token);
    if (current?.disabled_by) {
      await haWs(token, [
        { type: 'config/entity_registry/update', entity_id: current.entity_id, disabled_by: null },
      ]);
    }
    await recreateBridge(DEFAULT_BRIDGE_ENV);
    await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(POINT_ID));
  }
});
