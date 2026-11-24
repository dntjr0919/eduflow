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

    def test_hwp_com_conversion_priority_and_olefile_fallback(self) -> None:
        from unittest.mock import patch
        from services.schedule_parser_service import extract_schedule_text_from_document

        fake_hwp = self.tmp_path / "academic_2026_2.hwp"
        fake_hwp.write_bytes(b"dummy hwp bytes")
        sample_hwpx = self.tmp_path / "converted_sample.hwpx"
        _make_sample_hwpx(sample_hwpx)

        def _fake_convert(_src, out_hwpx):
            out_hwpx.write_bytes(sample_hwpx.read_bytes())
            return out_hwpx

        with patch("services.hwpx_agent_service.convert_hwp_to_hwpx", side_effect=_fake_convert) as mock_conv, patch(
            "services.multi_doc_extractor_service._extract_hwp_text_olefile",
            return_value="OLE_FALLBACK_SHOULD_NOT_BE_USED",
        ) as mock_ole:
            extracted, is_scanned = extract_schedule_text_from_document(fake_hwp)

        self.assertFalse(is_scanned)
        mock_conv.assert_called_once()
        mock_ole.assert_not_called()
        self.assertIn("[p_0]", extracted)
        self.assertIn("[tbl_", extracted)
        self.assertIn("2026-03-02 | 시업식 및 입학식 | 강당 09:00", extracted)

        # COM 변환 실패 시 olefile 폴백 동작 확인
        with patch("services.hwpx_agent_service.convert_hwp_to_hwpx", side_effect=RuntimeError("COM unavailable")), patch(
            "services.multi_doc_extractor_service.HAS_OLEFILE", True
        ), patch(
            "services.multi_doc_extractor_service._extract_hwp_text_olefile",
            return_value="폴백 추출 텍스트",
        ):
            fb_text, fb_scanned = extract_schedule_text_from_document(fake_hwp)
        self.assertFalse(fb_scanned)
        self.assertEqual(fb_text, "폴백 추출 텍스트")

    def test_enhanced_hwp_prompt_and_complex_json_preview_dialog(self) -> None:
        prompt = build_hwp_schedule_ai_prompt(
            "[tbl_0]\n9월 | 모의수능(3), 전국연합(인천-1,2)\n1월 | 6일-8일 겨울방학 캠프",
            file_name="2026_2학기_학사일정_변경후.hwp",
            today=date(2026, 8, 20),
        )
        self.assertIn("변경 전", prompt)
        self.assertIn("변경 후", prompt)
        self.assertIn("기준 연도 + 1", prompt)
        self.assertIn("모의수능(3)", prompt)
        self.assertIn("순수 JSON 배열", prompt)

        complex_ai_json = """```json
        [
          {"title": "모의수능(3)", "start_date": "2026-09-03", "end_date": "2026-09-03", "category": "학사일정", "memo": "3학년"},
          {"title": "전국연합(인천-1,2)", "start_date": "2026-09-03", "end_date": "2026-09-03", "category": "학사일정", "memo": "1,2학년"},
          {"title": "겨울방학 캠프", "start_date": "2027-01-06", "end_date": "2027-01-08", "category": "학사일정", "memo": "연도 전환"}
        ]
        ```"""
        parsed = parse_calendar_ai_response(complex_ai_json, today=date(2026, 8, 20))
        self.assertEqual(len(parsed["schedules"]), 3)
        self.assertEqual(parsed["schedules"][0]["title"], "모의수능(3)")
        self.assertEqual(parsed["schedules"][1]["title"], "전국연합(인천-1,2)")
        self.assertEqual(parsed["schedules"][2]["start_date"], "2027-01-06")
        self.assertEqual(parsed["schedules"][2]["end_date"], "2027-01-08")

        dlg = ScheduleImportPreviewDialog(
            parsed_items=parsed["schedules"],
            file_name="2026_2학기_학사일정_변경후.hwp",
        )
        self.assertEqual(dlg.table.rowCount(), 3)
        selected = dlg.get_selected_schedules()
        self.assertEqual(len(selected), 3)
        self.assertEqual(selected[2]["start_date"], "2027-01-06")

    def test_calendar_ai_dock_natural_language_direct_json_and_query(self) -> None:
        from unittest.mock import patch
        from services.schedule_parser_service import (
            build_calendar_ai_prompt,
            is_calendar_query_intent,
            try_parse_direct_calendar_json,
        )

        ref = date(2026, 11, 24)  # 화요일
        # 1) 자연어 프롬프트 날짜/요일 주입 & 조회 분기
        sys_p, usr_p = build_calendar_ai_prompt("내일 오전 10시 회의 등록", today=ref)
        self.assertIn("2026-11-24 (화요일)", sys_p)
        self.assertIn("item_type", sys_p)
        self.assertFalse(is_calendar_query_intent("내일 오전 10시 회의 등록"))
        self.assertTrue(is_calendar_query_intent("이번 주 일정 요약해줘"))
        self.assertTrue(is_calendar_query_intent("내일 빈 시간 알려줘"))

        q_sys, q_usr = build_calendar_ai_prompt(
            "이번 주 일정 요약해줘",
            today=ref,
            schedule_context="[등록된 일정]\n- 2026-11-25 10:00 | [업무] 교무회의",
        )
        self.assertIn("일반 텍스트", q_sys)
        self.assertIn("교무회의", q_usr)

        # 2) item_type ("schedule" | "todo") 분기 파싱 및 직접 JSON 감지
        direct_raw = """```json
        [
          {"item_type": "schedule", "title": "교무회의", "start_date": "내일", "start_time": "10:00", "category": "업무", "memo": "회의실"},
          {"item_type": "todo", "title": "공문 제출", "start_date": "이번 주 금요일", "priority": "높음", "memo": "나이스"}
        ]
        ```"""
        parsed_direct = try_parse_direct_calendar_json(direct_raw, today=ref)
        self.assertIsNotNone(parsed_direct)
        assert parsed_direct is not None
        self.assertEqual(len(parsed_direct["schedules"]), 1)
        self.assertEqual(len(parsed_direct["todos"]), 1)
        self.assertEqual(parsed_direct["schedules"][0]["start_date"], "2026-11-25")
        self.assertEqual(parsed_direct["schedules"][0]["start_time"], "10:00")
        self.assertEqual(parsed_direct["todos"][0]["due_date"], "2026-11-27")
        self.assertEqual(parsed_direct["todos"][0]["priority"], "high")

        # 3) CalendarTodoTab 1~2건 단건 바로 등록 vs 조회성 응답 무시
        tab = CalendarTodoTab(
            task_runner=self.task_runner,
            db=self.db,
            config=self.config,
            start_timer=False,
        )
        # 조회성 응답은 다이얼로그/경고 없이 무시
        tab.apply_ai_dock_result({"is_query_answer": True, "raw_response": "이번 주 일정은 없습니다."})

        # 1~2건 단건 바로 등록
        with patch(
            "ui.tabs.tab_calendar_todo.QMessageBox.question",
            return_value=16384,  # QMessageBox.StandardButton.Yes
        ), patch("ui.tabs.tab_calendar_todo.QMessageBox.information"):
            tab.apply_ai_dock_result({"parsed_calendar": parsed_direct})

        self.assertEqual(len(self.db.get_schedules()), 1)
        self.assertEqual(len(self.db.get_todos()), 1)

    def test_security_schema_whitelist_xss_limits_and_formula_injection(self) -> None:
        import csv
        import openpyxl
        from services.schedule_parser_service import (
            MAX_CALENDAR_AI_BATCH_ITEMS,
            MAX_CALENDAR_MEMO_LEN,
            MAX_CALENDAR_TITLE_LEN,
            escape_spreadsheet_formula,
            export_schedules_to_xlsx,
            try_parse_direct_calendar_json,
        )

        ref = date(2026, 11, 24)
        # 1) XSS, 제어문자, 알 수 없는 키, 잘못된 날짜/시간, 초대용량 문자열 검증
        malicious_payload = {
            "summary": "<script>alert(1)</script>요약\x00텍스트",
            "unknown_top_key": "drop table",
            "schedules": [
                {
                    "item_type": "invalid_type",
                    "title": '<img src=x onerror="alert(1)"><b>교무회의</b>\x07 ' + ("가" * 300),
                    "start_date": "2026-02-30",  # 존재하지 않는 날짜 -> 오늘 폴백
                    "end_date": "1999-01-01",    # 시작일보다 앞선 종료일 -> 시작일로 보정
                    "start_time": "99:99",       # 잘못된 시간 -> ""
                    "category": "악성카테고리",
                    "memo": "javascript:alert(1) <script>evil()</script>메모 " + ("나" * 1200),
                    "__proto__": {"polluted": True},
                    "evil_exec": "__import__('os').system('calc')",
                }
            ],
            "todos": [
                {
                    "title": "<svg onload=alert(1)>공문 제출</svg>",
                    "due_date": "not-a-date",
                    "priority": "urgent_unknown",
                    "extra_field": "ignored",
                }
            ],
        }
        parsed = parse_calendar_ai_response(malicious_payload, today=ref)
        self.assertEqual(parsed["summary"], "요약텍스트")
        self.assertEqual(len(parsed["schedules"]), 1)
        sch = parsed["schedules"][0]
        self.assertNotIn("<script>", sch["title"])
        self.assertNotIn("onerror", sch["title"].lower())
        self.assertNotIn("\x07", sch["title"])
        self.assertLessEqual(len(sch["title"]), MAX_CALENDAR_TITLE_LEN)
        self.assertEqual(sch["start_date"], "2026-11-24")
        self.assertEqual(sch["end_date"], "2026-11-24")
        self.assertEqual(sch["start_time"], "")
        self.assertTrue(sch["all_day"])
        self.assertEqual(sch["category"], "업무")  # '교무회의' 기반 안전 분류
        self.assertNotIn("<script>", sch["memo"])
        self.assertNotIn("javascript:", sch["memo"].lower())
        self.assertLessEqual(len(sch["memo"]), MAX_CALENDAR_MEMO_LEN)
        self.assertNotIn("__proto__", sch)
        self.assertNotIn("evil_exec", sch)

        td = parsed["todos"][0]
        self.assertEqual(td["title"], "공문 제출")
        self.assertEqual(td["due_date"], "")
        self.assertEqual(td["priority"], "normal")
        self.assertNotIn("extra_field", td)

        # 2) 일괄 등록 최대 500건 제한
        oversized = [
            {"title": f"일정 {i}", "start_date": "2026-11-24"}
            for i in range(MAX_CALENDAR_AI_BATCH_ITEMS + 50)
        ]
        parsed_cap = parse_calendar_ai_response(oversized, today=ref)
        self.assertEqual(len(parsed_cap["schedules"]), MAX_CALENDAR_AI_BATCH_ITEMS)

        # 3) 비정상 JSON / 코드 인젝션 시도 거부
        self.assertIsNone(try_parse_direct_calendar_json("{'bad_json': __import__('os')}"))
        with self.assertRaises(ValueError):
            parse_calendar_ai_response("not a valid json at all")

        # 4) Excel(.xlsx) 수식 인젝션 방어
        self.assertEqual(escape_spreadsheet_formula("=CMD|'/C calc'!A0"), "'=CMD|'/C calc'!A0")
        self.assertEqual(escape_spreadsheet_formula("+1+1"), "'+1+1")
        self.assertEqual(escape_spreadsheet_formula("-2+3"), "'-2+3")
        self.assertEqual(escape_spreadsheet_formula("@SUM(A1:A2)"), "'@SUM(A1:A2)")
        self.assertEqual(escape_spreadsheet_formula("정상 일정"), "정상 일정")

        xlsx_out = self.tmp_path / "formula_safe.xlsx"
        export_schedules_to_xlsx(
            [
                {
                    "start_date": "2026-11-24",
                    "end_date": "2026-11-24",
                    "title": "=HYPERLINK(\"http://evil\",\"클릭\")",
                    "category": "학사일정",
                    "memo": "@SUM(1,2)",
                }
            ],
            xlsx_out,
        )
        wb = openpyxl.load_workbook(xlsx_out)
        ws = wb.active
        self.assertEqual(ws.cell(row=2, column=3).value, "'=HYPERLINK(\"http://evil\",\"클릭\")")
        self.assertEqual(ws.cell(row=2, column=8).value, "'@SUM(1,2)")

    def test_ai_dock_calendar_todo_all_three_modes_execution(self) -> None:
        from unittest.mock import patch
        from PyQt6.QtWidgets import QApplication
        from ui.components.ai_execution_dock import AiExecutionDock

        dispatched = []

        class _CaptureRunner:
            def __init__(self) -> None:
                self.started = []

            def is_running(self, _key: str) -> bool:
                return False

            def start_worker(self, worker, task_key="", on_result=None, on_error=None, on_finished=None, **_kw):
                self.started.append((task_key, worker, on_result, on_error, on_finished))

            def stop_task(self, *_a, **_kw) -> None:
                pass

        runner = _CaptureRunner()
        dock = AiExecutionDock(task_runner=runner, db=self.db)
        try:
            dock.register_target(
                target_id="calendar_todo",
                label="캘린더 & 할일",
                context_getter=lambda: {
                    "target_id": "calendar_todo",
                    "kind": "calendar_todo",
                    "task_label": "일정·할일 자동 추출",
                    "schedule_context": "[등록된 일정] 없음",
                },
                on_result=lambda payload: dispatched.append(payload),
            )
            dock.set_target("calendar_todo", force=True)

            # 1) API 모드: 키 미등록 시 상태 라벨/채팅에 원인 가시화, 키 등록 시 실제 워커 시작
            dock._on_mode_segment_clicked(0)
            dock.txt_chat_input.setPlainText("내일 오전 10시 회의 등록")
            with patch(
                "ui.components.ai_execution_dock.get_active_ai_info",
                return_value={"has_key": False, "provider_display": "Gemini"},
            ), patch("ui.components.ai_execution_dock.QMessageBox.warning", return_value=65536):
                dock._on_chat_send_clicked()
            self.assertIn("API Key", dock.lbl_dock_progress_meta.text())
            self.assertEqual(len(runner.started), 0)

            dock.txt_chat_input.setPlainText("내일 오전 10시 회의 등록")
            with patch(
                "ui.components.ai_execution_dock.get_active_ai_info",
                return_value={"has_key": True, "provider": "gemini", "model": "gemini-2.5-flash", "provider_display": "Gemini"},
            ):
                dock._on_chat_send_clicked()
            self.assertEqual(len(runner.started), 1)
            self.assertEqual(runner.started[0][0], dock.TASK_DOCK_API)
            if callable(runner.started[0][4]):
                runner.started[0][4]()

            # 외부 요청(txt_calendar_instruction 사전 입력 후 빈 채팅창에서 전송) 시 기존 지시문 보존 확인
            dock.txt_calendar_instruction.setPlainText("기존 HWP 학사일정 추출 프롬프트")
            dock.txt_chat_input.clear()
            with patch(
                "ui.components.ai_execution_dock.get_active_ai_info",
                return_value={"has_key": True, "provider": "gemini", "model": "gemini-2.5-flash", "provider_display": "Gemini"},
            ):
                dock._on_chat_send_clicked()
            self.assertEqual(len(runner.started), 2)
            self.assertEqual(dock.txt_calendar_instruction.toPlainText(), "기존 HWP 학사일정 추출 프롬프트")

            # 2) 수동 복붙 모드: 프롬프트 클립보드 복사 + 응답 회수 대기 전환 + 응답 붙여넣기 시 결과 전달
            dock._on_mode_segment_clicked(1)
            dock.txt_chat_input.setPlainText("모레 오후 2시 학부모 상담 등록")
            with patch("ui.components.ai_execution_dock.webbrowser.open") as mock_open:
                dock._on_chat_send_clicked()
            mock_open.assert_called_once()
            self.assertTrue(dock._manual_waiting_collect)
            self.assertEqual(dock.btn_chat_send.text(), "응답 회수")
            self.assertIn("모레 오후 2시 학부모 상담 등록", QApplication.clipboard().text())

            dock.txt_chat_input.setPlainText(
                '[{"item_type": "schedule", "title": "학부모 상담", "start_date": "2026-11-26", "start_time": "14:00"}]'
            )
            dock._on_chat_send_clicked()
            self.assertFalse(dock._manual_waiting_collect)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0]["parsed_calendar"]["schedules"][0]["title"], "학부모 상담")

            # 3) Playwright 자동 모드: 실제 Playwright 워커 시작 확인
            dock._on_mode_segment_clicked(2)
            dock.txt_chat_input.setPlainText("다음 주 월요일 출장 등록")
            dock._on_chat_send_clicked()
            self.assertEqual(len(runner.started), 3)
            self.assertEqual(runner.started[2][0], dock.TASK_DOCK_PLAYWRIGHT)
            self.assertIsNotNone(dock._active_pw_worker)
        finally:
            dock.shutdown_workers()
            dock.close()
            dock.deleteLater()
            app = QApplication.instance()
            if app is not None:
                app.processEvents()

    def test_calendar_manual_crud_push_and_bidirectional_sync(self) -> None:
        from unittest.mock import MagicMock, patch
        from services.firestore_sync_service import FirestoreSyncService

        store = {"Schedules": {}, "Tasks": {}, "Notifications": {}}
        fs_mock = _FakeFirestoreClient(store)
        sync_svc = FirestoreSyncService(db=self.db, config=self.config)

        class _InlineTaskRunner:
            def is_running(self, _key: str) -> bool:
                return False

            def start_worker(self, worker, task_key="", on_result=None, on_error=None, on_finished=None, **_kw):
                res = worker.execute()
                if on_result is not None:
                    on_result(res)
                if on_finished is not None:
                    on_finished()

        with patch.object(
            self.config,
            "get_section",
            return_value={
                "teacher_id": "t_01",
                "school_code": "7530001",
                "school_name": "테스트고",
            },
        ), patch.object(sync_svc, "_get_client", return_value=fs_mock):
            tab = CalendarTodoTab(
                task_runner=_InlineTaskRunner(),
                db=self.db,
                config=self.config,
                start_timer=False,
            )
            tab.sync_service = sync_svc

            # 1) 수동 일정 추가 -> 즉시 Firestore Schedules에 school_code 포함 업로드 + 로컬 remote_id 역기록
            tab._apply_dialog_result(
                {
                    "action": "save",
                    "item_type": "schedule",
                    "id": None,
                    "data": {
                        "title": "수동 교무회의",
                        "category": "업무",
                        "start_date": "2026-11-25",
                        "end_date": "2026-11-25",
                        "start_time": "15:00",
                        "reminder_type": "10m",
                        "memo": "본관 2층",
                    },
                }
            )
            self.assertEqual(len(store["Schedules"]), 1)
            pushed_doc = next(iter(store["Schedules"].values()))
            self.assertEqual(pushed_doc["title"], "수동 교무회의")
            self.assertEqual(pushed_doc["school_code"], "7530001")
            self.assertEqual(pushed_doc["teacher_id"], "t_01")

            local_sch = self.db.get_schedules()[0]
            self.assertEqual(local_sch["remote_id"], pushed_doc["remote_id"])

            # 2) 수동 일정 수정 -> 동일 remote_id 문서 업데이트
            tab._apply_dialog_result(
                {
                    "action": "save",
                    "item_type": "schedule",
                    "id": local_sch["id"],
                    "data": {
                        "title": "수동 교무회의(변경)",
                        "category": "업무",
                        "start_date": "2026-11-25",
                        "end_date": "2026-11-25",
                        "start_time": "16:00",
                        "reminder_type": "10m",
                        "memo": "대회의실",
                    },
                }
            )
            self.assertEqual(len(store["Schedules"]), 1)
            self.assertEqual(
                store["Schedules"][local_sch["remote_id"]]["title"],
                "수동 교무회의(변경)",
            )

            # 3) 수동 일정 삭제 -> Firestore Schedules 문서도 삭제
            tab._apply_dialog_result(
                {
                    "action": "delete",
                    "item_type": "schedule",
                    "id": local_sch["id"],
                    "data": {},
                }
            )
            self.assertEqual(len(store["Schedules"]), 0)

            # 4) 로컬에만 있는 일정/할일 + 원격에만 있는 일정이 섞여 있을 때 sync_all() 양방향 Push + Pull 검증
            self.db.add_schedule(
                title="로컬 전용 학사일정",
                category="학사일정",
                start_date="2026-11-28",
                end_date="2026-11-28",
                is_read=1,
            )
            self.db.add_todo(title="로컬 전용 할일", due_date="2026-11-29", priority="high", is_read=1)
            store["Schedules"]["remote_only_1"] = {
                "remote_id": "remote_only_1",
                "title": "원격 학사일정",
                "category": "학사일정",
                "start_date": "2026-12-01",
                "end_date": "2026-12-01",
                "school_code": "7530001",
            }
            with patch("ui.tabs.tab_calendar_todo.QMessageBox.information") as mock_info:
                tab.sync_with_google_sheets(interactive=True)
            mock_info.assert_called_once()
            popup_text = mock_info.call_args[0][2]
            self.assertIn("업로드(Push): 일정 1건 · 할일 1건", popup_text)
            self.assertIn("수신(Pull): 일정 +1건", popup_text)
            self.assertEqual(len(store["Schedules"]), 2)
            self.assertEqual(len(store["Tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
