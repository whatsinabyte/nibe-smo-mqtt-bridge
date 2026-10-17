import { describe, it, expect, vi } from 'vitest';
import { createCard } from './support/create-card.js';
import {
  allMetadataPayload,
  sampleMetadataEntry,
  snapshotsPayload,
  sampleSnapshot,
  changelogHistoryPayload,
  changelogEntry,
} from './support/fixtures.js';


describe('showEntityDetails() — additional field-rendering branches', () => {
  it('renders the writable warning banner and unit-overridden row when applicable', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([
        sampleMetadataEntry({
          id: 1,
          writable: true,
          unit_overridden: true,
          unit_raw: 'raw-unit',
          category: 'Climate',
          intDefaultValue: 5,
          stringDefaultValue: 'default text',
        }),
      ])
    );
    el.showEntityDetails(1);
    const html = el.shadowRoot.getElementById('details-content').innerHTML;
    expect(html).toMatch(/Writing to this entity sends a command/);
    expect(html).toContain('Overridden');
    expect(html).toContain('raw-unit');
    expect(html).toContain('Climate');
    expect(html).toContain('Integer: 5');
    expect(html).toContain('default text');
  });

  it('renders "Not specified" for missing value range/defaults, and dynamic badge', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([
        sampleMetadataEntry({
          id: 2,
          is_dynamic: true,
          min_value: undefined,
          max_value: undefined,
          intDefaultValue: undefined,
          stringDefaultValue: '',
          divisor: 1,
        }),
      ])
    );
    el.showEntityDetails(2);
    const html = el.shadowRoot.getElementById('details-content').innerHTML;
    expect(html).toContain('Not specified');
    expect(html).toContain('Dynamic');
    expect(html).toContain('1 (no scaling)');
  });

  it('shows the value range when min/max are both finite numbers', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry({ id: 3, min_value: 5, max_value: 30 })])
    );
    el.showEntityDetails(3);
    const html = el.shadowRoot.getElementById('details-content').innerHTML;
    expect(html).toContain('5 to 30');
  });
});

describe('showEntityDetails() — value label status', () => {
  const details = (entry) => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry(entry)]));
    el.showEntityDetails(entry.id);
    return el.shadowRoot.getElementById('details-content');
  };

  it('asks for a report on partly confirmed labels, linking the value-labels template', () => {
    const content = details({ id: 56150, title: 'All sub units operating prio', mapping_status: 'partial' });
    expect(content.innerHTML).toContain('Value Labels');
    expect(content.innerHTML).toContain('Partly confirmed');
    const link = content.querySelector('.mapping-report a');
    expect(link.getAttribute('href')).toContain('template=value_labels.md');
    expect(decodeURIComponent(link.getAttribute('href'))).toContain('Point 56150 — All sub units operating prio');
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('shows confirmed labels without a report link', () => {
    const content = details({ id: 7022, mapping_status: 'confirmed' });
    expect(content.innerHTML).toContain('Confirmed');
    expect(content.querySelector('.mapping-report')).toBeNull();
  });

  it('shows no value label row for a point without a status', () => {
    const content = details({ id: 4 });
    expect(content.innerHTML).not.toContain('Value Labels');
  });
});

describe('showEntityDetails() — setting step', () => {
  const details = (entry) => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry(entry)]));
    el.showEntityDetails(entry.id);
    return el.shadowRoot.getElementById('details-content').innerHTML;
  };

  it("shows a number's step: the firmware's change over its divisor", () => {
    const html = details({ id: 32341, type: 'number', change: 5, divisor: 10, unit: '°C' });
    expect(html).toContain('Step');
    expect(html).toContain('0.5 °C');
  });

  it('falls back to one raw unit without a change', () => {
    expect(details({ id: 3869, type: 'number', change: 0, divisor: 10, unit: '°C' })).toContain('0.1 °C');
  });

  it('shows no step for a sensor', () => {
    expect(details({ id: 4, type: 'sensor', change: 5, divisor: 10, unit: '°C' })).not.toContain('>Step<');
  });
});

describe('formatDateTimeHA() fallback chain', () => {
  it('uses window.hassUtil.formatDateTime when available', () => {
    const { el } = createCard();
    window.hassUtil = { formatDateTime: vi.fn(() => 'HASSUTIL-FORMATTED') };
    el._hass.locale = { language: 'en', time_format: '24' };
    expect(el.formatDateTimeHA(new Date())).toBe('HASSUTIL-FORMATTED');
    delete window.hassUtil;
  });

  it('uses hass.formatDateTime when hassUtil is unavailable', () => {
    const { el } = createCard();
    el._hass.formatDateTime = vi.fn(() => 'HASS-FORMATTED');
    expect(el.formatDateTimeHA(new Date())).toBe('HASS-FORMATTED');
  });

  it('falls back to Intl/toLocaleString with hass.locale when no formatter function exists', () => {
    const { el } = createCard();
    el._hass.locale = { language: 'sv', time_format: '12' };
    const result = el.formatDateTimeHA(new Date(2024, 0, 1));
    expect(typeof result).toBe('string');
    expect(result.length).toBeGreaterThan(0);
  });

  it('falls back to plain toLocaleString when hass has no locale at all', () => {
    const { el } = createCard({ skipHass: true });
    const result = el.formatDateTimeHA(new Date(2024, 0, 1));
    expect(typeof result).toBe('string');
  });

  it('returns N/A for a null/undefined date', () => {
    const { el } = createCard();
    expect(el.formatDateTimeHA(null)).toBe('N/A');
  });
});

describe('_renderChangelogContent() — source variants', () => {
  async function publishAndOpen(el, harness, entryOverrides) {
    harness.publish(
      'nibe/browser/changelog/history',
      changelogHistoryPayload({ history: [changelogEntry(entryOverrides)], seq: 1 })
    );
    await harness.settle();
    el.showChangelog();
  }

  it('renders the ha_disabled source with its badge and no triggered-by line', async () => {
    const { el, harness } = createCard();
    await publishAndOpen(el, harness, {
      source: 'ha_disabled',
      added: [],
      removed: [{ id: 9, title: 'Some sensor', type: 'sensor' }],
      triggered_by: { id: 1, title: 'X' },
    });
    const html = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(html).toContain('Disabled via HA');
    expect(html).toContain('HA registry');
    expect(html).not.toContain('Triggered by');
  });

  it('renders the learning source with added-only and removed-only header variants', async () => {
    const { el, harness } = createCard();
    await publishAndOpen(el, harness, {
      source: 'learning',
      added: [{ id: 9, title: 'Learned pt', type: 'sensor' }],
      removed: [],
    });
    let html = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(html).toContain('Learned');
    expect(html).toContain('learned');
  });

  it('renders the firmware_change source with its note banner', () => {
    // `note` is not part of the sanitised changelog entry shape produced by
    // _cleanChangelogEntry() (see the MQTT path above), but
    // _renderChangelogContent() still reads entry.note directly off
    // whatever is in this.changelog — set it directly to exercise that
    // rendering branch without going through the sanitizer.
    const { el } = createCard();
    el.changelog = [
      changelogEntry({
        source: 'firmware_change',
        added: [{ id: 9, title: 'Changed pt', type: 'sensor' }],
        removed: [],
        note: 'Register type changed unexpectedly',
      }),
    ];
    el.showChangelog();
    const html = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(html).toContain('Firmware updated');
    expect(html).toContain('firmware update');
    expect(html).toContain('Register type changed unexpectedly');
  });

  it('keeps a firmware update from MQTT with its note, even without point changes', async () => {
    // The bridge writes one firmware_change entry per update; the versions
    // are in `note`, and an update that adds or removes no point is still
    // worth seeing. The sanitizer used to drop both.
    const { el, harness } = createCard();
    await publishAndOpen(el, harness, {
      source: 'firmware_change',
      added: [],
      removed: [],
      note: 'Version (EB100): 4.13 → 4.14',
    });
    const html = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(html).toContain('Firmware updated');
    expect(html).toContain('Version (EB100): 4.13 → 4.14');
    expect(html).toContain('Showing 1 event');
  });

  it('lists points whose register description a firmware update changed', async () => {
    const { el, harness } = createCard();
    await publishAndOpen(el, harness, {
      source: 'firmware_change',
      added: [],
      removed: [],
      changed: [
        { id: 3702, title: 'Stop temperature', type: 'number', changes: 'min 55 → 550' },
        { id: 1, title: 'No description given' },
      ],
      note: 'Version (EB100): 4.13 → 4.14',
    });
    const rows = el.shadowRoot.querySelectorAll('#changelog-content .changelog-changed');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('#3702');
    expect(rows[0].textContent).toContain('min 55 → 550');
  });

  it('labels ordinary appear/disappear entries "controller", not "firmware"', async () => {
    // "firmware" next to "firmware update" read as if every entry were an
    // update; these are the controller showing or hiding points.
    const { el, harness } = createCard();
    await publishAndOpen(el, harness, {
      source: 'firmware',
      added: [{ id: 9, title: 'New pt', type: 'sensor' }],
      removed: [],
    });
    const html = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(html).toContain('>controller</span>');
    expect(html).not.toContain('>firmware</span>');
  });

  it('renders the default firmware source with a triggered_by line including a written value', async () => {
    const { el, harness } = createCard();
    await publishAndOpen(el, harness, {
      source: 'firmware',
      added: [{ id: 9, title: 'New pt', type: 'sensor' }],
      removed: [{ id: 10, title: 'Gone pt', type: 'sensor' }],
      triggered_by: { id: 5, title: 'Mode switch', value: 3 },
    });
    const html = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(html).toContain('Discovered');
    expect(html).toContain('Removed');
    expect(html).toContain('Triggered by');
    expect(html).toContain('Mode switch');
    expect(html).toContain('value written');
  });

  it('renders a triggered_by point with no distinct title using the "Point {id}" fallback', async () => {
    const { el, harness } = createCard();
    await publishAndOpen(el, harness, {
      source: 'firmware',
      added: [{ id: 9, title: 'New pt', type: 'sensor' }],
      removed: [],
      triggered_by: { id: 7 },
    });
    const html = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(html).toContain('Point 7');
  });

  it('shows "No changes recorded yet" when changelog is empty', () => {
    const { el } = createCard();
    el.showChangelog();
    const html = el.shadowRoot.getElementById('changelog-content').innerHTML;
    expect(html).toMatch(/No changes recorded yet/);
  });
});

describe('snapshots modal — restore options panel flow', () => {
  it('clicking Restore reveals the restore-options panel for that snapshot only', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/snapshots',
      snapshotsPayload([sampleSnapshot({ name: 'Summer' }), sampleSnapshot({ name: 'Winter' })])
    );
    el.showSnapshots();

    const summerBtn = el.shadowRoot.querySelector('.snapshot-restore-btn[data-snap-name="Summer"]');
    summerBtn.click();

    const summerPanel = el.shadowRoot.querySelector('.snapshot-restore-options[data-for="Summer"]');
    const winterPanel = el.shadowRoot.querySelector('.snapshot-restore-options[data-for="Winter"]');
    expect(summerPanel.style.display).toBe('block');
    expect(winterPanel.style.display).toBe('none');
  });

  it('clicking Cancel hides the restore-options panel', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot({ name: 'Summer' })]));
    el.showSnapshots();
    el.shadowRoot.querySelector('.snapshot-restore-btn[data-snap-name="Summer"]').click();
    el.shadowRoot.querySelector('.snapshot-cancel-restore[data-snap-name="Summer"]').click();
    const panel = el.shadowRoot.querySelector('.snapshot-restore-options[data-for="Summer"]');
    expect(panel.style.display).toBe('none');
  });

  it('clicking a restore-mode button sets the inline status message and schedules the panel to hide', () => {
    vi.useFakeTimers();
    const { el, harness } = createCard();
    harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot({ name: 'Summer' })]));
    el.showSnapshots();
    el.shadowRoot.querySelector('.snapshot-do-restore[data-mode="flush"]').click();

    const msgEl = el.shadowRoot.querySelector('.snapshot-restore-msg[data-for="Summer"]');
    expect(msgEl.textContent).toMatch(/Replacing selection/);

    vi.advanceTimersByTime(3000);
    const panel = el.shadowRoot.querySelector('.snapshot-restore-options[data-for="Summer"]');
    expect(panel.style.display).toBe('none');
    vi.useRealTimers();
  });

  it('the merge restore mode sets the "Adding to selection" status message', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/snapshots', snapshotsPayload([sampleSnapshot({ name: 'Summer' })]));
    el.showSnapshots();
    el.shadowRoot.querySelector('.snapshot-do-restore[data-mode="merge"]').click();
    const msgEl = el.shadowRoot.querySelector('.snapshot-restore-msg[data-for="Summer"]');
    expect(msgEl.textContent).toMatch(/Adding to selection/);
  });
});

describe('handleAllMetadataMessage — preserves existing entity enabled state without _lastKnownEnabledPoints', () => {
  it('keeps the prior optimistic enabled flag on a second all_metadata batch', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    expect(el.entities.get(1).enabled).toBe(false);

    // Simulate an optimistic update (e.g. from enableEntities) with no
    // enabled_state message ever having arrived.
    el.entities.get(1).enabled = true;

    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 1 })]));
    expect(el.entities.get(1).enabled).toBe(true);
  });
});
