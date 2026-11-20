"""
PC 에이전트 온라인/오프라인 하트비트 및 모바일 원격 작업 큐 실행기 단위 테스트.
- FirestoreSyncService Direct Admin / Vercel API 모드 양쪽의
  update_pc_agent_status, fetch_pending_remote_tasks, update_remote_task_status 검증
- ClassroomToolsTab 로그인 시 온라인 하트비트(90초 타이머), 로그아웃 및 앱 종료(cleanup_on_quit) 시 오프라인 보고 검증
- RemoteTaskExecutor 4종 task_type (comcigan_sync, schoolbell_sync, file_sort, homeroom_report)
  예약 시각 판별, running -> done / error 상태 전환 및 결과 memo 검증
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.base_worker import TaskRunner
from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService
from services.remote_task_executor import RemoteTaskExecutor, is_task_due
from ui.tabs.tab_classroom_tools import ClassroomToolsTab

_APP_INSTANCE = None


def _ensure_qapp():
    global _APP_INSTANCE
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        _APP_INSTANCE = QApplication(["eduflow-pc-agent-test"])
        return _APP_INSTANCE
    return app


class _FakeConfig:
    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def get_section(self, section: str) -> dict:
        return dict(self._data.get(section, {}))

    def update_section(self, section: str, updates: dict, auto_save: bool = True) -> None:
        self._data.setdefault(section, {}).update(updates)


class _FakeDocSnap:
    def __init__(self, doc_id: str, data: dict, exists: bool = True):
        self.id = doc_id
        self._data = dict(data)
        self.exists = exists

    def to_dict(self) -> dict:
        return dict(self._data)


class TestPcAgentHeartbeatAndRemoteTasks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db = DatabaseManager(db_rel_path=str(self.tmp_path / "test_eduflow.db"))
        self.cfg = _FakeConfig(
            {
                "teacher_profile": {
                    "teacher_id": "유우석",
                    "name": "유우석",
                    "school_code": "8140246",
                    "neis_school_code": "8140246",
                    "school_name": "천안오성고",
                    "grade": 1,
                    "class_no": 5,
                    "auth_password_hash": "c" * 64,
                },
                "api_and_integrations": {
                    "comcigan_url": "http://comci.net:4082/st",
                    "comcigan_school_code": "12345",
                    "comcigan_teacher_name": "유우석",
                },
                "file_sorter": {
                    "source_dirs": [str(self.tmp_path / "src")],
                    "target_base_dir": str(self.tmp_path / "out"),
                    "action_mode": "move",
                },
            }
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    @patch("services.firestore_sync_service.has_local_credentials", return_value=False)
    def test_firestore_sync_pc_agent_and_tasks_vercel_mode(self, _mock_cred) -> None:
        svc = FirestoreSyncService(db=self.db, config=self.cfg, firestore_client=None)
        self.assertTrue(svc.is_vercel_api_mode())

        calls = []

        def _fake_post(url, json=None, headers=None, timeout=None):
            calls.append(json)
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            if json.get("action") == "fetch_pending_remote_tasks":
                resp.json.return_value = {
                    "ok": True,
                    "tasks": [{"remote_id": "t_1", "task_type": "file_sort", "exec_status": "pending"}],
                }
            else:
                resp.json.return_value = {"ok": True}
            return resp

        with patch("requests.post", side_effect=_fake_post):
            self.assertTrue(svc.update_pc_agent_status(online=True, hostname="TEACHER-PC", app_version="2.0"))
            pending = svc.fetch_pending_remote_tasks()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["remote_id"], "t_1")
            self.assertTrue(svc.update_remote_task_status("t_1", "done", memo="완료", is_done=True))

        self.assertEqual(calls[0]["action"], "update_pc_agent_status")
        self.assertTrue(calls[0]["online"])
        self.assertTrue(calls[0]["pc_agent_online"])
        self.assertEqual(calls[0]["hostname"], "TEACHER-PC")
        self.assertEqual(calls[0]["app_version"], "2.0")
        self.assertEqual(calls[1]["action"], "fetch_pending_remote_tasks")
        self.assertEqual(calls[2]["action"], "update_remote_task_status")
        self.assertEqual(calls[2]["exec_status"], "done")
        self.assertTrue(calls[2]["is_done"])
        self.assertEqual(calls[2]["memo"], "완료")

    def test_firestore_sync_pc_agent_and_tasks_direct_admin_mode(self) -> None:
        fake_fs = MagicMock()
        teacher_col = MagicMock()
        tasks_col = MagicMock()
        teacher_doc = MagicMock()
        task_doc = MagicMock()
        teacher_doc.get.return_value = _FakeDocSnap("유우석", {"teacher_id": "유우석"}, exists=True)
        teacher_col.document.return_value = teacher_doc
        tasks_col.document.return_value = task_doc
        tasks_col.where.return_value.stream.return_value = [
            _FakeDocSnap(
                "task_due_1",
                {
                    "task_type": "comcigan_sync",
                    "exec_status": "pending",
                    "is_mobile_task": True,
                    "is_done": False,
                    "teacher_id": "유우석",
                    "school_code": "8140246",
                },
            ),
            _FakeDocSnap(
                "task_other_teacher",
                {
                    "task_type": "file_sort",
                    "exec_status": "pending",
                    "is_mobile_task": True,
                    "is_done": False,
                    "teacher_id": "다른교사",
                    "school_code": "8140246",
                },
            ),
        ]

        def _col_router(name: str):
            if name == "계정_교사":
                return teacher_col
            if name == "Tasks":
                return tasks_col
            return MagicMock()

        fake_fs.collection.side_effect = _col_router
        svc = FirestoreSyncService(db=self.db, config=self.cfg, firestore_client=fake_fs)
        self.assertEqual(svc.get_backend_mode(), "direct_admin")

        self.assertTrue(svc.update_pc_agent_status(online=False, hostname="TEACHER-PC", app_version="2.0"))
        teacher_doc.set.assert_called_once()
        written_agent = teacher_doc.set.call_args[0][0]
        self.assertFalse(written_agent["pc_agent_online"])
        self.assertEqual(written_agent["pc_agent_hostname"], "TEACHER-PC")

        pending = svc.fetch_pending_remote_tasks()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["remote_id"], "task_due_1")

        self.assertTrue(svc.update_remote_task_status("task_due_1", "running", memo="시작", is_done=False))
        task_doc.set.assert_called_once()
        written_task = task_doc.set.call_args[0][0]
        self.assertEqual(written_task["exec_status"], "running")
        self.assertFalse(written_task["is_done"])

    def test_classroom_tab_heartbeat_login_logout_and_quit_offline(self) -> None:
        _ensure_qapp()
        mock_svc = MagicMock()
        mock_svc.authenticate_account.return_value = (
            True,
            {
                "teacher_id": "유우석",
                "name": "유우석",
                "school_code": "8140246",
                "school_name": "천안오성고",
                "grade": 1,
                "class_no": 5,
            },
            "",
        )
        mock_svc.fetch_homeroom_students.return_value = [
            {"student_id": "10501", "name": "김유민"},
        ]
        mock_svc.update_pc_agent_status.return_value = True

        with patch.object(ClassroomToolsTab, "restore_saved_teacher_session", return_value=False):
            ctab = ClassroomToolsTab(task_runner=TaskRunner())
        ctab.db = self.db
        ctab.config = _FakeConfig({"teacher_profile": {"session_logged_in": False}})
        ctab._logged_in_teacher = None
        ctab._apply_teacher_auth_state(trigger_heartbeat=False)

        # 미로그인 시 타이머 비활성 & 하트비트 미호출
        self.assertFalse(ctab._heartbeat_timer.isActive())
        self.assertFalse(ctab._remote_task_timer.isActive())
        mock_svc.update_pc_agent_status.assert_not_called()

        # 교사 로그인 시 90초 하트비트 & 60초 원격 작업 타이머 가동 + 즉시 online=True 호출
        ok, err = ctab.login_teacher("유우석", "1234", sync_service=mock_svc)
        self.assertTrue(ok, err)
        self.assertTrue(ctab._heartbeat_timer.isActive())
        self.assertEqual(ctab._heartbeat_timer.interval(), 90_000)
        self.assertTrue(ctab._remote_task_timer.isActive())
        self.assertEqual(ctab._remote_task_timer.interval(), 60_000)
        self.assertEqual(mock_svc.update_pc_agent_status.call_count, 1)
        self.assertTrue(mock_svc.update_pc_agent_status.call_args.kwargs["online"])

        # 로그아웃 시 online=False 즉시 보고 & 타이머 중지
        ctab.logout_teacher(sync_service=mock_svc)
        self.assertFalse(ctab._heartbeat_timer.isActive())
        self.assertFalse(ctab._remote_task_timer.isActive())
        self.assertEqual(mock_svc.update_pc_agent_status.call_count, 2)
        self.assertFalse(mock_svc.update_pc_agent_status.call_args.kwargs["online"])

        # 재로그인 후 앱 종료(cleanup_on_quit -> notify_pc_agent_offline) 시 online=False 보고
        ctab.login_teacher("유우석", "1234", sync_service=mock_svc)
        self.assertTrue(ctab._heartbeat_timer.isActive())
        ctab.notify_pc_agent_offline(sync_service=mock_svc)
        self.assertFalse(ctab._heartbeat_timer.isActive())
        self.assertFalse(mock_svc.update_pc_agent_status.call_args.kwargs["online"])
        ctab.close()

    def test_remote_task_executor_four_task_types_and_error_handling(self) -> None:
        mock_sync = MagicMock()
        mock_sync.update_remote_task_status.return_value = True
        mock_sync.fetch_homeroom_students.return_value = [
            {"student_id": "10501", "name": "김유민"},
        ]
        mock_sync.fetch_homeroom_attendance.return_value = [
            {
                "student_id": "10501",
                "student_name": "김유민",
                "attendance_type": "질병결석",
                "date": "2026-11-20",
                "reason": "감기",
                "teacher_confirmed": True,
            }
        ]
        mock_sync.fetch_collection_rows.return_value = [
            {
                "title": "현장체험학습 안내",
                "category": "가정통신문",
                "due_date": "2026-11-25",
                "school_code": "8140246",
                "target_grade": 1,
                "target_class": 5,
                "is_published": True,
                "requires_reply": True,
            }
        ]

        now_ref = datetime(2026, 11, 20, 14, 0, 0)
        self.assertTrue(is_task_due({"scheduled_time": ""}, now=now_ref))
        self.assertTrue(is_task_due({"scheduled_time": "2026-11-20 13:59:00"}, now=now_ref))
        self.assertFalse(is_task_due({"scheduled_time": "2026-11-20 15:00:00"}, now=now_ref))

        executor = RemoteTaskExecutor(db=self.db, config=self.cfg, sync_service=mock_sync)

        # 1) comcigan_sync
        with patch(
            "services.remote_task_executor.sync_comcigan_from_web",
            return_value={"ok": True, "base_count": 30, "change_count": 2},
        ) as mock_comci:
            res1 = executor.execute_task(
                {"remote_id": "rt_comci", "task_type": "comcigan_sync", "title": "컴시간 동기화"}
            )
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["exec_status"], "done")
            mock_comci.assert_called_once()
            self.assertIn("기본 30건", res1["summary"])

        # 2) schoolbell_sync
        with patch(
            "services.remote_task_executor.load_saved_schoolbell_board_url",
            return_value="https://schoolbell-e.com/board/123",
        ), patch(
            "services.remote_task_executor.SchoolbellCrawlerService"
        ) as mock_sb_cls:
            mock_sb_cls.return_value.fetch_recent_notices.return_value = {
                "ok": True,
                "notices": [{"title": "11월 학사일정 안내"}],
            }
            res2 = executor.execute_task(
                {"remote_id": "rt_sb", "task_type": "schoolbell_sync", "title": "학교종이 수집"}
            )
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["exec_status"], "done")
            self.assertIn("1건 수집 완료", res2["summary"])

        # 3) file_sort
        with patch("services.remote_task_executor.BatchSortWorker") as mock_sort_cls:
            mock_sort_cls.return_value.execute.return_value = {"processed": 5}
            res3 = executor.execute_task(
                {"remote_id": "rt_sort", "task_type": "file_sort", "title": "다운로드 폴더 정리"}
            )
            self.assertTrue(res3["ok"])
            self.assertEqual(res3["exec_status"], "done")
            self.assertIn("5건 처리", res3["summary"])

        # 4) homeroom_report (.xlsx 실제 생성 및 경로 memo 기록 검증)
        res4 = executor.execute_task(
            {
                "remote_id": "rt_report",
                "task_type": "homeroom_report",
                "title": "학급 현황 보고서",
                "action_payload": {"output_dir": str(self.tmp_path / "reports")},
            }
        )
        self.assertTrue(res4["ok"])
        self.assertEqual(res4["exec_status"], "done")
        report_path = Path(res4["detail"]["report_path"])
        self.assertTrue(report_path.is_file())
        self.assertEqual(report_path.suffix, ".xlsx")
        self.assertIn(str(report_path), res4["summary"])

        # 5) 실패 시 running -> error + 오류 사유 기록 검증
        mock_sync.update_remote_task_status.reset_mock()
        with patch(
            "services.remote_task_executor.load_saved_schoolbell_board_url",
            return_value="",
        ):
            res_err = executor.execute_task(
                {"remote_id": "rt_fail", "task_type": "schoolbell_sync", "title": "URL 없음 테스트"}
            )
        self.assertFalse(res_err["ok"])
        self.assertEqual(res_err["exec_status"], "error")
        status_calls = [c.kwargs["exec_status"] for c in mock_sync.update_remote_task_status.call_args_list]
        self.assertEqual(status_calls, ["running", "error"])
        self.assertFalse(mock_sync.update_remote_task_status.call_args_list[-1].kwargs["is_done"])


if __name__ == "__main__":
    unittest.main()
