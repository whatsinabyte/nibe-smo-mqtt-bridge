import { test, expect, request as pwRequest } from '@playwright/test';
import { execFileSync } from 'child_process';
import { DEFAULT_BRIDGE_ENV, HA_CONTAINER, recreateBridge } from './support/stack';

/**
 * A comment after a credential in secrets.yaml is not part of the credential.
 *
 * secrets.yaml is YAML, where a '#' after whitespace starts a comment — and
 * Home Assistant's own secrets file is where people put them. The bridge
 * read a bare value to the end of the line, so
 * `nibe_basic_auth: <token>  # controller login` sent the comment along in
 * the Authorization header and the controller refused it.
 *
 * Uses an options file without Nibe credentials, so the token from
 * secrets.yaml is the one in use, and the mock's record of the last request's
 * Authorization header.
 */

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';
const TOKEN = 'ZTJlOnNlY3JldHM='; // "e2e:secrets"

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

test('a trailing comment on a secrets.yaml credential is not sent along', async () => {
  test.setTimeout(240_000);
  // Home Assistant's /config is the volume the bridge reads as /homeassistant.
  haShell(`printf 'nibe_basic_auth: ${TOKEN}  # controller login\\n' > /config/secrets.yaml`);
  try {
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-secrets-auth.json',
      BRIDGE_SUPERVISOR_TOKEN: '',
    });
    await expect
      .poll(lastAuthorization, {
        timeout: 60_000,
        message: 'the bridge sent the secrets.yaml comment as part of its credentials',
      })
      .toBe(`Basic ${TOKEN}`);
  } finally {
    haShell('rm -f /config/secrets.yaml');
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
