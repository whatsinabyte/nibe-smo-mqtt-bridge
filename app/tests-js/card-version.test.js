import { describe, it, expect } from 'vitest';
import { createCard } from './support/create-card.js';

// The card compares its own build — written into the copy the bridge serves
// — with the bridge's retained nibe/browser/card_version. Under vitest the
// card is the repository's file, placeholder and all, so its own build is
// unknown.

describe('card version check', () => {
  it('subscribes to the bridge card version topic', () => {
    const { harness } = createCard();
    expect(harness.publish('nibe/browser/card_version', 'abc123def456')).toBe(1);
  });

  it('never reloads or warns while its own version is unknown', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/card_version', 'abc123def456');
    expect(el.shadowRoot.getElementById('stale-card-notice').hidden).toBe(true);
  });

  it('keeps the stale-card notice through a re-render', () => {
    const { el } = createCard();
    el._showStaleCardNotice();
    expect(el.shadowRoot.getElementById('stale-card-notice').hidden).toBe(false);
    el.render();
    expect(el.shadowRoot.getElementById('stale-card-notice').hidden).toBe(false);
  });
});
