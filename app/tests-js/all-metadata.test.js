import { describe, it, expect } from 'vitest';
import { createCard } from './support/create-card.js';
import { allMetadataPayload, sampleMetadataEntry, enabledStatePayload } from './support/fixtures.js';

describe('nibe/browser/all_metadata', () => {
  it('populates entities on the happy path', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry(), sampleMetadataEntry({ id: 4, title: 'Alarm', is_dynamic: true })])
    );

    expect(el.entities.size).toBe(2);
    expect(el.entities.get(3945).title).toBe('Heating setpoint');
    expect(el.entities.get(3945).divisor).toBe(10);
    expect(el.entities.get(4).isDynamic).toBe(true);
    expect(el.dynamicEntityIds.has(4)).toBe(true);
    expect(el.isLoading).toBe(false);
  });

  it('defaults enabled=false when enabled_state has not arrived yet', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry()]));
    expect(el.entities.get(3945).enabled).toBe(false);
  });

  it('applies enabled_state that arrived BEFORE all_metadata (_lastKnownEnabledPoints)', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/enabled_state', enabledStatePayload([3945]));
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry({ id: 3945 }), sampleMetadataEntry({ id: 4 })])
    );
    expect(el.entities.get(3945).enabled).toBe(true);
    expect(el.entities.get(4).enabled).toBe(false);
  });

  it('applies enabled_state that arrives AFTER all_metadata', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));
    expect(el.entities.get(3945).enabled).toBe(false);
    harness.publish('nibe/browser/enabled_state', enabledStatePayload([3945]));
    expect(el.entities.get(3945).enabled).toBe(true);
  });

  it('clears dynamicEntityIds on a fresh full batch so removed dynamic points do not linger', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry({ id: 4, is_dynamic: true })])
    );
    expect(el.dynamicEntityIds.has(4)).toBe(true);

    // A later full batch without point 4 at all (it disappeared from firmware).
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 5 })]));
    expect(el.dynamicEntityIds.has(4)).toBe(false);
  });

  it('fills in defaults for missing/optional fields', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([{ id: 99 }]) // only id present
    );
    const e = el.entities.get(99);
    expect(e.title).toBe('Point 99');
    expect(e.type).toBe('sensor');
    expect(e.writable).toBe(false);
    expect(e.unit).toBe('');
    expect(e.divisor).toBe(1);
    expect(e.decimal).toBe(0);
    expect(e.minValue).toBeNull();
  });

  it('does not throw on empty payload', () => {
    const { el, harness } = createCard();
    expect(() => harness.publish('nibe/browser/all_metadata', '')).not.toThrow();
    expect(el.entities.size).toBe(0);
  });

  it('does not throw on malformed JSON', () => {
    const { el, harness } = createCard();
    expect(() => harness.publish('nibe/browser/all_metadata', '{not json')).not.toThrow();
    expect(el.entities.size).toBe(0);
  });

  it('does not throw when metadata key is missing or wrong type', () => {
    const { el, harness } = createCard();
    expect(() => harness.publish('nibe/browser/all_metadata', JSON.stringify({ count: 0 }))).not.toThrow();
    expect(() =>
      harness.publish('nibe/browser/all_metadata', JSON.stringify({ metadata: 'not-an-object' }))
    ).not.toThrow();
    expect(el.entities.size).toBe(0);
  });

  it('does not throw on null payload', () => {
    const { harness } = createCard();
    expect(() => harness.publish('nibe/browser/all_metadata', null)).not.toThrow();
  });

  it('skips entries with a non-numeric point id key', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      JSON.stringify({ metadata: { abc: sampleMetadataEntry({ id: 'abc' }) }, count: 1 })
    );
    expect(el.entities.size).toBe(0);
  });
});
