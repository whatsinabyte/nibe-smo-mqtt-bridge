import { describe, it, expect } from 'vitest';
import '../nibe-entity-manager-card.js';

// customElements.define() registers the class globally; grab the
// constructor back off the registry so the static, side-effect-free
// helper methods can be tested directly against the shipped file.
const NibeEntityManager = customElements.get('nibe-entity-manager-card').prototype.constructor;

describe('NibeEntityManager._isValidChangelogItem', () => {
  it('accepts an object with a numeric id', () => {
    expect(NibeEntityManager._isValidChangelogItem({ id: 3945, title: 'x' })).toBe(true);
  });

  it.each([
    [null],
    [undefined],
    [42],
    ['string'],
    [{}],
    [{ id: '3945' }],
    [{ id: null }],
    [[1, 2, 3]],
  ])('rejects %p', (value) => {
    expect(NibeEntityManager._isValidChangelogItem(value)).toBe(false);
  });
});

describe('NibeEntityManager._isStaleChangelogSeq', () => {
  it('is stale when seq <= lastSeq', () => {
    expect(NibeEntityManager._isStaleChangelogSeq(5, 5)).toBe(true);
    expect(NibeEntityManager._isStaleChangelogSeq(4, 5)).toBe(true);
  });

  it('is not stale when seq > lastSeq', () => {
    expect(NibeEntityManager._isStaleChangelogSeq(6, 5)).toBe(false);
  });

  it('is never stale when lastSeq is null (nothing applied yet)', () => {
    expect(NibeEntityManager._isStaleChangelogSeq(1, null)).toBe(false);
  });

  it('is never stale when seq is not a number', () => {
    expect(NibeEntityManager._isStaleChangelogSeq(undefined, 5)).toBe(false);
    expect(NibeEntityManager._isStaleChangelogSeq('5', 5)).toBe(false);
  });
});

describe('NibeEntityManager._clampPage', () => {
  it('returns 0 when there are no results', () => {
    expect(NibeEntityManager._clampPage(3, 0, 50)).toBe(0);
  });

  it('leaves an in-range page untouched', () => {
    expect(NibeEntityManager._clampPage(1, 120, 50)).toBe(1);
  });

  it('clamps a page past the end to the last page', () => {
    // 120 items / 50 per page = 3 pages (0,1,2); page 5 is out of range.
    expect(NibeEntityManager._clampPage(5, 120, 50)).toBe(2);
  });

  it('clamps the boundary case where currentPage === totalPages', () => {
    expect(NibeEntityManager._clampPage(2, 100, 50)).toBe(1);
  });

  it('handles a single exact page-size boundary', () => {
    expect(NibeEntityManager._clampPage(0, 50, 50)).toBe(0);
    expect(NibeEntityManager._clampPage(1, 50, 50)).toBe(0);
  });
});

describe('NibeEntityManager._formatBulkOutcomeToast', () => {
  it('reports full success, singular', () => {
    expect(NibeEntityManager._formatBulkOutcomeToast('Enabled', 'enable', 1, 1)).toEqual({
      message: 'Enabled 1 entity',
      type: 'success',
    });
  });

  it('reports full success, plural', () => {
    expect(NibeEntityManager._formatBulkOutcomeToast('Enabled', 'enable', 3, 3)).toEqual({
      message: 'Enabled 3 entities',
      type: 'success',
    });
  });

  it('reports partial success', () => {
    expect(NibeEntityManager._formatBulkOutcomeToast('Disabled', 'disable', 2, 5)).toEqual({
      message: 'Disabled 2 of 5 — 3 failed',
      type: 'error',
    });
  });

  it('reports total failure, singular', () => {
    expect(NibeEntityManager._formatBulkOutcomeToast('Enabled', 'enable', 0, 1)).toEqual({
      message: 'Failed to enable 1 entity',
      type: 'error',
    });
  });

  it('reports total failure, plural', () => {
    expect(NibeEntityManager._formatBulkOutcomeToast('Enabled', 'enable', 0, 4)).toEqual({
      message: 'Failed to enable 4 entities',
      type: 'error',
    });
  });
});
