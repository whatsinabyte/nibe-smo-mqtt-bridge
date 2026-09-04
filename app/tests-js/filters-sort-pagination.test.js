import { describe, it, expect } from 'vitest';
import { createCard } from './support/create-card.js';
import { allMetadataPayload, sampleMetadataEntry, enabledStatePayload } from './support/fixtures.js';

function seedEntities(el, harness, n = 5) {
  const entries = [];
  for (let i = 1; i <= n; i++) {
    entries.push(
      sampleMetadataEntry({
        id: i,
        title: `Point ${String.fromCharCode(64 + i)}`, // A, B, C...
        type: i % 2 === 0 ? 'sensor' : 'switch',
        writable: i % 2 === 0,
        is_dynamic: i === n,
        modbusRegisterID: 40000 + i, // unique per entity so id-substring search tests don't collide
      })
    );
  }
  harness.publish('nibe/browser/all_metadata', allMetadataPayload(entries));
  harness.publish('nibe/browser/enabled_state', enabledStatePayload([2, 4]));
}

describe('filters', () => {
  it('filters by type', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.typeFilter = 'sensor';
    const result = el.getFilteredEntities();
    expect(result.every((e) => e.type === 'sensor')).toBe(true);
    expect(result).toHaveLength(2); // ids 2, 4
  });

  it('filters by status enabled/disabled', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.statusFilter = 'enabled';
    expect(el.getFilteredEntities().map((e) => e.id).sort()).toEqual([2, 4]);

    el.statusFilter = 'disabled';
    expect(el.getFilteredEntities().map((e) => e.id).sort()).toEqual([1, 3, 5]);
  });

  it('filters by writable true/false', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.writableFilter = 'true';
    expect(el.getFilteredEntities().every((e) => e.writable)).toBe(true);
    el.writableFilter = 'false';
    expect(el.getFilteredEntities().every((e) => !e.writable)).toBe(true);
  });

  it('filters by dynamic/static', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.dynamicFilter = 'dynamic';
    expect(el.getFilteredEntities().map((e) => e.id)).toEqual([5]);
    el.dynamicFilter = 'static';
    expect(el.getFilteredEntities().map((e) => e.id).sort()).toEqual([1, 2, 3, 4]);
  });

  it('combines multiple filters (AND semantics)', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.typeFilter = 'sensor';
    el.statusFilter = 'enabled';
    expect(el.getFilteredEntities().map((e) => e.id).sort()).toEqual([2, 4]);

    el.writableFilter = 'false';
    expect(el.getFilteredEntities()).toHaveLength(0); // sensor+enabled are writable, contradiction
  });

  it('search matches on id, title substring, and unit', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.searchTerm = '3';
    expect(el.getFilteredEntities().map((e) => e.id)).toEqual([3]);

    el.searchTerm = 'point c';
    expect(el.getFilteredEntities().map((e) => e.id)).toEqual([3]);

    el.searchTerm = '°c';
    expect(el.getFilteredEntities().length).toBe(5); // all share the same unit fixture
  });

  it('search on id/modbus register is exact-or-prefix, not bare substring (regression: "1021" must not match 11021/21021)', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([
        sampleMetadataEntry({ id: 1021, title: 'Operating mode PV panels', modbusRegisterID: 579 }),
        sampleMetadataEntry({ id: 11021, title: 'Unrelated point', modbusRegisterID: 5790 }),
        sampleMetadataEntry({ id: 21021, title: 'Another unrelated point', modbusRegisterID: 15790 }),
      ])
    );

    // "1021" is not a prefix of 11021 or 21021 (they start with "1102"/"2102"),
    // so a bare substring match would wrongly include them but exact-or-prefix does not.
    el.searchTerm = '1021';
    expect(el.getFilteredEntities().map((e) => e.id)).toEqual([1021]);

    // "579" is an exact match for point 1021's register and a *prefix* match for
    // 11021's register (5790) — prefix matching is intentional (lets a search
    // narrow progressively as more digits are typed), so both are included, but
    // the exact match must still rank first.
    el.searchTerm = '579';
    const results = el.getFilteredEntities();
    expect(results.map((e) => e.id).sort((a, b) => a - b)).toEqual([1021, 11021]);
    el.filteredEntities = results;
    el.sortEntities();
    expect(el.filteredEntities[0].id).toBe(1021);
  });

  it('clearFilters resets every filter, search term, and page', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.searchTerm = 'x';
    el.typeFilter = 'sensor';
    el.statusFilter = 'enabled';
    el.writableFilter = 'true';
    el.dynamicFilter = 'dynamic';
    el.currentPage = 2;

    el.clearFilters();

    expect(el.searchTerm).toBe('');
    expect(el.typeFilter).toBe('');
    expect(el.statusFilter).toBe('');
    expect(el.writableFilter).toBe('');
    expect(el.dynamicFilter).toBe('');
    expect(el.currentPage).toBe(0);
  });
});

describe('sorting', () => {
  it('sorts by id ascending and descending', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.filteredEntities = el.getFilteredEntities();
    el.sortField = 'id';
    el.sortAscending = true;
    el.sortEntities();
    expect(el.filteredEntities.map((e) => e.id)).toEqual([1, 2, 3, 4, 5]);

    el.sortAscending = false;
    el.sortEntities();
    expect(el.filteredEntities.map((e) => e.id)).toEqual([5, 4, 3, 2, 1]);
  });

  it('sorts by title case-insensitively', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.filteredEntities = el.getFilteredEntities();
    el.sortField = 'title';
    el.sortAscending = true;
    el.sortEntities();
    expect(el.filteredEntities.map((e) => e.title)).toEqual([
      'Point A',
      'Point B',
      'Point C',
      'Point D',
      'Point E',
    ]);
  });

  it('sorts by type', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.filteredEntities = el.getFilteredEntities();
    el.sortField = 'type';
    el.sortAscending = true;
    el.sortEntities();
    expect(el.filteredEntities[0].type <= el.filteredEntities[4].type).toBe(true);
  });

  it('sorts by enabled (boolean) both directions', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.filteredEntities = el.getFilteredEntities();
    el.sortField = 'enabled';
    el.sortAscending = false; // enabled first
    el.sortEntities();
    expect(el.filteredEntities[0].enabled).toBe(true);

    el.sortAscending = true; // disabled first
    el.sortEntities();
    expect(el.filteredEntities[0].enabled).toBe(false);
  });
});

describe('pagination', () => {
  it('clamps currentPage back into range when filtering shrinks the result set', () => {
    const { el, harness } = createCard({ config: { pageSize: 2 } });
    seedEntities(el, harness);
    el.currentPage = 2; // page index 2 valid for 5 items/pageSize 2 (pages 0,1,2)
    el.updateTable();
    expect(el.currentPage).toBe(2);

    el.typeFilter = 'sensor'; // shrinks to 2 results -> only page 0 exists
    el.updateTable();
    expect(el.currentPage).toBe(0);
  });

  it('previousPage/nextPage respect page boundaries', () => {
    const { el, harness } = createCard({ config: { pageSize: 2 } });
    seedEntities(el, harness);
    el.updateTable();
    expect(el.currentPage).toBe(0);
    el.previousPage();
    expect(el.currentPage).toBe(0); // already at first page

    el.nextPage();
    expect(el.currentPage).toBe(1);
    el.nextPage();
    expect(el.currentPage).toBe(2); // last page (5 items / 2 per page = pages 0,1,2)
    el.nextPage();
    expect(el.currentPage).toBe(2); // stays clamped at last page
  });

  it('page-size boundary: exactly pageSize items yields a single page', () => {
    const { el, harness } = createCard({ config: { pageSize: 5 } });
    seedEntities(el, harness, 5);
    el.updateTable();
    const nextButton = el.shadowRoot.getElementById('next-page');
    expect(nextButton.disabled).toBe(true);
  });
});

describe('selection', () => {
  it('selectAll snapshots the currently filtered set', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.typeFilter = 'sensor';
    el.updateTable();
    el.selectAll();
    expect(Array.from(el.selectedIds).sort()).toEqual([2, 4]);
  });

  it('clearSelection empties selectedIds', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.selectedIds.add(1);
    el.clearSelection();
    expect(el.selectedIds.size).toBe(0);
  });

  it('select-all checkbox reflects indeterminate state with a partial selection', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.updateTable();
    el.selectedIds.add(1);
    el.updateButtonStates();
    const checkbox = el.shadowRoot.getElementById('select-all-checkbox');
    expect(checkbox.indeterminate).toBe(true);
    expect(checkbox.checked).toBe(false);
  });

  it('select-all checkbox is fully checked when every filtered entity is selected', () => {
    const { el, harness } = createCard();
    seedEntities(el, harness);
    el.updateTable();
    el.filteredEntities.forEach((e) => el.selectedIds.add(e.id));
    el.updateButtonStates();
    const checkbox = el.shadowRoot.getElementById('select-all-checkbox');
    expect(checkbox.checked).toBe(true);
    expect(checkbox.indeterminate).toBe(false);
  });
});
