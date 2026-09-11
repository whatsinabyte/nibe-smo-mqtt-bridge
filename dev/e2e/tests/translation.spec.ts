import { test, expect, request as pwRequest } from '@playwright/test';
import { loginToHa, readToken } from './support/ha-login';

/**
 * Proves, against a real Home Assistant instance, that
 * nibe_entity_manager.py's hardcoded VALUE_MAPPINGS translation (added for
 * GitHub issue #39 — "value mappings are English-only, ignoring the
 * language setting") actually reaches a real published entity state, not
 * just the pytest suite's mocked EntityManager.
 *
 * This harness's bridge/options.json sets language: "nl" (see its own
 * comment for why the mock API's dump stays all_points_en.json regardless
 * — this isolates the bridge's OWN hardcoded-label translation from Nibe's
 * separate, server-side firmware description translation, which the mock
 * can't simulate anyway since it just replays one static dump verbatim).
 *
 * Point 3292 ("Operating mode Smart Price Adaption") is used: its raw
 * value in reference-dumps/all_points_en.json is 30, which
 * nibe_entity_detection.py's VALUE_MAPPINGS maps to the English label
 * "On" — translations/nl.yaml's value_mappings maps "On" -> "Aan". So a
 * real Dutch-configured bridge, polling this exact mock data, must
 * publish "Aan", not "On" and not "30", for this test to pass.
 */

const HA_URL = process.env.HA_URL || 'http://localhost:18123';

const POINT_ID = '3292';
const EXPECTED_TRANSLATED_STATE = 'Aan';

async function fetchStates(token: string): Promise<Array<{ entity_id: string; state: string }>> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const states = await resp.json();
  await ctx.dispose();
  return states;
}

async function fetchState(
  token: string,
  entityId: string
): Promise<{ entity_id: string; state: string } | null> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok()) {
    await ctx.dispose();
    return null;
  }
  const body = await resp.json();
  await ctx.dispose();
  return body;
}

/** Call HA's own select.select_option service — the exact call HA's
 * frontend makes when a user picks an option from a select entity's
 * dropdown. Used instead of driving the native HA more-info dialog's
 * shadow-DOM dropdown widget directly: this is the real integration
 * boundary (HA Core -> MQTT command topic -> the bridge), not a detail of
 * how HA happens to render the widget, which this project doesn't own. */
async function selectOption(token: string, entityId: string, option: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/select/select_option`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { entity_id: entityId, option },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('a hardcoded VALUE_MAPPINGS label is translated in a real HA entity state', async ({
  page,
}) => {
  const token = readToken();

  // 1. Log into the real HA frontend UI.
  await loginToHa(page);

  // 2. Navigate to the seeded Nibe Bridge dashboard / Entity Manager view.
  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });

  const searchInput = card.locator('#search-input');
  const before = await fetchStates(token);
  const beforeIds = new Set(before.map((s) => s.entity_id));

  // 3. Enable point 3292 through the real card (same round trip as
  // enable-entity.spec.ts: hass.callService -> real broker -> real bridge).
  await searchInput.fill(POINT_ID);
  const row = card.locator(`tr[data-id="${POINT_ID}"]`);
  await expect(row).toBeVisible({ timeout: 10_000 });

  const enableButton = row.locator('button[data-action="enable"]');
  if ((await enableButton.count()) > 0) {
    await enableButton.click();
    await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });
  }
  await searchInput.fill('');

  // 4. The real proof: poll HA's own REST API until a new sensor entity for
  // this point appears, and confirm its state is the translated Dutch
  // label, not the English one and not the raw integer.
  let translatedEntityId: string | null = null;
  await expect
    .poll(
      async () => {
        const after = await fetchStates(token);
        const candidate = after.find(
          (s) => !beforeIds.has(s.entity_id) && s.entity_id.startsWith('sensor.')
        );
        if (candidate && candidate.state === EXPECTED_TRANSLATED_STATE) {
          translatedEntityId = candidate.entity_id;
          return true;
        }
        return false;
      },
      {
        timeout: 60_000,
        message: `no new sensor.* entity reached state "${EXPECTED_TRANSLATED_STATE}"`,
      }
    )
    .toBeTruthy();

  expect(translatedEntityId).not.toBeNull();
});

/**
 * The select case is genuinely more complex than the sensor case above: a
 * select entity's published options list, its reported state, and its
 * write-back label-to-value lookup all have to agree on the same
 * translated strings (see nibe_discovery_config.build_select_config and
 * EntityManager._parse_command_payload's translated_reverse_map) — a bug
 * in any one of the three would show up differently (wrong dropdown
 * options published, state showing "1" instead of a label, or a selection
 * silently failing to write). This test exercises all three at once by
 * driving a real write through HA's own select.select_option service
 * (the same call HA's frontend makes when a user picks a dropdown option)
 * and confirming the state that comes back, after the mock API's
 * genuinely stateful write persists it and the bridge re-polls.
 *
 * Point 3751 ("Oper­ating mode") is used instead of 10614 (the actual
 * SG Ready point this translation feature was motivated by) because
 * 10614 doesn't exist in reference-dumps/all_points_en.json at all — it's
 * gated behind a separate "activate via API" point and only appears
 * dynamically once that's enabled (see
 * binary-sensor-reclassification.spec.ts for that dance). Point 3751 is a
 * plain, always-present select with two of its three states
 * ("Manual"/"Additional heat only") drawn from the same translated word
 * list, so it exercises the identical code path without that complexity.
 */
test('a select entity round-trips a translated write through a real HA select.select_option call', async ({
  page,
}) => {
  const token = readToken();

  await loginToHa(page);

  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });

  const searchInput = card.locator('#search-input');
  const before = await fetchStates(token);
  const beforeIds = new Set(before.map((s) => s.entity_id));

  const POINT_ID = '3751';
  await searchInput.fill(POINT_ID);
  const row = card.locator(`tr[data-id="${POINT_ID}"]`);
  await expect(row).toBeVisible({ timeout: 10_000 });

  const enableButton = row.locator('button[data-action="enable"]');
  if ((await enableButton.count()) > 0) {
    await enableButton.click();
    await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });
  }
  await searchInput.fill('');

  // Find the new select.* entity that appeared.
  let entityId: string | null = null;
  await expect
    .poll(
      async () => {
        const after = await fetchStates(token);
        const candidate = after.find(
          (s) => !beforeIds.has(s.entity_id) && s.entity_id.startsWith('select.')
        );
        if (candidate && candidate.state !== 'unavailable') {
          entityId = candidate.entity_id;
          return true;
        }
        return false;
      },
      { timeout: 60_000, message: 'no new available select.* entity appeared' }
    )
    .toBeTruthy();
  expect(entityId).not.toBeNull();

  // Raw value 0 ("Auto") is the dump's default — confirm the initial state
  // is the (untranslated, "Auto" is the same word in Dutch) label, proving
  // the entity is real and responding before attempting a write.
  const initial = await fetchState(token, entityId!);
  expect(initial?.state).toBe('Auto');

  // The real proof: write the *translated* Dutch label for "Manual" — this
  // is only accepted at all if the write-back path's translated_reverse_map
  // correctly maps it back to raw value 1, since "Handmatig" doesn't exist
  // anywhere in the untranslated English VALUE_MAPPINGS this bridge also
  // still checks as a fallback.
  await selectOption(token, entityId!, 'Handmatig');

  // Wait for the bridge's next poll to read back the mock API's now-mutated
  // raw value (confirmed genuinely persisted, not just optimistically
  // echoed) and re-publish it translated forward again — closing the full
  // write -> raw value -> re-read -> translate loop, not just the write
  // acceptance.
  await expect
    .poll(
      async () => {
        const state = await fetchState(token, entityId!);
        return state?.state === 'Handmatig';
      },
      {
        timeout: 60_000,
        message: `${entityId} never reached state "Handmatig" after selecting it`,
      }
    )
    .toBeTruthy();
});
