import { describe, it, expect, afterEach } from 'vitest';
import { createCard } from './support/create-card.js';
import {
  allMetadataPayload,
  changelogEntry,
  changelogHistoryPayload,
  dynamicChangePayload,
  enabledStatePayload,
  pointListPayload,
  sampleMetadataEntry,
  sampleSnapshot,
  snapshotsPayload,
} from './support/fixtures.js';

// Every topic the card subscribes to is writable by any MQTT client that can
// publish to the broker, and the card runs in the Home Assistant dashboard. A
// string from a payload that reaches innerHTML unescaped is script in that
// user's session. EVIL becomes an element if it is ever inserted as markup.
const EVIL = '"><img src=x onerror="window.__pwned=1">';
const injected = (el) => el.shadowRoot.querySelectorAll('img[src="x"]').length;

const evilEntry = (id) =>
  sampleMetadataEntry({
    id,
    title: EVIL,
    description: EVIL,
    unit: EVIL,
    unit_raw: EVIL,
    shortUnit: EVIL,
    category: EVIL,
    type: EVIL,
    variableType: EVIL,
    modbusRegisterType: EVIL,
  });

afterEach(() => {
  delete window.__pwned;
});

describe('hostile strings never become markup', () => {
  it('only integer point ids from the point list become entities', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/enabled_state', enabledStatePayload([EVIL, 5, 7]));
    harness.publish('nibe/browser/point_list', pointListPayload([EVIL, 5, '7', 7.5, null, {}, 6]));
    expect([...el.entities.keys()].sort()).toEqual([5, 6]);
    el.updateTable();
    el.renderMobileCards();
    expect(injected(el)).toBe(0);
  });

  it('strings in the metadata of a point are escaped in the table, cards and details', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/enabled_state', enabledStatePayload([5]));
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([evilEntry(5)]));
    harness.publish('nibe/browser/meta/5', JSON.stringify(evilEntry(5)));
    el.updateTable();
    el.renderMobileCards();
    el.showEntityDetails(5);
    expect(el.entities.size).toBeGreaterThan(0);
    expect(injected(el)).toBe(0);
    expect(window.__pwned).toBeUndefined();
  });

  it('strings in snapshots and the applied mode are escaped', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot({ name: EVIL, timestamp: EVIL, mode: EVIL, point_ids: [EVIL, 3945], point_count: EVIL })]));
    harness.publish('nibe/browser/applied_mode', EVIL);
    harness.publish('nibe/browser/snapshots/result', JSON.stringify({ ok: false, action: 'restore', message: EVIL }));
    el.showSnapshots();
    expect(injected(el)).toBe(0);
  });

  it('strings in dynamic changes and the changelog are escaped', async () => {
    const { el, harness } = createCard();
    const entry = changelogEntry({
      iso_timestamp: EVIL,
      source: EVIL,
      added: [{ id: EVIL, title: EVIL, type: EVIL }],
      removed: [{ id: EVIL, title: EVIL, type: EVIL }],
      triggered_by: { id: EVIL, title: EVIL },
    });
    harness.publish('nibe/browser/changelog/history', changelogHistoryPayload({ history: [entry] }));
    harness.publish(
      'nibe/browser/dynamic',
      dynamicChangePayload({ source: EVIL, triggered_by: { id: EVIL, title: EVIL }, added: [{ id: EVIL, title: EVIL, type: EVIL }] }),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    await el.showChangelog();
    expect(injected(el)).toBe(0);
  });
});
