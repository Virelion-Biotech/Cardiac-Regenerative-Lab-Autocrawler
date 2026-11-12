import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers import ROOT  # noqa: F401  (puts the repo root on sys.path)
import config


class ConfigTest(unittest.TestCase):
    def test_run_path_uses_registered_filenames(self):
        self.assertEqual(config.run_path("labs_final").name, "labs_final.json")
        with self.assertRaises(KeyError):
            config.run_path("unknown")

    def test_previous_run_dir_picks_latest_other_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("2023", "2025", "2099", "notes"):
                (Path(tmp) / name).mkdir()
            with mock.patch.object(config, "DATA_DIR", Path(tmp)), mock.patch.object(config, "CURRENT_YEAR", "2099"):
                self.assertEqual(config.get_previous_run_dir().name, "2025")
            with mock.patch.object(config, "DATA_DIR", Path(tmp) / "missing"):
                self.assertIsNone(config.get_previous_run_dir())

    def test_ensure_run_dir_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data" / "2099"
            with mock.patch.object(config, "CURRENT_RUN_DIR", target):
                self.assertEqual(config.ensure_run_dir(), target)
            self.assertTrue(target.is_dir())

    def test_required_env_vars(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EnvironmentError) as ctx:
                config.check_required_env_vars()
            self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x", "NCBI_EMAIL": "y"}, clear=True):
            config.check_required_env_vars()

    def test_avi_windows_are_ordered(self):
        self.assertLess(config.AVI_ACTIVE_WINDOW_MONTHS, config.AVI_INACTIVE_THRESHOLD_MONTHS)


if __name__ == "__main__":
    unittest.main()
