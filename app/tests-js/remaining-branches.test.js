// Branches the feature suites left open: fallback values for incomplete
// payloads, the less common paths through each handler, and the card staying
// usable when an element it looks up isn't in its DOM.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { createCard } from './support/create-card.js';
import {
  allMetadataPayload,
  sampleMetadataEntry,
  enabledStatePayload,
  dynamicChangePayload,
  gzipPayload,
} from './support/fixtures.js';

afterEach(() => {
  vi.useRealTimers();
});

const NibeEntityManager = () => customElements.get('nibe-entity-manager-card');
const $ = (el, id) => el.shadowRoot.getElementById(id);

function seeded(entries = [sampleMetadataEntry({ id: 1 })], enabled = []) {
  const { el, harness } = createCard();
  harness.publish('nibe/browser/all_metadata', allMetadataPayload(entries));
  harness.publish('nibe/browser/enabled_state', enabledStatePayload(enabled));
  return { el, harness };
}

describe('card build and setup', () => {
  it('a build written over the placeholder is the build; the placeholder is none', () => {
    expect(NibeEntityManager()._cardBuildFrom('3f2a9c1b7e44')).toBe('3f2a9c1b7e44');
    expect(NibeEntityManager()._cardBuildFrom('__NIBE_CARD_BUILD__')).toBeNull();
  });

  it('setConfig without a config keeps the current one', () => {
    const { el } = createCard({ config: { title: 'Nibe' } });
    el.setConfig(undefined);
    expect(el.config.title).toBe('Nibe');
    expect(el.shadowRoot.querySelector('h1.title').textContent).toBe('Nibe');
  });

  it('a second hass assignment does not subscribe again', () => {
    const { el, harness } = createCard();
    const before = harness.subscriptions.length;
    el.hass = harness.hass;
    expect(harness.subscriptions.length).toBe(before);
  });

  it('a subscription that resolved to something other than a function is just dropped', async () => {
    const { el } = createCard();
    el.mqttSubscriptions = [Promise.resolve(undefined), Promise.reject(new Error('x'))];
    el.cleanupSubscriptions();
    await Promise.resolve();
    expect(el.mqttSubscriptions).toEqual([]);
  });

  it('subscribing without hass does nothing', () => {
    const { el } = createCard({ skipHass: true });
    el.setupMqttSubscriptions();
    expect(el.mqttSubscriptions).toEqual([]);
  });

  it('Fuse is loaded once', async () => {
    const { el } = createCard();
    el._fuseLoaded = true;
    const append = vi.spyOn(document.head, 'appendChild');
    await el._loadFuse();
    expect(append).not.toHaveBeenCalled();
  });

  it('a loaded Fuse script builds the index', async () => {
    const { el } = createCard({ skipHass: true });
    const append = vi.spyOn(document.head, 'appendChild').mockImplementation((script) => {
      script.onload();
      return script;
    });
    const rebuild = vi.spyOn(el, '_rebuildFuseIndex');
    await el._loadFuse();
    expect(el._fuseLoaded).toBe(true);
    expect(rebuild).toHaveBeenCalled();
    append.mockRestore();
  });

  it('reloading reloads the page', () => {
    const { el } = createCard();
    const original = window.location;
    const reload = vi.fn();
    Object.defineProperty(window, 'location', { configurable: true, value: { reload } });
    try {
      el._reloadPage();
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original });
    }
    expect(reload).toHaveBeenCalledOnce();
  });

  it('a card_version message without a text payload counts as an empty version', () => {
    const { el } = createCard();
    const action = vi.spyOn(NibeEntityManager(), '_staleCardAction');
    el.handleCardVersionMessage({ payload: null });
    expect(action).toHaveBeenCalledWith(el._cardBuild, '', expect.any(Boolean));
  });

  it('the stale-card notice needs its element', () => {
    const { el } = createCard();
    $(el, 'stale-card-notice').remove();
    el._showStaleCardNotice();
    expect(el._staleCard).toBe(true);
  });
});

describe('view state restored by setConfig', () => {
  it('keeps a descending sort and an open snapshots modal', () => {
    const { el } = seeded();
    el.sortField = 'title';
    el.sortAscending = false;
    el.showSnapshots();
    el.setConfig({});
    expect($(el, 'mobile-sort-filter').value).toBe('title-desc');
    expect($(el, 'snapshots-modal').classList.contains('show')).toBe(true);
  });

  it('survives a skeleton without the mobile filter panel', () => {
    const { el } = seeded();
    const render = el.render.bind(el);
    vi.spyOn(el, 'render').mockImplementation(() => {
      render();
      $(el, 'mobile-filter-panel').remove();
      $(el, 'mobile-filter-indicator').remove();
    });
    expect(() => el.setConfig({})).not.toThrow();
  });
});

describe('mobile filter panel', () => {
  function withoutPanel() {
    const { el } = seeded();
    // Out of reach by id, its buttons still in place.
    $(el, 'mobile-filter-panel').removeAttribute('id');
    $(el, 'mobile-filter-indicator').removeAttribute('id');
    return el;
  }

  it('toggle, apply and clear work without the panel element', () => {
    const el = withoutPanel();
    $(el, 'mobile-filter-toggle').click();
    expect(el.showMobileFilters).toBe(true);
    $(el, 'mobile-apply-filters').click();
    expect(el.showMobileFilters).toBe(false);
    $(el, 'mobile-filter-toggle').click();
    $(el, 'mobile-clear-filters').click();
    expect(el.showMobileFilters).toBe(false);
  });

  it('sorts by type from the mobile sort list', () => {
    const { el } = seeded();
    $(el, 'mobile-sort-filter').value = 'type-desc';
    $(el, 'mobile-apply-filters').click();
    expect([el.sortField, el.sortAscending]).toEqual(['type', false]);
  });

  it('an unknown sort value leaves the sort as it was', () => {
    const { el } = seeded();
    el.sortField = 'title';
    const select = $(el, 'mobile-sort-filter');
    const option = document.createElement('option');
    option.value = 'bogus-asc';
    select.appendChild(option);
    select.value = 'bogus-asc';
    $(el, 'mobile-apply-filters').click();
    expect(el.sortField).toBe('title');
  });
});

describe('changelog entries and history payloads', () => {
  it('an entry gets defaults for every field it lacks', () => {
    const { el } = createCard();
    const now = 1_700_000_000_000;
    vi.spyOn(Date, 'now').mockReturnValue(now);
    const entry = el._cleanChangelogEntry({ source: 'firmware_change' });
    expect(entry).toMatchObject({
      timestamp: now / 1000,
      added: [],
      removed: [],
      changed: [],
      id: `change_${now}`,
      source: 'firmware_change',
      triggered_by: null,
      note: '',
    });
    expect(entry.iso_timestamp).toBe(el.formatDateTimeHA(new Date(now)));
    expect(el._cleanChangelogEntry({ added: [{ id: 1, title: 'A', type: 'sensor' }] }).source).toBe(
      'firmware'
    );
  });

  it('an entry with a timestamp but no ISO text is formatted from it', () => {
    const { el } = createCard();
    const entry = el._cleanChangelogEntry({
      timestamp: 1_700_000_000,
      added: [{ id: 1, title: 'A', type: 'sensor' }],
    });
    expect(entry.iso_timestamp).toBe(el.formatDateTimeHA(new Date(1_700_000_000_000)));
  });

  it('a history payload without history, seq or counts changes nothing', async () => {
    const { el } = createCard();
    el.changelog = [{ id: 'kept' }];
    el._lastChangelogSeq = 3;
    el.unreadChanges = 2;
    await el.handleChangelogHistoryMessage({ payload: gzipPayload({ _seq: 4 }) });
    expect(el.changelog).toEqual([{ id: 'kept' }]);
    await el.handleChangelogHistoryMessage({ payload: gzipPayload({ history: [] }) });
    expect(el.changelog).toEqual([]);
    expect(el._lastChangelogSeq).toBe(3);
    expect(el.unreadChanges).toBe(2);
  });
});

describe('dynamic change toasts', () => {
  function toastFor(event) {
    const { el, harness } = createCard();
    const toast = vi.spyOn(el, 'showToast');
    harness.publish('nibe/browser/dynamic', dynamicChangePayload(event));
    return toast.mock.calls[0];
  }

  it('an HA disable of an untitled point names its number', () => {
    expect(toastFor({ source: 'ha_disabled', removed: [{ id: 7 }] })).toEqual([
      'HA disabled: Point 7',
      'warning',
    ]);
  });

  it('an HA disable without its point says so', () => {
    expect(toastFor({ source: 'ha_disabled', removed: [] })).toEqual([
      'Entity disabled via HA entity registry',
      'warning',
    ]);
  });

  it('an untitled triggering switch is named by its number', () => {
    expect(toastFor({ added: [{ id: 1 }], triggered_by: { id: 9 } })[0]).toBe(
      '1 dynamic data point(s) appeared (triggered by: Point 9)'
    );
  });
});

describe('per-point metadata', () => {
  it('keeps the default value and the update time the bridge sent', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/meta/5',
      JSON.stringify(sampleMetadataEntry({ id: 5, intDefaultValue: 200, last_updated: 1_700_000_000 }))
    );
    const entity = el.entities.get(5);
    expect(entity.intDefaultValue).toBe(200);
    expect(entity.lastUpdated).toBe(1_700_000_000_000);
  });
});

describe('date formatting', () => {
  it('formats a timestamp that isn’t a Date yet', () => {
    const { el } = createCard();
    expect(el.formatDateTimeHA(1_700_000_000_000)).toBe(
      el.formatDateTimeHA(new Date(1_700_000_000_000))
    );
  });

  it('falls back to English without a locale language', () => {
    const { el } = createCard({ hass: { locale: {} } });
    const date = new Date(1_700_000_000_000);
    const withEnglish = createCard({ hass: { locale: { language: 'en' } } }).el.formatDateTimeHA(date);
    expect(el.formatDateTimeHA(date)).toBe(withEnglish);
  });
});

describe('enable and disable', () => {
  it('ignore an empty list and a card without hass', async () => {
    const { el, harness } = seeded();
    await el.enableEntities([]);
    await el.disableEntities([]);
    el._hass = null;
    await el.enableEntities([1]);
    await el.disableEntities([1]);
    expect(harness.published).toEqual([]);
  });

  it('handle a point the card doesn’t know, also when the publish fails', async () => {
    const { el, harness } = seeded();
    vi.spyOn(el, 'sleep').mockResolvedValue();
    harness.setCallServiceImpl(() => Promise.reject(new Error('down')));
    await el.enableEntities([99]);
    await el.disableEntities([99]);
    expect(el.entities.has(99)).toBe(false);
    expect(harness.published.map((p) => p.payload)).toEqual(['99', '99']);
  });
});

describe('small helpers', () => {
  it('a changelog entry that isn’t an object is dropped', () => {
    const { el } = createCard();
    expect(el._cleanChangelogEntry(null)).toBeNull();
    expect(el._cleanChangelogEntry('x')).toBeNull();
  });

  it('a point without a Modbus register is found by id and title only', () => {
    const { el } = seeded([sampleMetadataEntry({ id: 21, title: 'Alpha', modbusRegisterID: null })]);
    el.searchTerm = '21';
    expect(el.getFilteredEntities().map((e) => e.id)).toEqual([21]);
  });

  it('_findByDataFor gives null without a match, _esc gives "" for nothing', () => {
    const { el } = createCard();
    expect(el._findByDataFor(el.shadowRoot, '.nothing', 'x')).toBeNull();
    expect(el._esc(null)).toBe('');
    expect(el._esc(undefined)).toBe('');
  });

  it('a step without a divisor or unit is the plain change', () => {
    const { el } = createCard();
    expect(el._settingStep({ change: 5, divisor: 0, unit: '' })).toBe('5');
    expect(el._num('x', 7)).toBe(7);
  });
});

describe('rendering a point', () => {
  const unknownType = () =>
    seeded(
      [
        sampleMetadataEntry({ id: 1, type: 'gadget', is_dynamic: true }),
        sampleMetadataEntry({ id: 2, type: 'some_new_type' }),
      ],
      [1]
    );

  it('an unknown type is shown as it is, an enabled dynamic point can’t be disabled', () => {
    const { el } = unknownType();
    el.updateTable();
    const row = el.shadowRoot.querySelector('tr[data-id="1"]');
    expect(row.textContent).toContain('gadget');
    expect(row.querySelector('button[disabled]').textContent).toBe('Disable');
    const card = el.shadowRoot.querySelector('.entity-card[data-id="2"]');
    expect(card.textContent).toContain('some new type');
    const dynamicCard = el.shadowRoot.querySelector('.entity-card[data-id="1"]');
    expect(dynamicCard.querySelector('button[disabled]').textContent).toBe('Disable');
  });

  it('the details of a read-only enabled point with sparse metadata', () => {
    const { el } = seeded(
      [
        sampleMetadataEntry({
          id: 1,
          type: 'gadget',
          writable: false,
          description: '',
          shortUnit: '',
          unit_overridden: true,
          unit_raw: '',
          modbusRegisterType: '',
        }),
      ],
      [1]
    );
    el.showEntityDetails(1);
    const body = el.shadowRoot.querySelector('#details-modal .modal-body').innerHTML;
    expect(body).toContain('No description');
    expect(body).toContain('gadget');
    expect(body).toContain('Read-only sensor');
    expect(body).toContain('Enabled');
    expect(body).toContain('(empty)');
    expect(body).toContain('N/A');
  });

  it('opening the changelog without hass marks nothing read remotely', () => {
    const { el } = createCard({ skipHass: true });
    el.unreadChanges = 3;
    el.showChangelog();
    expect(el.unreadChanges).toBe(0);
  });
});

describe('without the elements it looks up', () => {
  it('every view update still runs', () => {
    const { el } = seeded([sampleMetadataEntry({ id: 1 })], [1]);
    for (const id of [
      'entity-table-body',
      'mobile-cards-container',
      'selection-stat',
      'selected-count',
      'clear-selection',
      'prev-page',
      'next-page',
      'enable-selected',
      'disable-selected',
      'select-all-checkbox',
      'show-changelog',
      'search-clear',
      'snapshots-list',
    ]) {
      $(el, id)?.remove();
    }
    el.shadowRoot.querySelector('.toast-container').remove();
    el.selectedIds.add(1);
    expect(() => {
      el.updateTable();
      el.selectedIds.clear();
      el.updateTable();
      el.updateChangelogBadge();
      el.updateSearchClearButton();
      el._renderSnapshotsList();
      el.showToast('x');
      el.setElementText('nothing-here', 'x');
      el.setModalContent('nothing-here', 't', 'c');
      el.showModal('nothing-here');
      el.hideModal('nothing-here');
    }).not.toThrow();
    expect(el._openModalId).toBeNull();
  });

  it('a modal without title or body keeps its other parts', () => {
    const { el } = createCard();
    const modal = $(el, 'details-modal');
    modal.querySelector('.modal-title').remove();
    modal.querySelector('.modal-body').remove();
    expect(() => el.setModalContent('details-modal', 't', 'c')).not.toThrow();
  });
});

describe('selection, paging and mobile cards', () => {
  it('nothing selected enables nothing; the first page has no previous one', () => {
    const { el, harness } = seeded();
    el.enableSelected();
    el.currentPage = 0;
    el.previousPage();
    expect(el.currentPage).toBe(0);
    expect(harness.published).toEqual([]);
  });

  it('enabling the selection, and paging back', async () => {
    const { el, harness } = seeded(
      Array.from({ length: 30 }, (_, i) => sampleMetadataEntry({ id: i + 1 }))
    );
    vi.spyOn(el, 'sleep').mockResolvedValue();
    el.config.pageSize = 10;
    el.updateTable();
    el.currentPage = 2;
    el.previousPage();
    expect(el.currentPage).toBe(1);
    el.selectedIds.add(3);
    el.enableSelected();
    await vi.waitFor(() =>
      expect(harness.getPublishedTo('homeassistant/text/nibe_enable_entity/set')).toHaveLength(1)
    );
  });

  it('attaching listeners without the table or card container', () => {
    const { el } = seeded();
    $(el, 'entity-table-body').remove();
    $(el, 'mobile-cards-container').remove();
    expect(() => {
      el.attachTableEventListeners();
      el.attachMobileEventListeners();
    }).not.toThrow();
  });

  it('a mobile checkbox without a desktop twin, and a mobile disable button', async () => {
    const { el, harness } = seeded([sampleMetadataEntry({ id: 1 })], [1]);
    vi.spyOn(el, 'sleep').mockResolvedValue();
    el.updateTable();
    el.shadowRoot.querySelector('.entity-checkbox[data-id="1"]').remove();
    const box = el.shadowRoot.querySelector('.mobile-entity-checkbox[data-id="1"]');
    box.checked = true;
    box.dispatchEvent(new Event('change'));
    expect(el.selectedIds.has(1)).toBe(true);
    el.shadowRoot.querySelector('.entity-card [data-action="disable"]').click();
    await vi.waitFor(() =>
      expect(harness.getPublishedTo('homeassistant/text/nibe_disable_entity/set')).toHaveLength(1)
    );
  });

  it('a click on a card’s button doesn’t also open the details', () => {
    const { el } = seeded([sampleMetadataEntry({ id: 1 })]);
    el.updateTable();
    const details = vi.spyOn(el, 'showEntityDetails');
    const card = el.shadowRoot.querySelector('.entity-card[data-id="1"]');
    card.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(details).toHaveBeenCalledTimes(1);
    details.mockClear();
    el.updateTable();
    // A button of the card's own, without an action listener of its own.
    const inner = document.createElement('button');
    el.shadowRoot.querySelector('.entity-card[data-id="1"]').appendChild(inner);
    inner.click();
    expect(details).not.toHaveBeenCalled();
  });
});

describe('snapshots', () => {
  function withSnapshot(snap) {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/snapshots', JSON.stringify([snap]));
    el.showSnapshots();
    return { el, harness };
  }

  it('a snapshot without count, time or mode', () => {
    const { el } = withSnapshot({ name: 'Bare' });
    const text = $(el, 'snapshots-list').textContent;
    expect(text).toContain('? entities · saved');
    expect(text).not.toContain('mode:');
  });

  it('cancel and restore work when their panel or message is gone', () => {
    const { el } = withSnapshot({ name: 'S', point_count: 1, timestamp: 't', mode: 'essential' });
    const list = $(el, 'snapshots-list');
    list.querySelector('.snapshot-restore-options').dataset.for = 'other';
    list.querySelector('.snapshot-restore-msg').dataset.for = 'other';
    expect(() => {
      list.querySelector('.snapshot-cancel-restore').click();
      list.querySelector('.snapshot-do-restore').click();
    }).not.toThrow();
  });

  it('saving without the message element', () => {
    const { el, harness } = withSnapshot({ name: 'S' });
    $(el, 'snapshot-save-msg').remove();
    el._handleSnapshotSave(); // no name: nothing to say it in
    $(el, 'snapshot-name-input').value = 'New';
    el._handleSnapshotSave();
    $(el, 'snapshot-name-input').remove();
    el._handleSnapshotSave();
    expect(harness.getPublishedTo('nibe/browser/snapshots/cmd')).toHaveLength(1);
  });

  it('a bridge result without a message says Done or refused, and clears itself', () => {
    vi.useFakeTimers();
    const { el } = withSnapshot({ name: 'S' });
    const toast = vi.spyOn(el, 'showToast');
    el.handleSnapshotResultMessage({ payload: '' });
    el.handleSnapshotResultMessage({ payload: JSON.stringify({ action: 'save', ok: true }) });
    expect($(el, 'snapshot-save-msg').textContent).toBe('Done.');
    vi.advanceTimersByTime(4000);
    expect($(el, 'snapshot-save-msg').textContent).toBe('');
    el.handleSnapshotResultMessage({ payload: JSON.stringify({ action: 'delete', ok: false }) });
    expect(toast.mock.calls.map((c) => c[0])).toEqual(['Done.', 'The bridge refused this.']);
  });

  it('a save result without the message element only toasts', () => {
    const { el } = withSnapshot({ name: 'S' });
    $(el, 'snapshot-save-msg').remove();
    const toast = vi.spyOn(el, 'showToast');
    el.handleSnapshotResultMessage({ payload: JSON.stringify({ action: 'save', ok: true }) });
    expect(toast).toHaveBeenCalledOnce();
  });

  it('a restore result without a name, and a panel gone before it would close', () => {
    vi.useFakeTimers();
    const { el } = withSnapshot({ name: 'S' });
    el.handleSnapshotResultMessage({ payload: JSON.stringify({ action: 'restore', ok: false }) });
    el.handleSnapshotResultMessage({
      payload: JSON.stringify({ action: 'restore', ok: true, name: 'S' }),
    });
    $(el, 'snapshots-list').querySelector('.snapshot-restore-options').dataset.for = 'other';
    expect(() => vi.advanceTimersByTime(3000)).not.toThrow();
  });
});

describe('changelog rendering', () => {
  async function render(entries) {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/changelog/history', gzipPayload({ history: entries }));
    await harness.settle();
    el.showChangelog();
    return $(el, 'changelog-content').textContent;
  }

  it('header variants and items without title or type', async () => {
    const text = await render([
      { source: 'learning', removed: [{ id: 1 }], timestamp: 3 },
      { source: 'firmware', removed: [{ id: 2 }], timestamp: 2 },
      { source: 'firmware_change', changed: [{ id: 3, changes: '' }], added: [{ id: 4 }], timestamp: 1 },
    ]);
    expect(text).toContain('↩️ Deactivated');
    expect(text).not.toContain('🧠 Learned');
    expect(text).toContain('🗑️ Removed');
    expect(text).toContain('#2: Unknown');
    expect(text).toContain('unknown');
    expect(text).toContain('#3: Unknown');
    expect(text).toContain('Showing 3 events');
  });

  it('one event is singular', async () => {
    const text = await render([{ source: 'firmware', added: [{ id: 1, title: 'A', type: 'sensor' }] }]);
    expect(text).toMatch(/Showing 1 event\s/);
  });
});

describe('failures that only warn', () => {
  it('a Fuse script that fails to load leaves substring search', async () => {
    const { el } = createCard({ skipHass: true });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(document.head, 'appendChild').mockImplementation((script) => {
      script.onerror(new Error('blocked'));
      return script;
    });
    await el._loadFuse();
    expect(el._fuseLoaded).toBe(false);
    expect(warn).toHaveBeenCalled();
  });

  it('a snapshot command the broker refuses is warned about', async () => {
    const { el, harness } = createCard();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    harness.setCallServiceImpl(() => Promise.reject(new Error('down')));
    el._sendSnapshotCmd({ action: 'delete', name: 'S' });
    await vi.waitFor(() => expect(warn).toHaveBeenCalled());
  });

  it('disabling a selection of ordinary points', async () => {
    const { el, harness } = seeded([sampleMetadataEntry({ id: 1 })], [1]);
    vi.spyOn(el, 'sleep').mockResolvedValue();
    el.selectedIds.add(1);
    el.disableSelected();
    await vi.waitFor(() =>
      expect(harness.getPublishedTo('homeassistant/text/nibe_disable_entity/set')).toHaveLength(1)
    );
  });
});

describe('a payload that isn’t gzip', () => {
  it('rejects once, without an unhandled rejection from the write side', async () => {
    const { el } = createCard();
    // Large enough that the write itself fails too, not only the close.
    const bogus = 'gzip1:' + Buffer.alloc(200_000, 7).toString('base64');
    await expect(el._decompressPayload(bogus)).rejects.toThrow();
    await new Promise((resolve) => setTimeout(resolve, 0)); // let the write side settle
  });
});
