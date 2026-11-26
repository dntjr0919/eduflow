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

    def where(self, field_path: str, op_string: str, value: Any) -> "_FakeCollection":
        if op_string == "==":
            filtered = {
                k: v for k, v in self._store.items() if v.get(field_path) == value
            }
            return _FakeCollection(filtered)
        return self

    def stream(self) -> List[_FakeSnap]:
        return [_FakeSnap(k, v) for k, v in self._store.items()]


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

    def test_save_mobile_notice_aborts_drive_upload_on_hwpx_pdf_failure(self) -> None:
        hwpx_path = self.tmp_dir / "notice_test.hwpx"
        hwpx_path.write_bytes(b"fake-hwpx-content")

        tab = ClassroomToolsTab(task_runner=TaskRunner())
        tab.config = self.cfg
        tab.db = self.db
        tab.edit_notice_title.setText("HWPX 변환 실패 테스트")
        tab._selected_notice_pdf_path = str(hwpx_path)

        def _sync_run_async(*, work_fn, on_result, **kwargs):
            res = work_fn()
            on_result(res)

        with (
            patch.object(tab, "_run_async_button_task", side_effect=_sync_run_async),
            patch(
                "services.notice_parser_service.convert_hwp_to_pdf_with_reason",
                return_value=(None, "PDF 저장 실패(SaveAs 및 FileSaveAs_S 모두 실패)"),
            ),
            patch.object(FirestoreSyncService, "upload_notice_attachment") as mock_upload,
            patch.object(FirestoreSyncService, "publish_mobile_notice") as mock_publish,
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel
            ) as mock_q,
        ):
            tab._save_mobile_notice(is_published=True)

        mock_upload.assert_not_called()
        mock_publish.assert_not_called()
        mock_q.assert_called_once()
        msg_text = mock_q.call_args[0][2]
        self.assertIn("모바일에서 원문 PDF 보기를 위해 PDF 변환이 필요합니다.", msg_text)
        self.assertIn("PDF 저장 실패", msg_text)
        self.assertIn("PDF 변환 실패", tab.lbl_notice_pdf_status.text())

    def test_fetch_notice_detail_and_restore_ui(self) -> None:
        fake_fs = _FakeFirestore()
        svc = FirestoreSyncService(db=self.db, config=self.cfg)
        svc._firestore_client = fake_fs
        svc._vercel_api_url = ""
        tenant = svc._get_tenant_defaults()
        notice_id = "noti_doc_restore_001"
        fake_fs.cols["가정통신문_목록"] = {
            notice_id: {
                "remote_id": notice_id,
                "title": "현장체험학습 안내",
                "category": "체험학습",
                "content": "안내 본문입니다.",
                "due_date": "2026-12-05",
                "requires_reply": True,
                "signature_mode": "both",
                "attachment_url": "https://drive.google.com/file/d/FILE_ID_123/view",
                "attachment_file_id": "FILE_ID_123",
                "reply_schema_json": '[{"key":"consent","label":"참가 동의","type":"radio","options":["동의","미동의"],"required":true}]',
                "pdf_field_map_json": '[{"palette_id":"field:consent","field_key":"consent","page":0,"x":0.2,"y":0.3,"w":0.15,"h":0.05}]',
                "teacher_id": tenant.get("teacher_id", ""),
                "school_code": tenant.get("school_code", ""),
            }
        }

        with patch.object(svc, "is_vercel_api_mode", return_value=False):
            detail = svc.fetch_notice_detail(notice_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["title"], "현장체험학습 안내")
        self.assertEqual(len(detail["reply_schema"]), 1)
        self.assertEqual(len(detail["pdf_field_map"]), 1)

        tab = ClassroomToolsTab(task_runner=TaskRunner())
        tab.config = self.cfg
        tab.db = self.db
        tab.edit_active_notice_id.setText(notice_id)

        def _sync_run_async(*, work_fn, on_result, **kwargs):
            on_result(work_fn())

        with (
            patch.object(tab, "_run_async_button_task", side_effect=_sync_run_async),
            patch.object(FirestoreSyncService, "fetch_notice_replies", return_value=[]),
            patch.object(FirestoreSyncService, "fetch_notice_detail", return_value=detail),
        ):
            tab._on_refresh_notice_replies()

        self.assertEqual(tab.edit_notice_title.text(), "현장체험학습 안내")
        self.assertEqual(tab.edit_notice_due.text(), "2026-12-05")
        self.assertEqual(tab.combo_notice_category.currentText(), "체험학습")
        self.assertEqual(tab.txt_notice_content.toPlainText(), "안내 본문입니다.")
        self.assertEqual(tab.combo_notice_signature_mode.currentData(), "both")
        self.assertEqual(
            tab.edit_notice_attachment_url.text(),
            "https://drive.google.com/file/d/FILE_ID_123/view",
        )
        self.assertEqual(tab._selected_notice_attachment_file_id, "FILE_ID_123")
        self.assertEqual(tab.table_reply_schema.rowCount(), 1)
        self.assertEqual(len(tab._notice_pdf_field_map), 1)
        self.assertIn("좌표 1개", tab.lbl_notice_pdf_status.text())

    def test_drive_pdf_download_fallback_and_mapper_open(self) -> None:
        import base64

        svc = FirestoreSyncService(db=self.db, config=self.cfg)
        pdf_bytes = b"%PDF-1.4\n%fake-drive-pdf-content\n%%EOF"
        b64_pdf = base64.b64encode(pdf_bytes).decode("ascii")

        with (
            patch.object(svc, "get_vercel_api_url", return_value="https://example.vercel.app/api/firestore"),
            patch.object(
                svc,
                "_post_vercel_api",
                return_value={"ok": True, "file_base64": b64_pdf},
            ),
            patch("urllib.request.urlopen", side_effect=RuntimeError("network blocked")),
        ):
            dl_path = svc.download_notice_pdf(
                attachment_url="https://drive.google.com/file/d/DRIVE_PDF_999/view",
                attachment_file_id="DRIVE_PDF_999",
            )

        self.assertIsNotNone(dl_path)
        self.assertTrue(dl_path.is_file())
        self.assertEqual(dl_path.read_bytes(), pdf_bytes)

        tab = ClassroomToolsTab(task_runner=TaskRunner())
        tab.config = self.cfg
        tab.db = self.db
        tab._selected_notice_pdf_path = ""
        tab.edit_notice_attachment_url.setText("https://drive.google.com/file/d/DRIVE_PDF_999/view")
        tab._selected_notice_attachment_file_id = "DRIVE_PDF_999"

        def _sync_run_async(*, work_fn, on_result, **kwargs):
            on_result(work_fn())

        with (
            patch.object(tab, "_run_async_button_task", side_effect=_sync_run_async),
            patch.object(FirestoreSyncService, "download_notice_pdf", return_value=dl_path) as mock_dl,
            patch.object(tab, "_open_notice_pdf_mapper_with_path") as mock_open,
        ):
            tab._on_open_notice_pdf_mapper()

        mock_dl.assert_called_once()
        mock_open.assert_called_once_with(str(dl_path))
        self.assertEqual(tab._selected_notice_pdf_path, str(dl_path))

        # 사후 좌표 업데이트(update_notice_pdf_field_map) 검증
        fake_fs = _FakeFirestore()
        svc_admin = FirestoreSyncService(db=self.db, config=self.cfg)
        svc_admin._firestore_client = fake_fs
        svc_admin._vercel_api_url = ""
        with patch.object(svc_admin, "is_vercel_api_mode", return_value=False):
            ok_update = svc_admin.update_notice_pdf_field_map(
                "noti_doc_restore_001",
                [{"palette_id": "sig:parent", "page": 0, "x": 0.5, "y": 0.8, "w": 0.2, "h": 0.08}],
            )
        self.assertTrue(ok_update)
        saved_doc = fake_fs.cols["가정통신문_목록"]["noti_doc_restore_001"]
        self.assertEqual(len(saved_doc["pdf_field_map"]), 1)
        self.assertIn("sig:parent", saved_doc["pdf_field_map_json"])

    def test_fetch_published_notices_isolation_and_combo_binding(self) -> None:
        fake_fs = _FakeFirestore()
        svc = FirestoreSyncService(db=self.db, config=self.cfg)
        svc._firestore_client = fake_fs
        svc._vercel_api_url = ""
        tenant = svc._get_tenant_defaults()
        my_tid = tenant.get("teacher_id") or "teacher_me"
        my_sc = tenant.get("school_code") or "SC001"
        if not tenant.get("teacher_id"):
            self.cfg.update_section(
                "teacher_profile",
                {"teacher_id": my_tid, "school_code": my_sc, "grade": 3, "class_no": 2},
                auto_save=True,
            )

        fake_fs.cols["가정통신문_목록"] = {
            "noti_old": {
                "remote_id": "noti_old",
                "title": "오래된 안내문",
                "due_date": "2026-11-10",
                "is_published": True,
                "teacher_id": my_tid,
                "school_code": my_sc,
                "updated_at": "2026-11-10 09:00:00",
            },
            "noti_new": {
                "remote_id": "noti_new",
                "title": "최신 체험학습",
                "due_date": "2026-12-01",
                "is_published": False,
                "teacher_id": my_tid,
                "school_code": my_sc,
                "updated_at": "2026-11-26 10:00:00",
            },
            "noti_other_teacher": {
                "remote_id": "noti_other_teacher",
                "title": "다른 교사 문서",
                "due_date": "2026-12-02",
                "is_published": True,
                "teacher_id": "other_teacher_999",
                "school_code": my_sc,
                "updated_at": "2026-11-26 11:00:00",
            },
        }

        with patch.object(svc, "is_vercel_api_mode", return_value=False):
            notices = svc.fetch_published_notices(limit=10)

        self.assertEqual([n["remote_id"] for n in notices], ["noti_new", "noti_old"])

        tab = ClassroomToolsTab(task_runner=TaskRunner())
        tab.config = self.cfg
        tab.db = self.db

        def _sync_run_async(*, work_fn, on_result, **kwargs):
            on_result(work_fn())

        with (
            patch.object(tab, "_run_async_button_task", side_effect=_sync_run_async),
            patch.object(FirestoreSyncService, "fetch_published_notices", return_value=notices),
            patch.object(tab, "_on_refresh_notice_replies") as mock_refresh_replies,
        ):
            tab._on_refresh_published_notices()
            self.assertEqual(tab.combo_published_notices.count(), 3)
            self.assertIn("[최신 체험학습] (2026-12-01/후보)", tab.combo_published_notices.itemText(1))
            self.assertIn("[오래된 안내문] (2026-11-10/게시됨)", tab.combo_published_notices.itemText(2))
            mock_refresh_replies.assert_not_called()

            tab.combo_published_notices.setCurrentIndex(1)
            self.assertEqual(tab.edit_active_notice_id.text(), "noti_new")
            mock_refresh_replies.assert_called_once()


if __name__ == "__main__":
    unittest.main()
