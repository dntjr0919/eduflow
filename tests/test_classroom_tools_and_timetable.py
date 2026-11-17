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
        from PyQt6.QtWidgets import QGraphicsBlurEffect

        tab = ClassroomToolsTab(task_runner=self.task_runner)
        self.assertIsInstance(tab._main_scroll.graphicsEffect(), QGraphicsBlurEffect)
        self.assertFalse(tab.auth_overlay_backdrop.isHidden())
        self.assertTrue(tab.auth_status_widget.isHidden())

        ok, err = tab.login_teacher("유우석", "wrong_pw", sync_service=self.sync_svc)
        self.assertFalse(ok)
        self.assertIn("비밀번호", err)

        ok, err = tab.login_teacher("유우석", "pw1234", sync_service=self.sync_svc)
        self.assertTrue(ok, err)
        self.assertIsNone(tab._main_scroll.graphicsEffect())
        self.assertTrue(tab.auth_overlay_backdrop.isHidden())
        self.assertFalse(tab.auth_status_widget.isHidden())
        self.assertEqual(tab._teacher_meta()["school_code"], "7010001")
        self.assertEqual(tab._teacher_meta()["grade"], 2)
        self.assertEqual(tab._teacher_meta()["class_no"], 4)
        self.assertEqual(tab._get_active_roster(), [("20401", "김철수"), ("20402", "이영희")])
        self.assertIn("유우석", tab.lbl_teacher_auth_status.text())
        self.assertIn("2학년 4반", tab.lbl_teacher_auth_status.text())

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

        # NEIS 7자리 school_code vs 컴시간 5자리 comcigan_school_code 분리 저장 검증
        from services.mobile_integration_worker import sync_comcigan_from_web

        self.cfg.update_section(
            "teacher_profile",
            {
                "teacher_id": "유우석",
                "name": "유우석",
                "school_code": "7010001",
                "neis_school_code": "7010001",
                "school_name": "에듀플로우고",
            },
        )

        class _StubFetcher:
            def fetch(self):
                return {
                    "base_rows": [
                        {
                            "remote_id": "tb_t_유우석_화_3",
                            "entry_type": "teacher",
                            "grade": "2",
                            "class_no": "4",
                            "day_of_week": "화",
                            "period": "3",
                            "subject": "수학",
                            "teacher_name": "유우석T",
                        }
                    ],
                    "change_rows": [],
                    "class_base_count": 0,
                    "teacher_base_count": 1,
                }

        res = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            school_code="31234",
            teacher_name="유우석",
            target_date="2026-11-17",
            sync_service=self.sync_svc,
            fetcher=_StubFetcher(),
            use_local_cache=False,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["school_code"], "7010001")
        self.assertEqual(res["comcigan_school_code"], "31234")
        self.assertEqual(self.fake_fs.cols["계정_교사"]["유우석"]["comcigan_school_code"], "31234")
        saved_tt = self.fake_fs.cols["시간표_기본"]["tb_t_유우석_화_3"]
        self.assertEqual(saved_tt["school_code"], "7010001")
        self.assertEqual(saved_tt["comcigan_school_code"], "31234")
        self.assertEqual(saved_tt["teacher_name"], "유우석")

    def test_hybrid_backend_vercel_api_fallback(self) -> None:
        from unittest.mock import patch
        from services.firestore_sync_service import DEFAULT_VERCEL_API_URL

        self.cfg.update_section(
            "api_and_integrations",
            {"google_credentials_path": "", "vercel_api_url": DEFAULT_VERCEL_API_URL},
        )
        svc = FirestoreSyncService(config=self.cfg)
        with patch("services.firestore_sync_service.has_local_credentials", return_value=False):
            self.assertEqual(svc.get_backend_mode(), "vercel_api")
            self.assertTrue(svc.is_vercel_api_mode())
            self.assertEqual(svc.get_vercel_api_url(), "https://m.eduflow.kro.kr/api/firestore")

            captured_calls: List[Dict[str, Any]] = []

            class _FakeResp:
                ok = True
                status_code = 200

                def __init__(self, payload: Dict[str, Any]) -> None:
                    self._payload = payload

                def json(self) -> Dict[str, Any]:
                    return self._payload

            def _fake_post(url: str, json: Dict[str, Any], timeout: int = 20):
                captured_calls.append({"url": url, "json": json})
                act = json.get("action")
                if act == "login":
                    return _FakeResp({"ok": True, "user": {"teacher_id": "유우석", "name": "유우석", "school_code": "7010001", "grade": 2, "class_no": 4, "status": "active"}})
                if act == "fetch_homeroom_students":
                    return _FakeResp({"ok": True, "students": [{"student_id": "20401", "name": "김철수"}]})
                if act == "publish_mobile_notice":
                    return _FakeResp({"ok": True, "remote_id": "noti_doc_1"})
                return _FakeResp({"ok": True})

            with patch("requests.post", side_effect=_fake_post):
                ok, user, err = svc.authenticate_account("teacher", "유우석", "pw1234")
                self.assertTrue(ok, err)
                self.assertEqual(user["teacher_id"], "유우석")
                self.cfg.update_section(
                    "teacher_profile",
                    {"teacher_id": "유우석", "auth_password_hash": hash_password_sha256("pw1234")},
                )
                students = svc.fetch_homeroom_students(school_code="7010001", grade=2, class_no=4)
                self.assertEqual(len(students), 1)
                self.assertTrue(svc.upsert_timetable_base([{"day_of_week": "화", "period": 7, "subject": "보강"}]))
                pub_ok, rid = svc.publish_mobile_notice({"title": "안내", "content": "내용"})
                self.assertTrue(pub_ok)
                self.assertEqual(rid, "noti_doc_1")
                sent_ok, _ = svc.send_class_notification("공지", "내용")
                self.assertTrue(sent_ok)

            actions = [c["json"]["action"] for c in captured_calls]
            self.assertEqual(
                actions,
                ["login", "fetch_homeroom_students", "sync_timetables_batch", "publish_mobile_notice", "send_class_notification"],
            )
            self.assertEqual(captured_calls[1]["json"]["auth_teacher_id"], "유우석")
            self.assertTrue(captured_calls[1]["json"]["auth_password_hash"])

    def test_comcigan_local_cache_delta_sync(self) -> None:
        from services.mobile_integration_worker import sync_comcigan_from_web

        self.cfg.update_section(
            "teacher_profile",
            {
                "teacher_id": "유우석",
                "name": "유우석",
                "school_code": "7010001",
                "neis_school_code": "7010001",
                "school_name": "에듀플로우고",
            },
        )

        state = {"rows": [
            {"remote_id": "tb_1", "grade": "2", "class_no": "4", "day_of_week": "화", "period": "1", "subject": "국어", "teacher_name": "김교사"},
            {"remote_id": "tb_2", "grade": "2", "class_no": "4", "day_of_week": "화", "period": "2", "subject": "영어", "teacher_name": "이교사"},
            {"remote_id": "tb_3", "grade": "2", "class_no": "4", "day_of_week": "화", "period": "3", "subject": "수학", "teacher_name": "유우석"},
        ], "changes": [
            {"remote_id": "tc_7", "date": "2026-11-17", "grade": "2", "class_no": "4", "period": "7", "changed_subject": "보강", "changed_teacher": "유우석"},
        ]}

        class _DynamicFetcher:
            def fetch(self):
                return {
                    "base_rows": [dict(r) for r in state["rows"]],
                    "change_rows": [dict(c) for c in state["changes"]],
                    "class_base_count": 3,
                    "teacher_base_count": 0,
                }

        pushed_base_batches: List[List[Dict[str, Any]]] = []
        pushed_chg_batches: List[List[Dict[str, Any]]] = []
        read_calls = {"count": 0}

        class _TrackingSyncService(FirestoreSyncService):
            def fetch_sheet_rows(self, *args, **kwargs):
                read_calls["count"] += 1
                return []

            def upsert_timetable_base(self, rows, *args, **kwargs):
                pushed_base_batches.append(list(rows))
                return super().upsert_timetable_base(rows, *args, **kwargs)

            def push_timetable_changes(self, rows, *args, **kwargs):
                pushed_chg_batches.append(list(rows))
                return super().push_timetable_changes(rows, *args, **kwargs)

        tracking_svc = _TrackingSyncService(firestore_client=self.fake_fs, config=self.cfg)

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "comcigan_cache.json"

            # 1차 동기화: 전체 3 + 1건 전송, Firestore 전체 읽기 0회
            res1 = sync_comcigan_from_web(
                url="http://comci.net:4082/st",
                school_code="31234",
                teacher_name="유우석",
                target_date="2026-11-17",
                sync_service=tracking_svc,
                fetcher=_DynamicFetcher(),
                cache_path=cache_file,
            )
            self.assertTrue(res1["ok"])
            self.assertFalse(res1["skipped"])
            self.assertEqual(res1["base_pushed"], 3)
            self.assertEqual(res1["changes_pushed"], 1)
            self.assertEqual(read_calls["count"], 0)
            self.assertEqual(len(pushed_base_batches), 1)
            self.assertEqual(len(pushed_chg_batches), 1)

            # 2차 동기화 (동일 데이터): 0건 반영, 전송 스킵
            res2 = sync_comcigan_from_web(
                url="http://comci.net:4082/st",
                school_code="31234",
                teacher_name="유우석",
                target_date="2026-11-17",
                sync_service=tracking_svc,
                fetcher=_DynamicFetcher(),
                cache_path=cache_file,
            )
            self.assertTrue(res2["ok"])
            self.assertTrue(res2["skipped"])
            self.assertFalse(res2["changed"])
            self.assertEqual(res2["base_pushed"], 0)
            self.assertEqual(res2["changes_pushed"], 0)
            self.assertEqual(len(pushed_base_batches), 1)
            self.assertEqual(len(pushed_chg_batches), 1)

            # 3차 동기화 (1개 교시만 변경): 정확히 1건만 전송
            state["rows"][1]["subject"] = "물리학Ⅰ"
            res3 = sync_comcigan_from_web(
                url="http://comci.net:4082/st",
                school_code="31234",
                teacher_name="유우석",
                target_date="2026-11-17",
                sync_service=tracking_svc,
                fetcher=_DynamicFetcher(),
                cache_path=cache_file,
            )
            self.assertTrue(res3["ok"])
            self.assertFalse(res3["skipped"])
            self.assertTrue(res3["changed"])
            self.assertEqual(res3["base_pushed"], 1)
            self.assertEqual(res3["changes_pushed"], 0)
            self.assertEqual(len(pushed_base_batches), 2)
            self.assertEqual(len(pushed_base_batches[1]), 1)
            self.assertEqual(pushed_base_batches[1][0]["remote_id"], "tb_2")
            self.assertEqual(pushed_base_batches[1][0]["subject"], "물리학Ⅰ")
            self.assertEqual(len(pushed_chg_batches), 1)
            self.assertEqual(read_calls["count"], 0)

            # 4차 동기화 (로컬 캐시는 있지만 Firestore 시간표_기본이 0건으로 초기화된 상황): 자동 전체 재동기화
            self.fake_fs.cols["시간표_기본"].clear()
            self.fake_fs.cols["시간표_변동"].clear()
            res4 = sync_comcigan_from_web(
                url="http://comci.net:4082/st",
                school_code="31234",
                teacher_name="유우석",
                target_date="2026-11-17",
                sync_service=tracking_svc,
                fetcher=_DynamicFetcher(),
                cache_path=cache_file,
            )
            self.assertTrue(res4["ok"])
            self.assertFalse(res4["skipped"])
            self.assertEqual(res4["base_pushed"], 3)
            self.assertEqual(res4["changes_pushed"], 1)
            self.assertEqual(len(self.fake_fs.cols["시간표_기본"]), 3)
            self.assertEqual(len(self.fake_fs.cols["시간표_변동"]), 1)

            # 5차 동기화 (force_full=True 강제 재동기화): 캐시 무시하고 전체 3+1건 덮어쓰기
            res5 = sync_comcigan_from_web(
                url="http://comci.net:4082/st",
                school_code="31234",
                teacher_name="유우석",
                target_date="2026-11-17",
                sync_service=tracking_svc,
                fetcher=_DynamicFetcher(),
                cache_path=cache_file,
                force_full=True,
            )
            self.assertTrue(res5["ok"])
            self.assertFalse(res5["skipped"])
            self.assertEqual(res5["base_pushed"], 3)
            self.assertEqual(res5["changes_pushed"], 1)

    def test_vercel_api_headers_and_timeout_and_async_roster_button_restore(self) -> None:
        import time
        from unittest.mock import patch
        from PyQt6.QtWidgets import QApplication, QMessageBox
        from services.firestore_sync_service import DEFAULT_VERCEL_TIMEOUT

        # 1) _post_vercel_api가 (5, 45) 타임아웃 및 브라우저 User-Agent 헤더를 전달하는지 검증
        self.cfg.update_section("api_and_integrations", {"google_credentials_path": ""})
        svc = FirestoreSyncService(config=self.cfg)
        captured_kwargs: List[Dict[str, Any]] = []

        class _FakeOkResp:
            ok = True
            status_code = 200

            def json(self) -> Dict[str, Any]:
                return {"ok": True, "students": [{"student_id": "20401", "name": "김철수"}]}

        def _fake_post_with_headers(url: str, json: Dict[str, Any], headers: Dict[str, str] = None, timeout: Any = None):
            captured_kwargs.append({"url": url, "headers": headers or {}, "timeout": timeout})
            return _FakeOkResp()

        with patch("services.firestore_sync_service.has_local_credentials", return_value=False):
            with patch("requests.post", side_effect=_fake_post_with_headers):
                rows = svc.fetch_homeroom_students(school_code="7010001", grade=2, class_no=4)
                self.assertEqual(len(rows), 1)
                self.assertEqual(captured_kwargs[0]["timeout"], DEFAULT_VERCEL_TIMEOUT)
                self.assertIn("Mozilla/5.0", captured_kwargs[0]["headers"].get("User-Agent", ""))
                self.assertEqual(captured_kwargs[0]["headers"].get("Accept"), "application/json")

        # 2) ClassroomToolsTab.refresh_firebase_roster 비동기 호출 시 버튼 비활성화 및 성공/실패 후 복구 검증
        tab = ClassroomToolsTab(task_runner=self.task_runner)
        tab.login_teacher("유우석", "pw1234", sync_service=self.sync_svc)
        orig_refresh_text = tab.btn_teacher_refresh.text()
        orig_sync_text = tab.btn_sync_roster.text()

        def _wait_idle(timeout_sec: float = 3.0) -> None:
            qapp = QApplication.instance()
            deadline = time.time() + timeout_sec
            while tab._roster_sync_running and time.time() < deadline:
                if qapp is not None:
                    qapp.processEvents()
                time.sleep(0.01)
            if qapp is not None:
                qapp.processEvents()

        # 2-a) 성공 케이스
        with patch.object(
            FirestoreSyncService,
            "fetch_homeroom_students",
            return_value=[{"student_id": "20401", "name": "김철수"}],
        ):
            tab.refresh_firebase_roster(silent=False)
            self.assertFalse(tab.btn_teacher_refresh.isEnabled())
            self.assertFalse(tab.btn_sync_roster.isEnabled())
            _wait_idle()
            self.assertTrue(tab.btn_teacher_refresh.isEnabled())
            self.assertTrue(tab.btn_sync_roster.isEnabled())
            self.assertEqual(tab.btn_teacher_refresh.text(), orig_refresh_text)
            self.assertEqual(tab.btn_sync_roster.text(), orig_sync_text)

        # 2-b) 실패(ReadTimeout) 케이스에서도 버튼 활성화 및 원래 텍스트 복구 보장
        with patch.object(
            FirestoreSyncService,
            "fetch_homeroom_students",
            side_effect=RuntimeError("Read timed out. (read timeout=45)"),
        ):
            with patch.object(QMessageBox, "warning") as mock_warn:
                tab.refresh_firebase_roster(silent=False)
                self.assertFalse(tab.btn_teacher_refresh.isEnabled())
                self.assertFalse(tab.btn_sync_roster.isEnabled())
                _wait_idle()
                self.assertTrue(tab.btn_teacher_refresh.isEnabled())
                self.assertTrue(tab.btn_sync_roster.isEnabled())
                self.assertEqual(tab.btn_teacher_refresh.text(), orig_refresh_text)
                self.assertEqual(tab.btn_sync_roster.text(), orig_sync_text)
                mock_warn.assert_called_once()

        # 3) 슬림 헤더 및 공용 Session Retry 설정 검증
        self.assertEqual(tab.btn_teacher_refresh.text(), "")
        self.assertEqual(tab.btn_teacher_refresh.width(), 34)
        self.assertEqual(tab.btn_teacher_refresh.height(), 34)
        self.assertFalse(tab.btn_teacher_refresh.icon().isNull())

        session = svc._get_http_session()
        adapter = session.get_adapter("https://m.eduflow.kro.kr/api/firestore")
        self.assertEqual(adapter.max_retries.total, 2)
        self.assertEqual(adapter.max_retries.backoff_factor, 0.5)
        self.assertIn(502, adapter.max_retries.status_forcelist)
        self.assertIn(503, adapter.max_retries.status_forcelist)
        self.assertIn(504, adapter.max_retries.status_forcelist)


if __name__ == "__main__":
    unittest.main()
