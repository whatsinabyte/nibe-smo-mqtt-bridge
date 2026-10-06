import { test, expect, request as pwRequest } from '@playwright/test';
import { readToken } from './support/ha-login';
import { DEFAULT_BRIDGE_ENV, FAKE_SUPERVISOR_TOKEN, haWs, recreateBridge } from './support/stack';

/**
 * language: auto with Home Assistant set to Norwegian queries in "no".
 *
 * Home Assistant names Norwegian by its written standards ("nb" Bokmål, "nn"
 * Nynorsk) and other languages with region parts ("en-GB"); the bridge's
 * language list and translations use bare codes and "no". Auto-detection
 * passed HA's code through unchanged, so a Norwegian installation got no
 * translations file and English value labels.
 *
 * Sets HA's own language through its core config, starts the bridge in
 * "auto" behind the fake Supervisor (which serves HA's config), and reads the
 * language of the bridge's requests from the mock.
 */

const MOCK_API_URL = process.env.MOCK_API_URL || 'https://localhost:18443';

async function lastAcceptLanguage(): Promise<string | null> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.get(`${MOCK_API_URL}/mock-control/last-request`);
  const body = resp.ok() ? await resp.json() : {};
  await ctx.dispose();
  return body.accept_language ?? null;
}

test('auto language with Home Assistant in Norwegian (nb) queries in "no"', async () => {
  test.setTimeout(240_000);
  const token = readToken();
  const [config] = await haWs(token, [{ type: 'get_config' }]);
  const originalLanguage = config.result?.language ?? 'en';

  try {
    const [resp] = await haWs(token, [{ type: 'config/core/update', language: 'nb' }]);
    expect(resp.success, JSON.stringify(resp)).toBe(true);
    await recreateBridge({
      BRIDGE_OPTIONS: './bridge/options-auto-language.json',
      BRIDGE_SUPERVISOR_TOKEN: FAKE_SUPERVISOR_TOKEN,
    });
    await expect
      .poll(lastAcceptLanguage, {
        timeout: 60_000,
        message: "Home Assistant's \"nb\" reached the controller unchanged instead of the bridge's \"no\"",
      })
      .toBe('no');
  } finally {
    await haWs(token, [{ type: 'config/core/update', language: originalLanguage }]);
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
