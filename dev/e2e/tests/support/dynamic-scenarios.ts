import { expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import {
  BRIDGE_CONTAINER,
  DEFAULT_BRIDGE_ENV,
  FAKE_SUPERVISOR_TOKEN,
  HA_URL,
  MQTT_CONTAINER,
  bridgeLogsSince,
  pointEntityId,
  recreateBridge,
  setMockValue,
} from './stack';

/**
 * Scenario helpers for dynamic-point learning: switches that show settings,
 * a REST API point list that lags behind its values, and a way to read what
 * the bridge learned. See mock_nibe_api.py (/mock-control/shows, /list,
 * /list-refresh, /list-snapshot, /fail) and docs/known-firmware-quirks.md.
 *
 * Two real switches from an SMO S40 log: SG Ready activation (10613) shows
 * 3260, 4694 and 10614; "Power at DOT, manual value" (6984) shows 6983.
 */

export const SG_READY = 10613;
export const SG_POINTS = [3260, 4694, 10614];
export const DOT = 6984;
export const DOT_POINTS = [6983];
export const SETPOINT = 32341; // a number, "Desired room temperature for zone 1"

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';

function holding(id: number, title: string, extra: Record<string, unknown>, value: number) {
  return {
    title,
    description: '',
    metadata: {
      variableId: id,
      variableType: 'integer',
      unit: '',
      shortUnit: '',
      modbusRegisterType: 'MODBUS_HOLDING_REGISTER',
      isWritable: true,
      divisor: 1,
      decimal: 0,
      change: 1,
      stringDefaultValue: '',
      ...extra,
    },
    value: { variableId: id, integerValue: value, stringValue: '', isOk: true },
  };
}

/** Definitions of the points the two switches show, none of which the
 * reference dump has (they were hidden when it was taken). 3260, 4694 and
 * 10614 are real (GitHub issue #40, as in sg-ready-dynamic-discovery.spec.ts);
 * 6983's title is from a real log, the rest of it a plausible number for
 * these tests, not a captured definition. */
export const DEFINITIONS: Record<number, unknown> = {
  3260: {
    title: 'Operating mode (SG Ready)',
    description: '',
    metadata: {
      variableId: 3260,
      variableType: 'integer',
      variableSize: 'u8',
      unit: '',
      modbusRegisterType: 'MODBUS_INPUT_REGISTER',
      shortUnit: '',
      isWritable: false,
      divisor: 1,
      decimal: 0,
      modbusRegisterID: 1911,
      minValue: 0,
      maxValue: 0,
      intDefaultValue: 0,
      change: 1,
      stringDefaultValue: '',
    },
    value: { variableId: 3260, integerValue: 10, stringValue: '', isOk: true },
  },
  4694: holding(4694, 'Cooling (SG Ready)', { variableSize: 'u8', modbusRegisterID: 761, minValue: 0, maxValue: 1, intDefaultValue: 1 }, 0),
  10614: holding(10614, 'Req. op. mode (SG Ready)', { variableSize: 'u8', modbusRegisterID: 6008, minValue: 0, maxValue: 3, intDefaultValue: 1 }, 1),
  6983: holding(6983, 'Power [kW] required for dimensioned outdoor temperature', { variableSize: 's16', unit: 'kW', shortUnit: 'kW', divisor: 10, decimal: 1, minValue: 0, maxValue: 1000, intDefaultValue: 0 }, 60),
};

/** POST to the mock's control channel; returns its JSON answer. */
export async function mock(path: string, data: unknown = {}): Promise<any> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${MOCK_API_URL}${path}`, { data });
  expect(resp.ok(), `${path}: ${resp.status()} ${await resp.text()}`).toBeTruthy();
  const body = await resp.json();
  await ctx.dispose();
  return body;
}

/** The list catches up to the current state. */
export const refreshList = () => mock('/mock-control/list-refresh');
/** Remember what the switches show now… */
export const snapshotList = () => mock('/mock-control/list-snapshot');
/** …and let the list catch up to that, even if a switch has changed since. */
export const refreshListFromSnapshot = () => mock('/mock-control/list-refresh', { from_snapshot: true });

export function mqttPublish(topic: string, payload: string): void {
  execSync(`docker exec ${MQTT_CONTAINER} mosquitto_pub -h localhost -t '${topic}' -m '${payload}'`);
}

/** Calls a Home Assistant service. */
export async function haCall(token: string, domain: string, service: string, data: object): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/${domain}/${service}`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data,
  });
  expect(resp.ok(), `${domain}.${service}: ${resp.status()}`).toBeTruthy();
  await ctx.dispose();
}

export async function haState(token: string, entityId: string): Promise<string | undefined> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = resp.ok() ? await resp.json() : null;
  await ctx.dispose();
  return body?.state;
}

/** Enables a point in the bridge (if needed) and returns its HA entity id. */
export async function entityFor(token: string, pointId: number): Promise<string> {
  let id = (await pointEntityId(token, pointId)) ?? '';
  if (!id) {
    mqttPublish('homeassistant/text/nibe_enable_entity/set', String(pointId));
    await expect
      .poll(async () => (id = (await pointEntityId(token, pointId)) ?? ''), {
        timeout: 60_000,
        message: `point ${pointId} never got an HA entity`,
      })
      .not.toBe('');
  }
  await expect
    .poll(() => haState(token, id), { timeout: 60_000, message: `${id} never became available` })
    .not.toMatch(/^(unavailable|unknown)?$/);
  return id;
}

export interface Learned {
  byValue: Record<string, number[]>;
  unprocessed: number[];
}

/** What the bridge has learned about a switch (its persisted map). */
export function learned(controller: number): Learned | undefined {
  const raw = execSync(`docker exec ${BRIDGE_CONTAINER} cat /data/dynamic_point_map.json`, {
    encoding: 'utf-8',
  });
  const entry = JSON.parse(raw)[String(controller)];
  if (!entry) return undefined;
  return {
    byValue: Object.fromEntries(
      Object.entries(entry.dynamic_points_by_value ?? {}).map(([v, pts]) => [
        v,
        [...(pts as number[])].sort((a, b) => a - b),
      ])
    ),
    unprocessed: [...(entry.unprocessed_values ?? [])].sort(),
  };
}

export async function waitForLog(since: string, text: string | RegExp, timeout = 60_000): Promise<void> {
  await expect
    .poll(() => {
      const logs = bridgeLogsSince(since);
      return typeof text === 'string' ? logs.includes(text) : text.test(logs);
    }, { timeout, intervals: [500], message: `bridge never logged ${text}` })
    .toBe(true);
}

/** The bridge sends Home Assistant notifications only through the
 * Supervisor; the harness's fake one forwards them. */
export const WITH_SUPERVISOR = { ...DEFAULT_BRIDGE_ENV, BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN };

/** A clean start for a scenario: both switches off, the points they show
 * defined, rules in place, the list lagging (caught up to now), a fresh
 * bridge (no scan window or late-arrival period left from before) with an
 * empty learned map, and both switches enabled in HA. */
export async function startScenario(
  token: string,
  bridgeEnv: Record<string, string> = DEFAULT_BRIDGE_ENV
): Promise<{ sg: string; dot: string }> {
  await mock('/mock-control/shows', { reset: true });
  for (const [id, definition] of Object.entries(DEFINITIONS)) {
    await mock(`/mock-control/points/${id}`, definition);
    await mock(`/mock-control/points/${id}`, { integerValue: (definition as any).value.integerValue });
    await mock(`/mock-control/hidden/${id}`, { hidden: false });
  }
  await setMockValue(SG_READY, 0);
  await setMockValue(DOT, 0);
  await mock('/mock-control/shows', { controller: SG_READY, value: 1, points: SG_POINTS });
  await mock('/mock-control/shows', { controller: DOT, value: 1, points: DOT_POINTS });
  await mock('/mock-control/list', { lagging: true });
  await refreshList();
  await recreateBridge(bridgeEnv);
  const since = new Date().toISOString();
  mqttPublish('homeassistant/button/nibe_flush_dynamic_map/press', 'PRESS');
  await waitForLog(since, 'Dynamic map flushed');
  return { sg: await entityFor(token, SG_READY), dot: await entityFor(token, DOT) };
}

/** Leaves the stack as other specs expect: no rules, no lag, switches off,
 * the shown points withheld as the dump has them. */
export async function endScenario(): Promise<void> {
  await mock('/mock-control/shows', { reset: true });
  await setMockValue(SG_READY, 0);
  await setMockValue(DOT, 0);
  for (const id of Object.keys(DEFINITIONS)) {
    await mock(`/mock-control/hidden/${id}`, { hidden: true });
  }
}
