import { test, expect } from '@playwright/test';
import { readToken } from './support/ha-login';
import {
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  haWs,
  pointEntityId,
  publishRetained,
  recreateBridge,
} from './support/stack';

/**
 * After an update changes entity types, the user is told which entity ids
 * changed.
 *
 * A changed type is a new entity in Home Assistant, under a new id, so
 * automations using the old one stop working. This simulates an upgrade the
 * way the broker sees it: point 4 (outdoor temperature, a sensor) also has a
 * retained discovery config under `number`, as a release that published it as
 * a number would have left. On restart the bridge clears that domain and must
 * then send one "entity ids changed" notification listing number → sensor —
 * once the change has settled (60 s) and the registry knows the sensor's id.
 * Needs the fake Supervisor: notifications and the registry go through it.
 */

const POINT = 4;
const NOTIFICATION_ID = 'nibe_entity_ids_changed';
const WITH_SUPERVISOR = {
  BRIDGE_OPTIONS: './bridge/options.json',
  BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
};

async function notificationMessage(token: string): Promise<string> {
  const [resp] = await haWs(token, [{ type: 'persistent_notification/get' }]);
  const note = (resp.result ?? []).find((n: any) => n.notification_id === NOTIFICATION_ID);
  return note?.message ?? '';
}

test('an entity type changed by an update is reported old id → new id', async () => {
  test.setTimeout(360_000);
  const token = readToken();
  const sensorId = await pointEntityId(token, POINT);
  expect(sensorId, `precondition: point ${POINT} has no entity`).toMatch(/^sensor\./);
  await haWs(token, [{ type: 'persistent_notification/dismiss', notification_id: NOTIFICATION_ID }]);

  try {
    publishRetained(
      `homeassistant/number/nibe_${POINT}/config`,
      JSON.stringify({
        name: 'Old outdoor temperature',
        unique_id: `nibe_${POINT}`,
        command_topic: `nibe/test/number_${POINT}/set`,
        state_topic: `nibe/test/number_${POINT}/state`,
      })
    );
    await recreateBridge(WITH_SUPERVISOR);

    await expect
      .poll(() => notificationMessage(token), {
        timeout: 240_000,
        intervals: [10_000],
        message: 'no "entity ids changed" notification for the number → sensor change',
      })
      .toContain(`(point ${POINT}): \`number.`);
    expect(await notificationMessage(token)).toContain(`→ \`${sensorId}\``);
  } finally {
    publishRetained(`homeassistant/number/nibe_${POINT}/config`, '');
    await haWs(token, [{ type: 'persistent_notification/dismiss', notification_id: NOTIFICATION_ID }]);
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
