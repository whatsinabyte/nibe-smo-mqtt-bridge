"""
js_card_harness.py
===================
Loads the real nibe-entity-manager-card.js into a QuickJS context so tests
can call its actual methods, instead of maintaining a hand-written Python
mirror of the JS logic that can silently drift from the shipped file.

The card is a browser Web Component (extends HTMLElement, self-registers
via customElements.define at module scope), so a couple of minimal stubs
are required just to let the file load — this does NOT provide a real DOM.
Only methods that are pure, or depend on a small/stubbable slice of `this`
(no shadowRoot/DOM queries, no browser-only APIs like DecompressionStream),
can be meaningfully exercised this way. That's a deliberate scope: it turns
"does the shipped file's decision logic behave correctly" from an assertion
into something actually verified, without attempting to fake an entire
browser environment.

quickjs is a development/test-only dependency (see requirements-dev.txt) —
not installed in the on-device Docker image, since a prebuilt wheel isn't
guaranteed on ARM64 and building the C extension isn't worth requiring on
an ODROID-M1. Tests using this harness must skip gracefully when it's
unavailable rather than failing the on-device "Run Test Suite" button.
"""

import os

try:
    import quickjs

    QUICKJS_AVAILABLE = True
except ImportError:
    QUICKJS_AVAILABLE = False

_APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
if not os.path.exists(os.path.join(_APP_DIR, "nibe-entity-manager-card.js")):
    _APP_DIR = "/mnt/project/app"
_CARD_JS_PATH = os.path.join(_APP_DIR, "nibe-entity-manager-card.js")

# Minimal browser-global stubs needed only so the file can be parsed and
# evaluated (class NibeEntityManager extends HTMLElement; the trailing
# customElements.define(...) call). Nothing beyond this is provided —
# methods that touch a real DOM aren't reachable through this harness.
_STUBS = """
class HTMLElement {
  attachShadow(opts) { this.shadowRoot = {}; return this.shadowRoot; }
}
var customElements = { define: function() {} };
"""


def load_card_context():
    """Return a quickjs.Context with the real card file evaluated into it.

    NibeEntityManager is then available as a global class in the context —
    call static methods directly (NibeEntityManager.someMethod(...)) or
    instance methods via NibeEntityManager.prototype.someMethod.call(fakeThis, ...).
    """
    ctx = quickjs.Context()
    ctx.eval(_STUBS)
    with open(_CARD_JS_PATH, encoding="utf-8") as f:
        ctx.eval(f.read())
    return ctx
