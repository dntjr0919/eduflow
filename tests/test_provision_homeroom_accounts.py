"""
SettingsTab._on_provision_homeroom_accounts() 및 DatabaseManager 담임 학급 명렬표 조회 단위 테스트.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService
from ui.tabs.tab_settings import SettingsTab

_APP_INSTANCE = None


def _ensure_qapp():
    global _APP_INSTANCE
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        _APP_INSTANCE = QApplication(["eduflow-provision-test"])
        return _APP_INSTANCE
    return app


class _InlineTaskRunner:
    def start_worker(
        self,
        worker,
        task_key=None,
        on_result=None,
        on_error=None,
        on_finished=None,
    ):
        try:
            res = worker.execute()
            if on_result is not None:
                on_result(res)
        except Exception as exc:
            if on_error is not None:
                on_error(str(exc))
            else:
                raise
        finally:
            if on_finished is not None:
                on_finished()
        return task_key or "inline"


class TestProvisionHomeroomAccounts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db = DatabaseManager(db_rel_path=str(self.tmp_path / "test_eduflow.db"))
        self.cfg = ConfigManager()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_database_homeroom_group_and_students_wrappers(self) -> None:
        self.assertIsNone(self.db.get_active_homeroom_group())
        self.assertIsNone(self.db.get_homeroom_group())

        gid = self.db.create_roster_group(
            group_type="homeroom",
            group_name="1학년 5반",
            is_active=1,
        )
        self.db.add_roster_students(
            gid,
            [
                {"seq_no": 1, "student_id": "10501", "grade": 1, "class_no": 5, "number": 1, "name": "김유민"},
                {"seq_no": 2, "student_id": "10502", "grade": 1, "class_no": 5, "number": 2, "name": "박시우"},
            ],
        )

        active_grp = self.db.get_active_homeroom_group()
        compat_grp = self.db.get_homeroom_group()
        self.assertIsNotNone(active_grp)
        self.assertEqual(active_grp["id"], gid)
        self.assertEqual(compat_grp["id"], gid)

        students_by_roster = self.db.get_roster_students(gid)
        students_by_compat = self.db.get_students(group_id=gid)
        self.assertEqual(len(students_by_roster), 2)
        self.assertEqual([s["student_id"] for s in students_by_compat], ["10501", "10502"])

    @patch("PyQt6.QtWidgets.QMessageBox.warning")
    def test_on_provision_homeroom_accounts_warns_when_empty(self, mock_warn) -> None:
        _ensure_qapp()
        with patch.object(SettingsTab, "_start_model_fetch", return_value=None):
            stab = SettingsTab(db=self.db, config=self.cfg)
        stab.task_runner = _InlineTaskRunner()

        stab._on_provision_homeroom_accounts()
        mock_warn.assert_called_once()
        self.assertIn("학급명렬표", mock_warn.call_args[0][2])
        stab.close()

    @patch("PyQt6.QtWidgets.QMessageBox.information")
    @patch("PyQt6.QtWidgets.QMessageBox.critical")
    def test_on_provision_homeroom_accounts_runs_without_error(self, mock_crit, mock_info) -> None:
        _ensure_qapp()
        gid = self.db.create_roster_group(
            group_type="homeroom",
            group_name="1학년 5반",
            is_active=1,
        )
        self.db.add_roster_students(
            gid,
            [
                {"seq_no": 1, "student_id": "10501", "grade": 1, "class_no": 5, "number": 1, "name": "김유민"},
                {"seq_no": 2, "student_id": "10502", "grade": 1, "class_no": 5, "number": 2, "name": "박시우"},
            ],
        )

        with patch.object(SettingsTab, "_start_model_fetch", return_value=None):
            stab = SettingsTab(db=self.db, config=self.cfg)
        stab.task_runner = _InlineTaskRunner()
        stab.edit_neis_school_code.setText("8140246")
        stab.edit_school_name.setText("천안오성고")

        with patch.object(
            FirestoreSyncService,
            "provision_homeroom_accounts",
            return_value={
                "students_created": 2,
                "students_skipped": 0,
                "parents_created": 2,
                "parents_skipped": 0,
            },
        ) as mock_prov:
            stab._on_provision_homeroom_accounts()

        mock_crit.assert_not_called()
        mock_prov.assert_called_once()
        call_kwargs = mock_prov.call_args.kwargs
        self.assertEqual(call_kwargs["school_code"], "8140246")
        self.assertEqual(call_kwargs["school_name"], "천안오성고")
        self.assertEqual([s["student_id"] for s in call_kwargs["students"]], ["10501", "10502"])
        mock_info.assert_called_once()
        self.assertIn("학생 신규 생성: 2명", mock_info.call_args[0][2])
        stab.close()


if __name__ == "__main__":
    unittest.main()
