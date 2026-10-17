"""The built-in "Run Test Suite" button runs this suite inside the add-on,
where /data is the installation's real state. The autouse fixtures in
conftest.py redirect every /data path to a temporary directory; this guard
fails when a module gains a /data path the fixtures don't cover yet — as
reclassified_points.json once did, so a test run overwrote the real set of
reclassified binary sensors with a test point id."""

import unittest

import generate_nibe_mqtt
import nibe_dynamic_map
import nibe_entity_manager
import nibe_lovelace

_MODULES = (generate_nibe_mqtt, nibe_dynamic_map, nibe_entity_manager, nibe_lovelace)


class TestNoDataPathDuringTests(unittest.TestCase):
    def test_every_data_path_is_redirected(self):
        leaks = [
            f"{module.__name__}.{name} = {value!r}"
            for module in _MODULES
            for name, value in vars(module).items()
            if isinstance(value, str) and value.startswith("/data/")
        ]
        self.assertEqual(leaks, [], "add these to the autouse fixtures in conftest.py")


class TestNoSupervisorTokenDuringTests(unittest.TestCase):
    def test_supervisor_token_is_removed(self):
        """In the add-on the suite inherits the real SUPERVISOR_TOKEN; with it,
        an unpatched notify_ha reaches the live Home Assistant."""
        import os

        self.assertNotIn("SUPERVISOR_TOKEN", os.environ)
