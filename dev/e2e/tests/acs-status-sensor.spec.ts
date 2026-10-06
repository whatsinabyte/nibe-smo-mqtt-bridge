import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId, setMockValue } from './support/stack';

/**
 * A two-state status that isn't 0/1 is a sensor from the start.
 *
 * 2701 "Status (ACS)" has a boolean shape in the firmware (u8, no unit, input
 * register), but its known values are 3 = Passive and 7 = Active. As a
 * binary_sensor, the first real value would only get it reclassified (new
 * entity id, history lost, a warning in the log). The mock serves the
 * reference dump's 0 (no ACS installed), which a binary_sensor shows as off.
 */

const POINT = 2701;
const DUMP_VALUE = 0;

async function mqttPublish(token: string, topic: string, payload: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic, payload },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('ACS status (2701) is discovered as a sensor, not a binary_sensor', async () => {
  test.setTimeout(240_000);
  const token = readToken();
  await setMockValue(POINT, DUMP_VALUE);
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
    expect(id, `${POINT} was discovered as ${id}`).toMatch(/^sensor\./);
  } finally {
    if (enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(POINT));
    }
  }
});
