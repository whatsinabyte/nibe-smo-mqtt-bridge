import { test, expect, request as pwRequest } from '@playwright/test';
import { execFileSync } from 'child_process';
import { DEFAULT_BRIDGE_ENV, HA_CONTAINER, recreateBridge } from './support/stack';

/**
 * Nibe credentials entered in the add-on UI win over a token in secrets.yaml.
 *
 * DOCS.md: secrets.yaml "only fills in credential fields left blank in the
 * app configuration UI". A nibe_basic_auth token there used to win over the
 * UI's username and password, so someone who once used secrets.yaml and
 * later entered new credentials in the UI kept sending the old token.
 *
 * The harness's options carry dev/dev; secrets.yaml gets a different token.
 * The mock records the Authorization header of the bridge's last request.
 */

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const UI_AUTH = `Basic ${Buffer.from('dev:dev').toString('base64')}`;
const SECRETS_TOKEN = 'c3RhbGU6dG9rZW4='; // "stale:token"

function haShell(script: string): void {
  execFileSync('docker', ['exec', HA_CONTAINER, 'sh', '-c', script]);
}

async function lastAuthorization(): Promise<string | null> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.get(`${MOCK_API_URL}/mock-control/last-request`);
  const body = resp.ok() ? await resp.json() : {};
  await ctx.dispose();
  return body.authorization ?? null;
}

test('UI credentials win over a secrets.yaml token', async () => {
  test.setTimeout(240_000);
  haShell(`printf 'nibe_basic_auth: "${SECRETS_TOKEN}"\\n' > /config/secrets.yaml`);
  try {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
    await expect
      .poll(lastAuthorization, {
        timeout: 60_000,
        message: 'the bridge sent the secrets.yaml token although the UI has credentials',
      })
      .toBe(UI_AUTH);
  } finally {
    haShell('rm -f /config/secrets.yaml');
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
