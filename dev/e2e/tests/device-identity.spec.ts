import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import { loginToHa, readToken } from './support/ha-login';

/**
 * ARCHITECTURE.md §4.1's "device identity persistence": the HA device
 * identifier is derived from the controller's serial number, and persisted to
 * `/data/device_id` so that a later startup where the controller happens to be
 * unreachable reuses it instead of falling back to the generic config default.
 *
 * Without that, `device_id` flip-flops across restarts depending on whether
 * that particular startup's connection attempt succeeded, and every entity —
 * most visibly the Management device, which is published unconditionally at
 * every startup whether or not point discovery works — is recreated under a
 * different HA device identity. The old device is never cleaned up, because
 * the bridge has no way to know it was ever assigned. The user is left with an
 * accumulating pile of empty "ghost" devices sharing one display name, which
 * they have to notice and delete by hand.
 *
 * Two things make this worth an e2e test rather than a unit test. The failure
 * is only visible in HA's own registry — a second device, and entity_ids
 * suffixed `_2` where they collide with the originals. And the trigger is a
 * *combination* of conditions (a restart, while the controller is
 * unreachable, after an id has already been learned) that no mocked test
 * assembles end to end.
 *
 * The unreachable controller is real here, not simulated: the mock API
 * container is stopped, so even DNS resolution of its hostname fails, which
 * is what the bridge faces when the heat pump is off or off-network.
 */

const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const BRIDGE_CONTAINER = process.env.BRIDGE_CONTAINER || 'nibe-e2e-bridge';
const MOCK_API_CONTAINER = process.env.MOCK_API_CONTAINER || 'nibe-e2e-mock-api';
const MQTT_CONTAINER = process.env.MQTT_CONTAINER || 'nibe-e2e-mosquitto-1';

/** Retry a request that fails at the network layer.
 *
 * Both specs here stop and start containers, and Colima re-syncs its
 * published port forwards on every container event — so a request issued
 * moments afterwards can fail with `socket hang up` or
 * `net::ERR_EMPTY_RESPONSE` even though Home Assistant is perfectly healthy.
 * That is not what these specs are testing, and left unhandled it fails them
 * with an error that points at the wrong thing entirely. Only transport-level
 * failures are retried; an HTTP error response is passed straight through.
 */
async function withTransientRetry<T>(operation: () => Promise<T>): Promise<T> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 2000 * attempt));
    }
  }
  throw lastError;
}

async function fetchStates(token: string): Promise<Array<{ entity_id: string; state: string }>> {
  return withTransientRetry(async () => {
    const ctx = await pwRequest.newContext();
    try {
      const resp = await ctx.get(`${HA_URL}/api/states`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(resp.ok()).toBeTruthy();
      return await resp.json();
    } finally {
      await ctx.dispose();
    }
  });
}

function bridgeEntityIds(states: Array<{ entity_id: string }>): string[] {
  return states
    .map((s) => s.entity_id)
    .filter((id) => /smo_s40|nibe/i.test(id))
    .sort();
}

/** The management entities specifically — the ones published at every startup
 * regardless of discovery, and therefore the ones a changed device identity
 * would duplicate first. */
function managementEntityIds(states: Array<{ entity_id: string }>): string[] {
  return bridgeEntityIds(states).filter((id) => /management/i.test(id));
}

/** The `device.identifiers` in the bridge's own retained management discovery
 * config — what it tells Home Assistant this device *is*.
 *
 * This is the assertion that actually catches the bug, and finding that out
 * took three attempts against a deliberately broken build. Entity ids, the
 * entity's device association (`device_id()`) and that device's entity count
 * were all completely unchanged, because HA 2024.10 updates an
 * already-registered entity in place and ignores a discovery config that
 * names a different device. No second device was created, so even the device
 * registry looked clean.
 *
 * The wrong identity is nonetheless right there in the retained payload, and
 * that payload is what any *fresh* consumer sees — a new HA instance, a
 * re-added MQTT integration, or the same instance after the user deletes the
 * device to tidy up. That is when the duplicate appears. */
function retainedManagementDeviceIdentifiers(): string[] {
  const raw = execSync(
    `docker exec ${MQTT_CONTAINER} mosquitto_sub -h localhost ` +
      `-t 'homeassistant/binary_sensor/nibe_api_reachable/config' -C 1 -W 10`,
    { encoding: 'utf-8' }
  );
  return JSON.parse(raw).device.identifiers;
}

/** Read HA's device registry through the frontend's own authenticated
 * WebSocket connection — a secondary check, which would catch the fresh-HA
 * case above where a genuinely new device does get created. */
async function nibeDeviceCount(page: import('@playwright/test').Page): Promise<number> {
  return page.evaluate(async () => {
    const root = document.querySelector('home-assistant') as any;
    const hass = root?.hass;
    if (!hass?.connection) throw new Error('hass connection not available on this page');
    const devices: Array<{ name?: string; identifiers?: Array<[string, string]> }> =
      await hass.connection.sendMessagePromise({ type: 'config/device_registry/list' });
    return devices.filter((d) =>
      (d.identifiers ?? []).some(([, id]) => /^nibe_/i.test(id ?? ''))
    ).length;
  });
}

/** Evaluate a Jinja template through HA's own template API. */
async function renderTemplate(token: string, template: string): Promise<string> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/template`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { template },
  });
  expect(resp.ok(), `template API rejected: ${template}`).toBeTruthy();
  const body = (await resp.text()).trim();
  await ctx.dispose();
  return body;
}

function persistedDeviceId(): string {
  return execSync(`docker exec ${BRIDGE_CONTAINER} cat /data/device_id`, {
    encoding: 'utf-8',
  }).trim();
}

/** Restart the bridge and wait for readiness, matching only log lines emitted
 * after the restart so a previous run's "Bridge ready" cannot satisfy this.
 * Readiness is still reported when discovery fails — the bridge logs
 * "Bridge ready — 0 points" and keeps retrying in the poll loop — which is
 * exactly the state this test needs to inspect. */
async function restartBridgeAndWaitForReady(): Promise<void> {
  const since = new Date().toISOString();
  execSync(`docker restart ${BRIDGE_CONTAINER}`, { stdio: 'ignore' });
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const logs = execSync(`docker logs --since ${since} ${BRIDGE_CONTAINER} 2>&1 || true`, {
      encoding: 'utf-8',
    });
    if (logs.includes('Bridge ready')) return;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error('bridge did not report ready within 120s of being restarted');
}

test('restarting while the controller is unreachable does not create a duplicate HA device', async ({
  page,
}) => {
  test.setTimeout(240_000);

  const token = readToken();

  await loginToHa(page);

  // The id learned from the controller's serial on a healthy startup.
  const deviceIdBefore = persistedDeviceId();
  expect(deviceIdBefore, 'no device_id has been learned yet').toMatch(/^nibe_.+/);

  const statesBefore = await fetchStates(token);
  const managementBefore = managementEntityIds(statesBefore);
  expect(managementBefore.length, 'no management entities found').toBeGreaterThan(0);
  const entitiesBefore = bridgeEntityIds(statesBefore);
  const unavailableBefore = statesBefore.filter(
    (s) => /smo_s40|nibe/i.test(s.entity_id) && s.state === 'unavailable'
  ).length;

  // Anchor on one management entity and the HA device it currently belongs
  // to. Management entities are published unconditionally at every startup,
  // whether or not discovery succeeds, so they are the ones a changed
  // device_id re-parents first.
  const managementEntity = managementBefore[0];
  const deviceRegistryIdBefore = await renderTemplate(
    token,
    `{{ device_id("${managementEntity}") }}`
  );
  expect(deviceRegistryIdBefore, `${managementEntity} has no HA device`).toMatch(/^[0-9a-f]{8,}$/);
  const managementEntityCountBefore = Number(
    await renderTemplate(token, `{{ device_entities("${deviceRegistryIdBefore}") | count }}`)
  );
  expect(managementEntityCountBefore).toBeGreaterThan(0);

  // What the bridge is currently telling HA this device is.
  const identifiersBefore = retainedManagementDeviceIdentifiers();
  expect(identifiersBefore.length).toBeGreaterThan(0);
  expect(identifiersBefore.join(','), 'the management device identity is not serial-derived').toContain(
    deviceIdBefore
  );

  // The registry snapshot: how many HA devices this bridge currently owns.
  const deviceCountBefore = await nibeDeviceCount(page);
  expect(deviceCountBefore, 'no Nibe devices found in the HA device registry').toBeGreaterThan(0);

  // The controller is off / off-network, and the bridge restarts.
  execSync(`docker stop ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });
  try {
    await restartBridgeAndWaitForReady();

    // 1. The learned id is reused, not relearned and not replaced by the
    // generic default from the config.
    expect(
      persistedDeviceId(),
      'the persisted device_id changed on a startup that could not read the serial'
    ).toBe(deviceIdBefore);

    // 2. The observable consequence, asserted where it is actually visible:
    // HA's device registry. The management entities' unique_ids do not
    // include the device_id, so a changed identity does not duplicate the
    // *entities* — HA moves them to a newly created device and leaves the
    // original behind with nothing in it. That orphan is the "ghost device"
    // users are left deleting by hand, and it is invisible in /api/states.
    //
    // (Entity ids are no help here for a second reason: this firmware is full
    // of legitimately numbered registers — climate system 1, shunt 5, relay
    // ERS 1 — so a "collision suffix" heuristic on trailing digits flags
    // eight perfectly normal entities.)
    //
    // Primary check: the bridge is still publishing the same device identity.
    expect(
      retainedManagementDeviceIdentifiers(),
      'the bridge republished its management device under a different identity'
    ).toEqual(identifiersBefore);

    // Secondary: HA's registry gained nothing. Give it time to process the
    // republished discovery configs before concluding that.
    await page.goto('/');
    await expect
      .poll(async () => nibeDeviceCount(page), {
        timeout: 60_000,
        message:
          'a new HA device appeared — the bridge published under a different ' +
          'device identity, orphaning the original',
        intervals: [5_000],
      })
      .toBe(deviceCountBefore);

    // 3. The management entities are still attached to the device they were
    // on, and it still owns all of them.
    expect(
      await renderTemplate(token, `{{ device_id("${managementEntity}") }}`),
      `${managementEntity} was re-parented to a different HA device`
    ).toBe(deviceRegistryIdBefore);
    expect(
      Number(
        await renderTemplate(
          token,
          `{{ device_entities("${deviceRegistryIdBefore}") | count }}`
        )
      ),
      'the original HA device lost entities across an offline restart'
    ).toBe(managementEntityCountBefore);

    // And the management entity ids themselves are unchanged.
    expect(
      managementEntityIds(await fetchStates(token)),
      'the management entities changed identity across an offline restart'
    ).toEqual(managementBefore);

    // 4. Nothing was destroyed either. Point discovery failed, so the bridge
    // has no metadata for any point and cannot restore them — it must leave
    // their retained discovery configs alone rather than clearing them.
    // Clearing them would delete every entity in HA whenever the bridge
    // restarted while the heat pump was off.
    expect(
      bridgeEntityIds(await fetchStates(token)),
      'entities were removed from HA by a restart that could not reach the controller'
    ).toEqual(entitiesBefore);
  } finally {
    execSync(`docker start ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });
  }

  // 5. The controller comes back and the entities become live again on their
  // own, with no restart and no user action — driven by the wanted-points
  // reconcile after a successful bulk fetch.
  await expect
    .poll(
      async () => {
        const states = await fetchStates(token);
        const bridge = states.filter((s) => /smo_s40|nibe/i.test(s.entity_id));
        return bridge.filter((s) => s.state === 'unavailable').length;
      },
      {
        timeout: 180_000,
        message: 'entities never became available again after the controller returned',
      }
    )
    .toBeLessThanOrEqual(unavailableBefore);
});
