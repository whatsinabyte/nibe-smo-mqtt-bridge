import { describe, it, expect, afterEach } from 'vitest';
import { createCard } from './support/create-card.js';
import { allMetadataPayload, sampleMetadataEntry } from './support/fixtures.js';

afterEach(() => {
  delete globalThis.Fuse;
});

describe('Fuse.js CDN load path (jsdom cannot fetch it)', () => {
  it('_fuse stays null after hass is set — jsdom never resolves the <script src> load', () => {
    const { el } = createCard();
    expect(el._fuseLoaded).toBe(false);
    expect(el._fuse).toBeNull();
  });

  it('falls back to substring title matching when Fuse never loads', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([
        sampleMetadataEntry({ id: 1, title: 'Heating setpoint' }),
        sampleMetadataEntry({ id: 2, title: 'Cooling setpoint' }),
      ])
    );
    el.searchTerm = 'heat';
    expect(el.getFilteredEntities().map((e) => e.id)).toEqual([1]);

    // A typo does NOT match — proves this is substring, not fuzzy, matching.
    el.searchTerm = 'heeting';
    expect(el.getFilteredEntities()).toHaveLength(0);
  });

  it('substring search on title requires no minimum length (unlike the Fuse path\'s 3-char rule)', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1, title: 'AB' })]));
    el.searchTerm = 'a';
    expect(el.getFilteredEntities().map((e) => e.id)).toEqual([1]);
  });
});

describe('Fuse.js active path (fake global Fuse injected directly)', () => {
  it('uses fuzzy results when a Fuse index is present, ranking by match score', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([
        sampleMetadataEntry({ id: 1, title: 'Heating setpoint' }),
        sampleMetadataEntry({ id: 2, title: 'Room temperature' }),
      ])
    );

    // Minimal fake Fuse: "search" returns whichever items contain the query
    // as a substring anywhere in title, in a caller-controlled rank order —
    // enough to exercise getFilteredEntities()/sortEntities()'s Fuse branch
    // without depending on the real fuse.js scoring algorithm.
    globalThis.Fuse = class {
      constructor(items) {
        this.items = items;
      }
      search(term) {
        return this.items
          .filter((i) => i.title.toLowerCase().includes(term.toLowerCase()))
          .map((item) => ({ item }));
      }
    };
    el._fuseLoaded = true;
    el._rebuildFuseIndex();

    el.searchTerm = 'temperature';
    const results = el.getFilteredEntities();
    expect(results.map((e) => e.id)).toEqual([2]);
  });

  it('sorts Fuse rank order first, falling back to id for entities matched only by exact id/unit', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([
        sampleMetadataEntry({ id: 10, title: 'Zzz result', unit: '' }),
        sampleMetadataEntry({ id: 5, title: 'Best result', unit: '' }),
      ])
    );
    globalThis.Fuse = class {
      constructor(items) {
        this.items = items;
      }
      search() {
        // id 5 ranks first (best match), id 10 ranks second.
        return [
          { item: this.items.find((i) => i.id === 5) },
          { item: this.items.find((i) => i.id === 10) },
        ];
      }
    };
    el._fuseLoaded = true;
    el._rebuildFuseIndex();
    el.searchTerm = 'result';
    el.filteredEntities = el.getFilteredEntities();
    el.sortEntities();
    expect(el.filteredEntities.map((e) => e.id)).toEqual([5, 10]);
  });

  it('breaks a tie between two entities with equal Fuse rank by falling back to id order', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([
        sampleMetadataEntry({ id: 20, title: 'Neither matches title', unit: '' }),
        sampleMetadataEntry({ id: 15, title: 'Neither matches title either', unit: '' }),
      ])
    );
    globalThis.Fuse = class {
      constructor(items) {
        this.items = items;
      }
      // Neither item is returned by Fuse — both get rank Infinity, so the
      // exact-substring ID match is what puts them in filteredEntities, and
      // the tiebreaker (a.id - b.id) must decide the final order.
      search() {
        return [];
      }
    };
    el._fuseLoaded = true;
    el._rebuildFuseIndex();
    el.searchTerm = 'neither'; // matches both titles via the Fuse branch (length >= 3)
    el.filteredEntities = el.getFilteredEntities(); // populates _fuseResultOrder (empty Map)
    // Both entities got rank Infinity (Fuse returned no results) — reverse
    // the pre-sort order so the tiebreaker is what fixes it back to id order.
    el.filteredEntities = [el.entities.get(20), el.entities.get(15)];
    el.sortEntities();
    expect(el.filteredEntities.map((e) => e.id)).toEqual([15, 20]);
  });
});
