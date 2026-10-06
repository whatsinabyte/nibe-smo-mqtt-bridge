import { test, expect, request as pwRequest } from '@playwright/test';
import { spawn } from 'child_process';
import { readToken } from './support/ha-login';
import {
  BRIDGE_CONTAINER,
  DEFAULT_BRIDGE_ENV,
  HA_URL,
  MQTT_CONTAINER,
  readRetained,
  recreateBridge,
} from './support/stack';

/**
 * A snapshot restore is refused in menus mode even where /data/applied_mode
 * is missing.
 *
 * Restoring into menus or all mode is blocked, since the mode re-applies and
 * overwrites the restored selection. The guard read the mode from the /data
 * file alone, which is missing wherever /data didn't survive — the mode then
 * comes from the retained nibe/browser/applied_mode topic — and the restore
 * went ahead. (The card disables Restore from the topic itself, so this is
 * about commands sent to the bridge directly.) A recreated container in this
 * harness is exactly that case.
 */

const MENUS_ENV = { BRIDGE_OPTIONS: './bridge/options-menus.json', BRIDGE_SUPERVISOR_TOKEN: '' };
const CMD_TOPIC = 'nibe/browser/snapshots/cmd';
const SNAPSHOT = 'e2e-restore-guard';

async function snapshotCmd(token: string, cmd: object): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic: CMD_TOPIC, payload: JSON.stringify(cmd) },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

/** Send a snapshot command and return the bridge's result message for it. */
async function snapshotCmdResult(token: string, cmd: object): Promise<any> {
  const sub = spawn('docker', [
    'exec', MQTT_CONTAINER, 'mosquitto_sub', '-t', 'nibe/browser/snapshots/result', '-C', '1', '-W', '30',
  ]);
  let out = '';
  sub.stdout.on('data', (d) => (out += d));
  const done = new Promise<void>((resolve) => sub.on('close', () => resolve()));
  await new Promise((r) => setTimeout(r, 1500)); // subscribed before the command
  await snapshotCmd(token, cmd);
  await done;
  return out.trim() ? JSON.parse(out.trim()) : null;
}

function snapshotNames(): string[] {
  const raw = readRetained('nibe/browser/snapshots');
  return raw ? JSON.parse(raw).map((s: any) => s.name) : [];
}

function hasModeFile(): boolean {
  try {
    const { execFileSync } = require('child_process');
    execFileSync('docker', ['exec', BRIDGE_CONTAINER, 'test', '-f', '/data/applied_mode']);
    return true;
  } catch {
    return false;
  }
}

test('a snapshot restore is refused in menus mode without the /data mode file', async () => {
  test.setTimeout(600_000);
  const token = readToken();

  try {
    // Applies menus mode, then a fresh container in the same mode: its mode
    // comes from the retained topic, and /data has no mode file.
    await recreateBridge(MENUS_ENV);
    await recreateBridge(MENUS_ENV);
    expect(readRetained('nibe/browser/applied_mode'), 'precondition: not in menus mode').toBe('menus');
    expect(hasModeFile(), 'precondition: the recreated container still has /data/applied_mode').toBe(false);
    // Saved now: snapshots live in /data too, which a recreate here discards.
    await snapshotCmd(token, { action: 'save', name: SNAPSHOT });
    await expect.poll(snapshotNames, { timeout: 30_000 }).toContain(SNAPSHOT);

    const result = await snapshotCmdResult(token, { action: 'restore', name: SNAPSHOT, mode: 'flush' });
    expect(result, 'precondition: no snapshot result from the bridge').not.toBeNull();
    expect(
      result.ok,
      `the bridge restored a snapshot in menus mode (${result.message}) — its guard read only ` +
        'the missing /data mode file'
    ).toBe(false);
    expect(result.message, 'refused, but not for the mode').toContain('menus');
  } finally {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
    await snapshotCmd(token, { action: 'delete', name: SNAPSHOT });
  }
});
