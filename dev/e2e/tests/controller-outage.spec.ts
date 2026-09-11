import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import { loginToHa, readToken } from './support/ha-login';

/**
 * The whole controller going away — the big sibling of
 * transient-absence.spec.ts. That spec withholds one point; this one
 * withholds all 1169, by stopping the mock API outright.
 *
 * Two things are asserted, and the second is the one that matters.
 *
 * 1. The bridge notices. `api_failure_threshold` (3 by default, and 3 in
 *    dev/e2e/bridge/options.json) consecutive failed bulk fetches flip the
 *    "API Reachable" management binary_sensor off — the diagnostic a user
 *    actually looks at when their heat pump entities stop moving.
 *
 * 2. **Nothing is destroyed.** An unreachable controller is not a controller
 *    that removed every one of its points. `_fetch_bulk_data` returns early
 *    on failure without touching `self.bulk_data`, so the cached values stay
 *    put, every entity keeps its last known state, and the absence handling
 *    in `_update_entity_state` is never reached. That is deliberate and it is
 *    load-bearing: if a failed fetch ever cleared or emptied `bulk_data`
 *    instead, every enabled point would look absent, and five minutes of
 *    downtime (`_ABSENT_GRACE_S`) would delete the user's entire entity set —
 *    the 4.13.12 incident multiplied by the whole register map. Nothing in
 *    the code says "do not clear bulk_data here", so this test says it.
 *
 * Note what is deliberately *not* asserted: that entities go `unavailable`.
 * They do not, by design — they hold their last value while the availability
 * topic stays `online`, and the API Reachable sensor is what reports the
 * outage. Asserting unavailability here would be asserting a behaviour the
 * bridge does not have.
 */

const HA_URL = process.env.HA_URL || 'http://localhost:18123';
const MOCK_API_CONTAINER = process.env.MOCK_API_CONTAINER || 'nibe-e2e-mock-api';

/** poll_interval and api_failure_threshold in dev/e2e/bridge/options.json. */
const POLL_INTERVAL_S = 15;
const FAILURE_THRESHOLD = 3;

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

async function fetchState(token: string, entityId: string): Promise<string> {
  return withTransientRetry(async () => {
    const ctx = await pwRequest.newContext();
    try {
      const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok()) return 'deleted';
      return (await resp.json()).state;
    } finally {
      await ctx.dispose();
    }
  });
}

/** Every entity this bridge owns — the set that must be intact afterwards. */
function bridgeEntityIds(states: Array<{ entity_id: string }>): string[] {
  return states
    .map((s) => s.entity_id)
    .filter((id) => /smo_s40|nibe/i.test(id))
    .sort();
}

test('the controller going unreachable is reported, and destroys nothing', async ({ page }) => {
  // Bounded by real poll intervals: enough failed polls to cross the
  // threshold, then a recovery poll.
  test.setTimeout(240_000);

  const token = readToken();

  await loginToHa(page);

  const before = await fetchStates(token);
  const entitiesBefore = bridgeEntityIds(before);
  expect(entitiesBefore.length, 'no bridge entities found to protect').toBeGreaterThan(1);

  const apiReachable = before.find((s) => /_api_reachable$/.test(s.entity_id));
  expect(apiReachable, 'the API Reachable management entity was not found').toBeTruthy();

  // Polled, not asserted once. The entity exists in HA as soon as its
  // retained discovery config is processed, but its *state* arrives in a
  // separate message, so immediately after the harness starts it can still
  // read `unavailable` — which then fails a test about outages for reasons
  // that have nothing to do with one.
  await expect
    .poll(async () => fetchState(token, apiReachable!.entity_id), {
      timeout: 60_000,
      message: 'API Reachable never reported healthy before the outage was simulated',
    })
    .toBe('on');

  // The controller drops off the network entirely.
  execSync(`docker stop ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });

  try {
    // 1. Reported: FAILURE_THRESHOLD consecutive failed polls, plus the
    // client's own retry-with-backoff on each one, so allow generous slack
    // over the nominal interval.
    await expect
      .poll(async () => fetchState(token, apiReachable!.entity_id), {
        timeout: (FAILURE_THRESHOLD + 3) * POLL_INTERVAL_S * 1000,
        message: 'API Reachable never went off while the controller was unreachable',
      })
      .toBe('off');

    // 2. Destroyed: nothing. Checked after the threshold has been crossed
    // and the bridge has had several full poll cycles to misbehave in.
    expect(
      bridgeEntityIds(await fetchStates(token)),
      'entities were removed from Home Assistant while the controller was merely unreachable'
    ).toEqual(entitiesBefore);
  } finally {
    // Always bring the controller back, even on failure — later specs in the
    // run share this stack.
    execSync(`docker start ${MOCK_API_CONTAINER}`, { stdio: 'ignore' });
  }

  // 3. Recovery is automatic: no restart, no user action.
  await expect
    .poll(async () => fetchState(token, apiReachable!.entity_id), {
      timeout: 4 * POLL_INTERVAL_S * 1000,
      message: 'API Reachable never came back on after the controller returned',
    })
    .toBe('on');

  // And the entity set is still exactly what it was before any of this.
  expect(bridgeEntityIds(await fetchStates(token))).toEqual(entitiesBefore);
});
