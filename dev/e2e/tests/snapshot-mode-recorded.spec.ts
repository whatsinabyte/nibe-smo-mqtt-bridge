import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { DEFAULT_BRIDGE_ENV, HA_URL, readRetained, recreateBridge } from './support/stack';

/**
 * A saved snapshot records the mode the bridge is running in.
 *
 * Saving read the mode from /data/applied_mode only, but the bridge's record
 * of its mode is the retained nibe/browser/applied_mode topic, with the file
 * as a fallback. Wherever the file was missing — a reinstalled add-on, or
 * this harness's freshly created container — every snapshot was saved with
 * mode "unknown", and restoring it could not tell whether the entity set
 * matched the running mode.
 */

const CMD_TOPIC = 'nibe/browser/snapshots/cmd';
const NAME = 'e2e-mode-recorded';

async function snapshotCmd(token: string, cmd: object): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic: CMD_TOPIC, payload: JSON.stringify(cmd) },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

function savedMode(): string | undefined {
  const raw = readRetained('nibe/browser/snapshots');
  return raw ? JSON.parse(raw).find((s: any) => s.name === NAME)?.mode : undefined;
}

test('a saved snapshot records the running mode', async () => {
  test.setTimeout(240_000);
  const token = readToken();
  // A fresh container: no /data/applied_mode, only the retained topic.
  await recreateBridge(DEFAULT_BRIDGE_ENV);
  const appliedMode = readRetained('nibe/browser/applied_mode');
  expect(appliedMode, 'precondition: no retained applied mode').toBeTruthy();

  try {
    await snapshotCmd(token, { action: 'save', name: NAME });
    await expect.poll(savedMode, { timeout: 30_000, message: 'precondition: snapshot not saved' }).toBeTruthy();
    expect(
      savedMode(),
      'the snapshot was saved with mode "unknown" although the bridge knows its mode'
    ).toBe(appliedMode);
  } finally {
    await snapshotCmd(token, { action: 'delete', name: NAME });
  }
});
