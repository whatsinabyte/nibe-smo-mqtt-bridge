import { test, expect } from '@playwright/test';
import { execSync } from 'child_process';
import { readToken } from './support/ha-login';
import { HA_URL, MQTT_CONTAINER, publishRetained, readRetained } from './support/stack';

/**
 * The bridge puts its retained state back when the broker has lost it.
 *
 * Retained messages survive a broker restart only if the broker persists
 * them — this harness's Mosquitto doesn't (persistence false, Mosquitto's
 * own default), and even a persisting broker saves only periodically, so a
 * crash loses recent changes. The bridge treats its retained discovery
 * configs as the record of which entities exist, and reconnecting used to
 * restore only subscriptions, availability and states: the card went empty,
 * entities vanished at Home Assistant's next restart, and the bridge's own
 * next restart found no configs, took it for a fresh install and re-applied
 * the mode.
 *
 * Two triggers: the bridge reconnecting to a restarted broker, and Home
 * Assistant's MQTT birth message ("online" on homeassistant/status) when it
 * starts — HA's documented convention for discovery publishers.
 */

const CONFIG_TOPIC = 'homeassistant/sensor/nibe_4/config';
const E2E_DIR = `${__dirname}/..`;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function waitForHaRunning(token: string): Promise<void> {
  const deadline = Date.now() + 240_000;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${HA_URL}/api/config`, { headers: { Authorization: `Bearer ${token}` } });
      if (resp.ok && (await resp.json()).state === 'RUNNING') return;
    } catch {
      // not up yet
    }
    await sleep(3000);
  }
  throw new Error('Home Assistant did not reach RUNNING');
}

test.describe.configure({ mode: 'serial' });

test('a restarted broker gets the discovery configs and card topics back', async () => {
  test.setTimeout(300_000);
  expect(readRetained(CONFIG_TOPIC), 'precondition: point 4 has no retained config').toBeTruthy();

  // persistence false: the restarted broker has no retained messages at all.
  execSync(`docker restart ${MQTT_CONTAINER}`, { stdio: 'ignore' });

  for (const topic of [CONFIG_TOPIC, 'nibe/browser/point_list', 'nibe/browser/enabled_state', 'nibe/browser/applied_mode']) {
    await expect
      .poll(() => readRetained(topic), {
        timeout: 60_000,
        message:
          `the bridge reconnected to a broker that lost its retained messages, but never ` +
          `republished ${topic}`,
      })
      .not.toBeNull();
  }
});

test("Home Assistant's birth message gets a missing discovery config back", async () => {
  test.setTimeout(400_000);
  const token = readToken();

  try {
    execSync('docker compose stop homeassistant', { cwd: E2E_DIR, stdio: 'ignore' });
    // The broker loses the config while HA is down (and the bridge stays
    // connected, so no reconnect republishes it).
    publishRetained(CONFIG_TOPIC, '');
    expect(readRetained(CONFIG_TOPIC)).toBeNull();
    execSync('docker compose start homeassistant', { cwd: E2E_DIR, stdio: 'ignore' });
    await waitForHaRunning(token);

    await expect
      .poll(() => readRetained(CONFIG_TOPIC), {
        timeout: 60_000,
        message:
          'Home Assistant came online, but the bridge did not republish the discovery config ' +
          'missing from the broker',
      })
      .not.toBeNull();
  } finally {
    execSync('docker compose start homeassistant', { cwd: E2E_DIR, stdio: 'ignore' });
    await waitForHaRunning(token);
  }
});
