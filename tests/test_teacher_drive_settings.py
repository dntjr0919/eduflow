"""
교사 모바일 Drive 저장소 1순위 설정 및 서명 필요 가정통신문 게시 전 경고 단위 테스트.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtWidgets import QApplication, QMessageBox

from core.config_manager import ConfigManager
from core.base_worker import TaskRunner
from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService
from ui.tabs.tab_classroom_tools import ClassroomToolsTab
from ui.tabs.tab_settings import SettingsTab


class _FakeSnap:
    def __init__(self, doc_id: str, data: Dict[str, Any] | None):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, store: Dict[str, Dict[str, Any]], doc_id: str):
        self._store = store
        self._doc_id = doc_id

    def get(self) -> _FakeSnap:
        return _FakeSnap(self._doc_id, self._store.get(self._doc_id))

    def set(self, data: Dict[str, Any], merge: bool = False) -> None:
        if merge and self._doc_id in self._store:
            merged = dict(self._store[self._doc_id])
            merged.update(data)
            self._store[self._doc_id] = merged
        else:
            self._store[self._doc_id] = dict(data)


class _FakeCollection:
    def __init__(self, store: Dict[str, Dict[str, Any]]):
        self._store = store

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, doc_id)


class _FakeFirestore:
    def __init__(self) -> None:
        self.cols: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self.cols.setdefault(name, {}))


class _DummyDB:
    def __init__(self) -> None:
        self.logs: List[tuple] = []

    def add_log(self, level: str, source: str, message: str) -> None:
        self.logs.append((level, source, message))


class TestTeacherDriveSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication(["eduflow-drive-test"])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.db = DatabaseManager(str(self.tmp_dir / "test.db"))
        self.cfg = ConfigManager()
        self._orig_api = dict(self.cfg.get_section("api_and_integrations") or {})
        self._orig_tp = dict(self.cfg.get_section("teacher_profile") or {})

    def tearDown(self) -> None:
        self.cfg.update_section("api_and_integrations", self._orig_api, auto_save=False)
        self.cfg.update_section("teacher_profile", self._orig_tp, auto_save=True)
        try:
            self.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_save_teacher_drive_settings_direct_admin(self) -> None:
        fake_fs = _FakeFirestore()
        fake_fs.cols["계정_교사"] = {
            "teacher01": {"teacher_id": "teacher01", "name": "홍길동"}
        }
        svc = FirestoreSyncService(db=_DummyDB(), config=self.cfg)
        svc._firestore_client = fake_fs
        with patch.object(svc, "is_vercel_api_mode", return_value=False):
            ok = svc.save_teacher_drive_settings(
                teacher_id="teacher01",
                drive_priority="personal",
                workspace_drive_folder_id="ws_folder_123",
                personal_gas_url="https://script.google.com/macros/s/abc/exec",
                personal_drive_folder_id="pers_folder_456",
            )
        self.assertTrue(ok)
        saved = fake_fs.cols["계정_교사"]["teacher01"]
        self.assertEqual(saved["name"], "홍길동")
        self.assertEqual(saved["drive_priority"], "personal")
        self.assertEqual(saved["workspace_drive_folder_id"], "ws_folder_123")
        self.assertEqual(saved["personal_gas_url"], "https://script.google.com/macros/s/abc/exec")
        self.assertEqual(saved["personal_drive_folder_id"], "pers_folder_456")
        self.assertTrue(saved.get("updated_at"))

    def test_save_teacher_drive_settings_vercel_api(self) -> None:
        svc = FirestoreSyncService(db=_DummyDB(), config=self.cfg)
        with (
            patch.object(svc, "is_vercel_api_mode", return_value=True),
            patch.object(svc, "_post_vercel_api", return_value={"ok": True}) as mock_post,
        ):
            ok = svc.save_teacher_drive_settings(
                teacher_id="teacher01",
                drive_priority="invalid_mode",
                workspace_drive_folder_id="ws_folder_999",
                personal_gas_url="",
                personal_drive_folder_id="",
            )
        self.assertTrue(ok)
        mock_post.assert_called_once_with(
            "update_teacher_drive_settings",
            {
                "teacher_id": "teacher01",
                "drive_priority": "workspace",
                "workspace_drive_folder_id": "ws_folder_999",
                "personal_gas_url": "",
                "personal_drive_folder_id": "",
            },
        )

    def test_save_teacher_drive_settings_empty_teacher_id_returns_false(self) -> None:
        svc = FirestoreSyncService(db=_DummyDB(), config=self.cfg)
        self.assertFalse(svc.save_teacher_drive_settings(teacher_id=""))

    def test_settings_tab_saves_config_and_syncs_teacher_drive_settings(self) -> None:
        self.cfg.update_section(
            "teacher_profile",
            {"teacher_id": "teacher01", "name": "김교사"},
            auto_save=False,
        )
        self.cfg.update_section(
            "api_and_integrations",
            {
                "drive_priority": "personal",
                "workspace_drive_folder_id": "ws_init",
                "google_apps_script_url": "https://script.google.com/macros/s/init/exec",
                "personal_drive_folder_id": "pers_init",
            },
            auto_save=True,
        )
        with patch.object(SettingsTab, "_start_model_fetch", return_value=None):
            stab = SettingsTab(db=self.db, config=self.cfg)

        self.assertEqual(stab.combo_drive_priority.currentData(), "personal")
        self.assertEqual(stab.edit_workspace_drive_folder_id.text(), "ws_init")
        self.assertEqual(stab.edit_apps_script_url.text(), "https://script.google.com/macros/s/init/exec")
        self.assertEqual(stab.edit_personal_drive_folder_id.text(), "pers_init")

        idx = stab.combo_drive_priority.findData("workspace")
        stab.combo_drive_priority.setCurrentIndex(idx)
        stab.edit_workspace_drive_folder_id.setText("ws_updated")
        stab.edit_apps_script_url.setText("https://script.google.com/macros/s/updated/exec")
        stab.edit_personal_drive_folder_id.setText("pers_updated")

        with (
            patch.object(FirestoreSyncService, "save_teacher_drive_settings", return_value=True) as mock_sync,
            patch.object(QMessageBox, "information") as mock_info,
            patch.object(QMessageBox, "warning") as mock_warn,
        ):
            stab.save_ui_to_settings()

        mock_sync.assert_called_once_with(
            teacher_id="teacher01",
            drive_priority="workspace",
            workspace_drive_folder_id="ws_updated",
            personal_gas_url="https://script.google.com/macros/s/updated/exec",
            personal_drive_folder_id="pers_updated",
        )
        mock_info.assert_called_once()
        mock_warn.assert_not_called()

        api = self.cfg.get_section("api_and_integrations")
        self.assertEqual(api["drive_priority"], "workspace")
        self.assertEqual(api["workspace_drive_folder_id"], "ws_updated")
        self.assertEqual(api["google_apps_script_url"], "https://script.google.com/macros/s/updated/exec")
        self.assertEqual(api["personal_drive_folder_id"], "pers_updated")

    def test_settings_tab_warns_when_drive_sync_fails(self) -> None:
        self.cfg.update_section("teacher_profile", {"teacher_id": "teacher01"}, auto_save=True)
        with patch.object(SettingsTab, "_start_model_fetch", return_value=None):
            stab = SettingsTab(db=self.db, config=self.cfg)

        with (
            patch.object(FirestoreSyncService, "save_teacher_drive_settings", return_value=False),
            patch.object(QMessageBox, "information") as mock_info,
            patch.object(QMessageBox, "warning") as mock_warn,
        ):
            stab.save_ui_to_settings()

        mock_warn.assert_called_once()
        mock_info.assert_not_called()

    def test_publish_notice_warns_when_signature_required_and_drive_missing(self) -> None:
        self.cfg.update_section(
            "api_and_integrations",
            {
                "drive_priority": "workspace",
                "workspace_drive_folder_id": "",
                "google_apps_script_url": "",
            },
            auto_save=True,
        )
        tab = ClassroomToolsTab(task_runner=TaskRunner())
        tab.config = self.cfg
        tab.chk_notice_requires_reply.setChecked(True)
        idx = tab.combo_notice_signature_mode.findData("parent")
        tab.combo_notice_signature_mode.setCurrentIndex(idx)

        # 1) 취소 선택 시 게시 중단
        with (
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel) as mock_q,
            patch.object(tab, "_save_mobile_notice") as mock_save,
        ):
            tab._on_publish_mobile_notice()
        mock_q.assert_called_once()
        mock_save.assert_not_called()

        # 2) 예(Yes) 선택 시 게시 진행
        with (
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as mock_q,
            patch.object(tab, "_save_mobile_notice") as mock_save,
        ):
            tab._on_publish_mobile_notice()
        mock_q.assert_called_once()
        mock_save.assert_called_once_with(is_published=True)

    def test_publish_notice_skips_warning_when_no_signature_or_drive_configured(self) -> None:
        # 1) signature_mode == 'none' 이면 경고 없이 게시
        self.cfg.update_section(
            "api_and_integrations",
            {
                "drive_priority": "personal",
                "workspace_drive_folder_id": "",
                "google_apps_script_url": "",
            },
            auto_save=True,
        )
        tab = ClassroomToolsTab(task_runner=TaskRunner())
        tab.config = self.cfg
        tab.chk_notice_requires_reply.setChecked(False)

        with (
            patch.object(QMessageBox, "question") as mock_q,
            patch.object(tab, "_save_mobile_notice") as mock_save,
        ):
            tab._on_publish_mobile_notice()
        mock_q.assert_not_called()
        mock_save.assert_called_once_with(is_published=True)

        # 2) 1순위 Drive가 설정되어 있으면 서명 필요여도 경고 없이 게시
        self.cfg.update_section(
            "api_and_integrations",
            {
                "drive_priority": "personal",
                "workspace_drive_folder_id": "",
                "google_apps_script_url": "https://script.google.com/macros/s/ok/exec",
            },
            auto_save=True,
        )
        tab.chk_notice_requires_reply.setChecked(True)
        idx = tab.combo_notice_signature_mode.findData("both")
        tab.combo_notice_signature_mode.setCurrentIndex(idx)

        with (
            patch.object(QMessageBox, "question") as mock_q,
            patch.object(tab, "_save_mobile_notice") as mock_save,
        ):
            tab._on_publish_mobile_notice()
        mock_q.assert_not_called()
        mock_save.assert_called_once_with(is_published=True)


if __name__ == "__main__":
    unittest.main()
