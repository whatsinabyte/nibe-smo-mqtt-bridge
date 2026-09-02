import { describe, it, expect } from 'vitest';
import { createCard } from './support/create-card.js';
import { gzipPayload } from './support/fixtures.js';

describe('_decompressPayload (gzip1: sentinel + DecompressionStream)', () => {
  it('round-trips a gzip-compressed JSON payload back to the original string', async () => {
    const { el } = createCard();
    const original = { hello: 'world', nested: { n: 42 }, list: [1, 2, 3] };
    const payload = gzipPayload(original);
    expect(payload.startsWith('gzip1:')).toBe(true);

    const jsonStr = await el._decompressPayload(payload);
    expect(JSON.parse(jsonStr)).toEqual(original);
  });

  it('round-trips larger payloads (many keys) correctly across multiple stream chunks', async () => {
    const { el } = createCard();
    const big = {};
    for (let i = 0; i < 2000; i++) {
      big[String(i)] = { id: i, title: `Point ${i}`, description: 'x'.repeat(50) };
    }
    const payload = gzipPayload(big);
    const jsonStr = await el._decompressPayload(payload);
    expect(JSON.parse(jsonStr)).toEqual(big);
  });

  it('DecompressionStream is available in the jsdom test environment (polyfilled in setup.js)', () => {
    expect(typeof globalThis.DecompressionStream).toBe('function');
  });
});
