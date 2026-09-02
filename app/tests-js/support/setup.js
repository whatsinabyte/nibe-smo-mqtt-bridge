// Vitest global setup for the jsdom environment.
//
// jsdom does not implement the Compression Streams API, but the card's
// gzip decompression path (_decompressPayload) relies on the global
// DecompressionStream constructor. Node 18+ ships a spec-compliant
// implementation in node:stream/web, so we polyfill it onto the jsdom
// global here rather than skipping gzip coverage.
import { CompressionStream, DecompressionStream } from 'node:stream/web';

if (typeof globalThis.DecompressionStream === 'undefined') {
  globalThis.DecompressionStream = DecompressionStream;
}
if (typeof globalThis.CompressionStream === 'undefined') {
  globalThis.CompressionStream = CompressionStream;
}

// jsdom's `<script src>` elements never fire load/error events for
// programmatically appended scripts with no network stack behind them,
// which is what the card relies on for its Fuse.js CDN fallback path
// (_loadFuse never settles). That's fine: the promise is fire-and-forget
// from the `hass` setter and nothing in the suite awaits it, so no
// jsdom "Not implemented: navigation" noise is expected either — we
// simply never let a <script src="https://cdnjs..."> tag attempt a real
// network request.
const originalCreateElement = document.createElement.bind(document);
document.createElement = (tagName, options) => {
  const el = originalCreateElement(tagName, options);
  if (typeof tagName === 'string' && tagName.toLowerCase() === 'script') {
    // Prevent jsdom from ever trying to resolve the src (no network in CI).
    Object.defineProperty(el, 'src', {
      configurable: true,
      get() {
        return this._src || '';
      },
      set(value) {
        this._src = value;
        // Never actually loads; onerror/onload simply never fire, matching
        // real jsdom behaviour for external <script> resources by default.
      },
    });
  }
  return el;
};
