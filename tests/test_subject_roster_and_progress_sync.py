"""
교과·동아리 명렬표(`교과_명렬표`) 및 수업 진도(`수업_진도`) Firestore 동기화 단위 테스트.
Direct Admin 모드와 Vercel API(`/api/firestore`) 모드 양쪽의 페이로드·직렬화 및 UI 연동을 검증합니다.
"""

from __future__ import annotations

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

from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService


class _FakeConfig:
    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def get_section(self, section: str) -> dict:
        return dict(self._data.get(section, {}))


class _FakeDocSnap:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = dict(data)
        self.exists = True

    def to_dict(self) -> dict:
        return dict(self._data)


class TestSubjectRosterAndProgressSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db = DatabaseManager(db_rel_path=str(self.tmp_path / "test_eduflow.db"))
        self.cfg = _FakeConfig(
            {
                "teacher_profile": {
                    "teacher_id": "t_kim",
                    "name": "김선생",
                    "school_code": "8140246",
                    "auth_password_hash": "a" * 64,
                },
                "api_and_integrations": {},
            }
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_build_subject_and_club_roster_payload_serialization(self) -> None:
        subj_payload = FirestoreSyncService.build_subject_roster_payload(
            group={"id": 1, "group_type": "subject", "group_name": "정보", "extra_info": "1학년 3반"},
            students=[
                {"seq_no": 1, "student_id": "10301", "grade": 1, "class_no": 3, "number": 1, "name": "홍길동"},
                {"seq_no": 2, "student_id": "10302", "grade": 1, "class_no": 3, "number": 2, "name": "김철수"},
            ],
            school_code="8140246",
            teacher_id="t_kim",
            teacher_name="김선생",
        )
        self.assertTrue(subj_payload["remote_id"].startswith("sr_subject_"))
        self.assertEqual(subj_payload["school_code"], "8140246")
        self.assertEqual(subj_payload["teacher_id"], "t_kim")
        self.assertEqual(subj_payload["teacher_name"], "김선생")
        self.assertEqual(subj_payload["roster_type"], "subject")
        self.assertEqual(subj_payload["subject"], "정보")
        self.assertEqual(subj_payload["class_label"], "1학년 3반")
        self.assertEqual(subj_payload["grade"], 1)
        self.assertEqual(subj_payload["class_no"], 3)
        self.assertTrue(subj_payload["updated_at"])
        self.assertEqual(
            subj_payload["students"],
            [
                {"student_id": "10301", "name": "홍길동", "number": 1, "serial_no": 1, "grade": 1, "class_no": 3},
                {"student_id": "10302", "name": "김철수", "number": 2, "serial_no": 2, "grade": 1, "class_no": 3},
            ],
        )

        club_payload = FirestoreSyncService.build_subject_roster_payload(
            group={"id": 2, "group_type": "club", "group_name": "AI코딩반", "extra_info": "담당: 김선생"},
            students=[
                {"seq_no": 1, "student_id": "10105", "grade": 1, "class_no": 1, "number": 5, "name": "이영희"},
                {"seq_no": 2, "student_id": "20410", "grade": 2, "class_no": 4, "number": 10, "name": "박민수"},
            ],
            school_code="8140246",
            teacher_id="t_kim",
            teacher_name="김선생",
        )
        self.assertTrue(club_payload["remote_id"].startswith("sr_club_"))
        self.assertEqual(club_payload["roster_type"], "club")
        self.assertEqual(club_payload["subject"], "AI코딩반")
        self.assertIsNone(club_payload["grade"])
        self.assertIsNone(club_payload["class_no"])
        self.assertEqual(len(club_payload["students"]), 2)

    def test_sync_subject_rosters_batch_direct_admin_and_vercel_api(self) -> None:
        roster_input = [
            {
                "roster_type": "subject",
                "subject": "수학Ⅰ",
                "class_label": "2학년 5반",
                "students": [
                    {"student_id": "20501", "name": "강감찬", "number": 1, "serial_no": 1, "grade": 2, "class_no": 5}
                ],
            }
        ]

        # 1) Direct Admin 모드
        fake_fs = MagicMock()
        fake_batch = MagicMock()
        fake_fs.batch.return_value = fake_batch
        svc_direct = FirestoreSyncService(db=self.db, config=self.cfg, firestore_client=fake_fs)
        self.assertFalse(svc_direct.is_vercel_api_mode())
        ok_direct = svc_direct.sync_subject_rosters_batch(roster_input)
        self.assertTrue(ok_direct)
        fake_fs.collection.assert_called_with("교과_명렬표")
        fake_batch.set.assert_called_once()
        fake_batch.commit.assert_called_once()
        written_doc = fake_batch.set.call_args.args[1]
        self.assertEqual(written_doc["teacher_id"], "t_kim")
        self.assertEqual(written_doc["school_code"], "8140246")
        self.assertEqual(written_doc["grade"], 2)
        self.assertEqual(written_doc["class_no"], 5)

        # 2) Vercel API 모드
        with patch("services.firestore_sync_service.has_local_credentials", return_value=False):
            svc_vercel = FirestoreSyncService(db=self.db, config=self.cfg)
            self.assertTrue(svc_vercel.is_vercel_api_mode())
            fake_resp = MagicMock(ok=True, status_code=200)
            fake_resp.json.return_value = {"ok": True, "count": 1}
            with patch("requests.post", return_value=fake_resp) as mock_post:
                ok_vercel = svc_vercel.sync_subject_rosters_batch(roster_input)
                self.assertTrue(ok_vercel)
                mock_post.assert_called_once()
                sent_body = mock_post.call_args.kwargs["json"]
                self.assertEqual(sent_body["action"], "sync_subject_rosters_batch")
                self.assertEqual(sent_body["auth_teacher_id"], "t_kim")
                self.assertEqual(sent_body["auth_password_hash"], "a" * 64)
                self.assertEqual(len(sent_body["rosters"]), 1)
                self.assertEqual(sent_body["rosters"][0]["subject"], "수학Ⅰ")

    def test_sync_and_fetch_lesson_progress_direct_and_vercel(self) -> None:
        progress_items = [
            {
                "date": "2026-11-18",
                "period": 3,
                "subject": "정보",
                "class_label": "1학년 3반",
                "roster_key": "subject_1",
                "unit_title": "II. 자료와 정보",
                "lesson_no": "4차시",
                "content": "이진수 변환 실습",
                "next_plan": "ASCII 코드 탐구",
                "memo": "모둠 활동 완료",
            }
        ]

        # 1) Direct Admin Upsert + Fetch
        fake_fs = MagicMock()
        fake_batch = MagicMock()
        fake_fs.batch.return_value = fake_batch
        svc_direct = FirestoreSyncService(db=self.db, config=self.cfg, firestore_client=fake_fs)
        self.assertTrue(svc_direct.sync_lesson_progress_batch(progress_items))
        fake_fs.collection.assert_called_with("수업_진도")
        written = fake_batch.set.call_args.args[1]
        for required_key in (
            "remote_id",
            "school_code",
            "teacher_id",
            "date",
            "period",
            "subject",
            "class_label",
            "roster_key",
            "unit_title",
            "lesson_no",
            "content",
            "next_plan",
            "memo",
            "updated_at",
        ):
            self.assertIn(required_key, written)
        self.assertEqual(written["period"], 3)

        fake_query = MagicMock()
        fake_query.stream.return_value = [_FakeDocSnap(written["remote_id"], written)]
        fake_fs.collection.return_value.where.return_value = fake_query
        fetched_direct = svc_direct.fetch_lesson_progress(roster_key="subject_1")
        self.assertEqual(len(fetched_direct), 1)
        self.assertEqual(fetched_direct[0]["unit_title"], "II. 자료와 정보")

        # 2) Vercel API Upsert + Fetch
        with patch("services.firestore_sync_service.has_local_credentials", return_value=False):
            svc_vercel = FirestoreSyncService(db=self.db, config=self.cfg)
            fake_resp_up = MagicMock(ok=True, status_code=200)
            fake_resp_up.json.return_value = {"ok": True}
            with patch("requests.post", return_value=fake_resp_up) as mock_post_up:
                self.assertTrue(svc_vercel.sync_lesson_progress_batch(progress_items))
                self.assertEqual(mock_post_up.call_args.kwargs["json"]["action"], "sync_lesson_progress_batch")

            fake_resp_get = MagicMock(ok=True, status_code=200)
            fake_resp_get.json.return_value = {"ok": True, "items": [written]}
            with patch("requests.post", return_value=fake_resp_get) as mock_post_get:
                fetched_vercel = svc_vercel.fetch_lesson_progress(roster_key="subject_1", subject="정보")
                self.assertEqual(len(fetched_vercel), 1)
                self.assertEqual(fetched_vercel[0]["content"], "이진수 변환 실습")
                self.assertEqual(mock_post_get.call_args.kwargs["json"]["action"], "fetch_lesson_progress")

    def test_grade_material_progress_sync_roundtrip(self) -> None:
        from PyQt6.QtWidgets import QApplication
        from core.base_worker import TaskRunner
        from ui.tabs.tab_grade_material import GradeMaterialTab

        if QApplication.instance() is None:
            self._qapp = QApplication(["eduflow-test"])

        gid = self.db.create_roster_group("subject", "정보", extra_info="1학년 3반")
        self.db.add_roster_students(
            gid,
            [{"seq_no": 1, "student_id": "10301", "grade": 1, "class_no": 3, "number": 1, "name": "홍길동"}],
        )
        tab = GradeMaterialTab(task_runner=TaskRunner())
        tab.db = self.db
        tab._populate_subject_combo()

        subj_key = tab._current_subject_key()
        self.db.upsert_subject_material_progress(subj_key, "1단원/수업안.pdf", "진도 완료", "p.15까지 완료")

        items = tab._build_lesson_progress_items_for_sync(config=self.cfg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["roster_key"], subj_key)
        self.assertEqual(items[0]["subject"], "정보")
        self.assertEqual(items[0]["class_label"], "1학년 3반")
        self.assertEqual(items[0]["memo"], "p.15까지 완료")

        applied = tab._apply_remote_lesson_progress(
            [
                {
                    "roster_key": subj_key,
                    "subject": "정보",
                    "unit_title": "2단원_알고리즘",
                    "lesson_no": "3차시",
                    "content": "정렬 알고리즘 실습",
                    "next_plan": "탐색 알고리즘",
                    "memo": "모바일 수업 중 입력",
                    "updated_at": "2099-01-01 10:00:00",
                }
            ]
        )
        self.assertEqual(applied, 1)
        pmap = self.db.get_subject_material_progress_map(subj_key)
        self.assertIn("2단원_알고리즘 (3차시)", pmap)
        self.assertEqual(pmap["2단원_알고리즘 (3차시)"]["status"], "진도 완료")
        self.assertIn("모바일 수업 중 입력", pmap["2단원_알고리즘 (3차시)"]["memo"])
        tab.close()


if __name__ == "__main__":
    unittest.main()
