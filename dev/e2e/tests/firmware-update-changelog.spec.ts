import { test, expect, request as pwRequest } from '@playwright/test';
import { execSync } from 'child_process';
import * as zlib from 'zlib';
import { BRIDGE_CONTAINER, readRetained, setMockValue } from './support/stack';

/**
 * A firmware update gets one entry in the card's changelog.
 *
 * The bridge records the version registers and the point list once the point
 * set has been stable for its 5-minute grace period (a restarting controller
 * serves a partial list for about a minute). Raising the SMO S40's version
 * register (2509) from 4.13 to 4.14 must then produce a `firmware_change`
 * history entry naming both versions. The same update raises the declared
 * maximum of 25154 (an energy log point, 0 in the firmware) to 100, and the
 * entry must list it as changed. Points added or removed by an update are
 * covered by the unit tests: withholding one here would restart the 5-minute
 * wait.
 */

const HISTORY_TOPIC = 'nibe/browser/changelog/history';
const VERSION_POINT = 2509;
const CHANGED_POINT = 25154;

async function setMockMetadata(pointId: number, metadata: Record<string, unknown>): Promise<void> {
  const ctx = await pwRequest.newContext({ ignoreHTTPSErrors: true });
  const resp = await ctx.post(`${process.env.MOCK_API_URL}/mock-control/points/${pointId}`, {
    data: { metadata },
  });
  await ctx.dispose();
  if (!resp.ok()) throw new Error(`mock-control/points/${pointId}: ${resp.status()}`);
}

function firmwareRecord(): { versions: Record<string, number> } | null {
  try {
    const raw = execSync(`docker exec ${BRIDGE_CONTAINER} cat /data/firmware_record.json`, {
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function firmwareEntries(): { note?: string; changed?: { id: number; changes: string }[] }[] {
  const raw = readRetained(HISTORY_TOPIC);
  if (!raw) return [];
  const json = raw.startsWith('gzip1:')
    ? zlib.gunzipSync(Buffer.from(raw.slice('gzip1:'.length), 'base64')).toString('utf-8')
    : raw;
  const history = JSON.parse(json).history ?? [];
  return history.filter((e: { source?: string }) => e.source === 'firmware_change');
}

// @slow: the bridge only records once the point set has been stable for 5 minutes.
test('a firmware update is recorded in the changelog', { tag: '@slow' }, async () => {
  test.setTimeout(600_000);

  let original: number | undefined;
  await expect
    .poll(() => (original = firmwareRecord()?.versions?.[String(VERSION_POINT)]), {
      timeout: 420_000,
      intervals: [15_000],
      message: 'precondition: the bridge never recorded the firmware versions',
    })
    .toBeDefined();

  const updated = original! + 1;
  const versionText = (raw: number) => `${(raw >> 8) & 0xff}.${raw & 0xff}`;
  const expectedNote = `${versionText(original!)} → ${versionText(updated)}`;
  const before = firmwareEntries().length;
  const newEntry = () =>
    firmwareEntries()
      .slice(0, firmwareEntries().length - before)
      .find((e) => (e.note ?? '').includes(expectedNote));
  try {
    await setMockMetadata(CHANGED_POINT, { maxValue: 100 });
    await setMockValue(VERSION_POINT, updated);
    await expect
      .poll(() => newEntry() !== undefined, {
        timeout: 120_000,
        message: `no firmware_change changelog entry noting ${expectedNote}`,
      })
      .toBe(true);
    const changed = newEntry()!.changed ?? [];
    expect(
      changed.find((c) => c.id === CHANGED_POINT)?.changes,
      `${CHANGED_POINT}'s raised maximum is not listed as changed`
    ).toBe('max 0 → 100');
  } finally {
    await setMockMetadata(CHANGED_POINT, { maxValue: 0 });
    await setMockValue(VERSION_POINT, original!);
  }
});
