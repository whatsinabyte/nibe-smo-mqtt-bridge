import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import { loginToHa, readToken } from './support/ha-login';

/**
 * ARCHITECTURE.md §3's hardest invariant: **no exception may escape an MQTT
 * callback**.
 *
 * paho is configured with its default `suppress_exceptions=False`, and its
 * network thread is also its reconnect loop. An exception escaping any
 * callback therefore does not merely drop one message — it kills the thread
 * that both receives messages and re-establishes the connection. The process
 * keeps running, the container stays healthy, the logs go quiet, and the
 * bridge never responds to anything again. Every handler is consequently
 * wrapped by `_guard_callback()` in nibe_ha_integration.py.
 *
 * A real broker is the only place that invariant means anything: with a
 * mocked client, a handler that raises just propagates into the test. So this
 * spec fires deliberately malformed payloads — the shapes that actually reach
 * these topics when a card version drifts, an automation templates a value
 * wrong, or a user types into the management text entity — at every subscribed
 * management topic, and then proves the bridge is *still processing MQTT*.
 *
 * That last part is the whole test. Asserting the container is still up would
 * prove nothing: the failure mode leaves it up. The only honest proof is that
 * a subsequent, valid command sent over the same connection still takes
 * effect, so this ends by enabling a real entity over MQTT and watching it
 * appear in Home Assistant.
 */

const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const BRIDGE_CONTAINER = process.env.BRIDGE_CONTAINER || 'nibe-e2e-bridge';
const MQTT_CONTAINER = process.env.MQTT_CONTAINER || 'nibe-e2e-mosquitto-1';

const HA_BASE = 'homeassistant';
const NIBE_PREFIX = 'nibe/browser';

async function fetchStates(token: string): Promise<Array<{ entity_id: string; state: string }>> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const states = await resp.json();
  await ctx.dispose();
  return states;
}

/** Publish from inside the broker container — mosquitto_pub ships in the
 * eclipse-mosquitto image, so nothing needs installing on the host. */
function publish(topic: string, payload: string): void {
  const escaped = payload.replace(/'/g, `'\\''`);
  execSync(
    `docker exec ${MQTT_CONTAINER} mosquitto_pub -h localhost -t '${topic}' -m '${escaped}'`,
    { stdio: 'ignore' }
  );
}

/** Publish raw bytes that are not valid UTF-8 — the classic way to crash a
 * handler that calls .decode() without errors=. */
function publishInvalidUtf8(topic: string): void {
  execSync(
    `docker exec ${MQTT_CONTAINER} sh -c "printf '\\377\\376\\000bad' > /tmp/bad.bin && ` +
      `mosquitto_pub -h localhost -t '${topic}' -f /tmp/bad.bin"`,
    { stdio: 'ignore' }
  );
}

test('malformed payloads on every management topic cannot stop the bridge processing MQTT', async ({
  page,
}) => {
  test.setTimeout(180_000);

  const token = readToken();

  await loginToHa(page);

  // Find a point that is genuinely still disabled, so that enabling it later
  // is an observable change rather than a no-op. Any disabled point will do —
  // whether its register currently reports a usable value is irrelevant here,
  // and asserting on availability would make this spec fail for reasons that
  // have nothing to do with the invariant (the first disabled row in this
  // dump is point 5, one of the not-connected accessory registers the other
  // specs filter out; its entity is created correctly and is unavailable).
  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  const disabledRow = card.locator('tr[data-id]:has(button[data-action="enable"])').first();
  await expect(disabledRow).toBeVisible({ timeout: 30_000 });
  const victimPointId = await disabledRow.getAttribute('data-id');
  expect(victimPointId, 'no disabled point available to enable over MQTT').toBeTruthy();

  const beforeIds = new Set((await fetchStates(token)).map((s) => s.entity_id));

  // Every subscribed topic (nibe_ha_integration.py's _subscribe_all), fed
  // what it least expects. None of these should do anything; all of them
  // must be survivable.
  const garbage: Array<[string, string]> = [
    // Point-id text entities: not a number, out of range, negative, empty.
    [`${HA_BASE}/text/nibe_enable_entity/set`, 'not-a-number'],
    [`${HA_BASE}/text/nibe_enable_entity/set`, '99999999999999999999'],
    [`${HA_BASE}/text/nibe_disable_entity/set`, '-1'],
    [`${HA_BASE}/text/nibe_disable_entity/set`, ''],
    // Mode select / switch: values outside their option lists.
    [`${HA_BASE}/select/nibe_smart_mode/set`, 'definitely not a mode'],
    [`${HA_BASE}/switch/nibe_aid_mode/set`, 'MAYBE'],
    // Buttons: payload is irrelevant to them, which is exactly why an
    // unexpected one must not matter.
    [`${HA_BASE}/button/nibe_force_poll/press`, '{"unexpected":"object"}'],
    [`${HA_BASE}/button/nibe_reset_alarms/press`, ''],
    [`${HA_BASE}/button/nibe_mark_changes_read/press`, '[]'],
    // Snapshot command channel: malformed JSON, right JSON but wrong shape,
    // and a known action with a missing argument.
    [`${NIBE_PREFIX}/snapshots/cmd`, '{"action":'],
    [`${NIBE_PREFIX}/snapshots/cmd`, '[1,2,3]'],
    [`${NIBE_PREFIX}/snapshots/cmd`, '"just a string"'],
    [`${NIBE_PREFIX}/snapshots/cmd`, '{"action":"restore"}'],
    [`${NIBE_PREFIX}/snapshots/cmd`, '{"action":"no_such_action","name":"x"}'],
  ];

  for (const [topic, payload] of garbage) {
    publish(topic, payload);
  }
  publishInvalidUtf8(`${HA_BASE}/text/nibe_enable_entity/set`);
  publishInvalidUtf8(`${NIBE_PREFIX}/snapshots/cmd`);

  // Necessary but nowhere near sufficient — the failure mode this guards
  // against leaves the process running.
  const running = execSync(
    `docker inspect -f '{{.State.Running}}' ${BRIDGE_CONTAINER}`,
    { encoding: 'utf-8' }
  ).trim();
  expect(running, 'the bridge container died outright').toBe('true');

  // The real assertion: MQTT still gets through. If any of those payloads had
  // escaped its handler, paho's network thread — and with it the reconnect
  // loop — would be gone, and this command would vanish without a trace. A
  // new entity reaching HA proves the command was received, acted on, and
  // published back out over the same connection.
  publish(`${HA_BASE}/text/nibe_enable_entity/set`, victimPointId!);

  await expect
    .poll(
      async () => {
        const after = await fetchStates(token);
        return after.some((s) => !beforeIds.has(s.entity_id));
      },
      {
        timeout: 60_000,
        message:
          `enabling point ${victimPointId} over MQTT had no effect after the malformed ` +
          `payloads — the bridge is most likely still running but no longer receiving`,
      }
    )
    .toBe(true);
});
