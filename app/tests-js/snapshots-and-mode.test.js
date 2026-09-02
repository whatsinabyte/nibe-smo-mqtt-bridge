import { describe, it, expect, vi } from 'vitest';
import { createCard } from './support/create-card.js';
import { snapshotsPayload, sampleSnapshot } from './support/fixtures.js';

describe('nibe/browser/snapshots', () => {
  it('stores the snapshot list on the happy path', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot()]));
    expect(el.snapshots).toHaveLength(1);
    expect(el.snapshots[0].name).toBe('Summer Profile');
  });

  it('falls back to an empty array on malformed JSON', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/snapshots', '{bad');
    expect(el.snapshots).toEqual([]);
  });

  it('handles a null-ish payload without throwing', () => {
    const { el, harness } = createCard();
    expect(() => harness.publish('nibe/browser/snapshots', 'null')).not.toThrow();
    expect(el.snapshots).toEqual([]);
  });

  it('falls back to [] for a valid-JSON, non-array payload (e.g. a bare object)', () => {
    // handleSnapshotsMessage validates Array.isArray(parsed) before assigning,
    // so a non-array JSON value (object, number, boolean, string) resets
    // this.snapshots to [] instead of propagating into _renderSnapshotsList's
    // this.snapshots.map(...) and throwing out of the MQTT message handler.
    const { el, harness } = createCard();
    expect(() => harness.publish('nibe/browser/snapshots', '{}')).not.toThrow();
    expect(el.snapshots).toEqual([]);
  });

  it('re-renders the snapshots modal in place when open', () => {
    const { el, harness } = createCard();
    el.showSnapshots();
    harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot({ name: 'Winter' })]));
    const html = el.shadowRoot.getElementById('snapshots-list').innerHTML;
    expect(html).toContain('Winter');
  });

  describe('save / restore / delete commands (nibe/browser/snapshots/cmd)', () => {
    it('publishes a save command with the exact documented shape', () => {
      const { el, harness } = createCard();
      el.showSnapshots();
      const input = el.shadowRoot.getElementById('snapshot-name-input');
      input.value = 'Summer Profile';
      el._handleSnapshotSave();

      const calls = harness.getPublishedTo('nibe/browser/snapshots/cmd');
      expect(calls).toHaveLength(1);
      expect(JSON.parse(calls[0].payload)).toEqual({ action: 'save', name: 'Summer Profile' });
    });

    it('does not publish and shows an inline error when the name is empty', () => {
      const { el, harness } = createCard();
      el.showSnapshots();
      el.shadowRoot.getElementById('snapshot-name-input').value = '   ';
      el._handleSnapshotSave();
      expect(harness.getPublishedTo('nibe/browser/snapshots/cmd')).toHaveLength(0);
      expect(el.shadowRoot.getElementById('snapshot-save-msg').textContent).toMatch(/enter a snapshot name/i);
    });

    it('publishes a flush restore command with the exact documented shape', () => {
      const { el, harness } = createCard();
      harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot()]));
      el.showSnapshots();
      const btn = el.shadowRoot.querySelector('.snapshot-do-restore[data-mode="flush"]');
      btn.click();

      const calls = harness.getPublishedTo('nibe/browser/snapshots/cmd');
      expect(JSON.parse(calls[0].payload)).toEqual({
        action: 'restore',
        name: 'Summer Profile',
        mode: 'flush',
      });
    });

    it('publishes a merge restore command with the exact documented shape', () => {
      const { el, harness } = createCard();
      harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot()]));
      el.showSnapshots();
      const btn = el.shadowRoot.querySelector('.snapshot-do-restore[data-mode="merge"]');
      btn.click();

      const calls = harness.getPublishedTo('nibe/browser/snapshots/cmd');
      expect(JSON.parse(calls[0].payload)).toEqual({
        action: 'restore',
        name: 'Summer Profile',
        mode: 'merge',
      });
    });

    it('publishes a delete command with the exact documented shape', () => {
      const { el, harness } = createCard();
      harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot()]));
      el.showSnapshots();
      vi.stubGlobal('confirm', () => true);
      el.shadowRoot.querySelector('.snapshot-delete-btn').click();

      const calls = harness.getPublishedTo('nibe/browser/snapshots/cmd');
      expect(JSON.parse(calls[0].payload)).toEqual({ action: 'delete', name: 'Summer Profile' });
      vi.unstubAllGlobals();
    });

    it('does not publish a delete command when the user cancels the confirm', () => {
      const { el, harness } = createCard();
      harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot()]));
      el.showSnapshots();
      vi.stubGlobal('confirm', () => false);
      el.shadowRoot.querySelector('.snapshot-delete-btn').click();
      expect(harness.getPublishedTo('nibe/browser/snapshots/cmd')).toHaveLength(0);
      vi.unstubAllGlobals();
    });

    it('disables Restore and shows a warning when the applied mode blocks it', () => {
      const { el, harness } = createCard();
      harness.publish('nibe/browser/applied_mode', 'menus');
      harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot()]));
      el.showSnapshots();

      const restoreBtn = el.shadowRoot.querySelector('.snapshot-restore-btn');
      expect(restoreBtn.disabled).toBe(true);
      expect(el.shadowRoot.getElementById('snapshots-list').innerHTML).toMatch(/Restore is disabled/i);
    });

    it('does not attempt to publish when hass is unavailable', () => {
      const { el } = createCard({ skipHass: true });
      expect(() => el._sendSnapshotCmd({ action: 'save', name: 'x' })).not.toThrow();
    });
  });
});

describe('nibe/browser/applied_mode', () => {
  it('stores the plain-string payload trimmed', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/applied_mode', ' essential \n');
    expect(el.appliedMode).toBe('essential');
  });

  it.each(['essential', 'monitoring', 'advanced', 'menus', 'all', 'none'])(
    'accepts documented mode value %s',
    (mode) => {
      const { el, harness } = createCard();
      harness.publish('nibe/browser/applied_mode', mode);
      expect(el.appliedMode).toBe(mode);
    }
  );
});
