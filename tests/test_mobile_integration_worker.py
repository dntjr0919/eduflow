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
        from services.firestore_sync_service import FirestoreSyncService

        mock_db = MagicMock()
        fake_fs = MagicMock()
        fake_batch = MagicMock()
        fake_batch.commit.side_effect = RuntimeError("Firestore quota exceeded")
        fake_fs.batch.return_value = fake_batch
        svc = FirestoreSyncService(db=mock_db, firestore_client=fake_fs)

        ok_base = svc.upsert_timetable_base([{"remote_id": "tb_c_1_1_월_1"}])
        ok_chg = svc.push_timetable_changes([{"remote_id": "tc_20261116_1_1_1"}])

        self.assertFalse(ok_base)
        self.assertFalse(ok_chg)
        warn_calls = [c for c in mock_db.add_log.call_args_list if c.args[0] == "WARN"]
        self.assertEqual(len(warn_calls), 2)
        self.assertIn("Firestore quota exceeded", warn_calls[0].args[2])
        self.assertIn("Firestore quota exceeded", warn_calls[1].args[2])

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
        fake_sync.push_timetable_snapshots.return_value = False

        res = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            sync_service=fake_sync,
            fetcher=fake_fetcher,
            cache_path=self.tmp_path / "prefs_cache.json",
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["snapshots_pushed"], 0)
        self.assertIn("시간표_스냅샷 Push 실패", res["error"])
        pushed_snaps = fake_sync.push_timetable_snapshots.call_args.args[0]
        self.assertEqual(len(pushed_snaps), 1)
        self.assertEqual(pushed_snaps[0]["date"], "2026-11-16")
        slot_1 = pushed_snaps[0]["classes"]["1-3"]["1"]
        self.assertEqual(slot_1["subject"], "자습")
        self.assertEqual(slot_1["teacher_name"], "")
        self.assertTrue(slot_1["is_changed"])

    def test_extract_grade_class_variants_and_chunked_sync_with_progress(self) -> None:
        from services.comcigan_playwright_fetcher import _extract_grade_class
        from services.firestore_sync_service import FirestoreSyncService
        from services.mobile_integration_worker import sync_comcigan_from_web

        self.assertEqual(_extract_grade_class("1-05", "", ""), ("1", "5"))
        self.assertEqual(_extract_grade_class("1학년 05반", "", ""), ("1", "5"))
        self.assertEqual(_extract_grade_class("105", "", ""), ("1", "5"))
        self.assertEqual(_extract_grade_class("105반", "", ""), ("1", "5"))

        fake_fs = MagicMock()
        fake_batch = MagicMock()
        fake_fs.batch.return_value = fake_batch
        svc = FirestoreSyncService(db=MagicMock(), firestore_client=fake_fs)
        rows = [{"remote_id": f"tb_c_1_5_월_{i}"} for i in range(250)]
        batch_events = []
        ok = svc.upsert_timetable_base(
            rows,
            chunk_size=100,
            progress_callback=lambda b, tot: batch_events.append((b, tot)),
        )

        self.assertTrue(ok)
        self.assertEqual(fake_batch.commit.call_count, 3)
        self.assertEqual(batch_events, [(1, 3), (2, 3), (3, 3)])

        progress_msgs = []
        fake_fetcher = MagicMock()
        fake_fetcher.fetch.return_value = {
            "ok": True,
            "base_rows": [{"remote_id": "tb_c_1_5_월_1", "grade": "1", "class_no": "5", "day_of_week": "월", "period": "1", "subject": "국어"}],
            "change_rows": [{"remote_id": "tc_20261116_1_5_1", "date": "2026-11-16", "grade": "1", "class_no": "5", "period": "1", "changed_subject": "수학"}],
        }
        fake_sync = MagicMock()
        fake_sync.push_timetable_snapshots.side_effect = (
            lambda snaps, progress_callback=None, **kwargs: (progress_callback(1, 2), progress_callback(2, 2), True)[-1]
        )

        res = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            target_grade="1",
            target_class="5",
            sync_service=fake_sync,
            fetcher=fake_fetcher,
            use_local_cache=False,
            progress_callback=lambda pct, msg: progress_msgs.append((pct, msg)),
        )
        self.assertTrue(res["ok"])
        fake_fetcher.fetch.assert_called_once()
        self.assertTrue(any("1/2일" in m for _, m in progress_msgs))
        self.assertTrue(any("스냅샷 동기화 완료" in m for _, m in progress_msgs))

    def test_gas_template_uses_bulk_upsert_for_timetable(self) -> None:
        gs_path = Path("docs/apps_script_template.gs")
        text = gs_path.read_text(encoding="utf-8")
        self.assertIn("function _bulkUpsertRowsByRemoteId", text)
        self.assertIn("_bulkUpsertRowsByRemoteId(tbSheet", text)
        self.assertIn("_bulkUpsertRowsByRemoteId(tcSheet", text)

    def test_comcigan_full_grid_empty_cell_diff_snapshot_skip_and_restore(self) -> None:
        from services.comcigan_playwright_fetcher import ComciganPlaywrightFetcher
        from services.mobile_integration_worker import sync_comcigan_from_web

        # 1) parse_html_with_grid가 하이라이트 없는 빈 셀(td.내용)도 ""로 전수 수집하는지 검증
        html_initial = """
        <html><body>
          <table>
            <caption>1학년 5반 시간표</caption>
            <tr><th>교시</th><th>월</th><th>화</th></tr>
            <tr><td>1</td><td class="내용">국어<br>(김교사)</td><td class="내용"></td></tr>
            <tr><td>3</td><td class="내용">자율<br>(이교사)</td><td class="내용">수학<br>(박교사)</td></tr>
          </table>
        </body></html>
        """
        base1, chg1, grid1 = ComciganPlaywrightFetcher.parse_html_with_grid(
            html_initial, grade_hint="1", class_hint="5", target_date="2026-11-16", mode="class"
        )
        self.assertEqual(len(base1), 3)
        self.assertEqual(len(chg1), 0)
        self.assertEqual(len(grid1), 4)
        self.assertEqual(grid1["c|1|5|화|1"]["subject"], "")
        self.assertEqual(grid1["c|1|5|월|3"]["subject"], "자율")

        html_deleted = """
        <html><body>
          <table>
            <caption>1학년 5반 시간표</caption>
            <tr><th>교시</th><th>월</th><th>화</th></tr>
            <tr><td>1</td><td class="내용">국어<br>(김교사)</td><td class="내용"></td></tr>
            <tr><td>3</td><td class="내용"></td><td class="내용">수학<br>(박교사)</td></tr>
          </table>
        </body></html>
        """
        html_extra_added = """
        <html><body>
          <table>
            <caption>1학년 5반 시간표</caption>
            <tr><th>교시</th><th>월</th><th>화</th></tr>
            <tr><td>1</td><td class="내용">국어<br>(김교사)</td><td class="내용"></td></tr>
            <tr><td>3</td><td class="내용"></td><td class="내용">수학<br>(박교사)</td></tr>
            <tr><td>7</td><td class="내용 변경">정보<br>(유교사)</td><td class="내용"></td></tr>
          </table>
        </body></html>
        """
        current_html = {"val": html_initial}

        class _GridStubFetcher:
            def fetch(self):
                b, c, g = ComciganPlaywrightFetcher.parse_html_with_grid(
                    current_html["val"],
                    grade_hint="1",
                    class_hint="5",
                    target_date="2026-11-16",
                    mode="class",
                )
                return {
                    "ok": True,
                    "base_rows": b,
                    "change_rows": c,
                    "grid_slots": g,
                    "class_base_count": len(b),
                    "teacher_base_count": 0,
                }

        stored_snaps: dict = {}
        push_calls: list = []

        class _MemSyncService:
            def has_timetable_snapshots(self, **kwargs):
                return len(stored_snaps) > 0

            def push_timetable_snapshots(self, snapshots, **kwargs):
                push_calls.append(len(snapshots))
                for s in snapshots:
                    # 통째로 덮어쓰기(Set/Overwrite)
                    stored_snaps[s["remote_id"]] = dict(s)
                return True

            def update_teacher_comcigan_code(self, *args, **kwargs):
                return True

        mem_svc = _MemSyncService()
        cache_file = self.tmp_path / "comcigan_grid_cache.json"

        # Step 1: 초기 동기화 (월/화 2일분 스냅샷 저장)
        r1 = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            target_grade="1",
            target_class="5",
            sync_service=mem_svc,
            fetcher=_GridStubFetcher(),
            cache_path=cache_file,
        )
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["snapshots_pushed"], 2)
        self.assertIn("snap_8140246_20261116", stored_snaps)
        self.assertEqual(stored_snaps["snap_8140246_20261116"]["classes"]["1-5"]["3"]["subject"], "자율")

        # Step 2: 3교시 '자율'이 공강("")으로 삭제됨 -> 월요일 스냅샷만 통째 교체, 3교시 빈 슬롯 반영
        current_html["val"] = html_deleted
        r2 = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            target_grade="1",
            target_class="5",
            sync_service=mem_svc,
            fetcher=_GridStubFetcher(),
            cache_path=cache_file,
        )
        self.assertTrue(r2["ok"])
        self.assertFalse(r2["skipped"])
        self.assertEqual(r2["snapshots_pushed"], 1)  # 화요일은 해시 동일하므로 월요일 1일분만 덮어씀
        self.assertEqual(stored_snaps["snap_8140246_20261116"]["classes"]["1-5"]["3"]["subject"], "")

        # Step 3: 동일 빈 셀 상태로 재실행 -> 로컬 캐시 해시 스킵 (Firestore 쓰기 0건)
        prev_push_count = len(push_calls)
        r3 = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            target_grade="1",
            target_class="5",
            sync_service=mem_svc,
            fetcher=_GridStubFetcher(),
            cache_path=cache_file,
        )
        self.assertTrue(r3["ok"])
        self.assertTrue(r3["skipped"])
        self.assertEqual(r3["snapshots_pushed"], 0)
        self.assertEqual(len(push_calls), prev_push_count)

        # Step 4: 7교시 보강 추가 -> 스냅샷에 is_changed=True로 반영
        current_html["val"] = html_extra_added
        r4 = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            target_grade="1",
            target_class="5",
            sync_service=mem_svc,
            fetcher=_GridStubFetcher(),
            cache_path=cache_file,
        )
        self.assertTrue(r4["ok"])
        self.assertEqual(stored_snaps["snap_8140246_20261116"]["classes"]["1-5"]["7"]["subject"], "정보")
        self.assertTrue(stored_snaps["snap_8140246_20261116"]["classes"]["1-5"]["7"]["is_changed"])

        # Step 5: 7교시 보강 취소 -> 스냅샷 통째 덮어쓰기로 7교시 즉시 제거됨
        current_html["val"] = html_deleted
        r5 = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            target_date="2026-11-16",
            target_grade="1",
            target_class="5",
            sync_service=mem_svc,
            fetcher=_GridStubFetcher(),
            cache_path=cache_file,
        )
        self.assertTrue(r5["ok"])
        self.assertEqual(stored_snaps["snap_8140246_20261116"]["classes"]["1-5"]["7"]["subject"], "")
        self.assertFalse(stored_snaps["snap_8140246_20261116"]["classes"]["1-5"]["7"]["is_changed"])


if __name__ == "__main__":
    unittest.main()
