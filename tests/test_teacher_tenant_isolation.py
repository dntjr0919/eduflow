"""
교사별·테넌트별 DB 격리, UI 명렬표 폴백 차단, 공통 교사 계정 배지 단위 테스트.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton

from core.base_worker import TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService
from ui.main_window import MainWindow
from ui.tabs.tab_classroom_tools import ClassroomToolsTab


_APP_INSTANCE = None


def _ensure_qapp() -> QApplication:
    global _APP_INSTANCE
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP_INSTANCE = app
    return app


class _FakeSnap:
    def __init__(self, doc_id: str, data: Dict[str, Any], exists: bool = True):
        self.id = doc_id
        self._data = dict(data)
        self.exists = exists

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)


class _FakeQuery:
    def __init__(self, store: Dict[str, Dict[str, Any]], filters=None, limit_n=None):
        self._store = store
        self._filters = list(filters or [])
        self._limit = limit_n

    def where(self, field: str, op: str, val: Any) -> "_FakeQuery":
        return _FakeQuery(self._store, self._filters + [(field, op, val)], self._limit)

    def limit(self, n: int) -> "_FakeQuery":
        return _FakeQuery(self._store, self._filters, n)

    def stream(self) -> List[_FakeSnap]:
        out: List[_FakeSnap] = []
        for doc_id, data in self._store.items():
            matched = True
            for field, op, val in self._filters:
                dv = data.get(field)
                if op == "==":
                    if dv != val:
                        matched = False
                        break
                elif op == ">":
                    if not (str(dv or "") > str(val or "")):
                        matched = False
                        break
            if matched:
                out.append(_FakeSnap(doc_id, data, exists=True))
                if self._limit is not None and len(out) >= self._limit:
                    break
        return out


class _FakeDocRef:
    def __init__(self, store: Dict[str, Dict[str, Any]], doc_id: str):
        self._store = store
        self._doc_id = doc_id

    def get(self) -> _FakeSnap:
        if self._doc_id in self._store:
            return _FakeSnap(self._doc_id, self._store[self._doc_id], exists=True)
        return _FakeSnap(self._doc_id, {}, exists=False)

    def set(self, data: Dict[str, Any], merge: bool = False) -> None:
        if merge and self._doc_id in self._store:
            merged = dict(self._store[self._doc_id])
            merged.update(data)
            self._store[self._doc_id] = merged
        else:
            self._store[self._doc_id] = dict(data)

    def delete(self) -> None:
        self._store.pop(self._doc_id, None)


class _FakeCollection(_FakeQuery):
    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, doc_id)


class _FakeBatch:
    def __init__(self):
        self._ops: List[Any] = []

    def set(self, doc_ref: _FakeDocRef, data: Dict[str, Any], merge: bool = False) -> None:
        self._ops.append(("set", doc_ref, dict(data), merge))

    def delete(self, doc_ref: _FakeDocRef) -> None:
        self._ops.append(("delete", doc_ref))

    def commit(self) -> None:
        for op in self._ops:
            if op[0] == "set":
                _, ref, data, merge = op
                ref.set(data, merge=merge)
            elif op[0] == "delete":
                _, ref = op
                ref.delete()


class _FakeFirestore:
    def __init__(self, initial: Dict[str, Dict[str, Dict[str, Any]]] = None):
        self.cols: Dict[str, Dict[str, Dict[str, Any]]] = dict(initial or {})

    def collection(self, name: str) -> _FakeCollection:
        if name not in self.cols:
            self.cols[name] = {}
        return _FakeCollection(self.cols[name])

    def batch(self) -> _FakeBatch:
        return _FakeBatch()


class TestTeacherTenantIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_db = DatabaseManager._instance
        self._prev_cfg = ConfigManager._instance
        DatabaseManager._instance = None
        ConfigManager._instance = None
        self.db = DatabaseManager(str(Path(self.tmp.name) / "test.db"))
        self.cfg = ConfigManager()
        self.cfg.config_path = str(Path(self.tmp.name) / "config.json")
        self.cfg.save()
        self.cfg.update_section(
            "teacher_profile",
            {
                "teacher_id": "teacher_a",
                "name": "김교사",
                "school_code": "7010001",
                "neis_school_code": "7010001",
                "school_name": "테스트고",
                "grade": 1,
                "class_no": 30,
                "session_logged_in": True,
            },
        )

    def tearDown(self) -> None:
        DatabaseManager._instance = self._prev_db
        ConfigManager._instance = self._prev_cfg
        self.tmp.cleanup()

    def test_read_isolation_fetch_collection_rows_and_sync_all(self) -> None:
        fake_fs = _FakeFirestore(
            {
                "Schedules": {
                    "s_mine": {
                        "remote_id": "s_mine",
                        "title": "내 일정",
                        "start_date": "2026-11-27",
                        "teacher_id": "teacher_a",
                        "school_code": "7010001",
                    },
                    "s_legacy": {
                        "remote_id": "s_legacy",
                        "title": "학교 공통 일정",
                        "start_date": "2026-11-28",
                        "school_code": "7010001",
                    },
                    "s_other_teacher": {
                        "remote_id": "s_other_teacher",
                        "title": "타 교사 일정",
                        "start_date": "2026-11-29",
                        "teacher_id": "teacher_b",
                        "school_code": "7010001",
                    },
                    "s_other_school": {
                        "remote_id": "s_other_school",
                        "title": "타 학교 일정",
                        "start_date": "2026-11-30",
                        "teacher_id": "teacher_c",
                        "school_code": "9999999",
                    },
                },
                "Tasks": {
                    "t_mine": {
                        "remote_id": "t_mine",
                        "title": "내 할일",
                        "task_type": "todo",
                        "is_mobile_task": False,
                        "teacher_id": "teacher_a",
                        "school_code": "7010001",
                    },
                    "t_other": {
                        "remote_id": "t_other",
                        "title": "타 교사 할일",
                        "task_type": "todo",
                        "is_mobile_task": False,
                        "teacher_id": "teacher_b",
                        "school_code": "7010001",
                    },
                },
                "Notifications": {
                    "n_mine": {
                        "remote_id": "n_mine",
                        "title": "내 알림",
                        "message": "내용1",
                        "teacher_id": "teacher_a",
                        "school_code": "7010001",
                    },
                    "n_other": {
                        "remote_id": "n_other",
                        "title": "타 교사 알림",
                        "message": "내용2",
                        "teacher_id": "teacher_b",
                        "school_code": "7010001",
                    },
                },
                "일일_출결": {
                    "att_mine": {
                        "remote_id": "att_mine",
                        "student_id": "13001",
                        "teacher_id": "teacher_a",
                        "school_code": "7010001",
                        "grade": 1,
                        "class_no": 30,
                    },
                    "att_other": {
                        "remote_id": "att_other",
                        "student_id": "10501",
                        "teacher_id": "teacher_b",
                        "school_code": "7010001",
                        "grade": 1,
                        "class_no": 5,
                    },
                },
            }
        )
        svc = FirestoreSyncService(db=self.db, config=self.cfg, firestore_client=fake_fs)

        sch_rows = svc.fetch_collection_rows("Schedules")
        self.assertEqual({r["remote_id"] for r in sch_rows}, {"s_mine", "s_legacy"})

        task_rows = svc.fetch_collection_rows("Tasks")
        self.assertEqual([r["remote_id"] for r in task_rows], ["t_mine"])

        att_rows = svc.fetch_homeroom_attendance(grade=1, class_no=30, school_code="7010001")
        self.assertEqual([r["remote_id"] for r in att_rows], ["att_mine"])

        summary = svc.sync_all()
        self.assertTrue(summary["ok"])
        local_titles = {s["title"] for s in self.db.get_schedules()}
        self.assertEqual(local_titles, {"내 일정", "학교 공통 일정"})
        local_todo_titles = {t["title"] for t in self.db.get_todos()}
        self.assertEqual(local_todo_titles, {"내 할일"})
        local_noti_titles = {n["title"] for n in self.db.get_notifications()}
        self.assertEqual(local_noti_titles, {"내 알림"})

    def test_sync_from_payload_blocks_foreign_teacher_allows_legacy(self) -> None:
        svc = FirestoreSyncService(db=self.db, config=self.cfg)
        payload = {
            "schedules": [
                {"remote_id": "s1", "title": "내 일정", "start_date": "2026-11-27", "teacher_id": "teacher_a"},
                {"remote_id": "s2", "title": "레거시 일정", "start_date": "2026-11-28"},
                {"remote_id": "s3", "title": "타 교사 일정", "start_date": "2026-11-29", "teacher_id": "teacher_b"},
            ],
            "tasks": [
                {"remote_id": "t1", "title": "내 할일", "task_type": "todo", "is_mobile_task": False, "teacher_id": "teacher_a"},
                {"remote_id": "t2", "title": "타 교사 할일", "task_type": "todo", "is_mobile_task": False, "teacher_id": "teacher_b"},
            ],
            "notifications": [
                {"remote_id": "n1", "title": "내 알림", "message": "ok", "teacher_id": "teacher_a"},
                {"remote_id": "n2", "title": "타 교사 알림", "message": "no", "teacher_id": "teacher_b"},
            ],
        }
        res = svc.sync_from_payload(payload)
        self.assertEqual(res["schedules_added"], 2)
        self.assertEqual(res["todos_added"], 1)
        self.assertEqual(res["notifications_added"], 1)
        self.assertEqual({s["title"] for s in self.db.get_schedules()}, {"내 일정", "레거시 일정"})
        self.assertEqual({t["title"] for t in self.db.get_todos()}, {"내 할일"})
        self.assertEqual({n["title"] for n in self.db.get_notifications()}, {"내 알림"})

    def test_write_and_delete_isolation(self) -> None:
        fake_fs = _FakeFirestore(
            {
                "학급회비": {
                    "fee_mine": {"remote_id": "fee_mine", "teacher_id": "teacher_a", "school_code": "7010001", "amount": 1000},
                    "fee_other": {"remote_id": "fee_other", "teacher_id": "teacher_b", "school_code": "7010001", "amount": 2000},
                },
                "Tasks": {
                    "task_other": {"remote_id": "task_other", "teacher_id": "teacher_b", "school_code": "7010001", "exec_status": "pending"},
                },
            }
        )
        svc = FirestoreSyncService(db=self.db, config=self.cfg, firestore_client=fake_fs)

        # 타 교사 문서 덮어쓰기 차단
        res_upsert = svc.upsert_collection_rows(
            "학급회비",
            [
                {"remote_id": "fee_other", "teacher_id": "teacher_b", "amount": 9999},
                {"remote_id": "fee_new", "amount": 5000},
            ],
        )
        self.assertEqual(res_upsert["saved_count"], 1)
        self.assertEqual(fake_fs.cols["학급회비"]["fee_other"]["amount"], 2000)
        self.assertEqual(fake_fs.cols["학급회비"]["fee_new"]["teacher_id"], "teacher_a")
        self.assertEqual(fake_fs.cols["학급회비"]["fee_new"]["school_code"], "7010001")
        self.assertEqual(fake_fs.cols["학급회비"]["fee_new"]["grade"], 1)
        self.assertEqual(fake_fs.cols["학급회비"]["fee_new"]["class_no"], 30)

        # 타 교사 문서 삭제 차단
        res_del = svc.delete_collection_rows("학급회비", ["fee_mine", "fee_other"])
        self.assertEqual(res_del["deleted_count"], 1)
        self.assertEqual(res_del["blocked_count"], 1)
        self.assertNotIn("fee_mine", fake_fs.cols["학급회비"])
        self.assertIn("fee_other", fake_fs.cols["학급회비"])

        # 타 교사 원격 작업 상태 변경 차단
        self.assertFalse(svc.update_remote_task_status("task_other", "done"))
        self.assertEqual(fake_fs.cols["Tasks"]["task_other"]["exec_status"], "pending")

        # 컬렉션 전체 동기화 시 타 교사 문서 로컬 캐시 제외
        full_res = svc.sync_collection_full("학급회비")
        self.assertTrue(full_res["ok"])
        cached_ids = {r["remote_id"] for r in full_res["rows"]}
        self.assertEqual(cached_ids, {"fee_new"})

    def test_classroom_tab_blocks_mismatched_local_homeroom_fallback(self) -> None:
        _ensure_qapp()
        # 로컬 SQLite에 1학년 5반 담임 명렬표 저장
        gid = self.db.create_roster_group("homeroom", "1학년 5반", is_active=1)
        self.db.add_roster_students(
            gid,
            [
                {"student_id": "10501", "name": "오반학생1", "number": 1, "grade": 1, "class_no": 5},
                {"student_id": "10502", "name": "오반학생2", "number": 2, "grade": 1, "class_no": 5},
            ],
        )

        with patch.object(ClassroomToolsTab, "restore_saved_teacher_session", return_value=False):
            tab = ClassroomToolsTab(task_runner=TaskRunner())
        tab.db = self.db
        tab.config = self.cfg
        # 1학년 30반 교사로 로그인했지만 Firebase 명렬표가 비어 있는 상태
        tab._logged_in_teacher = {
            "teacher_id": "teacher_30",
            "name": "삼십반교사",
            "school_code": "7010001",
            "school_name": "테스트고",
            "grade": 1,
            "class_no": 30,
        }
        tab._firebase_roster = []
        tab._refresh_roster_dependent_views()

        # 1학년 5반 로컬 명렬표로 폴백되지 않아야 함
        self.assertEqual(tab._get_active_roster(), [])
        self.assertEqual(tab._get_ordered_duty_students(), [])
        self.assertIn("1학년 30반 (0명)", tab.lbl_roster_status_badge.text())

        # 로컬 SQLite 1학년 5반 데이터 자체는 삭제/훼손되지 않아야 함
        hr_students = self.db.get_active_homeroom_students()
        self.assertEqual(len(hr_students), 2)
        self.assertEqual(hr_students[0]["name"], "오반학생1")

    def test_main_window_teacher_badge_updates_on_login_logout_and_profile_change(self) -> None:
        _ensure_qapp()
        win = QMainWindow()
        win.config = self.cfg
        win.sidebar_teacher_badge = QPushButton("")
        win.teacher_status_dot = QLabel()
        win.lbl_teacher_badge = QLabel("")
        win._shortcuts = {}
        win.tab_classroom = MagicMock()
        win.tab_classroom._logged_in_teacher = None

        self.cfg.add_listener(lambda _c: MainWindow._update_teacher_account_badge(win))
        self.addCleanup(lambda: (win.close(), QApplication.processEvents()))

        # 1) 초기 프로필/로그인 상태 반영 확인
        MainWindow._update_teacher_account_badge(win)
        self.assertIn("김교사", win.lbl_teacher_badge.text())
        self.assertIn("테스트고", win.lbl_teacher_badge.text())
        self.assertIn("1-30", win.lbl_teacher_badge.text())

        # 2) 프로필 변경 시 즉시 갱신 확인
        self.cfg.update_section(
            "teacher_profile",
            {
                "teacher_id": "teacher_b",
                "name": "이교사",
                "school_name": "새학교고",
                "grade": 2,
                "class_no": 7,
                "session_logged_in": True,
            },
        )
        QApplication.processEvents()
        self.assertIn("이교사", win.lbl_teacher_badge.text())
        self.assertIn("새학교고", win.lbl_teacher_badge.text())
        self.assertIn("2-7", win.lbl_teacher_badge.text())

        # 3) 로그아웃 시 즉시 갱신 확인
        self.cfg.update_section(
            "teacher_profile",
            {"session_logged_in": False, "auth_password_hash": ""},
        )
        QApplication.processEvents()
        self.assertIn("미로그인", win.lbl_teacher_badge.text())


if __name__ == "__main__":
    unittest.main()
