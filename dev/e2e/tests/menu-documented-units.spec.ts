import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId } from './support/stack';

/**
 * Units the firmware leaves out are filled in from the installer menus, and
 * Home Assistant accepts them.
 *
 * These registers report no unit; the menus give one (bar, °C, min, W, …).
 * Without it Home Assistant shows a bare number, with no device class, so a
 * pressure or temperature isn't treated as one. Read back from Home
 * Assistant's own state, which only carries a unit/device class it accepted.
 */

const EXPECTED: Record<number, { unit: string; deviceClass?: string }> = {
  995: { unit: 'bar', deviceClass: 'pressure' },
  3282: { unit: '°C', deviceClass: 'temperature' },
  4030: { unit: 'min' }, // a number entity: unit, no device class
  14314: { unit: 'W' },
};

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function attributes(token: string, entityId: string): Promise<Record<string, any>> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const attrs = resp.ok() ? (await resp.json()).attributes ?? {} : {};
  await ctx.dispose();
  return attrs;
}

test('menu-documented units reach Home Assistant', async () => {
  test.setTimeout(240_000);
  const token = readToken();
  try {
    for (const pointId of Object.keys(EXPECTED)) {
      await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', pointId);
    }
    for (const [pointId, want] of Object.entries(EXPECTED)) {
      let id: string | undefined;
      await expect
        .poll(async () => (id = await pointEntityId(token, pointId)) ?? '', {
          timeout: 60_000,
          message: `precondition: point ${pointId} never got an HA entity`,
        })
        .not.toBe('');
      await expect
        .poll(async () => (await attributes(token, id!)).unit_of_measurement, {
          timeout: 60_000,
          message: `${id} shows no unit; the installer menu gives ${want.unit}`,
        })
        .toBe(want.unit);
      if (want.deviceClass) {
        expect((await attributes(token, id!)).device_class, `${id} device class`).toBe(want.deviceClass);
      }
    }
  } finally {
    for (const pointId of Object.keys(EXPECTED)) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', pointId);
    }
  }
});
