import { test, expect, request as pwRequest } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { loginToHa, readToken } from './support/ha-login';

/**
 * DOCS.md's "Entity attributes" section makes a concrete promise to users
 * writing templates and automations: every entity carries `point_id`,
 * `modbus_register` and `writable`, plus `default_value` and `description`
 * where the firmware provides them — and DOCS.md ships a worked template
 * example that reads `default_value` off a real entity.
 *
 * Those attributes travel as a separate retained JSON payload on the entity's
 * `json_attributes_topic`, published once at enable time. That is a different
 * MQTT message from both the discovery config and the state, so an entity can
 * be perfectly correct and still arrive in HA with no attributes at all — and
 * nothing downstream would notice, because a template reading a missing
 * attribute renders `None` rather than failing. Only a real HA instance
 * parsing the real retained payload can show that the promise holds.
 *
 * Values are checked against the firmware dump the mock replays, not merely
 * for presence: `point_id` must be the point actually enabled, and
 * `default_value` must be the factory default converted through the
 * register's own divisor, which is what "in display units" means in DOCS.md
 * and the only form that makes its example meaningful.
 */

const REFERENCE_DUMP = path.join(__dirname, '..', '..', '..', 'reference-dumps', 'all_points_en.json');
const HA_URL = process.env.HA_URL || 'http://localhost:18123';

/** Same sentinel/isOk filter as enable-entity.spec.ts's knownGoodPointIds(),
 * further narrowed to points that actually declare a factory default, so the
 * `default_value` half of the promise is exercised rather than skipped. */
function candidateIds(): number[] {
  const dump = JSON.parse(fs.readFileSync(REFERENCE_DUMP, 'utf-8'));
  const sentinels: Record<string, number> = {
    s16: -32768,
    u16: 65535,
    s32: -2147483648,
    u32: 4294967295,
  };
  const ids: number[] = [];
  for (const [idStr, point] of Object.entries<any>(dump)) {
    const meta = point?.metadata;
    if (!meta) continue;
    if (point?.value?.isOk === false) continue;
    if (meta.variableSize in sentinels && point?.value?.integerValue === sentinels[meta.variableSize])
      continue;
    if (meta.intDefaultValue === undefined || meta.intDefaultValue === null) continue;
    if (meta.modbusRegisterID === undefined || meta.modbusRegisterID === null) continue;
    ids.push(Number(idStr));
  }
  ids.sort((a, b) => a - b);
  return ids;
}

function dumpPoint(pointId: string): any {
  const dump = JSON.parse(fs.readFileSync(REFERENCE_DUMP, 'utf-8'));
  return dump[pointId];
}

/** The numeric half of `default_value`, mirroring `apply_divisor()`: a
 * `divisor` of 0 or absent is treated as 1 (a firmware quirk documented in
 * ARCHITECTURE.md §4.3), and otherwise the value is formatted to the decimal
 * places the divisor implies and then stripped of trailing zeros.
 *
 * Only the number is reproduced here, not the unit suffix. The unit the
 * bridge appends is the *resolved* one — run through `UNIT_OVERRIDES` and
 * `clean_unit()` — and reimplementing those tables in TypeScript would test
 * this file's copy of them rather than the bridge's, which is worse than not
 * checking at all. The unit is asserted structurally instead (see below). */
function expectedDefaultDisplay(meta: any): string {
  const divisor = meta.divisor || 1;
  const raw = meta.intDefaultValue;
  if (divisor === 1) return String(raw);
  const decimals = Math.max(0, Math.ceil(Math.log10(divisor)));
  return (raw / divisor).toFixed(decimals).replace(/0+$/, '').replace(/\.$/, '');
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

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

async function fetchAttributes(token: string, entityId: string): Promise<Record<string, any>> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states/${entityId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  await ctx.dispose();
  return body.attributes ?? {};
}

test('an enabled entity carries the attributes DOCS.md promises, with the documented values', async ({
  page,
}) => {
  test.setTimeout(180_000);

  const token = readToken();

  await loginToHa(page);

  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  const searchInput = card.locator('#search-input');

  const beforeIds = new Set((await fetchStates(token)).map((s) => s.entity_id));
  let pointId: string | null = null;
  let entityId: string | null = null;

  for (const candidate of candidateIds()) {
    const candidateId = String(candidate);
    await searchInput.fill(candidateId);
    const row = card.locator(`tr[data-id="${candidateId}"]`);
    if ((await row.count()) === 0) continue;
    await expect(row).toBeVisible({ timeout: 10_000 });

    const enableButton = row.locator('button[data-action="enable"]');
    if ((await enableButton.count()) === 0) continue;
    await enableButton.click();
    await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });
    await searchInput.fill('');

    try {
      await expect
        .poll(
          async () => {
            const after = await fetchStates(token);
            return after.some((s) => !beforeIds.has(s.entity_id) && s.state !== 'unavailable');
          },
          { timeout: 20_000 }
        )
        .toBe(true);
    } catch {
      continue;
    }

    entityId = (await fetchStates(token)).find(
      (s) => !beforeIds.has(s.entity_id) && s.state !== 'unavailable'
    )!.entity_id;
    pointId = candidateId;
    break;
  }

  expect(pointId, 'no candidate point produced a new available HA entity').not.toBeNull();

  const meta = dumpPoint(pointId!).metadata;

  // The attributes payload is a separate retained message from the state, so
  // it can land slightly after the entity first appears.
  await expect
    .poll(async () => (await fetchAttributes(token, entityId!)).point_id, {
      timeout: 30_000,
      message: `${entityId} never received its json_attributes payload`,
    })
    .toBe(pointId);

  const attributes = await fetchAttributes(token, entityId!);

  // Documented as "the numeric register ID used internally by the bridge and
  // the REST API" — so it must identify the point that was actually enabled,
  // which is what makes it usable for cross-referencing at all.
  expect(attributes.point_id).toBe(pointId);

  // Documented as "the Modbus register address — for cross-referencing with
  // installer documentation", so it must be the firmware's own
  // modbusRegisterID and not the point id, which for many registers differs.
  expect(attributes.modbus_register).toBe(String(meta.modbusRegisterID));

  // Documented as a boolean, and used directly in template conditions.
  expect(typeof attributes.writable).toBe('boolean');
  expect(attributes.writable).toBe(Boolean(meta.isWritable));

  // Documented as "factory default value in display units — e.g. 20 °C".
  // The substantive claim is "display units": the divisor-applied value, not
  // the raw register integer, which is what makes DOCS.md's worked example
  // mean anything. The number must match exactly, and be followed either by
  // nothing or by a space and a unit.
  const expectedDisplay = expectedDefaultDisplay(meta);
  expect(typeof attributes.default_value).toBe('string');
  expect(attributes.default_value).toMatch(
    new RegExp(`^${escapeRegex(expectedDisplay)}(?: \\S.*)?$`)
  );

  // And when the firmware declares a unit for the register, one is actually
  // appended rather than silently dropped — asserted by shape, since the
  // exact string is the bridge's resolved unit (see expectedDefaultDisplay).
  if (meta.unit) {
    expect(
      attributes.default_value.length,
      `default_value "${attributes.default_value}" carries no unit, but the firmware declares "${meta.unit}"`
    ).toBeGreaterThan(expectedDisplay.length);
  }

  // Documented as present "where provided" — so it is asserted only when the
  // firmware dump actually carries one, rather than demanding it always.
  const description = dumpPoint(pointId!).description;
  if (description) {
    expect(attributes.description).toBe(description);
  }
});
