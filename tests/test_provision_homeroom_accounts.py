"""
SettingsTab._on_provision_homeroom_accounts() 및 DatabaseManager 담임 학급 명렬표 조회 단위 테스트.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService
from ui.tabs.tab_settings import SettingsTab

_APP_INSTANCE = None


def _ensure_qapp():
    global _APP_INSTANCE
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        _APP_INSTANCE = QApplication(["eduflow-provision-test"])
        return _APP_INSTANCE
    return app


class _InlineTaskRunner:
    def start_worker(
        self,
        worker,
        task_key=None,
        on_result=None,
        on_error=None,
        on_finished=None,
    ):
        try:
            res = worker.execute()
            if on_result is not None:
                on_result(res)
        except Exception as exc:
            if on_error is not None:
                on_error(str(exc))
            else:
                raise
        finally:
            if on_finished is not None:
                on_finished()
        return task_key or "inline"


class TestProvisionHomeroomAccounts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db = DatabaseManager(db_rel_path=str(self.tmp_path / "test_eduflow.db"))
        self.cfg = ConfigManager()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_database_homeroom_group_and_students_wrappers(self) -> None:
        self.assertIsNone(self.db.get_active_homeroom_group())
        self.assertIsNone(self.db.get_homeroom_group())

        gid = self.db.create_roster_group(
            group_type="homeroom",
            group_name="1학년 5반",
            is_active=1,
        )
        self.db.add_roster_students(
            gid,
            [
                {"seq_no": 1, "student_id": "10501", "grade": 1, "class_no": 5, "number": 1, "name": "김유민"},
                {"seq_no": 2, "student_id": "10502", "grade": 1, "class_no": 5, "number": 2, "name": "박시우"},
            ],
        )

        active_grp = self.db.get_active_homeroom_group()
        compat_grp = self.db.get_homeroom_group()
        self.assertIsNotNone(active_grp)
        self.assertEqual(active_grp["id"], gid)
        self.assertEqual(compat_grp["id"], gid)

        students_by_roster = self.db.get_roster_students(gid)
        students_by_compat = self.db.get_students(group_id=gid)
        self.assertEqual(len(students_by_roster), 2)
        self.assertEqual([s["student_id"] for s in students_by_compat], ["10501", "10502"])

    @patch("PyQt6.QtWidgets.QMessageBox.warning")
    def test_on_provision_homeroom_accounts_warns_when_empty(self, mock_warn) -> None:
        _ensure_qapp()
        with patch.object(SettingsTab, "_start_model_fetch", return_value=None):
            stab = SettingsTab(db=self.db, config=self.cfg)
        stab.task_runner = _InlineTaskRunner()

        stab._on_provision_homeroom_accounts()
        mock_warn.assert_called_once()
        self.assertIn("학급명렬표", mock_warn.call_args[0][2])
        stab.close()

    @patch("PyQt6.QtWidgets.QMessageBox.information")
    @patch("PyQt6.QtWidgets.QMessageBox.critical")
    def test_on_provision_homeroom_accounts_runs_without_error(self, mock_crit, mock_info) -> None:
        _ensure_qapp()
        gid = self.db.create_roster_group(
            group_type="homeroom",
            group_name="1학년 5반",
            is_active=1,
        )
        self.db.add_roster_students(
            gid,
            [
                {"seq_no": 1, "student_id": "10501", "grade": 1, "class_no": 5, "number": 1, "name": "김유민"},
                {"seq_no": 2, "student_id": "10502", "grade": 1, "class_no": 5, "number": 2, "name": "박시우"},
            ],
        )

        with patch.object(SettingsTab, "_start_model_fetch", return_value=None):
            stab = SettingsTab(db=self.db, config=self.cfg)
        stab.task_runner = _InlineTaskRunner()
        stab.edit_neis_school_code.setText("8140246")
        stab.edit_school_name.setText("천안오성고")

        with patch.object(
            FirestoreSyncService,
            "provision_homeroom_accounts",
            return_value={
                "students_created": 2,
                "students_skipped": 0,
                "parents_created": 2,
                "parents_skipped": 0,
            },
        ) as mock_prov:
            stab._on_provision_homeroom_accounts()

        mock_crit.assert_not_called()
        mock_prov.assert_called_once()
        call_kwargs = mock_prov.call_args.kwargs
        self.assertEqual(call_kwargs["school_code"], "8140246")
        self.assertEqual(call_kwargs["school_name"], "천안오성고")
        self.assertEqual([s["student_id"] for s in call_kwargs["students"]], ["10501", "10502"])
        mock_info.assert_called_once()
        self.assertIn("학생 신규 생성: 2명", mock_info.call_args[0][2])
        self.assertFalse(call_kwargs["overwrite_password"])
        stab.close()

    def test_extract_birth_6digits_formats_and_datetime(self) -> None:
        cases = [
            ({"생년월일": "2009-05-03 00:00:00"}, "090503"),
            ({"생년월일": "2009-05-03T12:30:00"}, "090503"),
            ({"생년월일": "2009.5.3"}, "090503"),
            ({"생년월일": "2009-5-3"}, "090503"),
            ({"생년월일": "2009/5/3"}, "090503"),
            ({"생년월일": "20090503000000"}, "090503"),
            ({"주민등록번호": "090503-3123456"}, "090503"),
            ({"birth_date": "20090503"}, "090503"),
            ({"extra_data": '{"생년월일": "090503"}'}, "090503"),
            ({"name": "김유민"}, ""),
        ]
        for student, expected in cases:
            self.assertEqual(
                FirestoreSyncService.extract_birth_6digits(student),
                expected,
                f"Failed for {student}",
            )

    def test_provision_homeroom_accounts_overwrite_password(self) -> None:
        from services.firestore_sync_service import hash_password_sha256

        class _DocSnap:
            def __init__(self, doc_id, data):
                self.id = doc_id
                self._data = dict(data)

            def to_dict(self):
                return dict(self._data)

        class _DocRef:
            def __init__(self, col_name, doc_id):
                self.col_name = col_name
                self.doc_id = doc_id

        class _Batch:
            def __init__(self, store):
                self._store = store
                self._ops = []

            def set(self, ref, data, merge=False):
                self._ops.append((ref.col_name, ref.doc_id, dict(data), merge))

            def commit(self):
                for col_name, doc_id, data, merge in self._ops:
                    col = self._store.setdefault(col_name, {})
                    if merge and doc_id in col:
                        col[doc_id].update(data)
                    else:
                        col[doc_id] = data

        class _ColRef:
            def __init__(self, col_name, store):
                self.col_name = col_name
                self.store = store

            def stream(self):
                return [
                    _DocSnap(k, v)
                    for k, v in self.store.get(self.col_name, {}).items()
                ]

            def document(self, doc_id):
                return _DocRef(self.col_name, doc_id)

        class _FakeFS:
            def __init__(self, store):
                self.store = store

            def collection(self, name):
                return _ColRef(name, self.store)

            def batch(self):
                return _Batch(self.store)

        old_hash = hash_password_sha256("10501")
        store = {
            "계정_학생": {"8140246_10501": {"student_id": "10501", "password_hash": old_hash}},
            "계정_학부모": {"8140246_p10501": {"parent_id": "p10501", "password_hash": old_hash}},
        }
        svc = FirestoreSyncService(db=self.db, config=self.cfg)
        svc.is_vercel_api_mode = lambda: False
        svc._get_client = lambda: _FakeFS(store)

        students = [{"student_id": "10501", "name": "김유민", "생년월일": "2009-05-03 00:00:00"}]

        # 기본값(overwrite_password=False): 기존 계정 건너뜀
        res_skip = svc.provision_homeroom_accounts(students, school_code="8140246")
        self.assertEqual(res_skip["students_skipped"], 1)
        self.assertEqual(res_skip["parents_skipped"], 1)
        self.assertEqual(store["계정_학생"]["8140246_10501"]["password_hash"], old_hash)

        # overwrite_password=True: 기존 계정 비밀번호를 생년월일 6자리(090503)로 재설정
        res_upd = svc.provision_homeroom_accounts(
            students, school_code="8140246", overwrite_password=True
        )
        new_hash = hash_password_sha256("090503")
        self.assertEqual(res_upd["students_updated"], 1)
        self.assertEqual(res_upd["parents_updated"], 1)
        self.assertEqual(store["계정_학생"]["8140246_10501"]["password_hash"], new_hash)
        self.assertEqual(store["계정_학부모"]["8140246_p10501"]["password_hash"], new_hash)

    def test_provision_homeroom_accounts_clear_errors_on_bad_json_or_non_json_api(self) -> None:
        # 1) Direct Admin 키 파일이 빈 파일/비정상 JSON일 때
        bad_key = self.tmp_path / "empty_key.json"
        bad_key.write_text("", encoding="utf-8")
        svc = FirestoreSyncService(db=self.db, config=self.cfg)
        svc.is_vercel_api_mode = lambda: False
        svc._cred_path = lambda: str(bad_key)

        with patch(
            "services.firestore_sync_service.get_firestore_client",
            side_effect=ValueError("Expecting value: line 1 column 1 (char 0)"),
        ):
            with self.assertRaises(RuntimeError) as ctx1:
                svc.provision_homeroom_accounts([{"student_id": "10501", "name": "김유민"}])
        self.assertIn("Firebase 인증 초기화 실패", str(ctx1.exception))
        self.assertIn("empty_key.json", str(ctx1.exception))

        # 2) Vercel API가 비JSON(HTML) 응답을 반환할 때
        svc2 = FirestoreSyncService(db=self.db, config=self.cfg)
        svc2.is_vercel_api_mode = lambda: True

        class _HtmlResp:
            ok = False
            status_code = 502
            text = "<html><body>502 Bad Gateway</body></html>"

            def json(self):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        with patch("requests.post", return_value=_HtmlResp()):
            with self.assertRaises(RuntimeError) as ctx2:
                svc2.provision_homeroom_accounts([{"student_id": "10501", "name": "김유민"}])
        self.assertIn("Vercel API 일괄 발급 실패", str(ctx2.exception))
        self.assertIn("HTTP 502", str(ctx2.exception))
        self.assertIn("502 Bad Gateway", str(ctx2.exception))


if __name__ == "__main__":
    unittest.main()
