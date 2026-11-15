"""
모바일 원격 작업 실행기, 컴시간 알리미 파서, 가정통신문 PDF 추출 및 서명 일괄 합성 단위 테스트.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from services.mobile_integration_worker import (
    MobileRemoteTaskDispatcher,
    build_notice_extract_prompt,
    compose_signed_notice_pdfs,
    parse_comcigan_payload,
    push_comcigan_changes,
)


_APP_INSTANCE: QApplication | None = None


def _ensure_qapp() -> QApplication:
    global _APP_INSTANCE
    app = QApplication.instance()
    if app is None:
        _APP_INSTANCE = QApplication(["eduflow-test"])
        return _APP_INSTANCE
    return app


class TestMobileIntegrationWorker(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_qapp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_parse_comcigan_text_and_json(self) -> None:
        text_raw = "1-3 2교시 수학Ⅰ -> 영어 (한가인)\n3교시 화학Ⅰ → 자습 (박OO)"
        rows = parse_comcigan_payload(text_raw, target_date="2026-11-16", grade="1", class_no="3")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["period"], "2")
        self.assertEqual(rows[0]["original_subject"], "수학Ⅰ")
        self.assertEqual(rows[0]["changed_subject"], "영어")
        self.assertEqual(rows[0]["changed_teacher"], "한가인")
        self.assertEqual(rows[1]["source"], "comcigan")

        mock_sync = MagicMock()
        mock_sync.push_timetable_changes.return_value = True
        res = push_comcigan_changes(
            {"changes": [{"grade": "1", "class_no": "3", "period": 5, "changed_subject": "자습"}]},
            sync_service=mock_sync,
            target_date="2026-11-16",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["pushed"], 1)
        mock_sync.push_timetable_changes.assert_called_once()

    def test_build_notice_extract_prompt(self) -> None:
        prompt = build_notice_extract_prompt("체험학습_가정통신문.pdf")
        self.assertIn("체험학습_가정통신문.pdf", prompt)
        self.assertIn("reply_fields", prompt)

    def test_compose_signed_notice_pdfs_with_png(self) -> None:
        sig_path = self.tmp_path / "sig_10315.png"
        img = QImage(160, 60, QImage.Format.Format_ARGB32)
        img.fill(QColor(255, 255, 255))
        img.save(str(sig_path), "PNG")

        out_dir = self.tmp_path / "signed_pdfs"
        res = compose_signed_notice_pdfs(
            replies=[
                {
                    "student_id": "10315",
                    "student_name": "유재석",
                    "parent_name": "유OO 학부모",
                    "consent": "동의",
                    "signed_at": "2026-11-15 15:00:00",
                    "signature_local_path": str(sig_path),
                }
            ],
            output_dir=out_dir,
            notice_title="현장체험학습 참가 동의서",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 1)
        pdf_file = Path(res["files"][0])
        self.assertTrue(pdf_file.exists())
        self.assertGreater(pdf_file.stat().st_size, 500)

    @patch("services.mobile_integration_worker.WebGeminiAutoWorker")
    def test_dispatcher_web_llm_and_reports_done(self, mock_worker_cls) -> None:
        mock_worker = MagicMock()
        mock_worker.execute.return_value = {"ok": True}
        mock_worker_cls.return_value = mock_worker

        mock_db = MagicMock()
        mock_cfg = MagicMock()
        mock_sync = MagicMock()

        dispatcher = MobileRemoteTaskDispatcher(db=mock_db, config=mock_cfg, sync_service=mock_sync)
        res = dispatcher.dispatch_task(
            {
                "id": 42,
                "remote_id": "mob_1001",
                "task_type": "web_llm",
                "title": "세특 초안 생성",
                "action_payload": json.dumps({"prompt": "1학년 세특 초안 작성"}),
            }
        )
        self.assertTrue(res["ok"])
        mock_worker.execute.assert_called_once()
        mock_db.complete_mobile_task.assert_called_once_with(42)
        mock_sync.push_task_status.assert_any_call("mob_1001", "running", is_done=False)
        mock_sync.push_task_status.assert_any_call(
            "mob_1001", "done", is_done=True, memo="웹 LLM 작업 완료 (세특 초안 생성)"
        )


if __name__ == "__main__":
    unittest.main()
