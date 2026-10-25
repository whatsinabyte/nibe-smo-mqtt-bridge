import { describe, it, expect, vi } from 'vitest';
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

describe('a copy that knows its own build', () => {
  const stale = (storage = {}) => {
    const { el, harness } = createCard();
    el._cardBuild = 'aaa111aaa111';
    el._reloadPage = vi.fn();
    sessionStorage.clear();
    Object.entries(storage).forEach(([k, v]) => sessionStorage.setItem(k, v));
    return { el, harness };
  };
  const notice = (el) => el.shadowRoot.getElementById('stale-card-notice');

  it('does nothing when it is the current build', () => {
    const { el, harness } = stale();
    harness.publish('nibe/browser/card_version', 'aaa111aaa111');
    expect(el._reloadPage).not.toHaveBeenCalled();
    expect(notice(el).hidden).toBe(true);
  });

  it('reloads once towards a newer build, remembering the attempt', () => {
    const { el, harness } = stale();
    harness.publish('nibe/browser/card_version', 'bbb222bbb222');
    expect(el._reloadPage).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem('nibe-card-reload:bbb222bbb222')).toBe('1');
    expect(notice(el).hidden).toBe(true);
  });

  it('shows the notice instead when that reload already ran', () => {
    const { el, harness } = stale({ 'nibe-card-reload:bbb222bbb222': '1' });
    harness.publish('nibe/browser/card_version', 'bbb222bbb222');
    expect(el._reloadPage).not.toHaveBeenCalled();
    expect(notice(el).hidden).toBe(false);
  });

  it('shows the notice when the attempt cannot be remembered', () => {
    const { el, harness } = stale();
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });
    harness.publish('nibe/browser/card_version', 'bbb222bbb222');
    expect(el._reloadPage).not.toHaveBeenCalled();
    expect(notice(el).hidden).toBe(false);
  });

  it('shows the notice when storage cannot even be read', () => {
    const { el, harness } = stale();
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    harness.publish('nibe/browser/card_version', 'bbb222bbb222');
    expect(el._reloadPage).not.toHaveBeenCalled();
    expect(notice(el).hidden).toBe(false);
  });
});
