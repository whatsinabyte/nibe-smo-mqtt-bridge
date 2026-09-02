import { describe, it, expect, vi } from 'vitest';
import { createCard } from './support/create-card.js';
import { allMetadataPayload, sampleMetadataEntry } from './support/fixtures.js';

describe('outbound: enable / disable', () => {
  it('enableEntities publishes to the documented topic with the point ID as a plain string', async () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));

    await el.enableEntities([3945]);

    const calls = harness.getPublishedTo('homeassistant/text/nibe_enable_entity/set');
    expect(calls).toHaveLength(1);
    expect(calls[0].payload).toBe('3945');
    expect(el.entities.get(3945).enabled).toBe(true);
  });

  it('disableEntities publishes to the documented topic', async () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));
    el.entities.get(3945).enabled = true;

    await el.disableEntities([3945]);

    const calls = harness.getPublishedTo('homeassistant/text/nibe_disable_entity/set');
    expect(calls).toHaveLength(1);
    expect(calls[0].payload).toBe('3945');
    expect(el.entities.get(3945).enabled).toBe(false);
  });

  it('bulk-enables multiple point IDs sequentially', async () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry({ id: 1 }), sampleMetadataEntry({ id: 2 })])
    );
    await el.enableEntities([1, 2]);

    const calls = harness.getPublishedTo('homeassistant/text/nibe_enable_entity/set');
    expect(calls.map((c) => c.payload).sort()).toEqual(['1', '2']);
    expect(el.selectedIds.size).toBe(0); // clearSelection() runs at the end of enableEntities
  });

  it('enableSelected() reads the current selection and delegates to enableEntities', () => {
    const { el } = createCard();
    el.selectedIds.add(1);
    el.selectedIds.add(2);
    const spy = vi.spyOn(el, 'enableEntities').mockResolvedValue();
    el.enableSelected();
    expect(spy).toHaveBeenCalledWith([1, 2]);
  });

  it('reverts the optimistic update and reports failure when callService rejects', async () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));
    harness.setCallServiceImpl(() => Promise.reject(new Error('mqtt publish failed')));

    const toastSpy = vi.spyOn(el, 'showToast');
    await el.enableEntities([3945]);

    expect(el.entities.get(3945).enabled).toBe(false); // reverted
    expect(toastSpy).toHaveBeenCalledWith('Failed to enable 1 entity', 'error');
  });

  it('reports a partial-failure toast when some succeed and some fail', async () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry({ id: 1 }), sampleMetadataEntry({ id: 2 })])
    );
    let call = 0;
    harness.setCallServiceImpl(() => {
      call++;
      return call === 1 ? Promise.resolve() : Promise.reject(new Error('nope'));
    });
    const toastSpy = vi.spyOn(el, 'showToast');
    await el.enableEntities([1, 2]);

    expect(toastSpy).toHaveBeenCalledWith('Enabled 1 of 2 — 1 failed', 'error');
  });

  it('does nothing when pointIds is empty', async () => {
    const { el, harness } = createCard();
    await el.enableEntities([]);
    expect(harness.getPublishedTo('homeassistant/text/nibe_enable_entity/set')).toHaveLength(0);
  });

  it('does nothing when hass is unavailable', async () => {
    const { el } = createCard({ skipHass: true });
    await expect(el.enableEntities([1])).resolves.toBeUndefined();
  });

  it('disableEntities reverts the optimistic update and reports failure when callService rejects', async () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));
    el.entities.get(3945).enabled = true;
    harness.setCallServiceImpl(() => Promise.reject(new Error('mqtt publish failed')));

    const toastSpy = vi.spyOn(el, 'showToast');
    await el.disableEntities([3945]);

    expect(el.entities.get(3945).enabled).toBe(true); // reverted back to enabled
    expect(toastSpy).toHaveBeenCalledWith('Failed to disable 1 entity', 'error');
  });

  it('disableSelected silently skips dynamic entities and warns if the whole selection was dynamic', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry({ id: 4, is_dynamic: true })])
    );
    el.entities.get(4).enabled = true;
    el.selectedIds.add(4);
    const toastSpy = vi.spyOn(el, 'showToast');
    el.disableSelected();
    expect(harness.getPublishedTo('homeassistant/text/nibe_disable_entity/set')).toHaveLength(0);
    expect(toastSpy).toHaveBeenCalledWith(
      'Dynamic entities cannot be disabled — change the controlling register instead',
      'warning'
    );
  });
});

describe('outbound: mark changelog read', () => {
  it('publishes the mark-changes-read press command when the changelog is opened', async () => {
    const { el, harness } = createCard();
    await el.showChangelog();
    const calls = harness.getPublishedTo('homeassistant/button/nibe_mark_changes_read/press');
    expect(calls).toHaveLength(1);
    expect(calls[0].payload).toBe('');
  });

  it('does not throw when the mark-read publish rejects', async () => {
    const { el, harness } = createCard();
    harness.setCallServiceImpl(() => Promise.reject(new Error('offline')));
    await expect(el.showChangelog()).resolves.toBeUndefined();
  });
});
