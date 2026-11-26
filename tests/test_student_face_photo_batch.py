"""
학생 얼굴 사진 일괄 매칭 · 리사이즈 · Google Drive 업로드 및 `학생_안면데이터` 동기화 단위 테스트.
- 파일명(학번 `10501` / 학생 이름 `김유민` / 복합 파일명) 자동 매칭 검증
- `학생_안면데이터` 페이로드 직렬화 및 Direct Admin / Vercel API 모드 양방향 검증
- Pillow(PIL) 최대 640px JPEG 경량 리사이즈 및 다이얼로그 UI 매칭·수정 동작 검증
"""

from __future__ import annotations

import base64
import io
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

from PIL import Image

from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService
from ui.dialogs.student_face_photo_dialog import (
    StudentFacePhotoBatchDialog,
    build_student_photo_drive_folder,
    match_filename_to_student,
    match_photo_files_to_roster,
    resize_image_to_jpeg_bytes,
)

_APP_INSTANCE = None


def _ensure_qapp():
    global _APP_INSTANCE
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        _APP_INSTANCE = QApplication(["eduflow-face-photo-test"])
        return _APP_INSTANCE
    return app


class _FakeConfig:
    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def get_section(self, section: str) -> dict:
        return dict(self._data.get(section, {}))


class TestStudentFacePhotoBatch(unittest.TestCase):
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
                    "auth_password_hash": "b" * 64,
                },
                "api_and_integrations": {},
            }
        )
        self.roster = [
            {"student_id": "10501", "name": "김유민", "grade": 1, "class_no": 5, "number": 1},
            {"student_id": "10502", "name": "박시우", "grade": 1, "class_no": 5, "number": 2},
            {"student_id": "10503", "name": "이서진", "grade": 1, "class_no": 5, "number": 3},
        ]

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_filename_matching_by_student_id_and_name(self) -> None:
        # 1) 학번 정확 일치
        m1, r1 = match_filename_to_student("10501.jpg", self.roster)
        self.assertIsNotNone(m1)
        self.assertEqual(m1["student_id"], "10501")
        self.assertEqual(m1["name"], "김유민")
        self.assertIn("학번", r1)

        # 2) 학생 이름 정확 일치
        m2, r2 = match_filename_to_student("김유민.png", self.roster)
        self.assertIsNotNone(m2)
        self.assertEqual(m2["student_id"], "10501")
        self.assertIn("이름", r2)

        # 3) 학번 + 이름 복합 파일명
        m3, r3 = match_filename_to_student("10502_박시우_증명사진.webp", self.roster)
        self.assertIsNotNone(m3)
        self.assertEqual(m3["student_id"], "10502")
        self.assertEqual(m3["name"], "박시우")

        # 4) 번호 + 이름 복합 파일명
        m4, r4 = match_filename_to_student("03-이서진.jpeg", self.roster)
        self.assertIsNotNone(m4)
        self.assertEqual(m4["student_id"], "10503")
        self.assertEqual(m4["name"], "이서진")

        # 5) 미매칭 파일명
        m5, r5 = match_filename_to_student("IMG_9999.jpg", self.roster)
        self.assertIsNone(m5)
        self.assertEqual(r5, "미매칭")

    def test_match_photo_files_to_roster_batch(self) -> None:
        p1 = self.tmp_path / "10501.jpg"
        p2 = self.tmp_path / "박시우.png"
        p3 = self.tmp_path / "unknown.webp"
        p_txt = self.tmp_path / "ignore.txt"
        for p in (p1, p2, p3, p_txt):
            p.write_bytes(b"dummy")

        results = match_photo_files_to_roster([p1, p2, p3, p_txt], self.roster)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["matched_student_id"], "10501")
        self.assertEqual(results[0]["matched_student_name"], "김유민")
        self.assertEqual(results[1]["matched_student_id"], "10502")
        self.assertEqual(results[1]["matched_student_name"], "박시우")
        self.assertEqual(results[2]["matched_student_id"], "")

    def test_resize_image_to_max_400px_jpeg(self) -> None:
        src_path = self.tmp_path / "large_rgba.png"
        img = Image.new("RGBA", (1600, 1200), (30, 144, 255, 200))
        img.save(src_path, format="PNG")

        jpeg_bytes = resize_image_to_jpeg_bytes(src_path)
        self.assertGreater(len(jpeg_bytes), 100)
        self.assertLessEqual(len(jpeg_bytes), 20 * 1024)

        with Image.open(io.BytesIO(jpeg_bytes)) as out_img:
            self.assertEqual(out_img.format, "JPEG")
            self.assertEqual(out_img.mode, "RGB")
            self.assertEqual(out_img.size, (400, 300))

    def test_normalize_student_face_photo_doc_schema(self) -> None:
        sample_data_url = "data:image/jpeg;base64," + base64.b64encode(b"fake_jpeg_bytes").decode("ascii")
        doc = FirestoreSyncService.normalize_student_face_photo_doc(
            {
                "student_id": "10501",
                "student_name": "김유민",
                "photo_data_url": sample_data_url,
                "updated_at": "2026-11-20 12:00:00",
            },
            default_meta={
                "school_code": "8140246",
                "school_name": "천안오성고",
                "grade": 1,
                "class_no": 5,
            },
        )
        expected_keys = {
            "remote_id",
            "school_code",
            "school_name",
            "grade",
            "class_no",
            "student_id",
            "student_name",
            "photo_file_id",
            "photo_drive_url",
            "photo_data_url",
            "photo_hash",
            "updated_at",
        }
        self.assertEqual(set(doc.keys()), expected_keys)
        self.assertEqual(doc["remote_id"], "8140246_10501")
        self.assertEqual(doc["school_code"], "8140246")
        self.assertEqual(doc["school_name"], "천안오성고")
        self.assertEqual(doc["grade"], 1)
        self.assertEqual(doc["class_no"], 5)
        self.assertEqual(doc["student_id"], "10501")
        self.assertEqual(doc["student_name"], "김유민")
        self.assertEqual(doc["photo_file_id"], "")
        self.assertEqual(doc["photo_drive_url"], "")
        self.assertEqual(doc["photo_data_url"], sample_data_url)
        self.assertEqual(len(doc["photo_hash"]), 64)
        self.assertEqual(doc["updated_at"], "2026-11-20 12:00:00")

        folder = build_student_photo_drive_folder(
            school_name="천안오성고",
            grade=1,
            class_no=5,
            school_code="8140246",
        )
        self.assertEqual(folder, "EduFlow_Student_Photos/천안오성고_1학년5반")

    @patch("services.firestore_sync_service.has_local_credentials", return_value=False)
    def test_vercel_api_mode_upsert_batch_with_data_url(self, _mock_cred) -> None:
        svc = FirestoreSyncService(db=self.db, config=self.cfg, firestore_client=None)
        self.assertTrue(svc.is_vercel_api_mode())

        captured_calls = []

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured_calls.append(json)
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            resp.json.return_value = {"ok": True, "upserted": 1}
            return resp

        sample_data_url = "data:image/jpeg;base64," + base64.b64encode(b"fake_jpeg").decode("ascii")
        with patch("requests.Session.post", side_effect=_fake_post):
            ok = svc.upsert_student_face_photos_batch(
                [
                    {
                        "student_id": "10501",
                        "student_name": "김유민",
                        "photo_data_url": sample_data_url,
                        "photo_hash": "abc123hash",
                    }
                ]
            )
            self.assertTrue(ok)

        self.assertEqual(len(captured_calls), 1)
        self.assertEqual(captured_calls[0]["action"], "upsert_student_face_photos_batch")
        saved_item = captured_calls[0]["items"][0]
        self.assertEqual(saved_item["remote_id"], "8140246_10501")
        self.assertEqual(saved_item["school_code"], "8140246")
        self.assertEqual(saved_item["school_name"], "천안오성고")
        self.assertEqual(saved_item["grade"], 1)
        self.assertEqual(saved_item["class_no"], 5)
        self.assertEqual(saved_item["student_id"], "10501")
        self.assertEqual(saved_item["student_name"], "김유민")
        self.assertEqual(saved_item["photo_data_url"], sample_data_url)
        self.assertEqual(saved_item["photo_hash"], "abc123hash")

    def test_direct_admin_mode_upsert_student_face_photos_batch(self) -> None:
        fake_fs = MagicMock()
        fake_col = MagicMock()
        fake_batch = MagicMock()
        fake_fs.collection.return_value = fake_col
        fake_fs.batch.return_value = fake_batch
        fake_col.document.side_effect = lambda doc_id: f"DOC_REF::{doc_id}"

        svc = FirestoreSyncService(db=self.db, config=self.cfg, firestore_client=fake_fs)
        self.assertEqual(svc.get_backend_mode(), "direct_admin")

        data_url_1 = "data:image/jpeg;base64," + base64.b64encode(b"img1").decode("ascii")
        data_url_2 = "data:image/jpeg;base64," + base64.b64encode(b"img2").decode("ascii")
        ok = svc.upsert_student_face_photos_batch(
            [
                {
                    "student_id": "10501",
                    "student_name": "김유민",
                    "photo_data_url": data_url_1,
                    "photo_hash": "hash1",
                },
                {
                    "student_id": "10502",
                    "student_name": "박시우",
                    "photo_data_url": data_url_2,
                    "photo_hash": "hash2",
                },
            ]
        )
        self.assertTrue(ok)
        fake_fs.collection.assert_called_with("학생_안면데이터")
        self.assertEqual(fake_batch.set.call_count, 2)
        first_call_args = fake_batch.set.call_args_list[0]
        self.assertEqual(first_call_args[0][0], "DOC_REF::8140246_10501")
        doc_payload = first_call_args[0][1]
        self.assertEqual(doc_payload["remote_id"], "8140246_10501")
        self.assertEqual(doc_payload["student_name"], "김유민")
        self.assertEqual(doc_payload["grade"], 1)
        self.assertEqual(doc_payload["class_no"], 5)
        self.assertEqual(doc_payload["photo_data_url"], data_url_1)
        self.assertEqual(doc_payload["photo_hash"], "hash1")
        fake_batch.commit.assert_called_once()

    def test_dialog_preview_table_and_manual_override(self) -> None:
        _ensure_qapp()
        img1 = self.tmp_path / "10501.jpg"
        img2 = self.tmp_path / "unmatched.png"
        Image.new("RGB", (200, 200), (100, 150, 200)).save(img1, format="JPEG")
        Image.new("RGB", (200, 200), (200, 100, 150)).save(img2, format="PNG")

        dlg = StudentFacePhotoBatchDialog(
            roster=self.roster,
            school_code="8140246",
            school_name="천안오성고",
            grade=1,
            class_no=5,
            db=self.db,
            config=self.cfg,
        )
        added = dlg.add_image_files([img1, img2])
        self.assertEqual(added, 2)

        # 초기 상태: 10501.jpg만 자동 매칭되어 업로드 대상 1건
        targets = dlg.get_selected_upload_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["student_id"], "10501")
        self.assertEqual(targets[0]["student_name"], "김유민")

        # 교사가 미리보기 표에서 2번째 미매칭 사진을 '10503 이서진'으로 수동 지정
        combo_row1 = dlg._get_row_combo(1)
        self.assertIsNotNone(combo_row1)
        idx = combo_row1.findData("10503")
        self.assertGreaterEqual(idx, 0)
        combo_row1.setCurrentIndex(idx)

        targets_after = dlg.get_selected_upload_targets()
        self.assertEqual(len(targets_after), 2)
        self.assertEqual(targets_after[1]["student_id"], "10503")
        self.assertEqual(targets_after[1]["student_name"], "이서진")
        dlg.close()

    @patch("PyQt6.QtWidgets.QMessageBox.information")
    def test_on_upload_and_sync_connects_function_worker_signals_progress(self, _mock_info) -> None:
        _ensure_qapp()
        from core.base_worker import FunctionWorker

        img1 = self.tmp_path / "10501.jpg"
        Image.new("RGB", (300, 300), (120, 160, 210)).save(img1, format="JPEG")

        captured_workers = []

        class _InlineTaskRunner:
            def start_worker(
                self,
                worker,
                task_key=None,
                on_result=None,
                on_error=None,
                on_finished=None,
            ):
                captured_workers.append(worker)
                res = worker.execute()
                if on_result is not None:
                    on_result(res)
                if on_finished is not None:
                    on_finished()
                return task_key or "inline"

        dlg = StudentFacePhotoBatchDialog(
            roster=self.roster,
            school_code="8140246",
            school_name="천안오성고",
            grade=1,
            class_no=5,
            db=self.db,
            config=self.cfg,
            task_runner=_InlineTaskRunner(),
        )
        dlg.add_image_files([img1])

        with patch.object(
            FirestoreSyncService,
            "upload_file",
            side_effect=AssertionError("Google Drive upload_file은 호출되면 안 됩니다."),
        ) as mock_upload, patch.object(
            FirestoreSyncService,
            "upsert_student_face_photos_batch",
            return_value=True,
        ) as mock_upsert:
            # 실제 FunctionWorker 인스턴스를 생성해 worker.signals.progress.connect(...)가
            # AttributeError 없이 정상 연결되고, upload_file 없이 photo_data_url이 직접 전송되는지 검증
            dlg._on_upload_and_sync()

        mock_upload.assert_not_called()
        mock_upsert.assert_called_once()
        sent_docs = mock_upsert.call_args[0][0]
        self.assertEqual(len(sent_docs), 1)
        self.assertEqual(sent_docs[0]["remote_id"], "8140246_10501")
        self.assertTrue(sent_docs[0]["photo_data_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(len(sent_docs[0]["photo_hash"]), 64)
        self.assertEqual(len(captured_workers), 1)
        self.assertIsInstance(captured_workers[0], FunctionWorker)
        self.assertEqual(dlg.progress_bar.value(), 100)
        self.assertEqual(dlg.table.item(0, 4).text(), "동기화 완료")
        dlg.close()


if __name__ == "__main__":
    unittest.main()
