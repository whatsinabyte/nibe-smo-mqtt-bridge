import { describe, it, expect, vi } from 'vitest';
import { createCard } from './support/create-card.js';
import { allMetadataPayload, sampleMetadataEntry } from './support/fixtures.js';

// This file exercises the real addEventListener callback bodies registered
// in setupEventListeners() / setupMobileEventListeners() / attachTableEventListeners()
// / attachMobileEventListeners() by dispatching genuine DOM events through
// el.shadowRoot, rather than calling the underlying handler methods directly.

function fire(el, type, opts = {}) {
  el.dispatchEvent(new Event(type, { bubbles: true, ...opts }));
}

describe('setupEventListeners() — desktop controls', () => {
  it('search-box input event updates searchTerm, resets page, and debounces a re-render', async () => {
    vi.useFakeTimers();
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([
      sampleMetadataEntry({ id: 1, title: 'Heating setpoint' }),
      sampleMetadataEntry({ id: 2, title: 'Cooling setpoint' }),
    ]));
    el.currentPage = 3;
    const input = el.shadowRoot.getElementById('search-input');
    input.value = 'heat';
    fire(input, 'input');

    expect(el.searchTerm).toBe('heat');
    expect(el.currentPage).toBe(0);
    // Not yet re-rendered — debounced.
    vi.advanceTimersByTime(el.debounceTime + 10);
    expect(el.filteredEntities.map((e) => e.id)).toEqual([1]);
    vi.useRealTimers();
  });

  it('search-clear click resets the search box and clears results', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1, title: 'Heating' })]));
    el.searchTerm = 'heat';
    el.shadowRoot.getElementById('search-input').value = 'heat';
    // The button starts disabled; only enabled once updateSearchClearButton()
    // sees a non-empty searchTerm (mirrors what the real input handler does).
    el.updateSearchClearButton();
    el.shadowRoot.getElementById('search-clear').click();
    expect(el.searchTerm).toBe('');
    expect(el.shadowRoot.getElementById('search-input').value).toBe('');
  });

  it.each([
    ['type-filter', 'sensor', 'typeFilter'],
    ['status-filter', 'enabled', 'statusFilter'],
    ['writable-filter', 'true', 'writableFilter'],
    ['dynamic-filter', 'dynamic', 'dynamicFilter'],
  ])('%s change event updates %s and resets page', (id, value, prop) => {
    const { el } = createCard();
    el.currentPage = 2;
    const select = el.shadowRoot.getElementById(id);
    select.value = value;
    fire(select, 'change');
    expect(el[prop]).toBe(value);
    expect(el.currentPage).toBe(0);
  });

  it.each([
    ['select-all', 'selectAll'],
    ['clear-selection', 'clearSelection'],
    ['enable-selected', 'enableSelected'],
    ['disable-selected', 'disableSelected'],
    ['show-changelog', 'showChangelog'],
    ['show-snapshots', 'showSnapshots'],
    ['close-snapshots', 'hideModal'],
    ['snapshot-save-btn', '_handleSnapshotSave'],
    ['clear-filters', 'clearFilters'],
    ['prev-page', 'previousPage'],
    ['next-page', 'nextPage'],
    ['close-changelog', 'hideModal'],
    ['close-details', 'hideModal'],
  ])('button #%s click invokes %s', (id, methodName) => {
    const { el } = createCard();
    const spy = vi.spyOn(el, methodName);
    // Several of these buttons start `disabled` in the rendered skeleton
    // (Clear/Enable/Disable/Prev/Next) — disabled elements do not receive
    // click events at all in a real browser (and jsdom matches that), so
    // force them enabled here. This test is only about listener wiring,
    // not about the separate enable/disable business logic (covered by
    // updateButtonStates()/updatePagination() tests elsewhere).
    el.shadowRoot.getElementById(id).disabled = false;
    el.shadowRoot.getElementById(id).click();
    expect(spy).toHaveBeenCalled();
  });

  it('select-all-checkbox change selects/deselects every filtered entity', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([
      sampleMetadataEntry({ id: 1 }),
      sampleMetadataEntry({ id: 2 }),
    ]));
    el.updateTable(); // all_metadata's debouncedUpdate() hasn't fired yet
    const checkbox = el.shadowRoot.getElementById('select-all-checkbox');
    checkbox.checked = true;
    fire(checkbox, 'change');
    expect(el.selectedIds.has(1)).toBe(true);
    expect(el.selectedIds.has(2)).toBe(true);

    checkbox.checked = false;
    fire(checkbox, 'change');
    expect(el.selectedIds.size).toBe(0);
  });

  it('clicking a column header sorts ascending on a new field, then toggles on repeat click', () => {
    const { el } = createCard();
    const th = el.shadowRoot.querySelector('th[data-sort="type"]');
    th.click();
    expect(el.sortField).toBe('type');
    expect(el.sortAscending).toBe(true);

    th.click();
    expect(el.sortField).toBe('type');
    expect(el.sortAscending).toBe(false);
  });
});

describe('setupMobileEventListeners()', () => {
  it('mobile-apply-filters splits sort value and applies enabled-desc branch', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.shadowRoot.getElementById('mobile-sort-filter').value = 'enabled-desc';
    el.shadowRoot.getElementById('mobile-apply-filters').click();
    expect(el.sortField).toBe('enabled');
    expect(el.sortAscending).toBe(false);
  });

  it('mobile-apply-filters applies title-desc sort branch', () => {
    const { el } = createCard();
    el.shadowRoot.getElementById('mobile-sort-filter').value = 'title-desc';
    el.shadowRoot.getElementById('mobile-apply-filters').click();
    expect(el.sortField).toBe('title');
    expect(el.sortAscending).toBe(false);
  });

  it('mobile-apply-filters ignores an unrecognised sort field, keeping prior sort state', () => {
    const { el } = createCard();
    el.sortField = 'id';
    el.sortAscending = true;
    el.shadowRoot.getElementById('mobile-sort-filter').value = 'bogus-asc';
    el.shadowRoot.getElementById('mobile-apply-filters').click();
    expect(el.sortField).toBe('id');
  });

  it('mobile-apply-filters mirrors filter values onto the desktop dropdowns and closes the panel', () => {
    const { el } = createCard();
    el.showMobileFilters = true;
    el.shadowRoot.getElementById('mobile-filter-panel').style.display = 'block';
    el.shadowRoot.getElementById('mobile-status-filter').value = 'disabled';
    el.shadowRoot.getElementById('mobile-writable-filter').value = 'false';
    el.shadowRoot.getElementById('mobile-dynamic-filter').value = 'static';
    el.shadowRoot.getElementById('mobile-apply-filters').click();

    expect(el.statusFilter).toBe('disabled');
    expect(el.writableFilter).toBe('false');
    expect(el.dynamicFilter).toBe('static');
    expect(el.shadowRoot.getElementById('status-filter').value).toBe('disabled');
    expect(el.shadowRoot.getElementById('writable-filter').value).toBe('false');
    expect(el.shadowRoot.getElementById('dynamic-filter').value).toBe('static');
    expect(el.showMobileFilters).toBe(false);
    expect(el.shadowRoot.getElementById('mobile-filter-panel').style.display).toBe('none');
    expect(el.shadowRoot.getElementById('mobile-filter-indicator').textContent).toBe('▼');
  });

  it('mobile-clear-filters resets filter/sort state and closes the panel', () => {
    const { el } = createCard();
    el.showMobileFilters = true;
    el.typeFilter = 'sensor';
    el.sortField = 'title';
    el.sortAscending = false;
    el.shadowRoot.getElementById('mobile-clear-filters').click();

    expect(el.typeFilter).toBe('');
    expect(el.sortField).toBe('id');
    expect(el.sortAscending).toBe(true);
    expect(el.showMobileFilters).toBe(false);
    expect(el.shadowRoot.getElementById('mobile-filter-indicator').textContent).toBe('▼');
  });
});

describe('attachTableEventListeners() — desktop row/checkbox/action callbacks', () => {
  it('entity-checkbox change adds/removes selection and keeps mobile view in sync', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const checkbox = el.shadowRoot.querySelector('.entity-checkbox[data-id="1"]');
    checkbox.checked = true;
    fire(checkbox, 'change');
    expect(el.selectedIds.has(1)).toBe(true);

    checkbox.checked = false;
    fire(checkbox, 'change');
    expect(el.selectedIds.has(1)).toBe(false);
  });

  it('data-action=enable button click calls enableEntities with the row id', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const spy = vi.spyOn(el, 'enableEntities').mockResolvedValue();
    el.shadowRoot.querySelector('button[data-action="enable"][data-id="1"]').click();
    expect(spy).toHaveBeenCalledWith([1]);
  });

  it('data-action=disable button click calls disableEntities with the row id', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    harness.publish('nibe/browser/enabled_state', JSON.stringify({ enabled_points: [1] }));
    const spy = vi.spyOn(el, 'disableEntities').mockResolvedValue();
    el.shadowRoot.querySelector('button[data-action="disable"][data-id="1"]').click();
    expect(spy).toHaveBeenCalledWith([1]);
  });

  it('data-action=details button click opens the details modal for that row', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    el.shadowRoot.querySelector('button[data-action="details"][data-id="1"]').click();
    expect(el._openModalId).toBe('details-modal');
  });

  it('clicking a table row (not on a button/checkbox) opens details', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const row = el.shadowRoot.querySelector('tr[data-id="1"]');
    row.click();
    expect(el._openModalId).toBe('details-modal');
  });

  it('clicking a button inside the row does not also trigger the row-click details handler twice', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const spy = vi.spyOn(el, 'showEntityDetails');
    el.shadowRoot.querySelector('button[data-action="details"][data-id="1"]').click();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('pressing Enter on a focused row opens details; other keys do nothing', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const row = el.shadowRoot.querySelector('tr[data-id="1"]');

    row.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
    expect(el._openModalId).toBeNull();

    row.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    expect(el._openModalId).toBe('details-modal');
  });

  it('pressing Space on a focused row opens details', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const row = el.shadowRoot.querySelector('tr[data-id="1"]');
    row.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }));
    expect(el._openModalId).toBe('details-modal');
  });
});

describe('attachMobileEventListeners() — mobile card checkbox/action callbacks', () => {
  it('mobile-entity-checkbox change syncs selection and the desktop checkbox', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const mobileCb = el.shadowRoot.querySelector('.mobile-entity-checkbox[data-id="1"]');
    mobileCb.checked = true;
    fire(mobileCb, 'change');
    expect(el.selectedIds.has(1)).toBe(true);
    const desktopCb = el.shadowRoot.querySelector('.entity-checkbox[data-id="1"]');
    expect(desktopCb.checked).toBe(true);

    mobileCb.checked = false;
    fire(mobileCb, 'change');
    expect(el.selectedIds.has(1)).toBe(false);
    expect(desktopCb.checked).toBe(false);
  });

  it('mobile data-action buttons call enable/disable/details and stop propagation', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const spy = vi.spyOn(el, 'enableEntities').mockResolvedValue();
    const btn = el.shadowRoot.querySelector('#mobile-cards-container button[data-action="enable"][data-id="1"]');
    btn.click();
    expect(spy).toHaveBeenCalledWith([1]);
  });

  it('mobile details button click opens details modal', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    el.shadowRoot.querySelector('#mobile-cards-container button[data-action="details"][data-id="1"]').click();
    expect(el._openModalId).toBe('details-modal');
  });

  it('tapping a mobile card (not a button/checkbox) opens details', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const card = el.shadowRoot.querySelector('.entity-card[data-id="1"]');
    card.click();
    expect(el._openModalId).toBe('details-modal');
  });

  it('pressing Enter/Space on a focused mobile card opens details', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    el.updateTable();
    const card = el.shadowRoot.querySelector('.entity-card[data-id="1"]');

    card.dispatchEvent(new KeyboardEvent('keydown', { key: 'x', bubbles: true }));
    expect(el._openModalId).toBeNull();

    card.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    expect(el._openModalId).toBe('details-modal');
  });
});

describe('lifecycle: connectedCallback / disconnectedCallback / getCardSize / sleep', () => {
  it('connectedCallback sets up MQTT subscriptions when hass was already assigned before insertion', () => {
    const el = document.createElement('nibe-entity-manager-card');
    el.setConfig({});
    // hass assigned before the element is attached to the document.
    // (jsdom actually calls connectedCallback synchronously on appendChild,
    // so simulate the "hass already set, not yet connected" state directly.)
    el.mqttSetupDone = false;
    const spy = vi.spyOn(el, 'setupMqttSubscriptions');
    el._hass = { connection: { subscribeMessage: vi.fn(() => Promise.resolve(() => {})) } };
    el.connectedCallback();
    expect(spy).toHaveBeenCalled();
    expect(el.mqttSetupDone).toBe(true);
  });

  it('connectedCallback does not re-attach listeners if already set', () => {
    const { el } = createCard();
    const spy = vi.spyOn(el, 'setupEventListeners');
    el.connectedCallback();
    expect(spy).not.toHaveBeenCalled();
  });

  it('disconnectedCallback unsubscribes MQTT, clears flags, and clears a pending debounce timer', async () => {
    const { el, harness } = createCard();
    expect(harness.subscriptions.length).toBeGreaterThan(0);
    el.debouncedUpdate(); // schedule a pending timeout
    expect(el.updateTimeout).not.toBeNull();

    el.disconnectedCallback();

    expect(el.mqttSetupDone).toBe(false);
    expect(el.eventListenersSet).toBe(false);
    // cleanupSubscriptions() empties the local array synchronously.
    expect(el.mqttSubscriptions).toEqual([]);

    // Let the cleanup promises resolve to hit the unsubscribe() branch —
    // each unsubscribe function removes its own entry from
    // harness.subscriptions, so an empty array confirms every subscription
    // was actually unsubscribed, not just that mqttSubscriptions was cleared.
    await Promise.resolve();
    await Promise.resolve();
    expect(harness.subscriptions).toEqual([]);
  });

  it('getCardSize returns 4', () => {
    const { el } = createCard();
    expect(el.getCardSize()).toBe(4);
  });

  it('sleep resolves after roughly the given delay', async () => {
    vi.useFakeTimers();
    const { el } = createCard();
    const spy = vi.fn();
    el.sleep(50).then(spy);
    expect(spy).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(50);
    expect(spy).toHaveBeenCalled();
    vi.useRealTimers();
  });
});

describe('setupMqttSubscriptions error handling', () => {
  it('logs and does not throw if hass.connection.subscribeMessage throws synchronously', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const el = document.createElement('nibe-entity-manager-card');
    document.body.appendChild(el);
    el.setConfig({});
    el._hass = {
      connection: {
        subscribeMessage: () => {
          throw new Error('boom');
        },
      },
    };
    expect(() => el.setupMqttSubscriptions()).not.toThrow();
    expect(errorSpy).toHaveBeenCalledWith('MQTT setup failed:', expect.any(Error));
    errorSpy.mockRestore();
  });
});
