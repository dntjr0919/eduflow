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

    def test_google_sheets_timetable_timeouts_and_gas_error_logging(self) -> None:
        import inspect
        from services.google_sheets_sync_service import GoogleSheetsSyncService

        sig_base = inspect.signature(GoogleSheetsSyncService.upsert_timetable_base)
        sig_chg = inspect.signature(GoogleSheetsSyncService.push_timetable_changes)
        self.assertGreaterEqual(sig_base.parameters["timeout"].default, 30.0)
        self.assertGreaterEqual(sig_chg.parameters["timeout"].default, 30.0)

        mock_db = MagicMock()
        svc = GoogleSheetsSyncService(db=mock_db)
        with patch.object(
            svc,
            "_resolve_urls",
            return_value=("https://script.google.com/macros/s/demo/exec", "sheet123"),
        ), patch(
            "services.google_sheets_sync_service._http_post_json",
            return_value={"ok": False, "error": "GAS quota exceeded"},
        ):
            ok_base = svc.upsert_timetable_base([{"remote_id": "tb_c_1_1_월_1"}])
            ok_chg = svc.push_timetable_changes([{"remote_id": "tc_20261116_1_1_1"}])

        self.assertFalse(ok_base)
        self.assertFalse(ok_chg)
        warn_calls = [c for c in mock_db.add_log.call_args_list if c.args[0] == "WARN"]
        self.assertEqual(len(warn_calls), 2)
        self.assertIn("GAS quota exceeded", warn_calls[0].args[2])
        self.assertIn("GAS quota exceeded", warn_calls[1].args[2])

    def test_comcigan_teacher_mode_empty_change_and_no_caption_leak(self) -> None:
        from services.comcigan_playwright_fetcher import ComciganPlaywrightFetcher

        teacher_html = """
        <html><body>
          <table>
            <caption>홍길동 시간표</caption>
            <tr><th>교시</th><th>월</th><th>화</th></tr>
            <tr>
              <td>1</td>
              <td class="변경"></td>
              <td>2-3<br>수학</td>
            </tr>
            <tr>
              <td>2</td>
              <td class="변경"><del>2-4 수학</del> 자습</td>
              <td class="변경">과학 -> 보강</td>
            </tr>
          </table>
        </body></html>
        """
        t_base, t_chg = ComciganPlaywrightFetcher.parse_html(
            teacher_html,
            grade_hint="1",
            class_hint="1",
            target_date="2026-11-16",
            teacher_name="홍길동",
            mode="teacher",
        )
        self.assertEqual(len(t_base), 1)
        self.assertEqual(t_base[0]["remote_id"], "tb_t_홍길동_화_1")

        self.assertEqual(len(t_chg), 2)
        # 1) <del>2-4 수학</del> 자습 -> preserves grade/class from deleted_txt
        del_chg = [c for c in t_chg if c["period"] == "2" and c["changed_subject"] == "자습"][0]
        self.assertEqual(del_chg["grade"], "2")
        self.assertEqual(del_chg["class_no"], "4")
        self.assertEqual(del_chg["original_subject"], "수학")
        # 2) arrow_match without grade/class in teacher mode -> no homeroom leak
        arrow_chg = [c for c in t_chg if c["period"] == "2" and c["changed_subject"] == "보강"][0]
        self.assertEqual(arrow_chg["grade"], "")
        self.assertEqual(arrow_chg["class_no"], "")
        self.assertEqual(arrow_chg["remote_id"], "tc_t_20261117_홍길동_2")

        # 3) mode="class"에서 빈 <td class="변경"></td>는 "수업없음(휴업/공강)"으로 추출
        class_html = """
        <html><body>
          <table>
            <caption>1학년 5반 시간표</caption>
            <tr><th>교시</th><th>월</th><th>화</th></tr>
            <tr><td>1</td><td class="변경"></td><td>국어</td></tr>
          </table>
        </body></html>
        """
        c_base, c_chg = ComciganPlaywrightFetcher.parse_html(
            class_html, target_date="2026-11-16", mode="class"
        )
        self.assertEqual(len(c_base), 1)
        self.assertEqual(c_base[0]["subject"], "국어")
        self.assertEqual(len(c_chg), 1)
        self.assertEqual(c_chg[0]["grade"], "1")
        self.assertEqual(c_chg[0]["class_no"], "5")
        self.assertEqual(c_chg[0]["changed_subject"], "수업없음(휴업/공강)")

    def test_sync_comcigan_from_web_prefers_class_entry_and_reports_step_errors(self) -> None:
        from services.mobile_integration_worker import sync_comcigan_from_web

        fake_fetcher = MagicMock()
        fake_fetcher.fetch.return_value = {
            "ok": True,
            "base_rows": [
                {
                    "remote_id": "tb_t_김교사_월_1",
                    "entry_type": "teacher",
                    "grade": "1",
                    "class_no": "3",
                    "day_of_week": "월",
                    "period": "1",
                    "subject": "교사시트과목",
                    "teacher_name": "김교사",
                }
            ],
            "change_rows": [
                {
                    "remote_id": "tc_20261116_1_3_1",
                    "date": "2026-11-16",
                    "grade": "1",
                    "class_no": "3",
                    "period": "1",
                    "original_subject": "(기존수업)",
                    "changed_subject": "자습",
                    "original_teacher": "",
                    "changed_teacher": "",
                }
            ],
        }
        fake_sync = MagicMock()
        fake_sync.fetch_sheet_rows.return_value = [
            {
                "remote_id": "tb_c_1_3_월_1",
                "entry_type": "class",
                "grade": "1",
                "class_no": "3",
                "day_of_week": "월",
                "period": "1",
                "subject": "학급기본수학",
                "teacher_name": "이담임",
            }
        ]
        fake_sync.upsert_timetable_base.return_value = True
        fake_sync.push_timetable_changes.return_value = False

        res = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            sync_service=fake_sync,
            fetcher=fake_fetcher,
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["base_pushed"], 1)
        self.assertEqual(res["changes_pushed"], 0)
        self.assertIn("시간표_변동 Push 실패", res["error"])
        self.assertNotIn("시간표_기본 Upsert 실패", res["error"])
        pushed_changes = fake_sync.push_timetable_changes.call_args.args[0]
        self.assertEqual(pushed_changes[0]["original_subject"], "학급기본수학")
        self.assertEqual(pushed_changes[0]["original_teacher"], "이담임")

    def test_extract_grade_class_variants_and_chunked_sync_with_progress(self) -> None:
        import inspect
        from services.comcigan_playwright_fetcher import _extract_grade_class
        from services.google_sheets_sync_service import GoogleSheetsSyncService
        from services.mobile_integration_worker import sync_comcigan_from_web

        self.assertEqual(_extract_grade_class("1-05", "", ""), ("1", "5"))
        self.assertEqual(_extract_grade_class("1학년 05반", "", ""), ("1", "5"))
        self.assertEqual(_extract_grade_class("105", "", ""), ("1", "5"))
        self.assertEqual(_extract_grade_class("105반", "", ""), ("1", "5"))

        sig_base = inspect.signature(GoogleSheetsSyncService.upsert_timetable_base)
        sig_chg = inspect.signature(GoogleSheetsSyncService.push_timetable_changes)
        self.assertGreaterEqual(sig_base.parameters["timeout"].default, 60.0)
        self.assertGreaterEqual(sig_chg.parameters["timeout"].default, 60.0)

        svc = GoogleSheetsSyncService(db=MagicMock())
        rows = [{"remote_id": f"tb_c_1_5_월_{i}"} for i in range(250)]
        batch_events = []
        with patch.object(
            svc,
            "_resolve_urls",
            return_value=("https://script.google.com/macros/s/demo/exec", "sheet123"),
        ), patch(
            "services.google_sheets_sync_service._http_post_json",
            return_value={"ok": True},
        ) as mock_post:
            ok = svc.upsert_timetable_base(
                rows,
                chunk_size=100,
                progress_callback=lambda b, tot: batch_events.append((b, tot)),
            )

        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(batch_events, [(1, 3), (2, 3), (3, 3)])

        progress_msgs = []
        fake_fetcher = MagicMock()
        fake_fetcher.fetch.return_value = {
            "ok": True,
            "base_rows": [{"remote_id": "tb_c_1_5_월_1"}],
            "change_rows": [{"remote_id": "tc_20261116_1_5_1"}],
        }
        fake_sync = MagicMock()
        fake_sync.fetch_sheet_rows.return_value = []
        fake_sync.upsert_timetable_base.side_effect = (
            lambda r, progress_callback=None: (progress_callback(1, 2), progress_callback(2, 2), True)[-1]
        )
        fake_sync.push_timetable_changes.side_effect = (
            lambda r, progress_callback=None: (progress_callback(1, 1), True)[-1]
        )

        res = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            target_grade="1",
            target_class="5",
            sync_service=fake_sync,
            fetcher=fake_fetcher,
            progress_callback=lambda pct, msg: progress_msgs.append((pct, msg)),
        )
        self.assertTrue(res["ok"])
        fake_fetcher.fetch.assert_called_once()
        self.assertTrue(any("1/2 배치" in m for _, m in progress_msgs))
        self.assertTrue(any("동기화 완료" in m for _, m in progress_msgs))

    def test_gas_template_uses_bulk_upsert_for_timetable(self) -> None:
        gs_path = Path("docs/apps_script_template.gs")
        text = gs_path.read_text(encoding="utf-8")
        self.assertIn("function _bulkUpsertRowsByRemoteId", text)
        self.assertIn("_bulkUpsertRowsByRemoteId(tbSheet", text)
        self.assertIn("_bulkUpsertRowsByRemoteId(tcSheet", text)


if __name__ == "__main__":
    unittest.main()
