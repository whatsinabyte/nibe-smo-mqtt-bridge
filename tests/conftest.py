"""
conftest.py
===========
Shared fixtures, Hypothesis strategies, and profile configuration for the
Nibe S-Series MQTT Bridge test suite.

Profiles
--------
  ci       (default) — 20 examples, fast feedback during development
  thorough            — 500 examples, run manually before releases
  nightly             — 500 examples + stateful_step_count=50, midnight automation
                        (same settings as thorough; higher values cause ODROID-M1 timeout)

Select a profile via the HYPOTHESIS_PROFILE environment variable:
  HYPOTHESIS_PROFILE=thorough pytest tests/
  HYPOTHESIS_PROFILE=nightly  pytest tests/

The variable is read at import time so it takes effect for every test in the
run — setting it after conftest has already loaded has no effect.

database=None
    Hypothesis example database is disabled in all profiles.
    This prevents FlakyStrategyDefinition / FlakyFailure errors caused by
    st.text() strategies generating Unicode surrogates that hash non-deterministically
    on Python 3.12 — making replay of cached examples unreliable.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

try:
    from hypothesis import HealthCheck, settings
    from hypothesis import strategies as st

    # HealthCheck.too_slow: common in property-based tests with complex strategies.
    # HealthCheck.differing_executors: fires when pytest is invoked in-process
    #   multiple times from the same interpreter — exactly what mutmut 3.x does
    #   during its stats-collection and clean-test phases. Safe to suppress here.
    _suppress = [HealthCheck.too_slow]
    if hasattr(HealthCheck, 'differing_executors'):
        _suppress.append(HealthCheck.differing_executors)

    settings.register_profile(
        "ci",
        max_examples=20,
        deadline=None,
        suppress_health_check=_suppress,
        database=None,
    )

    settings.register_profile(
        "thorough",
        max_examples=500,
        deadline=None,
        suppress_health_check=_suppress,
        database=None,
    )

    # nightly is an alias for thorough — 500 examples is the practical ceiling
    # for the ODROID-M1 (~50 min total). Higher values cause the midnight
    # automation to time out before the suite finishes.
    settings.register_profile(
        "nightly",
        max_examples=500,
        stateful_step_count=50,
        deadline=None,
        suppress_health_check=_suppress,
        database=None,
    )

    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

except ImportError:
    pass  # hypothesis not installed — Hypothesis tests will use default settings


# ---------------------------------------------------------------------------
# Shared factory — EntityManager with minimal mocking, no live broker needed.
# device_info / device_name are set as attributes post-construction, exactly
# as generate_nibe_mqtt.py does at startup.
# ---------------------------------------------------------------------------

def _make_em():
    with patch('nibe_entity_manager.EntityManager.resubscribe_all'), \
         patch('nibe_entity_manager.EntityManager._setup_history_loading'), \
         patch('nibe_entity_manager.EntityManager._setup_dynamic_map_loading'):
        from nibe_entity_manager import EntityManager
        em = EntityManager(
            api_client  = MagicMock(),
            publisher   = MagicMock(),
            notify_fn   = MagicMock(),
            dismiss_fn  = MagicMock(),
            mqtt_client = MagicMock(),
        )
    em.device_info = {}
    em.device_name = 'Test'
    em._api.last_error = None
    return em


def _cannot_be_int(s: str) -> bool:
    """Return True when int(s) would raise.  Used as a Hypothesis filter to
    match what production code does: int() strips whitespace, so '0\\r', ' 1 '
    etc. all parse successfully and str.isdigit() would wrongly pass them.
    """
    try:
        int(s)
        return False
    except (ValueError, OverflowError):
        return True


# ---------------------------------------------------------------------------
# Hypothesis strategies for Nibe-relevant data types
# ---------------------------------------------------------------------------

try:
    from hypothesis import strategies as st

    _nibe_raw_value = st.integers(min_value=-32768, max_value=32767)
    _nibe_divisor   = st.integers(min_value=0, max_value=10000)

    # Nibe point ID strategy — designed to exercise both known and unknown IDs:
    #
    #   st.integers(0..65535): full Modbus 16-bit register range — covers any
    #     point ID Nibe might add in future firmware, including ones we've never
    #     seen. Does not cap at the current max (56150) to avoid missing new points.
    #
    #   st.sampled_from(known_interaction_pids): guarantees every run hits the
    #     handful of IDs that have VALUE_MAPPINGS or ENTITY_TYPE_OVERRIDES entries,
    #     regardless of example count. Without this, a 20-example CI run may never
    #     hit e.g. pid=4821 which routes to 'select' despite looking like a switch.
    #
    # The combination catches both future unknown IDs and known tricky IDs.
    def _make_nibe_point_id_strategy():
        from nibe_entity_detection import ENTITY_TYPE_OVERRIDES, VALUE_MAPPINGS
        known_pids = list({
            pid
            for reg in VALUE_MAPPINGS.values()
            for pid in reg
        } | set(ENTITY_TYPE_OVERRIDES.keys()))
        return st.one_of(
            st.integers(min_value=0, max_value=65535),
            st.sampled_from(known_pids) if known_pids else st.nothing(),
        )

    _nibe_point_id = _make_nibe_point_id_strategy()

    _unicode_text      = st.text(min_size=0, max_size=200)
    _safe_entity_id    = st.text(
        alphabet=st.characters(categories=['L', 'N'],
                               include_characters='_'),
        min_size=1, max_size=30,
    )
    _nibe_title_chars  = st.text(
        alphabet=st.characters(
            categories=['L', 'N', 'P', 'S', 'Z'],
            include_characters='\u00ad\u00c2\u00a0\xa0',
        ),
        min_size=0, max_size=100,
    )

    # Strategy: build a DynamicPointMap with a controlled set of entries
    _dyn_map_entry = st.fixed_dictionaries({
        'point_id':    st.integers(min_value=1, max_value=500),
        'title':       st.text(max_size=20),
        'entity_type': st.sampled_from(['switch', 'select']),
        'dynamic_points_by_value': st.dictionaries(
            st.integers(min_value=0, max_value=5),
            st.lists(st.integers(min_value=1000, max_value=2000), min_size=0, max_size=3),
            max_size=4,
        ),
    })

    # Strategy: a point dict shaped like EntityManager's all_points_by_id values
    _bulk_point = st.fixed_dictionaries({
        'variableId':    st.integers(min_value=1, max_value=9999),
        'display_title': st.text(max_size=20),
        'metadata': st.fixed_dictionaries({
            'minValue': st.integers(min_value=0, max_value=5),
            'maxValue': st.integers(min_value=0, max_value=5),
        }),
    })

    _point_metadata = st.fixed_dictionaries({
        'isWritable': st.booleans(),
        'modbusRegisterType': st.sampled_from([
            'MODBUS_HOLDING_REGISTER', 'MODBUS_INPUT_REGISTER', 'MODBUS_NO_REGISTER'
        ]),
        'minValue': st.integers(min_value=-100, max_value=100),
        'maxValue': st.integers(min_value=-100, max_value=100),
        'intDefaultValue': st.one_of(st.none(), st.integers(min_value=-100, max_value=100)),
        'divisor': st.integers(min_value=0, max_value=100),
        'unit': st.text(max_size=5),
        'shortUnit': st.text(max_size=5),
    })

    _point_entry = st.fixed_dictionaries({
        'metadata': _point_metadata,
        'variableId': st.integers(min_value=1, max_value=99999),
    })

except ImportError:
    # Hypothesis not installed — strategies won't be available but the
    # non-Hypothesis tests will still run.
    _nibe_raw_value = None
    _nibe_divisor   = None
    _nibe_point_id  = None
    _unicode_text   = None
    _safe_entity_id = None
    _nibe_title_chars = None
    _dyn_map_entry  = None
    _bulk_point     = None
    _point_metadata = None
    _point_entry    = None


# ---------------------------------------------------------------------------
# Path constants — derived from module location so tests work both in the
# dev container (/home/claude/tests/) and on the Odroid (/app/)
# ---------------------------------------------------------------------------
_APP_DIR  = os.path.dirname(os.path.abspath(__file__))
# Production modules may live in app/ (repo layout) or alongside tests
# (flat layout) — probe several candidate locations in priority order.
_REPO_DIR = os.path.dirname(_APP_DIR)

# Candidate: sister app/ directory (new repo layout: tests/ + app/)
if os.path.isdir(os.path.join(_APP_DIR, '..', 'app')):
    _APP_DIR = os.path.normpath(os.path.join(_APP_DIR, '..', 'app'))

# Candidate: /mnt/project/ (dev container — production code lives there)
if not os.path.exists(os.path.join(_APP_DIR, 'menu_structure.yaml')) and os.path.isdir('/mnt/project'):
    _APP_DIR = '/mnt/project'

# Resolve menu_structure.yaml — lives in app/ relative to repo root.
_MENU_YAML = os.path.join(_APP_DIR, 'menu_structure.yaml')
if not os.path.exists(_MENU_YAML):
    _MENU_YAML = os.path.join(_REPO_DIR, 'app', 'menu_structure.yaml')
if not os.path.exists(_MENU_YAML):
    _MENU_YAML = os.path.join(_REPO_DIR, 'menu_structure.yaml')


# ---------------------------------------------------------------------------
# menu_structure.yaml cache reset — global, not per-file
# ---------------------------------------------------------------------------
# nibe_lovelace._load_menu_structure_yaml() caches parsed menu_structure.yaml
# per path for the life of the process (see nibe_lovelace.py). Several test
# files mock open()/yaml.safe_load for the SAME real path this cache uses
# (test_ha_integration.py, test_lovelace.py, test_generate.py), so a mocked
# or fake-YAML result from one test can silently leak into a later test in
# ANY file expecting the real content — this bit us for real once already
# (two test_ha_integration.py failures under pytest-randomly, caused by a
# test_ha_integration.py test earlier in randomized order poisoning the
# cache for TestBuildMenuPointsProperties later in the same file). A
# per-file fixture isn't enough since the pollution isn't confined to one
# file — this must be global and autouse for every test in the suite.
@pytest.fixture(autouse=True)
def _reset_menu_structure_cache():
    import nibe_lovelace as nl
    nl._reset_menu_structure_cache()
    yield
    nl._reset_menu_structure_cache()


# nibe_entity_detection._description_mapping_cache is the same kind of
# process-lifetime, path/description-keyed module cache as the menu cache
# above. It's currently reset manually via .clear() in a handful of
# test_entity_detection.py tests rather than automatically — that's the
# exact pattern that let the menu-cache pollution bug above go undetected
# for a long time under pytest-randomly. Resetting it here for every test,
# not just the ones that remember to, closes that same latent risk.
@pytest.fixture(autouse=True)
def _reset_description_mapping_cache():
    import nibe_entity_detection as ned
    ned._description_mapping_cache.clear()
    yield
    ned._description_mapping_cache.clear()


# generate_nibe_mqtt._derive_device_id() persists the real, serial-derived
# device_id to _DEVICE_ID_FILE (defaults to /data/device_id) so a later
# startup during a transient outage reuses it instead of falling back to
# the generic default (see _derive_device_id's docstring for the real bug
# this fixes: a duplicate, empty "ghost" HA device from device_id
# flip-flopping across restarts). The nightly test suite runs *inside the
# real add-on container*, where /data genuinely exists and is writable —
# without this fixture, any test that exercises _build_infrastructure()
# without explicitly mocking _derive_device_id (most of them don't) would
# write real or fake serial-derived device_id strings straight over the
# genuine persisted file on a live installation. Same risk class as the
# menu-cache/description-cache pollution above, just against a real
# filesystem path instead of an in-process cache — autouse so no test can
# forget to isolate it, matching that same lesson.
@pytest.fixture(autouse=True)
def _isolate_device_id_persistence(tmp_path):
    import generate_nibe_mqtt as gn
    original = gn._DEVICE_ID_FILE
    gn._DEVICE_ID_FILE = str(tmp_path / 'device_id')
    yield
    gn._DEVICE_ID_FILE = original
