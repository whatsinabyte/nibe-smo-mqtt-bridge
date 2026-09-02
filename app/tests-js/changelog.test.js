import { describe, it, expect } from 'vitest';
import { createCard } from './support/create-card.js';
import { changelogHistoryPayload, changelogEntry, changelogUnreadPayload } from './support/fixtures.js';

// handleChangelogHistoryMessage is async — it awaits a real DecompressionStream
// read loop (node:stream/web), which needs more than a single microtask/macrotask
// tick to settle, and how long varies with machine load. A generous real
// timer is simpler and more robust here than guessing the exact tick count.
const flush = () => new Promise((r) => setTimeout(r, 100));

describe('nibe/browser/changelog/history (gzip)', () => {
  it('decompresses and stores the history on the happy path', async () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry()], total_entries: 50, unread_count: 1, seq: 1 })
    );
    await flush();

    expect(el.changelog).toHaveLength(1);
    expect(el.changelog[0].added[0].id).toBe(3755);
    expect(el.unreadChanges).toBe(1);
    expect(el.changelogCap).toBe(50);
    expect(el._lastChangelogSeq).toBe(1);
  });

  it('drops entries with neither added nor removed items', async () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry({ added: [], removed: [] })], seq: 1 })
    );
    await flush();
    expect(el.changelog).toHaveLength(0);
  });

  it('filters out malformed added/removed items but keeps valid ones', async () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({
        history: [changelogEntry({ added: [{ id: 1 }, null, { notAnId: true }, 42], removed: [] })],
        seq: 1,
      })
    );
    await flush();
    expect(el.changelog).toHaveLength(1);
    expect(el.changelog[0].added).toEqual([{ id: 1 }]);
  });

  it('skips a stale _seq relative to the last-applied one', async () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry({ id: 'a' })], seq: 5 })
    );
    await flush();
    expect(el._lastChangelogSeq).toBe(5);

    // Stale replay with an older seq must be ignored entirely.
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry({ id: 'b', added: [{ id: 999 }] })], seq: 3 })
    );
    await flush();
    expect(el._lastChangelogSeq).toBe(5);
    expect(el.changelog[0].id).toBe('a');
  });

  it('applies a fresher seq that arrives after', async () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry({ id: 'a' })], seq: 5 })
    );
    await flush();
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry({ id: 'b' })], seq: 6 })
    );
    await flush();
    expect(el._lastChangelogSeq).toBe(6);
    expect(el.changelog[0].id).toBe('b');
  });

  it('treats an empty payload as "clear the changelog"', async () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry()], seq: 1 })
    );
    await flush();
    expect(el.changelog).toHaveLength(1);

    harness.publish('nibe/browser/changelog/history', '');
    await flush();
    expect(el.changelog).toHaveLength(0);
  });

  it('does not throw on a gzip payload of malformed (non-JSON) content', async () => {
    const { el, harness } = createCard();
    // gzipPayload always JSON.stringifies, so simulate truly broken JSON by
    // hand-building the sentinel + gzip of raw invalid JSON text.
    const { gzipSync } = await import('node:zlib');
    const raw = 'gzip1:' + gzipSync(Buffer.from('{not valid json', 'utf-8')).toString('base64');
    expect(() => harness.publish('nibe/browser/changelog/history', raw)).not.toThrow();
    await flush();
    expect(el.changelog).toEqual([]);
  });

  it('does not throw on a non-gzip / garbage payload', async () => {
    const { el, harness } = createCard();
    expect(() => harness.publish('nibe/browser/changelog/history', 'not-even-gzip-prefixed')).not.toThrow();
    await flush();
    expect(el.changelog).toEqual([]);
  });

  it('does not throw on null payload', async () => {
    const { harness } = createCard();
    expect(() => harness.publish('nibe/browser/changelog/history', null)).not.toThrow();
    await flush();
  });

  it('refreshes the open changelog modal in place when fresh data arrives', async () => {
    const { el, harness } = createCard();
    el.showChangelog();
    expect(el._openModalId).toBe('changelog-modal');

    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry()], seq: 1 })
    );
    await flush();

    const content = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(content).toContain('Extra pump speed');
  });
});

describe('nibe/browser/changelog/unread', () => {
  it('updates the unread count and badge', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/changelog/unread', changelogUnreadPayload(3));
    expect(el.unreadChanges).toBe(3);
    const badge = el.shadowRoot.getElementById('show-changelog').querySelector('.change-badge');
    expect(badge.textContent).toBe('3');
  });

  it('caps the displayed badge at "99+"', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/changelog/unread', changelogUnreadPayload(150));
    const badge = el.shadowRoot.getElementById('show-changelog').querySelector('.change-badge');
    expect(badge.textContent).toBe('99+');
  });

  it('removes the badge when unread count drops to zero', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/changelog/unread', changelogUnreadPayload(3));
    harness.publish('nibe/browser/changelog/unread', changelogUnreadPayload(0));
    const badge = el.shadowRoot.getElementById('show-changelog').querySelector('.change-badge');
    expect(badge).toBeNull();
  });

  it('does not throw on empty/malformed/null payload', () => {
    const { harness } = createCard();
    expect(() => harness.publish('nibe/browser/changelog/unread', '')).not.toThrow();
    expect(() => harness.publish('nibe/browser/changelog/unread', '{bad')).not.toThrow();
    expect(() => harness.publish('nibe/browser/changelog/unread', null)).not.toThrow();
  });
});
