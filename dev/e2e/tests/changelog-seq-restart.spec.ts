import { test, expect, request as pwRequest } from '@playwright/test';
import * as zlib from 'zlib';
import { readToken } from './support/ha-login';
import { DEFAULT_BRIDGE_ENV, HA_URL, readRetained, recreateBridge } from './support/stack';

/**
 * A restarted bridge continues the changelog sequence.
 *
 * The retained changelog history carries a `_seq`, and the card discards any
 * history whose `_seq` isn't above the last one it applied (it guards against
 * a stale retained replay). The bridge restarted that sequence at 1 in every
 * new process, so a card open across a restart — a dashboard left on a wall
 * tablet, say — ignored every changelog update until it was reloaded, or
 * until the new process happened to count past the old one.
 *
 * Asserted on what the bridge publishes: in this harness restarting any
 * container reloads the page (Colima re-syncs its port forwards and the
 * frontend reconnects), so a card can't actually stay open across the
 * restart here, and a freshly loaded card accepts whatever arrives. The
 * card's side — discarding a history whose `_seq` isn't newer — is covered
 * by the card's own unit tests (_isStaleChangelogSeq).
 *
 * Marking the changelog read always publishes a new history, so no dynamic
 * point change is needed to produce one.
 */

const HISTORY_TOPIC = 'nibe/browser/changelog/history';
const MARK_READ_TOPIC = 'homeassistant/button/nibe_mark_changes_read/press';

function retainedSeq(): number | null {
  const raw = readRetained(HISTORY_TOPIC);
  if (!raw) return null;
  const json = raw.startsWith('gzip1:')
    ? zlib.gunzipSync(Buffer.from(raw.slice('gzip1:'.length), 'base64')).toString('utf-8')
    : raw;
  const seq = JSON.parse(json)._seq;
  return typeof seq === 'number' ? seq : null;
}

async function markChangelogRead(token: string): Promise<void> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.post(`${HA_URL}/api/services/mqtt/publish`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { topic: MARK_READ_TOPIC, payload: 'PRESS' },
  });
  expect(resp.ok()).toBeTruthy();
  await ctx.dispose();
}

test('a restarted bridge continues the changelog sequence', async () => {
  // One bridge restart plus a few round trips.
  test.setTimeout(300_000);
  const token = readToken();

  // ── 1. Get the sequence to at least 2 ────────────────────────────────────
  // At 2 or more, a restarted sequence's first publish (1) is unambiguously
  // "not newer" to a card that saw the current one.
  for (let i = 0; i < 3 && (retainedSeq() ?? 0) < 2; i++) {
    const before = retainedSeq() ?? 0;
    await markChangelogRead(token);
    await expect.poll(() => retainedSeq() ?? 0, { timeout: 30_000 }).toBeGreaterThan(before);
  }
  const seqBeforeRestart = retainedSeq()!;
  expect(seqBeforeRestart).toBeGreaterThanOrEqual(2);

  // ── 2. Restart the bridge ───────────────────────────────────────────────
  await recreateBridge(DEFAULT_BRIDGE_ENV);

  // ── 3. The next history must continue the sequence ──────────────────────
  await markChangelogRead(token);
  await expect
    .poll(() => retainedSeq(), {
      timeout: 30_000,
      message:
        `the restarted bridge's changelog history did not continue the sequence past ` +
        `${seqBeforeRestart} — a card open across the restart discards it as stale`,
    })
    .toBeGreaterThan(seqBeforeRestart);
});
