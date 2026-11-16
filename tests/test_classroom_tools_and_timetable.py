import csv
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core.base_worker import TaskRunner
from core.config_manager import ConfigManager
from services.comcigan_playwright_fetcher import ComciganPlaywrightFetcher
from services.firestore_sync_service import FirestoreSyncService, hash_password_sha256
from services.timetable_manual_service import (
    build_manual_timetable_change,
    normalize_teacher_name,
    parse_weekly_timetable_file,
)
from ui.tabs import tab_classroom_tools
from ui.tabs.tab_classroom_tools import ClassroomToolsTab, parse_notice_ai_json


class _FakeSnap:
    def __init__(self, doc_id: str, data: Dict[str, Any] | None):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data or {})


class _FakeQuery:
    def __init__(self, store: Dict[str, Dict[str, Any]], filters: List[tuple] | None = None, lim: int | None = None):
        self._store = store
        self._filters = list(filters or [])
        self._lim = lim

    def where(self, field: str, op: str, val: Any) -> "_FakeQuery":
        return _FakeQuery(self._store, self._filters + [(field, op, val)], self._lim)

    def limit(self, n: int) -> "_FakeQuery":
        return _FakeQuery(self._store, self._filters, n)

    def stream(self):
        out = []
        for doc_id, doc_data in self._store.items():
            ok = True
            for field, op, val in self._filters:
                if op == "==" and doc_data.get(field) != val:
                    ok = False
                    break
            if ok:
                out.append(_FakeSnap(doc_id, doc_data))
                if self._lim and len(out) >= self._lim:
                    break
        return out


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


class _FakeCollection(_FakeQuery):
    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, doc_id)


class _FakeBatch:
    def __init__(self) -> None:
        self._ops: List[tuple] = []

    def set(self, doc_ref: _FakeDocRef, data: Dict[str, Any], merge: bool = False) -> None:
        self._ops.append((doc_ref, data, merge))

    def commit(self) -> None:
        for doc_ref, data, merge in self._ops:
            doc_ref.set(data, merge=merge)


class _FakeFirestore:
    def __init__(self) -> None:
        self.cols: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self.cols.setdefault(name, {}))

    def batch(self) -> _FakeBatch:
        return _FakeBatch()


class TestClassroomToolsAndTimetable(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.cfg = ConfigManager()
        self._orig_tp = dict(self.cfg.get_section("teacher_profile") or {})
        self.cfg._config["teacher_profile"] = {}
        self.cfg.save()

        self.fake_fs = _FakeFirestore()
        self.fake_fs.cols["계정_교사"] = {
            "유우석": {
                "remote_id": "유우석",
                "teacher_id": "유우석",
                "login_id": "유우석",
                "name": "유우석",
                "school_code": "7010001",
                "school_name": "에듀플로우고",
                "grade": 2,
                "class_no": 4,
                "is_admin": True,
                "status": "active",
                "password_hash": hash_password_sha256("pw1234"),
            }
        }
        self.fake_fs.cols["계정_학생"] = {
            "7010001_20401": {
                "student_id": "20401",
                "name": "김철수",
                "school_code": "7010001",
                "school_name": "에듀플로우고",
                "grade": 2,
                "class_no": 4,
                "student_no": 1,
                "status": "active",
            },
            "7010001_20402": {
                "student_id": "20402",
                "name": "이영희",
                "school_code": "7010001",
                "school_name": "에듀플로우고",
                "grade": 2,
                "class_no": 4,
                "student_no": 2,
                "status": "active",
            },
            "9999999_20401": {
                "student_id": "20401",
                "name": "타학교학생",
                "school_code": "9999999",
                "school_name": "다른학교",
                "grade": 2,
                "class_no": 4,
                "student_no": 1,
                "status": "active",
            },
        }
        self.sync_svc = FirestoreSyncService(firestore_client=self.fake_fs)
        self.task_runner = TaskRunner()

    def tearDown(self) -> None:
        self.cfg._config["teacher_profile"] = self._orig_tp
        self.cfg.save()

    def test_teacher_login_and_dynamic_roster_binding(self) -> None:
        tab = ClassroomToolsTab(task_runner=self.task_runner)
        self.assertTrue(tab.auth_lock_overlay.isVisibleTo(tab) or not tab.pill_bar.isVisible())

        ok, err = tab.login_teacher("유우석", "wrong_pw", sync_service=self.sync_svc)
        self.assertFalse(ok)
        self.assertIn("비밀번호", err)

        ok, err = tab.login_teacher("유우석", "pw1234", sync_service=self.sync_svc)
        self.assertTrue(ok, err)
        self.assertEqual(tab._teacher_meta()["school_code"], "7010001")
        self.assertEqual(tab._teacher_meta()["grade"], 2)
        self.assertEqual(tab._teacher_meta()["class_no"], 4)
        self.assertEqual(tab._get_active_roster(), [("20401", "김철수"), ("20402", "이영희")])
        self.assertIn("유우석", tab.lbl_teacher_auth_status.text())
        self.assertIn("2학년 4반", tab.lbl_teacher_auth_status.text())
        self.assertIn("2명", tab.lbl_teacher_auth_status.text())

        # 앱 재시작 시 저장된 세션 자동 복원 검증
        tab2 = ClassroomToolsTab(task_runner=self.task_runner)
        restored = tab2.restore_saved_teacher_session(sync_service=self.sync_svc)
        self.assertTrue(restored)
        self.assertEqual(tab2._teacher_meta()["teacher_name"], "유우석")
        self.assertEqual(tab2._get_active_roster(), [("20401", "김철수"), ("20402", "이영희")])

    def test_pypdf_removed_and_notice_ai_json_applied(self) -> None:
        src = inspect.getsource(tab_classroom_tools)
        self.assertNotIn("import pypdf", src.lower())
        self.assertNotIn("from pypdf", src.lower())

        raw_llm = (
            "```json\n"
            "{\n"
            '  "title": "2학기 현장체험학습 안내",\n'
            '  "deadline": "2026-11-25",\n'
            '  "body": "현장체험학습 일정 및 준비물을 안내합니다.",\n'
            '  "reply_fields": ["참가 동의 여부 (동의/미동의)", "비상연락처"]\n'
            "}\n"
            "```"
        )
        parsed = parse_notice_ai_json(raw_llm)
        self.assertEqual(parsed["title"], "2학기 현장체험학습 안내")
        self.assertEqual(parsed["deadline"], "2026-11-25")
        self.assertEqual(parsed["reply_fields"], ["참가 동의 여부 (동의/미동의)", "비상연락처"])

        tab = ClassroomToolsTab(task_runner=self.task_runner)
        tab.login_teacher("유우석", "pw1234", sync_service=self.sync_svc)
        tab._selected_notice_pdf_path = "C:/dummy/notice.pdf"
        ctx = tab.get_ai_context()
        self.assertEqual(ctx["target_id"], "notice_ocr")
        self.assertEqual(ctx["kind"], "ocr")
        self.assertEqual(ctx["ocr_files"], ["C:/dummy/notice.pdf"])

        tab.apply_ai_dock_result({"target_id": "notice_ocr", "mode": "manual", "raw_response": raw_llm})
        self.assertEqual(tab.edit_notice_title.text(), "2학기 현장체험학습 안내")
        self.assertEqual(tab.edit_notice_due.text(), "2026-11-25")
        self.assertEqual(tab.txt_notice_content.toPlainText(), "현장체험학습 일정 및 준비물을 안내합니다.")
        self.assertEqual(tab.edit_notice_fields.text(), "참가 동의 여부 (동의/미동의), 비상연락처")

    def test_comcigan_normalization_and_manual_excel_timetable(self) -> None:
        self.assertEqual(normalize_teacher_name("유우석T"), "유우석")
        self.assertEqual(normalize_teacher_name("유우석 선생님"), "유우석")

        html = """
        <html><body>
        <table>
          <caption>1학년 1반 시간표</caption>
          <tr><th>교시</th><th>월</th><th>화</th></tr>
          <tr><td>1</td><td>국어</td><td>물리<br>(유우석T)</td></tr>
        </table>
        </body></html>
        """
        base_rows, _ = ComciganPlaywrightFetcher.parse_html(
            html, grade_hint="1", class_hint="1", target_date="2026-11-17", mode="class"
        )
        self.assertEqual(len(base_rows), 2)
        tue_row = [r for r in base_rows if r["day_of_week"] == "화"][0]
        self.assertEqual(tue_row["teacher_name"], "유우석")
        self.assertEqual(tue_row["source"], "comcigan")

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "weekly.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["교시", "월", "화"])
                w.writerow(["1", "국어(홍길동T)", "수학(유우석T)"])
                w.writerow(["2", "", "영어(이순신)"])
            rows = parse_weekly_timetable_file(
                csv_path,
                school_code="7010001",
                school_name="에듀플로우고",
                default_grade=2,
                default_class=4,
                entry_type="class",
            )
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(r["source"] == "excel" for r in rows))
            self.assertTrue(self.sync_svc.upsert_timetable_base(rows))
            self.assertEqual(len(self.fake_fs.cols["시간표_기본"]), 3)

        chg = build_manual_timetable_change(
            date_str="2026-11-17",
            period=7,
            changed_subject="자율보강",
            grade=2,
            class_no=4,
            changed_teacher="유우석T",
            reason="7교시 보강",
            school_code="7010001",
            school_name="에듀플로우고",
        )
        self.assertEqual(chg["source"], "manual")
        self.assertEqual(chg["changed_teacher"], "유우석")
        self.assertEqual(chg["period"], 7)
        self.assertTrue(self.sync_svc.push_timetable_changes([chg]))
        self.assertEqual(len(self.fake_fs.cols["시간표_변동"]), 1)


if __name__ == "__main__":
    unittest.main()
