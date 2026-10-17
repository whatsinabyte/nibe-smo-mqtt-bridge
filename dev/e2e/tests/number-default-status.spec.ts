import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId, setMockValue } from './support/stack';

/**
 * A number setting says live whether it differs from its factory default.
 *
 * The Nibe Menus dashboard's ✏️ badge in a section label is only as fresh as
 * the last dashboard build, so putting a setting back to its default left it
 * flagged. A number's tile shows the default_status attribute instead, which
 * follows the value: 32341 "Desired room temperature for zone 1" (default
 * 20 °C) set to 21.5 °C must read "✏️ default: 20 °C", and back at 20 °C
 * "default".
 */

const POINT = 32341;
const DUMP_VALUE = 210;

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

async function defaultStatus(token: string, entityId: string): Promise<string> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = resp.ok() ? await resp.json() : null;
  await ctx.dispose();
  return body?.attributes?.default_status ?? 'none';
}

test('a number setting reports live whether it differs from its default', async () => {
  test.setTimeout(240_000);
  const token = readToken();
  const enabledHere = !(await pointEntityId(token, POINT));
  try {
    if (enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_enable_entity/set', String(POINT));
    }
    let id = '';
    await expect
      .poll(async () => (id = (await pointEntityId(token, POINT)) ?? ''), {
        timeout: 60_000,
        message: `precondition: point ${POINT} never got an HA entity`,
      })
      .not.toBe('');
    for (const [raw, want] of [
      [215, '✏️ default: 20 °C'],
      [200, 'default'],
      [215, '✏️ default: 20 °C'],
    ] as [number, string][]) {
      await setMockValue(POINT, raw);
      await expect
        .poll(() => defaultStatus(token, id), {
          timeout: 60_000,
          message: `${id} at raw ${raw} should report "${want}"`,
        })
        .toBe(want);
    }
  } finally {
    await setMockValue(POINT, DUMP_VALUE);
    if (enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(POINT));
    }
  }
});
