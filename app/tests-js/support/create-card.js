import '../../nibe-entity-manager-card.js';
import { createFakeHass } from './fake-hass.js';

/**
 * Instantiate the real <nibe-entity-manager-card> custom element, attach it
 * to the document, run it through setConfig() and the hass setter exactly
 * as Lovelace does, and hand back both the element and the MQTT test
 * harness bound to it.
 */
export function createCard({ config = {}, hass: hassOverrides = {}, skipHass = false } = {}) {
  const el = document.createElement('nibe-entity-manager-card');
  document.body.appendChild(el);
  el.setConfig(config);

  const harness = createFakeHass(hassOverrides);
  if (!skipHass) {
    el.hass = harness.hass;
  }

  return { el, harness };
}
