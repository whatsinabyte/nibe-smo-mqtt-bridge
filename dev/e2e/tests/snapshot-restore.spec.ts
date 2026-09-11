import { test, expect, request as pwRequest } from '@playwright/test';
import { loginToHa, readToken } from './support/ha-login';

/**
 * Snapshot save → change the selection → restore, driven entirely through
 * the real card and verified against real Home Assistant entities.
 *
 * Snapshots are the one feature that stores user-authored data the bridge
 * cannot reconstruct from the controller: a named set of enabled points,
 * kept in /data/snapshots.json and a retained MQTT topic. Nothing in this
 * suite exercised them, so neither the card's save/restore round trip nor
 * the resulting mass enable/disable had any end-to-end coverage.
 *
 * "merge" is used rather than "flush" deliberately. Flush restore disables
 * everything not in the snapshot, which would tear down entities the other
 * specs in this shared stack rely on; merge only adds, so this test can
 * prove restore works without side effects on its neighbours. The two modes
 * share the same restore path — they differ only in whether the current
 * selection is cleared first.
 */

const HA_URL = process.env.HA_URL || 'http://localhost:18123';

// Two ordinary always-present writable switches from the real dump.
const SNAPSHOT_POINT = '3871'; // "Climate system 3" — captured in the snapshot
const SNAPSHOT_NAME = `E2E ${Date.now()}`;

interface HaState {
  entity_id: string;
  state: string;
}

async function fetchStates(token: string): Promise<HaState[]> {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${HA_URL}/api/states`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const states = await resp.json();
  await ctx.dispose();
  return states;
}

test('a snapshot saved from the card restores the entity selection it captured', async ({
  page,
}) => {
  test.setTimeout(180_000);

  const token = readToken();

  await loginToHa(page);

  await page.goto('/nibe-bridge/entity-manager');
  const card = page.locator('nibe-entity-manager-card');
  await expect(card).toBeVisible({ timeout: 30_000 });
  const searchInput = card.locator('#search-input');

  const row = card.locator(`tr[data-id="${SNAPSHOT_POINT}"]`);

  // 1. Enable the point, so it is part of the selection the snapshot captures.
  const before = await fetchStates(token);
  const beforeIds = new Set(before.map((s) => s.entity_id));

  await searchInput.fill(SNAPSHOT_POINT);
  await expect(row).toBeVisible({ timeout: 10_000 });
  const enableButton = row.locator('button[data-action="enable"]');
  if ((await enableButton.count()) > 0) {
    await enableButton.click();
  }
  await expect(row.locator('.badge-enabled')).toBeVisible({ timeout: 30_000 });

  let entityId: string | null = null;
  await expect
    .poll(
      async () => {
        const after = await fetchStates(token);
        const candidate = after.find(
          (s) => !beforeIds.has(s.entity_id) && s.entity_id.startsWith('switch.')
        );
        if (candidate && candidate.state !== 'unavailable') {
          entityId = candidate.entity_id;
          return true;
        }
        return false;
      },
      { timeout: 30_000, message: `no new entity appeared for point ${SNAPSHOT_POINT}` }
    )
    .toBeTruthy();
  const target = entityId as unknown as string;
  await searchInput.fill('');

  // 2. Save the current selection as a named snapshot.
  await card.locator('#show-snapshots').click();
  const snapshotsModal = card.locator('#snapshots-modal');
  await expect(snapshotsModal).toBeVisible({ timeout: 10_000 });

  await card.locator('#snapshot-name-input').fill(SNAPSHOT_NAME);
  await card.locator('#snapshot-save-btn').click();

  // The saved snapshot must appear in the list — this is the round trip
  // back from the bridge (save → persist → republish → card re-render),
  // not just local UI state.
  const savedEntry = card.locator(`.snapshot-restore-btn[data-snap-name="${SNAPSHOT_NAME}"]`);
  await expect(savedEntry).toBeVisible({ timeout: 30_000 });

  await card.locator('#close-snapshots').click();
  await expect(snapshotsModal).toBeHidden({ timeout: 10_000 });

  // 3. Disable the point again, so restoring it is a real change rather
  // than a no-op that would pass whatever restore did.
  await searchInput.fill(SNAPSHOT_POINT);
  await expect(row).toBeVisible({ timeout: 10_000 });
  await row.locator('button[data-action="disable"]').click();
  await expect(row.locator('.badge-disabled')).toBeVisible({ timeout: 30_000 });
  await searchInput.fill('');

  await expect
    .poll(
      async () => {
        const states = await fetchStates(token);
        const found = states.find((s) => s.entity_id === target);
        return !found || found.state === 'unavailable';
      },
      { timeout: 60_000, intervals: [2_000], message: `${target} did not go away when disabled` }
    )
    .toBeTruthy();

  // 4. Restore the snapshot in merge mode (see the file header for why
  // merge rather than flush) and confirm the entity comes back.
  await card.locator('#show-snapshots').click();
  await expect(snapshotsModal).toBeVisible({ timeout: 10_000 });
  await card.locator(`.snapshot-restore-btn[data-snap-name="${SNAPSHOT_NAME}"]`).click();
  await card
    .locator(`.snapshot-do-restore[data-snap-name="${SNAPSHOT_NAME}"][data-mode="merge"]`)
    .click();

  await expect
    .poll(
      async () => {
        const states = await fetchStates(token);
        const found = states.find((s) => s.entity_id === target);
        return !!found && found.state !== 'unavailable';
      },
      {
        timeout: 90_000,
        intervals: [2_000],
        message: `${target} did not come back after restoring snapshot "${SNAPSHOT_NAME}"`,
      }
    )
    .toBeTruthy();

  // 5. Best-effort tidy-up. Deliberately not asserted: what this test
  // exists to prove — save, then restore — is already established above,
  // and deletion is incidental to it. The card re-renders the snapshot list
  // asynchronously after a restore, which made an assertion here fail
  // intermittently and report a *restore* failure that had not happened.
  // A test that cries wolf about the wrong thing is worse than one that
  // leaves a row behind.
  //
  // Nothing leaks between runs regardless: the snapshot name is unique per
  // run, run.sh starts from `docker compose down -v`, and /data lives in
  // the bridge container's own writable layer, so both are recreated.
  // Delete is worth covering properly one day — as its own spec, asserting
  // its own round trip, rather than bolted onto this one.
  try {
    if (!(await snapshotsModal.isVisible())) {
      await card.locator('#show-snapshots').click();
      await expect(snapshotsModal).toBeVisible({ timeout: 10_000 });
    }
    await card
      .locator(`.snapshot-delete-btn[data-snap-name="${SNAPSHOT_NAME}"]`)
      .click({ timeout: 10_000 });
  } catch {
    // Tidy-up only — never fail the test for this.
  }
});
