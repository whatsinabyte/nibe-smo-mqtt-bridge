// First: points this process at its stack (see stacks.ts).
import './stacks';
import { request as pwRequest } from '@playwright/test';
import { execFileSync, execSync } from 'child_process';
import * as path from 'path';

/**
 * Helpers for specs that reconfigure the running stack: recreating the
 * bridge or Home Assistant container with different settings, talking to
 * Home Assistant's WebSocket API, and reading/writing retained MQTT topics
 * straight on the broker.
 */

export const HA_URL = process.env.HA_URL || 'http://localhost:18123';
export const BRIDGE_CONTAINER = process.env.BRIDGE_CONTAINER || 'nibe-e2e-bridge';
export const HA_CONTAINER = process.env.HA_CONTAINER || 'nibe-e2e-homeassistant';
export const MQTT_CONTAINER = process.env.MQTT_CONTAINER || 'nibe-e2e-mosquitto-1';
export const FAKE_SUPERVISOR_TOKEN = 'e2e-fake-supervisor-token';
const E2E_DIR = path.join(__dirname, '..', '..');

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** The harness default: essential mode, no Supervisor. */
export const DEFAULT_BRIDGE_ENV = { BRIDGE_OPTIONS: './bridge/options.json', BRIDGE_SUPERVISOR_TOKEN: '' };

/** Recreate the bridge container with the given environment, and wait for
 * this start's own "Bridge ready" (not an earlier run's, still in the log).
 *
 * --no-deps matters: the bridge depends_on homeassistant, so without it
 * compose also reconciles Home Assistant against *this* command's
 * environment — and recreates it with the default configuration whenever a
 * spec is running it with HA_CONFIGURATION overridden. That silently undid
 * bridge-dashboard-recovery.spec.ts's setup and let it pass vacuously. */
export async function recreateBridge(env: Record<string, string>): Promise<void> {
  const since = new Date().toISOString();
  execSync('docker compose up -d --no-deps --force-recreate bridge', {
    cwd: E2E_DIR,
    env: { ...process.env, ...env },
    stdio: 'ignore',
  });
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    if (bridgeLogsSince(since).includes('Bridge ready')) return;
    await sleep(2000);
  }
  throw new Error('bridge did not report ready within 180s of being recreated');
}

/** Stop the bridge the way the Supervisor does — SIGTERM, then a grace
 * period long enough for its full shutdown sequence. */
export function stopBridge(): void {
  execSync('docker compose stop -t 60 bridge', { cwd: E2E_DIR, stdio: 'ignore' });
}

export function bridgeLogsSince(since: string): string {
  return execSync(`docker logs --since ${since} ${BRIDGE_CONTAINER} 2>&1 || true`, {
    encoding: 'utf-8',
    maxBuffer: 64 * 1024 * 1024,
  });
}

/** Whether the bridge's registry watcher has an entity mapped, judged from
 * its debug log since `since`. HA's create event carries no unique_id, so a
 * created entity is mapped only by the debounced refresh that event triggers;
 * an entity HA created before the watcher subscribed sends no event and is
 * mapped by the watcher's connect fetch instead. A late-processed event can
 * still follow the connect line, hence the few seconds' allowance. */
export function registryWatcherHasMapped(since: string, entityId: string): boolean {
  const lines = bridgeLogsSince(since).split('\n');
  const connectAt = lines.findIndex((l) => l.includes('WebSocket connected and subscribed'));
  if (connectAt < 0) return false;
  let createdAt = -1;
  lines.forEach((l, i) => {
    if (l.includes(`action=create, entity_id=${entityId}`)) createdAt = i;
  });
  if (createdAt >= 0) {
    return lines.slice(createdAt + 1).some((l) => l.includes('Registry refresh: updated'));
  }
  const connectTime = Date.parse(`${new Date().toISOString().slice(0, 10)}T${lines[connectAt].slice(0, 12)}Z`);
  return Number.isNaN(connectTime) || Date.now() - connectTime > 5000;
}

/** Recreate the Home Assistant container (same ha-config volume) with the
 * given environment, e.g. HA_CONFIGURATION, and wait until it reports
 * RUNNING — it accepts connections well before that (see run.sh). */
export async function recreateHa(token: string, env: Record<string, string>): Promise<void> {
  execSync('docker compose up -d --force-recreate homeassistant', {
    cwd: E2E_DIR,
    env: { ...process.env, ...env },
    stdio: 'ignore',
  });
  await sleep(5000);
  const deadline = Date.now() + 240_000;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${HA_URL}/api/config`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok && (await resp.json()).state === 'RUNNING') return;
    } catch {
      // Not accepting connections yet (or Colima re-syncing port forwards).
    }
    await sleep(3000);
  }
  throw new Error('Home Assistant did not reach RUNNING within 240s of being recreated');
}

/** Run Home Assistant WebSocket commands in order on one authenticated
 * connection, returning each command's result message. Retries the whole
 * sequence's connection a few times — Colima re-syncs port forwards when a
 * container starts or stops, which can drop the first connection. */
export async function haWs(token: string, commands: Record<string, unknown>[]): Promise<any[]> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      return await haWsOnce(token, commands);
    } catch (e) {
      lastError = e;
      await sleep(3000);
    }
  }
  throw lastError;
}

function haWsOnce(token: string, commands: Record<string, unknown>[]): Promise<any[]> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(HA_URL.replace(/^http/, 'ws') + '/api/websocket');
    const results: any[] = [];
    let nextId = 1;
    const timer = setTimeout(() => {
      ws.close();
      reject(new Error('HA WebSocket sequence timed out'));
    }, 30_000);
    const sendNext = () => {
      if (results.length === commands.length) {
        clearTimeout(timer);
        ws.close();
        resolve(results);
        return;
      }
      ws.send(JSON.stringify({ ...commands[results.length], id: nextId++ }));
    };
    ws.onmessage = (event) => {
      const msg = JSON.parse(String(event.data));
      if (msg.type === 'auth_required') {
        ws.send(JSON.stringify({ type: 'auth', access_token: token }));
      } else if (msg.type === 'auth_ok') {
        sendNext();
      } else if (msg.type === 'auth_invalid') {
        clearTimeout(timer);
        reject(new Error('HA WebSocket auth rejected'));
      } else if (msg.type === 'result' && msg.id === nextId - 1) {
        results.push(msg);
        sendNext();
      }
    };
    ws.onerror = () => {
      clearTimeout(timer);
      reject(new Error('HA WebSocket error'));
    };
  });
}

/** The retained payload on a topic, or null if nothing is retained there. */
export function readRetained(topic: string): string | null {
  try {
    const out = execFileSync(
      'docker',
      ['exec', MQTT_CONTAINER, 'mosquitto_sub', '-t', topic, '-C', '1', '-W', '3', '--retained-only'],
      { encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024 }
    );
    return out.replace(/\n$/, '');
  } catch {
    // mosquitto_sub exits non-zero when -W expires with nothing received.
    return null;
  }
}

/** Publish (or with an empty payload, clear) a retained message. Payload is
 * passed on stdin so its size and content never hit the command line. */
export function publishRetained(topic: string, payload: string): void {
  const args = ['exec', '-i', MQTT_CONTAINER, 'mosquitto_pub', '-r', '-t', topic];
  if (payload === '') {
    execFileSync('docker', [...args, '-n']);
  } else {
    execFileSync('docker', [...args, '-s'], { input: payload });
  }
}

/** Seconds since the epoch on Home Assistant's own clock (not the host's —
 * the Docker VM can drift), comparable with savedMenuDashboardMtime(). */
export function haNow(): number {
  return Number(execSync(`docker exec ${HA_CONTAINER} date +%s`, { encoding: 'utf-8' }).trim());
}

/** The saved Nibe Menus dashboard's mtime on Home Assistant's clock, or 0. */
export function savedMenuDashboardMtime(): number {
  try {
    const out = execSync(
      `docker exec ${HA_CONTAINER} sh -c 'stat -c %Y /config/.storage/lovelace.*menus* 2>/dev/null'`,
      { encoding: 'utf-8' }
    );
    return Number(out.trim().split('\n')[0]) || 0;
  } catch {
    return 0;
  }
}

/** The saved Nibe Menus dashboard's raw storage JSON, or '' if not saved. */
export function savedMenuDashboardText(): string {
  try {
    return execSync(
      `docker exec ${HA_CONTAINER} sh -c 'cat /config/.storage/lovelace.*menus* 2>/dev/null'`,
      { encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024 }
    );
  } catch {
    return '';
  }
}

/** All entity_ids referenced by the saved Nibe Menus dashboard, read from
 * Home Assistant's own storage (what the frontend renders), or null if it
 * hasn't been saved. */
export function savedMenuDashboardEntities(): Set<string> | null {
  let raw: string;
  try {
    raw = execSync(
      `docker exec ${HA_CONTAINER} sh -c 'cat /config/.storage/lovelace.*menus* 2>/dev/null'`,
      { encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024 }
    );
  } catch {
    return null;
  }
  if (!raw.trim()) return null;
  const entities = new Set<string>();
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(walk);
    } else if (node && typeof node === 'object') {
      const obj = node as Record<string, unknown>;
      if (typeof obj.entity === 'string') entities.add(obj.entity);
      Object.values(obj).forEach(walk);
    }
  };
  walk(JSON.parse(raw));
  return entities;
}

/** The entity_id Home Assistant gave a point's entity, looked up by the
 * bridge's unique_id (nibe_<point>), or undefined. Specs used to find an
 * entity as "the one that wasn't there before", which fails whenever an
 * earlier spec on the same stack left the point enabled. */
export async function pointEntityId(token: string, pointId: string | number): Promise<string | undefined> {
  const [resp] = await haWs(token, [{ type: 'config/entity_registry/list' }]);
  return (resp.result ?? []).find((e: any) => e.unique_id === `nibe_${pointId}`)?.entity_id;
}

/** Set a point's value in the mock controller (its control channel). */
export async function setMockValue(pointId: string | number, value: number): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${process.env.MOCK_API_URL}/mock-control/points/${pointId}`, {
    data: { integerValue: value },
  });
  await ctx.dispose();
  if (!resp.ok()) throw new Error(`mock-control/points/${pointId}: ${resp.status()}`);
}
