"""
docs/apps_script_template.gs 스키마 정합성 및 GoogleSheetsSyncService 확장 메서드 단위 테스트.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.google_sheets_sync_service import GoogleSheetsSyncService


class TestAppsScriptTemplateAndSyncExtensions(unittest.TestCase):
    def setUp(self):
        self.gs_path = PROJECT_ROOT / "docs" / "apps_script_template.gs"
        self.gs_text = self.gs_path.read_text(encoding="utf-8")
        self.db = MagicMock()
        self.cfg = MagicMock()
        self.cfg.get.return_value = ""
        self.sync = GoogleSheetsSyncService(db=self.db, config=self.cfg)

    def tearDown(self):
        pass

    def test_gs_contains_all_required_sheets(self):
        required_sheets = [
            "Schedules",
            "Tasks",
            "Notifications",
            "계정_교사",
            "계정_학생",
            "계정_학부모",
            "일일_출결",
            "설정_서류규정",
            "서류_추적",
            "가정통신문_목록",
            "가정통신문_회신",
            "시간표_기본",
            "시간표_변동",
            "급식_식단표",
            "1:1_상담채팅",
        ]
        for sheet in required_sheets:
            self.assertIn(sheet, self.gs_text, f"누락된 시트: {sheet}")

    def test_gs_preserves_legacy_and_adds_mobile_actions(self):
        required_actions = [
            "ping",
            "pull_all",
            "init_sheets",
            "upsert_schedule",
            "delete_schedule",
            "upsert_task",
            "update_task_status",
            "add_notification",
            "mark_notification_read",
            "push_batch",
            "login",
            "get_mobile_bundle",
            "submit_attendance",
            "calculate_due_date",
            "mark_document_submitted",
            "submit_notice_reply",
            "send_chat_message",
            "set_consultation_status",
            "enqueue_remote_task",
            "push_timetable_changes",
            "fetchNeisMeals",
            "installNeisMealTrigger",
        ]
        for action in required_actions:
            self.assertIn(action, self.gs_text, f"누락된 액션/함수: {action}")

    @patch("services.google_sheets_sync_service._http_get_json")
    def test_fetch_sheet_rows(self, mock_get):
        mock_get.return_value = {
            "ok": True,
            "sheet": "가정통신문_회신",
            "rows": [{"remote_id": "reply_1", "student_name": "김민준"}],
        }
        rows = self.sync.fetch_sheet_rows(
            "가정통신문_회신",
            apps_script_url="https://script.google.com/macros/s/test/exec",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["student_name"], "김민준")
        mock_get.assert_called_once()

    @patch("services.google_sheets_sync_service._http_post_json")
    def test_push_timetable_changes_and_task_memo(self, mock_post):
        mock_post.return_value = {"ok": True}
        ok1 = self.sync.push_timetable_changes(
            [{"remote_id": "tc_1", "date": "2026-11-16", "period": 3, "changed_subject": "수학"}],
            apps_script_url="https://script.google.com/macros/s/test/exec",
        )
        ok2 = self.sync.push_task_status(
            "mob_001",
            "done",
            is_done=True,
            memo="자동 분류 12건 완료",
            apps_script_url="https://script.google.com/macros/s/test/exec",
        )
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(mock_post.call_count, 2)

    @patch("services.google_sheets_sync_service._http_post_json")
    @patch("services.google_sheets_sync_service._http_get_json")
    def test_api_secret_included_only_when_configured(self, mock_get, mock_post):
        self.assertIn("_verifyApiSecret", self.gs_text)
        self.assertIn("Unauthorized", self.gs_text)

        mock_get.return_value = {"ok": True, "spreadsheet_name": "Test", "counts": {}}
        mock_post.return_value = {"ok": True}

        # 1) 미설정 시 api_secret 미포함
        self.sync.test_connection(apps_script_url="https://script.google.com/macros/s/test/exec")
        self.assertNotIn("api_secret", mock_get.call_args[0][1])

        # 2) 설정 시 GET/POST 모두 api_secret 자동 포함
        self.cfg.get.side_effect = lambda sec, key, default="": (
            "my_secret_123" if (sec, key) == ("api_and_integrations", "gas_api_secret") else default
        )
        self.sync.test_connection(apps_script_url="https://script.google.com/macros/s/test/exec")
        self.assertEqual(mock_get.call_args[0][1].get("api_secret"), "my_secret_123")

        self.sync.push_task_status(
            "mob_002",
            "done",
            is_done=True,
            apps_script_url="https://script.google.com/macros/s/test/exec",
        )
        self.assertEqual(mock_post.call_args[0][1].get("api_secret"), "my_secret_123")


if __name__ == "__main__":
    unittest.main()
