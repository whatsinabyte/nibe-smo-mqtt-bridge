import { describe, it, expect, vi } from 'vitest';
import { createCard } from './support/create-card.js';
import { allMetadataPayload, sampleMetadataEntry, enabledStatePayload, dynamicChangePayload } from './support/fixtures.js';

describe('nibe/browser/enabled_state', () => {
  it('marks matching entities enabled and others disabled', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry({ id: 1 }), sampleMetadataEntry({ id: 2 })])
    );
    harness.publish('nibe/browser/enabled_state', enabledStatePayload([1]));
    expect(el.entities.get(1).enabled).toBe(true);
    expect(el.entities.get(2).enabled).toBe(false);

    harness.publish('nibe/browser/enabled_state', enabledStatePayload([2]));
    expect(el.entities.get(1).enabled).toBe(false);
    expect(el.entities.get(2).enabled).toBe(true);
  });

  it('does not throw on empty payload, malformed JSON, or missing enabled_points', () => {
    const { harness } = createCard();
    expect(() => harness.publish('nibe/browser/enabled_state', '')).not.toThrow();
    expect(() => harness.publish('nibe/browser/enabled_state', '{bad')).not.toThrow();
    expect(() => harness.publish('nibe/browser/enabled_state', JSON.stringify({}))).not.toThrow();
    expect(() => harness.publish('nibe/browser/enabled_state', null)).not.toThrow();
  });
});

describe('nibe/browser/dynamic (non-retained toast)', () => {
  it('shows a toast for appeared points', () => {
    const { el, harness } = createCard();
    el.isLoading = false; // toasts are suppressed while loading
    const spy = vi.spyOn(el, 'showToast');
    harness.publish(
      'nibe/browser/dynamic',
      dynamicChangePayload({ added: [{ id: 1 }, { id: 2 }], source: 'firmware' })
    );
    expect(spy).toHaveBeenCalledWith('2 dynamic data point(s) appeared', 'info');
  });

  it('shows a toast for disappeared points', () => {
    const { el, harness } = createCard();
    el.isLoading = false;
    const spy = vi.spyOn(el, 'showToast');
    harness.publish('nibe/browser/dynamic', dynamicChangePayload({ removed: [{ id: 1 }] }));
    expect(spy).toHaveBeenCalledWith('1 dynamic data point(s) disappeared', 'info');
  });

  it('shows a combined toast when both appeared and disappeared', () => {
    const { el, harness } = createCard();
    el.isLoading = false;
    const spy = vi.spyOn(el, 'showToast');
    harness.publish(
      'nibe/browser/dynamic',
      dynamicChangePayload({ added: [{ id: 1 }], removed: [{ id: 2 }] })
    );
    expect(spy).toHaveBeenCalledWith('1 data point(s) appeared, 1 disappeared', 'info');
  });

  it('appends the triggering point title when known', () => {
    const { el, harness } = createCard();
    el.isLoading = false;
    const spy = vi.spyOn(el, 'showToast');
    harness.publish(
      'nibe/browser/dynamic',
      dynamicChangePayload({ added: [{ id: 1 }], triggered_by: { id: 3754, title: 'Forced control' } })
    );
    expect(spy).toHaveBeenCalledWith(
      '1 dynamic data point(s) appeared (triggered by: Forced control)',
      'info'
    );
  });

  it('shows a warning toast for an ha_disabled event', () => {
    const { el, harness } = createCard();
    el.isLoading = false;
    const spy = vi.spyOn(el, 'showToast');
    harness.publish(
      'nibe/browser/dynamic',
      dynamicChangePayload({ source: 'ha_disabled', removed: [{ id: 5, title: 'Alarm' }] })
    );
    expect(spy).toHaveBeenCalledWith('HA disabled: Alarm', 'warning');
  });

  it('suppresses toasts while isLoading is true and suppressInitialToasts is on (default)', () => {
    const { el, harness } = createCard();
    expect(el.isLoading).toBe(true);
    const spy = vi.spyOn(el, 'showToast');
    harness.publish('nibe/browser/dynamic', dynamicChangePayload({ added: [{ id: 1 }] }));
    // showToast is still called; it just no-ops internally on isLoading — verify no toast lands in DOM.
    spy.mockRestore();
    const container = el.shadowRoot.querySelector('.toast-container');
    expect(container.children.length).toBe(0);
  });

  it('does not throw on empty/malformed/null payloads', () => {
    const { harness } = createCard();
    expect(() => harness.publish('nibe/browser/dynamic', '')).not.toThrow();
    expect(() => harness.publish('nibe/browser/dynamic', '{bad')).not.toThrow();
    expect(() => harness.publish('nibe/browser/dynamic', null)).not.toThrow();
    expect(() => harness.publish('nibe/browser/dynamic', JSON.stringify({ added: 'nope' }))).not.toThrow();
  });
});
