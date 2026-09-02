import { describe, it, expect } from 'vitest';
import { createCard } from './support/create-card.js';
import { allMetadataPayload, sampleMetadataEntry } from './support/fixtures.js';

describe('entity details modal', () => {
  it('opens and renders key metadata fields, HTML-escaping title/description', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([
        sampleMetadataEntry({
          id: 3945,
          title: '<script>alert(1)</script>',
          description: 'A "quoted" & <tricky> value',
        }),
      ])
    );
    el.showEntityDetails(3945);

    expect(el.shadowRoot.getElementById('details-modal').classList.contains('show')).toBe(true);
    const html = el.shadowRoot.getElementById('details-content').innerHTML;
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;');
    expect(html).toContain('40123'); // modbusRegisterID
  });

  it('does nothing for an unknown point id', () => {
    const { el } = createCard();
    expect(() => el.showEntityDetails(99999)).not.toThrow();
    expect(el.shadowRoot.getElementById('details-modal').classList.contains('show')).toBe(false);
  });

  it('hideModal removes the show class and clears _openModalId', () => {
    const { el, harness } = createCard();
    harness.publish('nibe/browser/all_metadata', allMetadataPayload([sampleMetadataEntry({ id: 3945 })]));
    el.showEntityDetails(3945);
    el.hideModal('details-modal');
    expect(el.shadowRoot.getElementById('details-modal').classList.contains('show')).toBe(false);
    expect(el._openModalId).toBeNull();
  });
});

describe('toasts', () => {
  it('renders a toast into the toast container with the right class', () => {
    const { el } = createCard();
    el.isLoading = false;
    el.showToast('Hello', 'success');
    const toast = el.shadowRoot.querySelector('.toast-success');
    expect(toast.textContent).toBe('Hello');
  });

  it('is suppressed while isLoading is true (default suppressInitialToasts config)', () => {
    const { el } = createCard();
    expect(el.isLoading).toBe(true);
    el.showToast('Should not show');
    expect(el.shadowRoot.querySelector('.toast-container').children.length).toBe(0);
  });

  it('is shown while isLoading even when suppressInitialToasts is disabled via config', () => {
    const { el } = createCard({ config: { suppressInitialToasts: false } });
    expect(el.isLoading).toBe(true);
    el.showToast('Shown anyway');
    expect(el.shadowRoot.querySelector('.toast-container').children.length).toBe(1);
  });
});

describe('mobile filter toggle', () => {
  it('toggles the mobile filter panel visibility and indicator arrow', () => {
    const { el } = createCard();
    const toggle = el.shadowRoot.getElementById('mobile-filter-toggle');
    const panel = el.shadowRoot.getElementById('mobile-filter-panel');
    const indicator = el.shadowRoot.getElementById('mobile-filter-indicator');

    expect(el.showMobileFilters).toBe(false);
    toggle.click();
    expect(el.showMobileFilters).toBe(true);
    expect(panel.style.display).toBe('block');
    expect(indicator.textContent).toBe('▲');

    toggle.click();
    expect(el.showMobileFilters).toBe(false);
    expect(panel.style.display).toBe('none');
    expect(indicator.textContent).toBe('▼');
  });

  it('mobile apply-filters mirrors values onto the desktop dropdowns', () => {
    const { el, harness } = createCard();
    harness.publish(
      'nibe/browser/all_metadata',
      allMetadataPayload([sampleMetadataEntry({ id: 1, type: 'sensor' })])
    );
    el.shadowRoot.getElementById('mobile-type-filter').value = 'sensor';
    el.shadowRoot.getElementById('mobile-apply-filters').click();

    expect(el.typeFilter).toBe('sensor');
    expect(el.shadowRoot.getElementById('type-filter').value).toBe('sensor');
  });

  it('mobile clear-filters resets both mobile and desktop controls', () => {
    const { el } = createCard();
    el.typeFilter = 'sensor';
    el.shadowRoot.getElementById('mobile-clear-filters').click();
    expect(el.typeFilter).toBe('');
    expect(el.shadowRoot.getElementById('mobile-sort-filter').value).toBe('id-asc');
  });
});
