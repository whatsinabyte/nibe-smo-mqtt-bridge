import { describe, it, expect } from 'vitest';
import { createCard } from './support/create-card.js';
import {
  allMetadataPayload,
  sampleMetadataEntry,
  enabledStatePayload,
  pointListPayload,
} from './support/fixtures.js';

describe('nibe/browser/meta/{id} (per-point)', () => {
  it('creates a new entity from a per-point message', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/meta/4', JSON.stringify(sampleMetadataEntry({ id: 4, title: 'Alarm' })));
    expect(el.entities.get(4).title).toBe('Alarm');
  });

  it('empty payload means the point was removed', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/meta/4', JSON.stringify(sampleMetadataEntry({ id: 4, is_dynamic: true })));
    expect(el.entities.has(4)).toBe(true);
    expect(el.dynamicEntityIds.has(4)).toBe(true);

    harness.publish('nibe/browser/meta/4', '');
    expect(el.entities.has(4)).toBe(false);
    expect(el.dynamicEntityIds.has(4)).toBe(false);
  });

  it('a whitespace-only payload is also treated as removal', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/meta/4', JSON.stringify(sampleMetadataEntry({ id: 4 })));
    harness.publish('nibe/browser/meta/4', '   ');
    expect(el.entities.has(4)).toBe(false);
  });

  it('preserves the enabled flag across a metadata refresh', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));
    harness.publish('nibe/browser/enabled_state', enabledStatePayload([3945]));
    expect(el.entities.get(3945).enabled).toBe(true);

    harness.publish('nibe/browser/meta/3945', JSON.stringify(sampleMetadataEntry({ id: 3945, title: 'Renamed' })));
    expect(el.entities.get(3945).enabled).toBe(true);
    expect(el.entities.get(3945).title).toBe('Renamed');
  });

  it('does not throw on malformed JSON, non-numeric topic id, or null payload', () => {
    const { harness } = createCard();
    expect(() => harness.publish('nibe/browser/meta/4', '{bad')).not.toThrow();
    expect(() => harness.publish('nibe/browser/meta/notanumber', '{}')).not.toThrow();
    expect(() => harness.publish('nibe/browser/meta/4', null)).not.toThrow();
  });
});

describe('nibe/browser/point_list', () => {
  it('adds stub entries for points not yet seen', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/point_list', pointListPayload([3, 4, 57]));
    expect(el.entities.size).toBe(3);
    expect(el.entities.get(3).title).toBe('Point 3');
  });

  it('seeds stub enabled state from _lastKnownEnabledPoints', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/enabled_state', enabledStatePayload([57]));
    harness.publish('nibe/browser/point_list', pointListPayload([3, 57]));
    expect(el.entities.get(3).enabled).toBe(false);
    expect(el.entities.get(57).enabled).toBe(true);
  });

  it('removes entities no longer in the authoritative list', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));
    harness.publish('nibe/browser/point_list', pointListPayload([1, 2]));
    expect(el.entities.has(3945)).toBe(false);
    expect(el.entities.size).toBe(2);
  });

  it('does not overwrite an already-populated entity with a stub', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));
    harness.publish('nibe/browser/point_list', pointListPayload([3945]));
    expect(el.entities.get(3945).title).toBe('Heating setpoint');
  });

  it('does not throw on malformed JSON, missing points array, or null', () => {
    const { el, harness } = createCard();
    expect(() => harness.publish('nibe/browser/point_list', '{bad')).not.toThrow();
    expect(() => harness.publish('nibe/browser/point_list', JSON.stringify({ count: 0 }))).not.toThrow();
    expect(() => harness.publish('nibe/browser/point_list', JSON.stringify({ points: 'nope' }))).not.toThrow();
    expect(() => harness.publish('nibe/browser/point_list', null)).not.toThrow();
    expect(el.entities.size).toBe(0);
  });
});
