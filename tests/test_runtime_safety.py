import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.firebase_client import DEFAULT_SERVICE_ACCOUNT_FILENAME, resolve_local_cred_path
from utils import path_helper


class TestRuntimeSafetyPaths(unittest.TestCase):
    def test_frozen_resource_and_user_data_separation(self) -> None:
        with tempfile.TemporaryDirectory() as meipass_dir, tempfile.TemporaryDirectory() as local_app_dir, tempfile.TemporaryDirectory() as exe_dir:
            fake_exe = Path(exe_dir) / "EduFlow.exe"
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "_MEIPASS", meipass_dir, create=True), \
                 patch.object(sys, "executable", str(fake_exe)), \
                 patch.dict(os.environ, {"LOCALAPPDATA": local_app_dir}):

                res_path = Path(path_helper.get_resource_path("ui/styles/theme.qss"))
                self.assertEqual(res_path, Path(meipass_dir).resolve() / "ui" / "styles" / "theme.qss")

                data_path = Path(path_helper.get_data_path("logs/crash.log"))
                expected_data_root = Path(local_app_dir).resolve() / "EduFlow"
                self.assertEqual(data_path, expected_data_root / "logs" / "crash.log")
                self.assertTrue(data_path.parent.exists())

    def test_frozen_firebase_cred_prefers_exe_dir(self) -> None:
        with tempfile.TemporaryDirectory() as exe_dir, tempfile.TemporaryDirectory() as local_app_dir:
            fake_exe = Path(exe_dir) / "EduFlow.exe"
            exe_cred = Path(exe_dir) / DEFAULT_SERVICE_ACCOUNT_FILENAME
            exe_cred.write_text("{}", encoding="utf-8")

            user_cred_dir = Path(local_app_dir) / "EduFlow"
            user_cred_dir.mkdir(parents=True, exist_ok=True)
            user_cred = user_cred_dir / DEFAULT_SERVICE_ACCOUNT_FILENAME
            user_cred.write_text("{}", encoding="utf-8")

            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(fake_exe)), \
                 patch.dict(os.environ, {"LOCALAPPDATA": local_app_dir}):
                resolved = resolve_local_cred_path()
                self.assertEqual(resolved, exe_cred)


if __name__ == "__main__":
    unittest.main()
