"""
vulture_whitelist.py
=====================
Known false positives for `vulture app/`.

Both entries below are exercised only from tests/ (test-isolation hooks and
test-only handler invocations), which a plain `vulture app/` scan can't see
— scanning `app/ tests/` together was tried instead but produces far more
noise than signal (mock-heavy test suites routinely trip vulture's generic
`__exit__`/`__enter__` heuristics). Whitelisting the two specific known
false positives keeps the app/-only scan meaningful for catching genuinely
new dead code.

Regenerate this file's contents with:
    vulture app/ --make-whitelist
"""

_._on_wanted_points_message  # unused attribute (app/nibe_entity_manager.py:4022) — only invoked directly by tests/test_entity_manager_dynamic.py
_reset_menu_structure_cache  # unused function (app/nibe_lovelace.py:53) — only called by the autouse fixture in tests/conftest.py
