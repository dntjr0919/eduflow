"""
HWP/HWPX 학사일정 AI 파싱·검수 표 및 통합 DB 관리(로컬 증분 캐시) 단위 테스트.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core.base_worker import TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.firestore_sync_service import DB_MANAGER_COLLECTIONS, FirestoreSyncService
from services.schedule_parser_service import (
    build_hwp_schedule_ai_prompt,
    extract_schedule_text_from_hwp_hwpx,
    parse_calendar_ai_response,
)
from ui.dialogs.schedule_dialogs import ScheduleImportPreviewDialog
from ui.tabs.tab_calendar_todo import CalendarTodoTab
from ui.tabs.tab_db_manager import DbManagerTab, format_cell_display, is_readonly_field


def _ensure_qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


def _make_sample_hwpx(path: Path) -> None:
    section_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
        'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
        "<hp:p><hp:run><hp:t>2026학년도 1학기 학사일정 안내</hp:t></hp:run></hp:p>"
        "<hp:tbl>"
        "<hp:tr>"
        "<hp:tc><hp:p><hp:run><hp:t>날짜</hp:t></hp:run></hp:p></hp:tc>"
        "<hp:tc><hp:p><hp:run><hp:t>행사명</hp:t></hp:run></hp:p></hp:tc>"
        "<hp:tc><hp:p><hp:run><hp:t>비고</hp:t></hp:run></hp:p></hp:tc>"
        "</hp:tr>"
        "<hp:tr>"
        "<hp:tc><hp:p><hp:run><hp:t>2026-03-02</hp:t></hp:run></hp:p></hp:tc>"
        "<hp:tc><hp:p><hp:run><hp:t>시업식 및 입학식</hp:t></hp:run></hp:p></hp:tc>"
        "<hp:tc><hp:p><hp:run><hp:t>강당 09:00</hp:t></hp:run></hp:p></hp:tc>"
        "</hp:tr>"
        "</hp:tbl>"
        "</hs:sec>"
    )
    with ZipFile(path, "w") as zf:
        zf.writestr("Contents/section0.xml", section_xml.encode("utf-8"))


class _FakeDoc:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = dict(data)

    def to_dict(self) -> dict:
        return dict(self._data)


class _FakeBatch:
    def __init__(self, store: dict):
        self._store = store
        self._ops = []

    def set(self, doc_ref, data: dict, merge: bool = False):
        self._ops.append(("set", doc_ref.col_name, doc_ref.doc_id, dict(data), merge))

    def delete(self, doc_ref):
        self._ops.append(("delete", doc_ref.col_name, doc_ref.doc_id))

    def commit(self):
        for op in self._ops:
            if op[0] == "set":
                _, col_name, doc_id, data, merge = op
                col = self._store.setdefault(col_name, {})
                if merge and doc_id in col:
                    col[doc_id].update(data)
                else:
                    col[doc_id] = data
            elif op[0] == "delete":
                _, col_name, doc_id = op
                self._store.get(col_name, {}).pop(doc_id, None)


class _FakeDocRef:
    def __init__(self, col_name: str, doc_id: str):
        self.col_name = col_name
        self.doc_id = doc_id


class _FakeQuery:
    def __init__(self, col_name: str, docs: dict, field: str, op: str, val: str):
        self.col_name = col_name
        self.docs = docs
        self.field = field
        self.op = op
        self.val = val

    def stream(self):
        out = []
        for doc_id, data in self.docs.items():
            fv = str(data.get(self.field, "") or "")
            if self.op == ">" and fv > self.val:
                out.append(_FakeDoc(doc_id, data))
        return out


class _FakeCollectionRef:
    def __init__(self, col_name: str, store: dict, recorded_queries: list):
        self.col_name = col_name
        self.store = store
        self.recorded_queries = recorded_queries

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self.col_name, doc_id)

    def stream(self):
        self.recorded_queries.append(("full", self.col_name, None, None))
        return [
            _FakeDoc(doc_id, data)
            for doc_id, data in self.store.get(self.col_name, {}).items()
        ]

    def where(self, field: str, op: str, val: str) -> _FakeQuery:
        self.recorded_queries.append(("where", self.col_name, field, val))
        return _FakeQuery(
            self.col_name, self.store.get(self.col_name, {}), field, op, val
        )


class _FakeFirestoreClient:
    def __init__(self, store: dict):
        self.store = store
        self.recorded_queries = []

    def collection(self, col_name: str) -> _FakeCollectionRef:
        return _FakeCollectionRef(col_name, self.store, self.recorded_queries)

    def batch(self) -> _FakeBatch:
        return _FakeBatch(self.store)


class TestHwpScheduleAndDbManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _ensure_qapp()

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp_dir.name)
        self.db = DatabaseManager(db_rel_path=str(self.tmp_path / "test.db"))
        self.config = ConfigManager()
        self.task_runner = TaskRunner()

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. HWP/HWPX 학사일정 AI 파싱 & 검수 표 테스트
    # -------------------------------------------------------------------------
    def test_hwpx_extract_prompt_and_json_array_parsing(self) -> None:
        hwpx_path = self.tmp_path / "academic_calendar.hwpx"
        _make_sample_hwpx(hwpx_path)

        extracted = extract_schedule_text_from_hwp_hwpx(hwpx_path)
        self.assertIn("2026학년도 1학기 학사일정 안내", extracted)
        self.assertIn("2026-03-02 | 시업식 및 입학식 | 강당 09:00", extracted)

        prompt = build_hwp_schedule_ai_prompt(
            extracted, file_name=hwpx_path.name, today=date(2026, 3, 1)
        )
        self.assertIn("academic_calendar.hwpx", prompt)
        self.assertIn('"start_date": "YYYY-MM-DD"', prompt)
        self.assertIn("시업식 및 입학식", prompt)

        # AI가 최상위 JSON 배열을 반환한 경우 파싱 검증
        ai_raw = """```json
        [
          {
            "title": "시업식 및 입학식",
            "start_date": "2026-03-02",
            "end_date": "2026-03-02",
            "category": "학사일정",
            "memo": "강당 09:00"
          },
          {
            "title": "1차 지필평가",
            "start_date": "2026-04-28",
            "end_date": "2026-04-30",
            "category": "수행평가",
            "memo": "전 학년"
          }
        ]
        ```"""
        parsed = parse_calendar_ai_response(ai_raw, today=date(2026, 3, 1))
        self.assertEqual(len(parsed["schedules"]), 2)
        self.assertEqual(parsed["schedules"][0]["title"], "시업식 및 입학식")
        self.assertEqual(parsed["schedules"][1]["end_date"], "2026-04-30")

    def test_schedule_import_preview_dialog_add_edit_delete(self) -> None:
        items = [
            {
                "title": "개학식",
                "start_date": "2026-03-02",
                "end_date": "2026-03-02",
                "category": "학사일정",
                "memo": "운동장",
            }
        ]
        dlg = ScheduleImportPreviewDialog(parsed_items=items, file_name="test.hwpx")
        self.assertEqual(dlg.table.rowCount(), 1)

        # 행 추가
        dlg._on_add_row()
        self.assertEqual(dlg.table.rowCount(), 2)
        dlg.table.item(1, 1).setText("2026-05-05")
        dlg.table.item(1, 2).setText("2026-05-05")
        dlg.table.item(1, 4).setText("어린이날")
        dlg.table.item(1, 6).setText("공휴일")

        selected = dlg.get_selected_schedules()
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[1]["title"], "어린이날")

        # 첫 번째 행 선택 후 삭제
        dlg.table.selectRow(0)
        dlg._on_delete_selected_rows()
        self.assertEqual(dlg.table.rowCount(), 1)
        selected_after = dlg.get_selected_schedules()
        self.assertEqual(len(selected_after), 1)
        self.assertEqual(selected_after[0]["title"], "어린이날")

    def test_calendar_tab_unified_bulk_import_dispatch(self) -> None:
        hwpx_path = self.tmp_path / "cal.hwpx"
        _make_sample_hwpx(hwpx_path)

        tab = CalendarTodoTab(
            task_runner=self.task_runner,
            db=self.db,
            config=self.config,
            start_timer=False,
        )
        self.assertEqual(tab.btn_bulk_import.text(), "학사일정 일괄 등록")
        self.assertFalse(hasattr(tab, "btn_hwp_ai_import"))

        received = []
        tab.ai_hwp_schedule_requested.connect(lambda p, fn: received.append((p, fn)))
        # 통합 import_schedule_file()이 .hwpx를 AI 도크로 분기하는지 확인
        ret = tab.import_schedule_file(str(hwpx_path))

        self.assertEqual(ret, 0)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1], "cal.hwpx")
        self.assertIn("시업식 및 입학식", received[0][0])

    # -------------------------------------------------------------------------
    # 2. 로컬 증분 캐시 (updated_at / created_at 델타 병합 & 전체 재동기화) 테스트
    # -------------------------------------------------------------------------
    def test_incremental_delta_sync_and_full_resync(self) -> None:
        store = {
            "Schedules": {
                "sch_1": {
                    "remote_id": "sch_1",
                    "title": "입학식",
                    "updated_at": "2026-11-20 10:00:00",
                },
                "sch_2": {
                    "remote_id": "sch_2",
                    "title": "소풍",
                    "updated_at": "2026-11-20 11:00:00",
                },
            },
            "Notifications": {
                "noti_1": {
                    "remote_id": "noti_1",
                    "title": "가정통신문 발송",
                    "created_at": "2026-11-20 10:00:00",
                }
            },
        }
        fake_fs = _FakeFirestoreClient(store)
        sync = FirestoreSyncService(db=self.db, config=self.config)
        sync._get_client = lambda: fake_fs

        # 최초 캐시 비어있을 때 delta 호출 시 full로 동작
        res1 = sync.sync_collection_delta("Schedules")
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["mode"], "full")
        self.assertEqual(res1["total_count"], 2)

        # last_sync_at을 11:30:00으로 고정하고, 11:29:00 이후 변경분 추가
        self.db.merge_collection_cache(
            "Schedules", [], last_sync_at="2026-11-20 11:30:00"
        )
        store["Schedules"]["sch_2"]["title"] = "가을 소풍(변경)"
        store["Schedules"]["sch_2"]["updated_at"] = "2026-11-20 11:29:30"
        store["Schedules"]["sch_3"] = {
            "remote_id": "sch_3",
            "title": "신규 일정",
            "updated_at": "2026-11-20 11:31:00",
        }

        res2 = sync.sync_collection_delta("Schedules", overlap_minutes=2)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["mode"], "delta")
        self.assertEqual(res2["ts_field"], "updated_at")
        self.assertEqual(res2["cutoff"], "2026-11-20 11:28:00")
        # sch_1은 updated_at이 10:00:00이므로 읽히지 않고 2건만 delta 조회됨
        self.assertEqual(res2["fetched_count"], 2)
        self.assertEqual(res2["total_count"], 3)

        cached = self.db.get_collection_cache("Schedules")
        self.assertEqual(cached["rows_by_id"]["sch_2"]["title"], "가을 소풍(변경)")
        self.assertIn("sch_1", cached["rows_by_id"])

        # Notifications는 created_at 기준 쿼리 확인
        sync.sync_collection_full("Notifications")
        self.db.merge_collection_cache(
            "Notifications", [], last_sync_at="2026-11-20 11:30:00"
        )
        res_noti = sync.sync_collection_delta("Notifications")
        self.assertEqual(res_noti["ts_field"], "created_at")

        # 외부에서 sch_1 삭제 후 [전체 다시 받기]로 캐시 교체 반영 확인
        store["Schedules"].pop("sch_1")
        res_full = sync.sync_collection_full("Schedules")
        self.assertTrue(res_full["ok"])
        self.assertEqual(res_full["total_count"], 2)
        cached_after_full = self.db.get_collection_cache("Schedules")
        self.assertNotIn("sch_1", cached_after_full["rows_by_id"])

    # -------------------------------------------------------------------------
    # 3. 통합 DB 관리 탭 (읽기 전용 잠금, Base64 요약, 캐시 즉시 로드, CSV 내보내기)
    # -------------------------------------------------------------------------
    def test_db_manager_tab_readonly_base64_and_cache_only_load(self) -> None:
        self.db.replace_collection_cache(
            "계정_학생",
            [
                {
                    "remote_id": "stu_30101",
                    "name": "김철수",
                    "password_hash": "abcd" * 16,
                    "photo_data_url": "data:image/png;base64," + ("A" * 120),
                    "face_descriptor": [0.1, 0.2, 0.3],
                    "updated_at": "2026-11-20 12:00:00",
                    "created_at": "2026-11-01 09:00:00",
                }
            ],
            last_sync_at="2026-11-20 12:00:00",
        )

        mock_sync = MagicMock(spec=FirestoreSyncService)
        tab = DbManagerTab(
            task_runner=self.task_runner,
            db=self.db,
            config=self.config,
            sync_service=mock_sync,
        )
        # 초기화 시 Firestore 호출 0건
        mock_sync.sync_collection_delta.assert_not_called()
        mock_sync.sync_collection_full.assert_not_called()

        # 컬렉션 전환 시에도 Firestore 호출 0건
        idx = tab.combo_collection.findData("계정_학생")
        self.assertGreaterEqual(idx, 0)
        tab.combo_collection.setCurrentIndex(idx)
        mock_sync.sync_collection_delta.assert_not_called()
        mock_sync.sync_collection_full.assert_not_called()

        self.assertEqual(tab.table.rowCount(), 1)
        col_map = {col: i for i, col in enumerate(tab._columns)}

        # 읽기 전용 컬럼 잠금 확인
        for ro_col in ("remote_id", "password_hash", "photo_data_url", "face_descriptor", "updated_at", "created_at"):
            self.assertTrue(is_readonly_field(ro_col))
            item = tab.table.item(0, col_map[ro_col])
            self.assertFalse(bool(item.flags() & Qt.ItemFlag.ItemIsEditable))

        # Base64 요약 표시 & 원본 보존 확인
        photo_item = tab.table.item(0, col_map["photo_data_url"])
        self.assertEqual(photo_item.text(), "[이미지 데이터]")
        self.assertTrue(str(photo_item.data(Qt.ItemDataRole.UserRole)).startswith("data:image/png;base64,"))

        # 일반 필드(name) 편집 시 dirty 추적 확인
        name_item = tab.table.item(0, col_map["name"])
        self.assertTrue(bool(name_item.flags() & Qt.ItemFlag.ItemIsEditable))
        name_item.setText("김영희")
        self.assertIn(0, tab._dirty_rows)
        self.assertTrue(tab.btn_save_changes.isEnabled())
        self.assertEqual(tab._row_records[0]["name"], "김영희")

        # CSV 내보내기 (UTF-8 BOM)
        csv_path = self.tmp_path / "students.csv"
        written = tab.export_visible_to_csv(str(csv_path))
        self.assertEqual(written, 1)
        raw_bytes = csv_path.read_bytes()
        self.assertTrue(raw_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertIn("김영희", raw_bytes.decode("utf-8-sig"))

    def test_db_manager_collections_list_complete(self) -> None:
        expected = {
            "Schedules",
            "일일_출결",
            "서류_추적",
            "가정통신문_목록",
            "가정통신문_회신",
            "계정_학생",
            "계정_학부모",
            "계정_교사",
            "학급회비",
            "수업_진도",
            "교과_출결",
            "학급_공지게시판",
            "시간표_기본",
            "시간표_변동",
            "급식_식단표",
            "Tasks",
            "Notifications",
        }
        self.assertEqual(set(DB_MANAGER_COLLECTIONS), expected)
        self.assertEqual(len(DB_MANAGER_COLLECTIONS), 17)

    def test_db_manager_function_worker_callbacks_accept_worker_arg(self) -> None:
        self.db.replace_collection_cache(
            "Schedules",
            [
                {
                    "remote_id": "sch_100",
                    "title": "개학식",
                    "updated_at": "2026-11-20 10:00:00",
                }
            ],
            last_sync_at="2026-11-20 10:00:00",
        )
        mock_sync = MagicMock(spec=FirestoreSyncService)
        mock_sync.sync_collection_delta.return_value = {"ok": True, "mode": "delta"}
        mock_sync.sync_collection_full.return_value = {"ok": True, "mode": "full"}
        mock_sync.upsert_collection_rows.return_value = {"ok": True, "saved_count": 1}
        mock_sync.delete_collection_rows.return_value = {"ok": True, "deleted_count": 1}

        captured_workers = []

        class _InlineRunner:
            def start_worker(self, *, worker, task_key="", on_result=None, on_error=None, **_kw):
                captured_workers.append((task_key, worker))
                # 실제 FunctionWorker.execute()를 호출해 TypeError가 없는지 검증
                res = worker.execute()
                if on_result is not None:
                    on_result(res)

        tab = DbManagerTab(
            task_runner=_InlineRunner(),
            db=self.db,
            config=self.config,
            sync_service=mock_sync,
        )

        # 1) 변경분 새로고침
        tab._on_delta_refresh_clicked()
        mock_sync.sync_collection_delta.assert_called_once_with("Schedules")

        # 2) 전체 다시 받기
        tab._on_full_refresh_clicked()
        mock_sync.sync_collection_full.assert_called_once_with("Schedules")

        # 3) 셀 수정 후 변경사항 일괄 저장
        col_map = {col: i for i, col in enumerate(tab._columns)}
        tab.table.item(0, col_map["title"]).setText("개학식(수정)")
        from unittest.mock import patch

        with patch("ui.tabs.tab_db_manager.QMessageBox.information"), patch(
            "ui.tabs.tab_db_manager.QMessageBox.question",
            return_value=16384,  # QMessageBox.StandardButton.Yes
        ):
            tab._on_save_changes_clicked()
            mock_sync.upsert_collection_rows.assert_called_once()

            # 4) 선택 행 삭제
            tab.table.selectRow(0)
            tab._on_delete_rows_clicked()
            mock_sync.delete_collection_rows.assert_called_once_with("Schedules", ["sch_100"])

        self.assertEqual(len(captured_workers), 4)


if __name__ == "__main__":
    unittest.main()
