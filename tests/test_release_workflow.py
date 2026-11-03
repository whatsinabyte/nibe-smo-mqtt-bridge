"""The publish workflow refuses a version that is not config.yaml's.

The step's own script is run, so the check cannot rot unnoticed. Skipped where
the repository is not there (the add-on image ships tests/ but not .github/).
"""

import os
import re
import subprocess
import unittest

import yaml

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_WORKFLOW = os.path.join(_ROOT, ".github", "workflows", "publish-image.yml")


@unittest.skipUnless(os.path.exists(_WORKFLOW), "repository checkout needed")
class TestPublishWorkflowVersionCheck(unittest.TestCase):
    def _step(self):
        with open(_WORKFLOW, encoding="utf-8") as f:
            workflow = yaml.safe_load(f)
        steps = workflow["jobs"]["build"]["steps"]
        found = [s for s in steps if s.get("name", "").startswith("Check that the version")]
        self.assertEqual(len(found), 1)
        # Before anything is built or pushed.
        self.assertLess(
            steps.index(found[0]),
            next(i for i, s in enumerate(steps) if "build-push" in s.get("uses", "")),
        )
        return found[0]

    def _run(self, requested):
        step = self._step()
        return subprocess.run(
            ["bash", "-c", step["run"]],
            cwd=_ROOT,
            env={**os.environ, "REQUESTED": requested},
            capture_output=True,
            text=True,
            check=False,
        )

    def _configured(self):
        with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
            return re.search(r'^version:\s*"([^"]+)"', f.read(), re.MULTILINE).group(1)

    def test_the_configured_version_passes(self):
        self.assertEqual(self._run(self._configured()).returncode, 0)

    def test_any_other_version_fails_and_says_why(self):
        for requested in ("0.0.0", self._configured() + ".1", "", "v" + self._configured()):
            with self.subTest(requested=requested):
                result = self._run(requested)
                self.assertEqual(result.returncode, 1)
                self.assertIn("does not match config.yaml's version", result.stdout)

    def test_the_input_reaches_the_script_through_env_only(self):
        self.assertNotIn("github.event.inputs", self._step()["run"])


if __name__ == "__main__":
    unittest.main()
