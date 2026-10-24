import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { HA_URL, pointEntityId, setMockValue } from './support/stack';

/**
 * AUX inputs are selects naming their function, over their whole range.
 *
 * Input AUX1 (3958) used to be a bare 0–69 number. It now carries the
 * function names an S1155 shows in menu 7.4 (GitHub issue #82): raw 20 must
 * read "SG ready A". A function nobody has named yet must stay a choice
 * under its number — the reference dump's 0 (no function assigned) reads
 * "0" — rather than making the select unavailable.
 */

const POINT = 3958;
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

async function haState(token: string, entityId: string): Promise<any> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = resp.ok() ? await resp.json() : null;
  await ctx.dispose();
  return body;
}

test('an AUX input is a select naming its function, unknown ones by number', async () => {
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
    expect(id, `point ${POINT} should be a select`).toMatch(/^select\./);

    for (const [raw, shown] of [
      [20, 'SG ready A'],
      [DUMP_VALUE, '0'],
    ] as [number, string][]) {
      await setMockValue(POINT, raw);
      await expect
        .poll(async () => (await haState(token, id))?.state, {
          timeout: 60_000,
          message: `${id} at raw ${raw} should show "${shown}"`,
        })
        .toBe(shown);
    }
    const options: string[] = (await haState(token, id))?.attributes?.options ?? [];
    expect(options.length, 'the select offers the whole 0–69 range').toBe(70);
  } finally {
    await setMockValue(POINT, DUMP_VALUE);
    if (enabledHere) {
      await mqttPublish(token, 'homeassistant/text/nibe_disable_entity/set', String(POINT));
    }
  }
});
