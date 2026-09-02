import { vi } from 'vitest';

/**
 * Build a fake `hass` object that mimics the surface of Home Assistant's
 * frontend `hass` object the card actually touches:
 *
 *   - hass.connection.subscribeMessage(cb, {type: 'mqtt/subscribe', topic})
 *     -> Promise<unsubscribeFn>. Callbacks are recorded per topic
 *     (supporting the single `#` multi-level wildcard the card uses for
 *     `nibe/browser/meta/#`) so tests can push a fake inbound MQTT message
 *     with harness.publish(topic, payloadString).
 *   - hass.callService('mqtt', 'publish', {topic, payload, qos, retain})
 *     -> Promise, recorded so tests can assert exactly what was published.
 *     Resolution/rejection behaviour is swappable via setCallServiceImpl.
 *   - hass.locale / hass.formatDateTime for the formatDateTimeHA() fallback
 *     chain.
 *
 * Returns { hass, publish, subscriptions, published, ... } — `hass` is what
 * you assign to `card.hass`; the rest are test-facing harness helpers.
 */
export function createFakeHass(overrides = {}) {
  const subscriptions = []; // {topic, prefix, wildcard, cb}
  const published = []; // flattened list of every callService('mqtt','publish', data) call

  let callServiceImpl = () => Promise.resolve();

  const connection = {
    subscribeMessage: vi.fn((cb, { topic }) => {
      const wildcard = topic.endsWith('#');
      const prefix = wildcard ? topic.slice(0, -1) : topic;
      const entry = { topic, prefix, wildcard, cb };
      subscriptions.push(entry);
      const unsubscribe = vi.fn(() => {
        const idx = subscriptions.indexOf(entry);
        if (idx >= 0) subscriptions.splice(idx, 1);
      });
      return Promise.resolve(unsubscribe);
    }),
  };

  const callService = vi.fn((domain, service, data) => {
    if (domain === 'mqtt' && service === 'publish') {
      published.push({ ...data });
    }
    return callServiceImpl(domain, service, data);
  });

  const hass = {
    locale: { language: 'en', time_format: '24' },
    connection,
    callService,
    ...overrides,
  };

  return {
    hass,
    subscriptions,
    published,

    /**
     * Deliver a fake inbound MQTT message to every subscriber whose topic
     * matches (exact match, or the `#` wildcard prefix the card uses for
     * `nibe/browser/meta/#`). Returns the number of callbacks invoked.
     */
    publish(topic, payload) {
      const matches = subscriptions.filter((s) =>
        s.wildcard ? topic.startsWith(s.prefix) : s.topic === topic
      );
      matches.forEach((s) => s.cb({ topic, payload }));
      return matches.length;
    },

    /** Find the subscription entry for an exact topic string (non-wildcard). */
    findSubscription(topic) {
      return subscriptions.find((s) => s.topic === topic);
    },

    /** Swap what hass.callService('mqtt','publish', ...) resolves/rejects with. */
    setCallServiceImpl(fn) {
      callServiceImpl = fn;
    },

    /** All recorded publish() calls whose topic matches. */
    getPublishedTo(topic) {
      return published.filter((p) => p.topic === topic);
    },
  };
}
