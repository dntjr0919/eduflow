"""
캘린더 & 할일, Google Sheets 동기화, 파서, 미확인 배지, 중복 실행 방지, 알림 체커 단위 테스트.
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

import openpyxl
from PyQt6.QtCore import QSize, Qt, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication, QComboBox

from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.firestore_sync_service import (
    FirestoreSyncService as GoogleSheetsSyncService,
)
from services.schedule_parser_service import (
    ScheduleReminderChecker,
    build_calendar_ai_prompt,
    calculate_dday_badge,
    export_schedules_to_ics,
    export_schedules_to_xlsx,
    infer_schedule_category,
    normalize_date_str,
    normalize_time_str,
    parse_calendar_ai_response,
    parse_csv_schedules,
    parse_ics_schedules,
    parse_schedule_file,
    parse_xlsx_schedules,
)
from ui.components.sidebar_nav_delegate import SidebarNavDelegate
from ui.dialogs.schedule_dialogs import ScheduleImportPreviewDialog
from ui.main_window import MainWindow
from utils.path_helper import get_active_workspace_dir


def _get_or_create_qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(["eduflow-test"])
    return app


class TestScheduleParsersAndExport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_normalization_and_category_inference(self) -> None:
        s, e = normalize_date_str("2026.11.02 ~ 2026.11.04", default_year=2026)
        self.assertEqual(s, "2026-11-02")
        self.assertEqual(e, "2026-11-04")

        s2, e2 = normalize_date_str("11월 18일", default_year=2026)
        self.assertEqual(s2, "2026-11-18")
        self.assertEqual(e2, "2026-11-18")

        self.assertEqual(normalize_time_str("9:5"), "09:05")
        self.assertEqual(normalize_time_str("14:30:00"), "14:30")
        self.assertEqual(normalize_time_str("종일"), "")

        self.assertEqual(infer_schedule_category("수학 수행평가 안내", ""), "수행평가")
        self.assertEqual(infer_schedule_category("2학기 기말고사", ""), "수행평가")
        self.assertEqual(infer_schedule_category("NEIS 생기부 마감", ""), "업무")
        self.assertEqual(infer_schedule_category("병원 진료", ""), "개인")
        self.assertEqual(infer_schedule_category("개교기념일", ""), "학사일정")

        from datetime import date

        text, role = calculate_dday_badge("2026-11-15", today=date(2026, 11, 15))
        self.assertEqual(text, "오늘 마감")
        self.assertEqual(role, "badgeDanger")
        text2, role2 = calculate_dday_badge("2026-11-17", today=date(2026, 11, 15))
        self.assertEqual(text2, "D-2")
        self.assertEqual(role2, "badgeWarning")

    def test_parse_xlsx_csv_ics_and_roundtrip(self) -> None:
        # 1) XLSX
        xlsx_path = self.tmp_path / "academic.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["날짜", "일정명", "카테고리", "시간", "비고"])
        ws.append(["2026-11-18", "2학년 수학 수행평가", "수행평가", "09:30", "3반 교실"])
        ws.append(["2026.11.20~2026.11.22", "교내 축제 준비", "", "", "학생회"])
        wb.save(xlsx_path)

        xlsx_items = parse_xlsx_schedules(str(xlsx_path), default_year=2026)
        self.assertEqual(len(xlsx_items), 2)
        self.assertEqual(xlsx_items[0]["start_date"], "2026-11-18")
        self.assertEqual(xlsx_items[0]["start_time"], "09:30")
        self.assertEqual(xlsx_items[0]["category"], "수행평가")
        self.assertEqual(xlsx_items[1]["start_date"], "2026-11-20")
        self.assertEqual(xlsx_items[1]["end_date"], "2026-11-22")

        # 2) CSV
        csv_path = self.tmp_path / "academic.csv"
        csv_path.write_text(
            "날짜,일정명,카테고리,메모\n"
            "2026-11-24,교직원 전체 회의,업무,대강당\n",
            encoding="utf-8-sig",
        )
        csv_items = parse_csv_schedules(str(csv_path), default_year=2026)
        self.assertEqual(len(csv_items), 1)
        self.assertEqual(csv_items[0]["title"], "교직원 전체 회의")
        self.assertEqual(csv_items[0]["category"], "업무")

        # 3) ICS (all-day multi-day exclusive DTEND + timed event)
        ics_path = self.tmp_path / "academic.ics"
        ics_path.write_text(
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:evt-1\r\n"
            "DTSTART;VALUE=DATE:20261110\r\n"
            "DTEND;VALUE=DATE:20261113\r\n"
            "SUMMARY:2학기 지필고사\r\n"
            "CATEGORIES:학사일정\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:evt-2\r\n"
            "DTSTART:20261119T153000\r\n"
            "SUMMARY:학부모 총회\\, 지원\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n",
            encoding="utf-8",
        )
        ics_items = parse_ics_schedules(str(ics_path))
        self.assertEqual(len(ics_items), 2)
        self.assertEqual(ics_items[0]["start_date"], "2026-11-10")
        self.assertEqual(ics_items[0]["end_date"], "2026-11-12")
        self.assertEqual(ics_items[1]["start_time"], "15:30")
        self.assertEqual(ics_items[1]["title"], "학부모 총회, 지원")

        # Export round-trip
        export_path = self.tmp_path / "exported.ics"
        written = export_schedules_to_ics(ics_items, str(export_path))
        self.assertEqual(written, 2)
        reimported = parse_schedule_file(str(export_path))
        self.assertEqual(len(reimported), 2)
        self.assertEqual(reimported[0]["start_date"], "2026-11-10")
        self.assertEqual(reimported[0]["end_date"], "2026-11-12")


class TestPreviewDialogAndReminderChecker(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _get_or_create_qapp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_eduflow.db"
        self.db = DatabaseManager(db_rel_path=str(self.db_path))

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_preview_before_save_requires_confirmation(self) -> None:
        parsed = [
            {
                "title": "국어 수행평가 발표",
                "start_date": "2026-11-16",
                "end_date": "2026-11-16",
                "start_time": "10:00",
                "category": "수행평가",
                "memo": "1반",
            },
            {
                "title": "제외할 테스트 일정",
                "start_date": "2026-11-17",
                "end_date": "2026-11-17",
                "start_time": "",
                "category": "개인",
                "memo": "",
            },
        ]
        dlg = ScheduleImportPreviewDialog(parsed_items=parsed, file_name="sample.xlsx")
        try:
            # 미리보기 모달을 띄운 직후에는 SQLite에 즉시 저장되지 않아야 함
            self.assertEqual(len(self.db.get_schedules()), 0)

            # 두 번째 행 체크 해제, 첫 번째 행 카테고리 변경
            dlg.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
            cat_combo = dlg.table.cellWidget(0, 5)
            self.assertIsInstance(cat_combo, QComboBox)
            idx = cat_combo.findData("학사일정")
            cat_combo.setCurrentIndex(idx)

            selected = dlg.get_selected_schedules()
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["title"], "국어 수행평가 발표")
            self.assertEqual(selected[0]["category"], "학사일정")

            added = self.db.bulk_add_schedules(selected, is_read=1)
            self.assertEqual(added, 1)
            saved = self.db.get_schedules()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["title"], "국어 수행평가 발표")
            self.assertEqual(saved[0]["category"], "학사일정")
        finally:
            dlg.close()
            dlg.deleteLater()
            self.app.processEvents()

    def test_schedule_reminder_checker_once_and_no_duplicates(self) -> None:
        self.db.add_schedule(
            title="교무회의 10분전 알림",
            category="업무",
            start_date="2026-11-15",
            end_date="2026-11-15",
            start_time="10:00",
            reminder_type="10m",
            is_read=1,
        )
        self.db.add_schedule(
            title="수학 수행평가 1시간전",
            category="수행평가",
            start_date="2026-11-15",
            end_date="2026-11-15",
            start_time="11:00",
            reminder_type="1h",
            is_read=1,
        )
        self.db.add_schedule(
            title="당일 오전 9시 알림",
            category="학사일정",
            start_date="2026-11-15",
            end_date="2026-11-15",
            start_time="",
            reminder_type="morning_9am",
            is_read=1,
        )

        mock_notifier = MagicMock()
        # 09:52 -> 10m(09:50~10:00) + morning_9am(09:00~09:00? -> event_dt 09:00 + 12h window allows 09:52!)
        # 1h 일정은 10:00~11:00이므로 09:52에는 아직 트리거되지 않음
        first_run = ScheduleReminderChecker.check_and_trigger_reminders(
            db=self.db,
            notifier=mock_notifier,
            now=datetime(2026, 11, 15, 9, 52),
        )
        self.assertEqual(len(first_run), 2)
        self.assertEqual(mock_notifier.notify.call_count, 2)
        self.assertEqual(self.db.get_notification_unread_count(), 2)

        # 동일 시각 재실행 시 이미 reminder_sent=1 이므로 중복 발송 안 됨
        dup_run = ScheduleReminderChecker.check_and_trigger_reminders(
            db=self.db,
            notifier=mock_notifier,
            now=datetime(2026, 11, 15, 9, 53),
        )
        self.assertEqual(len(dup_run), 0)

        # 10:05에는 1시간 전 일정이 트리거됨
        second_run = ScheduleReminderChecker.check_and_trigger_reminders(
            db=self.db,
            notifier=mock_notifier,
            now=datetime(2026, 11, 15, 10, 5),
        )
        self.assertEqual(len(second_run), 1)
        self.assertIn("수학 수행평가", second_run[0]["message"])
        self.assertEqual(self.db.get_notification_unread_count(), 3)


class TestGoogleSheetsSyncAndDedup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_sync.db"
        self.db = DatabaseManager(db_rel_path=str(self.db_path))

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_firestore_batch_limit_constant(self) -> None:
        from services.firestore_sync_service import FIRESTORE_BATCH_LIMIT

        self.assertLessEqual(FIRESTORE_BATCH_LIMIT, 500)

    def test_sync_from_payload_and_remote_id_deduplication(self) -> None:
        svc = GoogleSheetsSyncService(db=self.db)
        payload = {
            "ok": True,
            "server_time": "2026-11-15T09:00:00Z",
            "schedules": [
                {
                    "remote_id": "sch_remote_1",
                    "title": "모바일 등록 학사일정",
                    "category": "학사일정",
                    "start_date": "2026-11-25",
                    "end_date": "2026-11-25",
                    "start_time": "14:00",
                    "reminder_type": "10m",
                    "memo": "본관 2층",
                }
            ],
            "tasks": [
                {
                    "remote_id": "todo_remote_1",
                    "title": "NEIS 마감 확인",
                    "due_date": "2026-11-18",
                    "priority": "high",
                    "is_mobile_task": False,
                    "is_done": 0,
                },
                {
                    "remote_id": "mob_task_1",
                    "title": "3반 수행평가 제출물 자동 분류",
                    "task_type": "file_sort",
                    "is_mobile_task": True,
                    "scheduled_time": "오늘 14:30",
                    "target_info": "대상: D:\\다운로드\\3반_과제",
                    "exec_status": "pending",
                },
                {
                    "remote_id": "mob_task_2",
                    "title": "가정통신문 회신서 OCR",
                    "task_type": "ocr_batch",
                    "is_mobile_task": True,
                    "scheduled_time": "오늘 16:00",
                    "target_info": "파일 28장",
                    "exec_status": "pending",
                },
            ],
            "notifications": [
                {
                    "remote_id": "noti_remote_1",
                    "channel": "google_sheets",
                    "sender": "모바일 앱",
                    "title": "학부모 상담 신청 도착",
                    "message": "2학년 3반 김철수 학부모 신청",
                    "is_read": 0,
                }
            ],
        }

        res1 = svc.sync_from_payload(payload)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["schedules_added"], 1)
        self.assertEqual(res1["todos_added"], 1)
        self.assertEqual(res1["mobile_tasks_added"], 2)
        self.assertEqual(res1["notifications_added"], 1)

        # 미확인 카운트: schedule(1) + todo(1) + mobile_task(2) = 4, notification = 1
        self.assertEqual(self.db.get_calendar_todo_unread_count(), 4)
        self.assertEqual(self.db.get_notification_unread_count(), 1)
        self.assertEqual(len(self.db.get_mobile_tasks(pending_only=True)), 2)

        # mob_task_1 확인 완료(acknowledged), mob_task_2 실행 완료(done)
        all_mob = {m["remote_id"]: m["id"] for m in self.db.get_mobile_tasks(pending_only=False)}
        self.db.acknowledge_mobile_task(all_mob["mob_task_1"])
        self.db.complete_mobile_task(all_mob["mob_task_2"])
        self.assertEqual(len(self.db.get_mobile_tasks(pending_only=True)), 0)

        # 다음 부팅/동기화 시 시트에서 여전히 pending으로 내려와도 중복 팝업(pending)으로 부활하지 않아야 함
        res2 = svc.sync_from_payload(payload)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["mobile_tasks_added"], 0)
        self.assertEqual(len(self.db.get_mobile_tasks(pending_only=True)), 0)

        mob_after = {m["remote_id"]: m for m in self.db.get_mobile_tasks(pending_only=False)}
        self.assertEqual(mob_after["mob_task_1"]["exec_status"], "acknowledged")
        self.assertEqual(mob_after["mob_task_2"]["exec_status"], "done")


class TestSidebarUnreadBadgesAndMainWindow(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _get_or_create_qapp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_ui.db"
        self.db = DatabaseManager(db_rel_path=str(self.db_path))

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_sidebar_order_folder_tray_and_unread_dot_rules(self) -> None:
        # 미확인 외부 알림 1건 + 모바일 예약 작업 1건 준비
        noti_id, _ = self.db.add_notification(
            title="구글 시트 신규 응답",
            message="2학년 1반 설문 도착",
            channel="google_sheets",
            remote_id="ui_noti_1",
            is_read=0,
        )
        mob_id, _ = self.db.upsert_remote_mobile_task(
            remote_id="ui_mob_1",
            title="수행평가 파일 분류 예약",
            task_type="file_sort",
        )

        prev_db_inst = DatabaseManager._instance
        prev_cfg_inst = ConfigManager._instance
        DatabaseManager._instance = self.db
        ConfigManager._instance = None
        tmp_cfg = ConfigManager()
        tmp_cfg.config_path = str(Path(self.tmp_dir.name) / "test_config.json")
        tmp_cfg.save()
        win = None
        try:
            win = MainWindow(db=self.db, config=tmp_cfg, enable_startup_sync=False)
            # 1. 메뉴 순서 검증: 외부 연동·알림 센터 -> 캘린더 & 할일 -> 환경설정 & 명렬표
            titles = [win.sidebar_nav.item(i).text() for i in range(win.sidebar_nav.count())]
            notify_idx = titles.index("외부 연동·알림 센터")
            cal_idx = titles.index("캘린더 & 할일")
            settings_idx = titles.index("환경설정 & 명렬표")
            self.assertEqual(cal_idx, notify_idx + 1)
            self.assertEqual(settings_idx, cal_idx + 1)

            # 2. 앱/트레이 E 아이콘, 브랜딩 배지/간격, 하단 유틸리티 버튼(36px 통일, 단색 SVG) 및 접힘/펼침 검증
            self.assertFalse(win.windowIcon().isNull())
            self.assertFalse(win.tray_icon.icon().isNull())
            self.assertEqual(win.brand_logo_badge.size(), QSize(32, 32))
            self.assertFalse(win.brand_logo_badge.icon().isNull())
            self.assertFalse(hasattr(win, "btn_sidebar_toggle"))
            self.assertEqual(win.brand_text_layout.spacing(), 1)
            self.assertEqual(win.brand_text_layout.contentsMargins().top(), 0)
            self.assertEqual(win.brand_text_layout.contentsMargins().bottom(), 0)

            self.assertEqual(win.btn_open_folder.text(), "EduFlow 폴더 열기")
            self.assertNotIn("📁", win.btn_open_folder.text())
            self.assertFalse(win.btn_open_folder.icon().isNull())
            self.assertEqual(win.btn_hide_tray.text(), "")
            self.assertFalse(win.btn_hide_tray.icon().isNull())
            self.assertEqual(win.btn_open_folder.height(), 36)
            self.assertEqual(win.btn_hide_tray.size(), QSize(36, 36))
            self.assertEqual(
                win.btn_hide_tray.toolTip(),
                "시스템 트레이로 최소화 (백그라운드 알림 유지)",
            )
            win.toggle_sidebar()
            self.assertTrue(win._sidebar_collapsed)
            self.assertFalse(win.brand_logo_badge.isHidden())
            self.assertTrue(win.brand_text_container.isHidden())
            self.assertFalse(win.sidebar_ai_badge.isHidden())
            self.assertFalse(win.ai_status_dot.isHidden())
            self.assertTrue(win.lbl_ai_badge.isHidden())
            self.assertEqual(win.sidebar_ai_badge.size(), QSize(36, 36))
            self.assertTrue(bool(win.sidebar_ai_badge.toolTip()))
            self.assertEqual(win.btn_open_folder.text(), "")
            self.assertFalse(win.btn_open_folder.icon().isNull())
            self.assertEqual(win.btn_hide_tray.text(), "")
            self.assertEqual(win.btn_open_folder.size(), QSize(36, 36))
            self.assertEqual(win.btn_hide_tray.size(), QSize(36, 36))
            win.toggle_sidebar()
            self.assertFalse(win._sidebar_collapsed)
            self.assertFalse(win.brand_text_container.isHidden())
            self.assertFalse(win.lbl_ai_badge.isHidden())
            self.assertEqual(win.sidebar_ai_badge.height(), 36)
            self.assertEqual(win.btn_open_folder.text(), "EduFlow 폴더 열기")

            # 2-0a. 사이드바 AI 상태 배지 배치·3가지 모드 점 색상·모델 표시·클릭 토글 검증
            self.assertTrue(win.sidebar_frame.isAncestorOf(win.sidebar_ai_badge))
            self.assertFalse(win.workspace_frame.isAncestorOf(win.sidebar_ai_badge))
            self.assertEqual(
                win.sidebar_layout.indexOf(win.sidebar_ai_badge) + 1,
                win.sidebar_layout.indexOf(win.sidebar_util_container),
            )
            win.ai_dock.set_mode_index(1)
            self.assertEqual(win._ai_status_dot_color, "#3B82F6")
            self.assertIn("수동 복붙", win.lbl_ai_badge.text())
            self.assertIn("Gemini", win.lbl_ai_badge.text())
            self.assertIn("Gemini", win.sidebar_ai_badge.toolTip())

            win.ai_dock.set_mode_index(0)
            self.assertEqual(win._ai_status_dot_color, "#22C55E")
            self.assertIn("API", win.sidebar_ai_badge.toolTip())
            self.assertTrue(len(win.lbl_ai_badge.text().strip()) > 4)

            win.ai_dock.set_mode_index(2)
            win.ai_dock._on_pw_provider_chip_clicked("claude")
            self.assertIn(win._ai_status_dot_color, ("#38BDF8", "#A855F7"))
            self.assertIn("Playwright", win.lbl_ai_badge.text())
            self.assertIn("Claude", win.lbl_ai_badge.text())
            self.assertIn("Claude", win.sidebar_ai_badge.toolTip())

            # 2-0b. 좌측 메인 사이드바 <-> 우측 AI 실행 패널 완전 독립 동작 & 동시 열림 검증
            from PyQt6.QtWidgets import QScrollArea

            self.assertFalse(win._sidebar_collapsed)
            self.assertFalse(win._ai_dock_expanded)
            win.sidebar_ai_badge.click()
            self.assertTrue(win._ai_dock_expanded)
            self.assertFalse(win._sidebar_collapsed)
            win.sidebar_ai_badge.click()
            self.assertFalse(win._ai_dock_expanded)
            self.assertFalse(win._sidebar_collapsed)

            win.btn_toggle_ai_dock.click()
            self.assertTrue(win._ai_dock_expanded)
            self.assertFalse(win._sidebar_collapsed)
            win.brand_logo_badge.click()
            self.assertTrue(win._sidebar_collapsed)
            self.assertTrue(win._ai_dock_expanded)
            win.btn_toggle_ai_dock.click()
            self.assertFalse(win._ai_dock_expanded)
            self.assertTrue(win._sidebar_collapsed)
            win.brand_logo_badge.click()
            self.assertFalse(win._sidebar_collapsed)
            self.assertFalse(win._ai_dock_expanded)

            self.assertGreaterEqual(win.workspace_frame.minimumWidth(), 420)
            self.assertIsInstance(getattr(win.tab_calendar, "main_scroll", None), QScrollArea)
            self.assertIsInstance(win.tab_doc_ocr.sub_tabs.widget(3), QScrollArea)

            win.ai_dock._on_pw_provider_chip_clicked("gemini")
            win.ai_dock.set_mode_index(1)

            # 2-0. 캘린더&할일 상단 퀵 액션 버튼 & 우측 패널 Lucide 아이콘/레이블 통일 검증
            cal = win.tab_calendar
            self.assertEqual(cal.btn_bulk_import.text(), "학사일정 일괄 등록 (.xlsx)")
            self.assertFalse(cal.btn_bulk_import.icon().isNull())
            self.assertFalse(cal.btn_sync_sheets.icon().isNull())
            self.assertEqual(cal.btn_new_item.text(), "새 일정/할일")
            self.assertFalse(cal.btn_new_item.icon().isNull())
            self.assertEqual(cal.btn_refresh_panel.text(), "")
            self.assertEqual(cal.btn_refresh_panel.size(), QSize(28, 28))
            self.assertFalse(cal.btn_refresh_panel.icon().isNull())
            self.assertEqual(cal.lbl_mob_sec.text(), "대기 중인 원격 트리거")
            self.assertEqual(cal.lbl_mob_hint.text(), "자동 백그라운드 대기")
            self.assertEqual(cal.lbl_todo_sec.text(), "오늘의 교무/학급 할일")
            self.assertIsNotNone(cal.lbl_banner_icon.pixmap())
            self.assertFalse(cal.lbl_banner_icon.pixmap().isNull())

            expected_workspace = str(get_active_workspace_dir(tmp_cfg))
            with patch("os.startfile", create=True) as mock_startfile:
                opened_dir = win.open_eduflow_folder()
                self.assertEqual(opened_dir, expected_workspace)
                self.assertEqual(win.tab_doc_ocr._get_default_save_path(), expected_workspace)
                self.assertTrue(Path(opened_dir).exists())
                mock_startfile.assert_called_once_with(opened_dir)

            # 2-1. 명렬표 테이블 헤더 전체 너비 채움 설정 검증
            self.assertTrue(win.tab_settings.table_homeroom.horizontalHeader().stretchLastSection())
            self.assertTrue(win.tab_settings.table_subject.horizontalHeader().stretchLastSection())
            self.assertTrue(win.tab_settings.table_club.horizontalHeader().stretchLastSection())

            # 3. 초기 빨간 점 상태 (둘 다 True)
            self.assertTrue(
                bool(win.sidebar_nav.item(notify_idx).data(SidebarNavDelegate.UNREAD_ROLE))
            )
            self.assertTrue(
                bool(win.sidebar_nav.item(cal_idx).data(SidebarNavDelegate.UNREAD_ROLE))
            )

            # 4. 탭을 열기만 해서는 빨간 점이 사라지지 않아야 함
            win.sidebar_nav.setCurrentRow(notify_idx)
            win.sidebar_nav.setCurrentRow(cal_idx)
            win.sidebar_nav.setCurrentRow(0)
            self.app.processEvents()
            self.assertGreater(self.db.get_notification_unread_count(), 0)
            self.assertGreater(self.db.get_calendar_todo_unread_count(), 0)
            self.assertTrue(
                bool(win.sidebar_nav.item(notify_idx).data(SidebarNavDelegate.UNREAD_ROLE))
            )
            self.assertTrue(
                bool(win.sidebar_nav.item(cal_idx).data(SidebarNavDelegate.UNREAD_ROLE))
            )

            # 5. 개별 알림 읽음 클릭 -> 알림 센터 빨간 점만 해제, 캘린더&할일은 유지
            win.tab_notify.mark_notification_read(noti_id)
            self.assertEqual(self.db.get_notification_unread_count(), 0)
            self.assertFalse(
                bool(win.sidebar_nav.item(notify_idx).data(SidebarNavDelegate.UNREAD_ROLE))
            )
            self.assertTrue(
                bool(win.sidebar_nav.item(cal_idx).data(SidebarNavDelegate.UNREAD_ROLE))
            )

            # 6. [모두 읽음] 클릭 -> 캘린더&할일 빨간 점은 해제되지만 모바일 작업 exec_status='pending'은 유지
            win.tab_calendar.mark_all_read()
            self.assertEqual(self.db.get_calendar_todo_unread_count(), 0)
            self.assertFalse(
                bool(win.sidebar_nav.item(cal_idx).data(SidebarNavDelegate.UNREAD_ROLE))
            )
            pending_tasks = self.db.get_mobile_tasks(pending_only=True)
            self.assertEqual(len(pending_tasks), 1)
            self.assertEqual(pending_tasks[0]["id"], mob_id)

            # 7. [확인 완료] 처리 시 pending 목록에서도 제거
            win.tab_calendar.acknowledge_mobile_task(mob_id)
            self.assertEqual(len(self.db.get_mobile_tasks(pending_only=True)), 0)

            # 8. theme.qss QSS 파싱 오류 없음 + 오프스크린 사이드바/캘린더 렌더링 검증
            from ui.utils import load_svg_pixmap

            for icon_name in (
                "eduflow_app",
                "graduation-cap",
                "folder",
                "chevron-down",
                "file-spreadsheet",
                "refresh-cw",
                "plus",
                "rotate-cw",
                "zap",
                "check-square",
                "smartphone",
            ):
                svg_pix = load_svg_pixmap(
                    icon_name,
                    color_hex=None if icon_name == "eduflow_app" else "#2563EB",
                    size=18,
                )
                self.assertFalse(svg_pix.isNull(), f"SVG 렌더링 실패: {icon_name}")

            qss_path = Path(__file__).resolve().parent.parent / "ui" / "styles" / "theme.qss"
            qss_text = qss_path.read_text(encoding="utf-8")
            qt_warnings = []
            prev_handler = qInstallMessageHandler(
                lambda _mode, _ctx, msg: qt_warnings.append(str(msg))
            )
            prev_sheet = self.app.styleSheet()
            try:
                self.app.setStyleSheet(qss_text)
                win.sidebar_nav.setCurrentRow(cal_idx)
                win.show()
                self.app.processEvents()
                last_cell = win.tab_calendar.month_grid_layout.itemAtPosition(5, 0).widget()
                self.assertIsNotNone(last_cell)
                self.assertGreaterEqual(
                    win.tab_calendar.month_grid_host.height(),
                    last_cell.y() + last_cell.height(),
                )
                win_pix = win.grab()
                cal_pix = win.tab_calendar.grab()
                self.assertFalse(win_pix.isNull())
                self.assertFalse(cal_pix.isNull())
                self.assertGreater(win_pix.width(), 100)
                self.assertGreater(cal_pix.width(), 100)
            finally:
                self.app.setStyleSheet(prev_sheet)
                qInstallMessageHandler(prev_handler)

            qss_errors = [
                m
                for m in qt_warnings
                if "stylesheet" in m.lower()
                or "could not parse" in m.lower()
                or "svg" in m.lower()
            ]
            self.assertEqual(qss_errors, [], f"QSS/SVG 경고 발생: {qss_errors}")
        finally:
            if win is not None:
                if hasattr(win.tab_calendar, "reminder_timer"):
                    win.tab_calendar.reminder_timer.stop()
                win._force_quit = True
                win.close()
                win.deleteLater()
                self.app.processEvents()
            DatabaseManager._instance = prev_db_inst
            ConfigManager._instance = prev_cfg_inst

    def test_xlsx_export_and_reimport_roundtrip(self) -> None:
        schedules = [
            {
                "title": "2학년 수학 수행평가",
                "category": "수행평가",
                "start_date": "2026-11-18",
                "end_date": "2026-11-18",
                "start_time": "09:30",
                "reminder_type": "1h",
                "memo": "3반 교실",
            },
            {
                "title": "교직원 전체 연수",
                "category": "업무",
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "start_time": "",
                "reminder_type": "morning_9am",
                "memo": "시청각실",
            },
        ]
        out_xlsx = Path(self.tmp_dir.name) / "exported_schedules.xlsx"
        count = export_schedules_to_xlsx(schedules, out_xlsx, include_sample_when_empty=True)
        self.assertEqual(count, 2)
        self.assertTrue(out_xlsx.exists())

        wb = openpyxl.load_workbook(out_xlsx)
        try:
            ws = wb["학사일정"]
            headers = [ws.cell(row=1, column=i).value for i in range(1, 9)]
            self.assertEqual(
                headers,
                ["시작일", "종료일", "일정명", "카테고리", "시작시간", "종일여부", "알림", "메모"],
            )
            self.assertEqual(ws.freeze_panes, "A2")
        finally:
            wb.close()

        reparsed = parse_xlsx_schedules(str(out_xlsx), default_year=2026)
        self.assertEqual(len(reparsed), 2)
        self.assertEqual(reparsed[0]["title"], "2학년 수학 수행평가")
        self.assertEqual(reparsed[0]["start_date"], "2026-11-18")
        self.assertEqual(reparsed[0]["start_time"], "09:30")
        self.assertEqual(reparsed[0]["category"], "수행평가")
        self.assertEqual(reparsed[0]["reminder_type"], "1h")
        self.assertEqual(reparsed[1]["start_date"], "2026-11-20")
        self.assertEqual(reparsed[1]["end_date"], "2026-11-21")
        self.assertEqual(reparsed[1]["start_time"], "")
        self.assertEqual(reparsed[1]["reminder_type"], "morning_9am")

        # 빈 양식 내보내기: 예시 행 + 입력안내 시트 확인 및 재파싱 가능 검증
        tpl_xlsx = Path(self.tmp_dir.name) / "empty_template.xlsx"
        tpl_count = export_schedules_to_xlsx([], tpl_xlsx, include_sample_when_empty=True)
        self.assertEqual(tpl_count, 0)
        wb_tpl = openpyxl.load_workbook(tpl_xlsx, read_only=True)
        try:
            self.assertIn("입력안내", wb_tpl.sheetnames)
        finally:
            wb_tpl.close()
        tpl_parsed = parse_xlsx_schedules(str(tpl_xlsx))
        self.assertEqual(len(tpl_parsed), 2)

    def test_calendar_ai_prompt_and_response_parsing(self) -> None:
        from datetime import date

        ref_today = date(2026, 11, 15)  # 일요일
        sys_p, usr_p = build_calendar_ai_prompt("내일 교무회의", today=ref_today)
        self.assertIn("2026-11-15", sys_p)
        self.assertIn("학사일정, 수행평가, 업무, 개인", sys_p)
        self.assertIn("높음, 보통, 낮음", sys_p)
        self.assertIn("내일 교무회의", usr_p)

        # 1) 마크다운 ```json 블록 + 상대 날짜 + 누락/잘못된 필드 폴백
        md_resp = """안내문에서 추출한 결과입니다.
```json
{
  "summary": "교무회의 및 수행평가 채점",
  "schedules": [
    {
      "title": "교직원 회의",
      "start_date": "내일",
      "start_time": "15시 30분",
      "all_day": false,
      "category": "잘못된카테고리",
      "reminder": "1시간 전"
    },
    {
      "title": "수학 수행평가",
      "start_date": "잘못된날짜",
      "all_day": true,
      "category": "수행평가"
    }
  ],
  "todos": [
    {
      "title": "가정통신문 발송",
      "due_date": "다음 주 월요일",
      "priority": "높음",
      "memo": "1학년 전체"
    },
    {
      "title": "자료 정리"
    }
  ]
}
```
"""
        parsed = parse_calendar_ai_response(md_resp, today=ref_today)
        self.assertEqual(parsed["summary"], "교무회의 및 수행평가 채점")
        self.assertEqual(len(parsed["schedules"]), 2)
        self.assertEqual(parsed["schedules"][0]["start_date"], "2026-11-16")
        self.assertEqual(parsed["schedules"][0]["start_time"], "15:30")
        self.assertEqual(parsed["schedules"][0]["category"], "업무")  # 회의 -> 업무 추론
        self.assertEqual(parsed["schedules"][0]["reminder_type"], "1h")
        self.assertEqual(parsed["schedules"][1]["start_date"], "2026-11-15")  # 잘못된 날짜 -> 오늘 폴백
        self.assertEqual(parsed["schedules"][1]["start_time"], "")
        self.assertEqual(parsed["schedules"][1]["category"], "수행평가")

        self.assertEqual(len(parsed["todos"]), 2)
        self.assertEqual(parsed["todos"][0]["due_date"], "2026-11-16")  # 다음 주 월요일
        self.assertEqual(parsed["todos"][0]["priority"], "high")
        self.assertEqual(parsed["todos"][1]["due_date"], "")
        self.assertEqual(parsed["todos"][1]["priority"], "normal")

        # 2) 일반 JSON 문자열 파싱
        plain_json = '{"summary": "단일", "schedules": [{"title": "개교기념일", "start_date": "2026-11-20"}], "todos": []}'
        parsed_plain = parse_calendar_ai_response(plain_json, today=ref_today)
        self.assertEqual(len(parsed_plain["schedules"]), 1)
        self.assertEqual(parsed_plain["schedules"][0]["category"], "학사일정")

    def test_preview_dialog_with_todos_and_dock_target_auto_switch(self) -> None:
        app = _get_or_create_qapp()
        dlg = ScheduleImportPreviewDialog(
            parsed_items=[
                {
                    "title": "과학 실험",
                    "start_date": "2026-11-20",
                    "end_date": "2026-11-20",
                    "start_time": "10:00",
                    "category": "수행평가",
                    "reminder_type": "1h",
                    "memo": "실험실",
                }
            ],
            file_name="AI 자동 추출",
            parsed_todos=[
                {
                    "title": "실험 재료 준비",
                    "due_date": "2026-11-19",
                    "priority": "high",
                    "memo": "비커 10개",
                }
            ],
            summary="과학 실험 관련 일정 및 준비물",
        )
        sel_s = dlg.get_selected_schedules()
        sel_t = dlg.get_selected_todos()
        self.assertEqual(len(sel_s), 1)
        self.assertEqual(sel_s[0]["reminder_type"], "1h")
        self.assertEqual(len(sel_t), 1)
        self.assertEqual(sel_t[0]["priority"], "high")
        dlg.deleteLater()
        app.processEvents()

    def test_duplicate_schedule_prevention(self) -> None:
        id1 = self.db.add_schedule(
            title="교무회의",
            category="업무",
            start_date="2026-11-25",
            end_date="2026-11-25",
            start_time="15:00",
            memo="본관",
        )
        self.assertGreater(id1, 0)
        # 동일 (title, start_date, end_date, start_time) 재등록 차단
        id2 = self.db.add_schedule(
            title="교무회의",
            category="학사일정",
            start_date="2026-11-25",
            end_date="2026-11-25",
            start_time="15:00",
            memo="다른 메모",
        )
        self.assertEqual(id2, 0)

        # bulk_add_schedules에서도 기존 일정 + 배치 내 중복 모두 차단
        added = self.db.bulk_add_schedules(
            [
                {
                    "title": "교무회의",
                    "category": "업무",
                    "start_date": "2026-11-25",
                    "end_date": "2026-11-25",
                    "start_time": "15:00",
                },
                {
                    "title": "신규 연수",
                    "category": "업무",
                    "start_date": "2026-11-26",
                    "end_date": "2026-11-26",
                    "start_time": "",
                },
                {
                    "title": "신규 연수",
                    "category": "업무",
                    "start_date": "2026-11-26",
                    "end_date": "2026-11-26",
                    "start_time": "",
                },
            ]
        )
        self.assertEqual(added, 1)
        all_sch = self.db.get_schedules(start_date="2026-11-25", end_date="2026-11-26")
        self.assertEqual(len(all_sch), 2)

    def test_ai_dock_target_ui_isolation_and_calendar_modes(self) -> None:
        from core.base_worker import TaskRunner
        from ui.components.ai_execution_dock import AiExecutionDock

        dock = AiExecutionDock(task_runner=TaskRunner())
        try:
            dock.register_target(
                target_id="agg_step2",
                label="과제 파이프라인 > 2단계 세특 생성",
                context_getter=lambda: {"target_id": "agg_step2", "kind": "excel_batch"},
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="ocr",
                label="2. AI OCR 및 텍스트 보정",
                context_getter=lambda: {"target_id": "ocr", "kind": "ocr", "ocr_files": []},
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="hwpx_agent",
                label="4. HWPX AI 문서 에이전트",
                context_getter=lambda: {"target_id": "hwpx_agent", "kind": "hwpx_agent"},
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="calendar_todo",
                label="캘린더 & 할일 (일정·할일 자동 추출)",
                context_getter=lambda: {"target_id": "calendar_todo", "kind": "calendar_todo"},
                on_result=lambda _r: None,
            )

            # 1) agg_step2에서는 작업 시작 버튼 라벨 & btn_dock_open_excel 제거 확인
            dock.set_target("agg_step2", force=True)
            self.assertFalse(hasattr(dock, "btn_dock_open_excel"))
            self.assertEqual(dock.btn_chat_send.text(), "작업 시작")
            self.assertTrue(dock.body_scroll.isHidden())

            # 2) ocr / hwpx_agent에서는 배치 바 숨김
            for tid in ("ocr", "hwpx_agent"):
                dock.set_target(tid, force=True)
                self.assertTrue(dock.batch_bar_widget.isHidden(), f"{tid}에서 배치 바가 보임")

            # 3) calendar_todo에서는 3개 모드 모두 body_scroll 숨김, combo_pw_exec_mode는 항상 선택 가능
            dock.set_target("calendar_todo", force=True)
            for mode_idx in (0, 1, 2):
                dock.set_mode_index(mode_idx)
                self.assertTrue(dock.body_scroll.isHidden())
                self.assertTrue(dock.batch_bar_widget.isHidden())
                self.assertFalse(dock.combo_pw_exec_mode.isHidden())
                self.assertEqual(dock.btn_chat_send.text(), "전송")
        finally:
            dock.deleteLater()
            self.app.processEvents()

    def test_ai_dock_combo_transition_sequence_visibility(self) -> None:
        from core.base_worker import TaskRunner
        from ui.components.ai_execution_dock import AiExecutionDock

        dock = AiExecutionDock(task_runner=TaskRunner())
        try:
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            dock.register_target(
                target_id="agg_step2",
                label="과제 파이프라인 > 2단계 세특 생성",
                context_getter=lambda: {"target_id": "agg_step2", "kind": "excel_batch"},
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="ocr",
                label="2. AI OCR 및 텍스트 보정",
                context_getter=lambda: {"target_id": "ocr", "kind": "ocr", "ocr_files": []},
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="hwpx_agent",
                label="4. HWPX AI 문서 에이전트",
                context_getter=lambda: {"target_id": "hwpx_agent", "kind": "hwpx_agent"},
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="calendar_todo",
                label="캘린더 & 할일 (일정·할일 자동 추출)",
                context_getter=lambda: {"target_id": "calendar_todo", "kind": "calendar_todo"},
                on_result=lambda _r: None,
            )

            dock.show()
            self.app.processEvents()

            # 실제 콤보박스 전환 시퀀스: agg_step2 -> ocr -> hwpx_agent -> calendar_todo
            for tid in ("agg_step2", "ocr", "hwpx_agent", "calendar_todo"):
                idx = dock.combo_target_selector.findData(tid)
                self.assertGreaterEqual(idx, 0)
                dock.combo_target_selector.setCurrentIndex(idx)
                self.app.processEvents()
                self.assertEqual(dock.active_target_id(), tid)

            # 최종 calendar_todo 상태에서 3개 모드 모두 body_scroll 및 레거시 위젯 숨김 확인
            for mode_idx in (0, 1, 2):
                dock.set_mode_index(mode_idx)
                self.app.processEvents()
                self.assertFalse(dock.body_scroll.isVisible())
                self.assertFalse(dock.hwpx_instruction_card.isVisible())
                self.assertFalse(dock.api_summary_card.isVisible())
                self.assertFalse(dock.chk_api_run_integrated.isVisible())
                self.assertFalse(dock.manual_batch_card.isVisible())
                self.assertFalse(dock.manual_ocr_card.isVisible())
                self.assertFalse(dock.combo_pw_exec_mode.isHidden())
        finally:
            dock.close()
            dock.deleteLater()
            self.app.processEvents()


class TestGoogleCalendarPushAndFirebaseCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(db_rel_path=str(Path(self.tmp_dir.name) / "test.db"))
        self.cfg = ConfigManager()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_google_calendar_push_insert_patch_delete_and_exclude_personal(self) -> None:
        from services.google_calendar_service import GoogleCalendarService

        # 일정 2건 등록 (학사일정 1, 개인 1)
        sch_id = self.db.add_schedule(
            title="개교기념일",
            category="학사일정",
            start_date="2026-05-01",
            end_date="2026-05-01",
            memo="휴업일",
        )
        self.db.add_schedule(
            title="개인 병원 예약",
            category="개인",
            start_date="2026-05-02",
            end_date="2026-05-02",
        )

        class _FakeExec:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class _FakeEventsApi:
            def __init__(self):
                self.inserted = []
                self.patched = []
                self.deleted = []

            def insert(self, calendarId, body):
                self.inserted.append((calendarId, body))
                return _FakeExec({"id": f"gcal_{len(self.inserted)}"})

            def patch(self, calendarId, eventId, body):
                self.patched.append((calendarId, eventId, body))
                return _FakeExec({"id": eventId})

            def delete(self, calendarId, eventId):
                self.deleted.append((calendarId, eventId))
                return _FakeExec({})

        events_api = _FakeEventsApi()

        class _FakeGcalService:
            def events(self):
                return events_api

        svc = GoogleCalendarService(db=self.db, config=self.cfg)
        # 1) 개인 일정 제외하고 푸시 -> insert 1건, skipped 1건
        res1 = svc.push_schedules(exclude_personal=True, service_override=_FakeGcalService())
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["inserted"], 1)
        self.assertEqual(res1["skipped"], 1)
        self.assertEqual(events_api.inserted[0][1]["summary"], "개교기념일")
        self.assertEqual(events_api.inserted[0][1]["end"]["date"], "2026-05-02")  # exclusive end date

        # 2) 로컬 일정 수정 후 재푸시 -> patch 1건
        self.db.update_schedule(
            sch_id,
            title="개교기념일(변경)",
            category="학사일정",
            start_date="2026-05-01",
            end_date="2026-05-01",
            memo="휴업일 확정",
        )
        # gcal_updated_at보다 updated_at이 같거나 큰 경우 보장을 위해 과거 시각으로 세팅
        self.db.update_schedule_gcal_meta(sch_id, "gcal_1", "2000-01-01 00:00:00")
        res2 = svc.push_schedules(exclude_personal=True, service_override=_FakeGcalService())
        self.assertEqual(res2["patched"], 1)
        self.assertEqual(events_api.patched[0][1], "gcal_1")

        # 3) 로컬 일정 삭제 후 재푸시 -> delete 1건
        self.db.delete_schedule(sch_id)
        res3 = svc.push_schedules(exclude_personal=True, service_override=_FakeGcalService())
        self.assertEqual(res3["deleted"], 1)
        self.assertEqual(events_api.deleted[0][1], "gcal_1")
        self.assertEqual(self.db.get_pending_gcal_deletions(), [])

    def test_calendar_tab_firebase_button_and_gcal_export_worker(self) -> None:
        from PyQt6.QtWidgets import QApplication
        from core.base_worker import TaskRunner
        from ui.tabs.tab_calendar_todo import CalendarTodoTab

        app = QApplication.instance() or QApplication(sys.argv[:1])
        runner = TaskRunner()
        cal = CalendarTodoTab(
            task_runner=runner,
            db=self.db,
            config=self.cfg,
            start_timer=False,
        )
        try:
            self.assertEqual(cal.btn_sync_sheets.text(), "Firebase 동기화")
            self.assertEqual(cal.btn_export_gcal.text(), "Google 캘린더")
            gcal_menu = cal.btn_export_gcal.menu()
            self.assertIsNotNone(gcal_menu)
            action_texts = [a.text() for a in gcal_menu.actions()]
            self.assertEqual(
                action_texts,
                [
                    "Google 캘린더에서 가져오기 (연결된 계정 → EduFlow)",
                    "",
                    "Google 캘린더로 내보내기 (개인 계정)",
                    "Google 캘린더로 내보내기 (Workspace 계정)",
                ],
            )
            self.assertEqual(cal.btn_sync_gtasks.text(), "Google Tasks 동기화")
            self.assertIsNotNone(cal.btn_sync_gtasks.menu())
            self.assertIn(
                cal.lbl_sync_status_pill.text(),
                ("Firebase 클라우드 실시간 연동 활성화", "로컬 캘린더 모드"),
            )
        finally:
            cal.close()
            cal.deleteLater()
            app.processEvents()

    def test_google_calendar_pull_schedules_and_dedup(self) -> None:
        from services.google_calendar_service import GoogleCalendarService

        # 기존 학사일정에 gcal_event_id 연결
        existing_id = self.db.add_schedule(
            title="기존 학사일정",
            category="학사일정",
            start_date="2026-06-10",
            end_date="2026-06-10",
            memo="초기 메모",
        )
        self.db.update_schedule_gcal_meta(existing_id, "gcal_existing", "2026-06-01 09:00:00")

        class _FakeExec:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class _FakeListApi:
            def __init__(self, items):
                self._items = items
                self.last_kwargs = None

            def list(self, **kwargs):
                self.last_kwargs = kwargs
                return _FakeExec({"items": self._items})

        items = [
            {
                "id": "gcal_existing",
                "status": "confirmed",
                "summary": "기존 학사일정(구글에서 수정)",
                "description": "수정된 메모",
                "start": {"date": "2026-06-11"},
                "end": {"date": "2026-06-12"},
            },
            {
                "id": "gcal_new_1",
                "status": "confirmed",
                "summary": "개인 치과 예약",
                "description": "오후 방문",
                "start": {"date": "2026-06-01"},
                "end": {"date": "2026-06-03"},
            },
            {
                "id": "gcal_cancelled",
                "status": "cancelled",
                "summary": "취소된 일정",
                "start": {"date": "2026-06-05"},
                "end": {"date": "2026-06-06"},
            },
        ]
        list_api = _FakeListApi(items)

        class _FakeGcalService:
            def events(self):
                return list_api

        svc = GoogleCalendarService(db=self.db, config=self.cfg)
        res1 = svc.pull_schedules(service_override=_FakeGcalService())
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["added"], 1)
        self.assertEqual(res1["updated"], 1)
        self.assertEqual(res1["skipped"], 1)
        self.assertTrue(list_api.last_kwargs.get("singleEvents"))
        self.assertEqual(list_api.last_kwargs.get("orderBy"), "startTime")

        all_sch = {s["gcal_event_id"]: s for s in self.db.get_schedules() if s.get("gcal_event_id")}
        # 기존 일정은 학사일정 카테고리 유지
        self.assertEqual(all_sch["gcal_existing"]["category"], "학사일정")
        self.assertEqual(all_sch["gcal_existing"]["title"], "기존 학사일정(구글에서 수정)")
        self.assertEqual(all_sch["gcal_existing"]["start_date"], "2026-06-11")

        # 신규 일정은 학생·학부모 노출 방지를 위해 반드시 '개인' 카테고리 저장
        self.assertEqual(all_sch["gcal_new_1"]["category"], "개인")
        self.assertEqual(all_sch["gcal_new_1"]["start_date"], "2026-06-01")
        self.assertEqual(all_sch["gcal_new_1"]["end_date"], "2026-06-02")

        # 재실행 시 중복 생성되지 않고 갱신만 수행됨
        res2 = svc.pull_schedules(service_override=_FakeGcalService())
        self.assertEqual(res2["added"], 0)
        self.assertEqual(res2["updated"], 2)

    def test_google_tasks_pull_and_push_sync(self) -> None:
        from services.google_calendar_service import GoogleCalendarService

        class _FakeExec:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class _FakeTasksApi:
            def __init__(self):
                self.remote_items = [
                    {
                        "id": "gtask_1",
                        "title": "나이스 출결 마감",
                        "notes": "오후 4시 전",
                        "due": "2026-06-15T00:00:00.000Z",
                        "status": "needsAction",
                    },
                    {
                        "id": "gtask_2",
                        "title": "완료된 과제",
                        "status": "completed",
                    },
                ]
                self.inserted = []
                self.patched = []

            def list(self, **kwargs):
                return _FakeExec({"items": self.remote_items})

            def insert(self, tasklist, body):
                self.inserted.append((tasklist, body))
                return _FakeExec({"id": f"gtask_new_{len(self.inserted)}"})

            def patch(self, tasklist, task, body):
                self.patched.append((tasklist, task, body))
                return _FakeExec({"id": task})

        tasks_api = _FakeTasksApi()

        class _FakeTasksService:
            def tasks(self):
                return tasks_api

        svc = GoogleCalendarService(db=self.db, config=self.cfg)
        res_pull = svc.pull_tasks(account="workspace", service_override=_FakeTasksService())
        self.assertTrue(res_pull["ok"])
        self.assertEqual(res_pull["added"], 2)

        todos = {t["gtask_id"]: t for t in self.db.get_todos() if t.get("gtask_id")}
        self.assertEqual(todos["gtask_1"]["title"], "나이스 출결 마감")
        self.assertEqual(todos["gtask_1"]["due_date"], "2026-06-15")
        self.assertEqual(todos["gtask_1"]["gtask_account"], "workspace")
        self.assertEqual(todos["gtask_2"]["is_done"], 1)

        # 로컬 신규 할일 추가 후 push_tasks 검증
        self.db.add_todo(title="학부모 상담 안내", due_date="2026-06-20", memo="문자 발송")
        res_push = svc.push_tasks(account="workspace", service_override=_FakeTasksService())
        self.assertTrue(res_push["ok"])
        self.assertEqual(res_push["inserted"], 1)
        self.assertEqual(tasks_api.inserted[0][1]["title"], "학부모 상담 안내")

    def test_upload_notice_attachment_uses_upload_file_in_direct_admin_fallback(self) -> None:
        from services.firestore_sync_service import FirestoreSyncService

        pdf_path = Path(self.tmp_dir.name) / "notice.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 sample")

        svc = FirestoreSyncService(db=self.db, config=self.cfg)
        # Direct Admin 모드(is_vercel_api_mode() == False)이고 GAS URL이 비어 있는 상황 시뮬레이션
        svc.is_vercel_api_mode = lambda: False
        svc.get_vercel_api_url = lambda: "https://m.eduflow.kro.kr/api/firestore"
        self.cfg._config.setdefault("api_and_integrations", {})["google_apps_script_url"] = ""

        called = []

        def _fake_post(action, payload=None):
            called.append((action, payload))
            return {"ok": True, "downloadUrl": "https://drive.google.com/uc?id=abc", "fileId": "abc"}

        svc._post_vercel_api = _fake_post
        res = svc.upload_notice_attachment(str(pdf_path))
        self.assertTrue(res["ok"])
        self.assertEqual(res["attachment_url"], "https://drive.google.com/uc?id=abc")
        self.assertEqual(res["attachment_file_id"], "abc")
        self.assertEqual(called[0][0], "upload_file")
        self.assertEqual(called[0][1]["action"], "upload_file")

    def test_oauth_drive_upload_file_and_anyone_permission(self) -> None:
        from services.google_calendar_service import GoogleCalendarService

        pdf_path = Path(self.tmp_dir.name) / "notice.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 oauth")

        class _FakeExec:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class _FakeFilesApi:
            def __init__(self):
                self.created_bodies = []

            def list(self, **kwargs):
                return _FakeExec({"files": []})

            def create(self, **kwargs):
                body = kwargs.get("body") or {}
                self.created_bodies.append(body)
                if body.get("mimeType") == "application/vnd.google-apps.folder":
                    return _FakeExec({"id": "folder_1"})
                return _FakeExec(
                    {
                        "id": "oauth_file_1",
                        "webViewLink": "https://drive.google.com/file/d/oauth_file_1/view",
                        "webContentLink": "https://drive.google.com/uc?export=download&id=oauth_file_1",
                    }
                )

        class _FakePermApi:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return _FakeExec({"id": "perm_1"})

        files_api = _FakeFilesApi()
        perm_api = _FakePermApi()

        class _FakeDriveService:
            def files(self):
                return files_api

            def permissions(self):
                return perm_api

        svc = GoogleCalendarService(db=self.db, config=self.cfg)
        res = svc.upload_file_to_drive(
            str(pdf_path),
            account="personal",
            folder_name="EduFlow_가정통신문",
            service_override=_FakeDriveService(),
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["file_id"], "oauth_file_1")
        self.assertEqual(res["attachment_url"], "https://drive.google.com/file/d/oauth_file_1/view")
        self.assertEqual(perm_api.calls[0]["body"], {"role": "reader", "type": "anyone"})

    def test_upload_notice_attachment_handles_non_json_gas_and_oauth_priority(self) -> None:
        from unittest.mock import patch
        from services.firestore_sync_service import FirestoreSyncService
        from services.google_calendar_service import GoogleCalendarService

        pdf_path = Path(self.tmp_dir.name) / "notice2.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test")

        svc = FirestoreSyncService(db=self.db, config=self.cfg)
        self.cfg._config.setdefault("api_and_integrations", {})["google_apps_script_url"] = (
            "https://script.google.com/macros/s/fake/exec"
        )
        svc.get_vercel_api_url = lambda: ""

        class _HtmlResp:
            ok = False
            status_code = 502
            text = "<html><body>Bad Gateway</body></html>"

            def json(self):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        class _FakeSess:
            def post(self, *args, **kwargs):
                return _HtmlResp()

        svc._get_http_session = lambda: _FakeSess()

        # 1) OAuth 미연결 + GAS 비JSON 응답 시 예외 없이 안전한 실패 dict 반환
        with patch.object(GoogleCalendarService, "get_connected_accounts", return_value=[]):
            res_fail = svc.upload_notice_attachment(str(pdf_path))
        self.assertFalse(res_fail["ok"])
        self.assertIn("GAS 비JSON 응답 (HTTP 502)", res_fail["error"])

        # 2) OAuth 연결 시 GAS/Vercel보다 우선해 OAuth Drive 직접 업로드 사용
        with patch.object(
            GoogleCalendarService, "get_connected_accounts", return_value=["personal"]
        ), patch.object(
            GoogleCalendarService,
            "upload_file_to_drive",
            return_value={
                "ok": True,
                "attachment_url": "https://drive.google.com/file/d/oauth_99/view",
                "attachment_file_id": "oauth_99",
            },
        ) as mock_oauth:
            res_ok = svc.upload_notice_attachment(str(pdf_path))
        self.assertTrue(res_ok["ok"])
        self.assertEqual(res_ok["attachment_file_id"], "oauth_99")
        mock_oauth.assert_called_once()

    def test_oauth_connect_cancel_check_and_timeout_close_server(self) -> None:
        import time
        from services.google_calendar_service import GoogleCalendarService

        class _FakeFlow:
            def __init__(self):
                self.redirect_uri = ""
                self.credentials = None

            def authorization_url(self, **_kwargs):
                return "http://localhost/fake_auth", "state_1"

            def fetch_token(self, **_kwargs):
                raise AssertionError("취소/타임아웃 시 fetch_token이 호출되면 안 됩니다.")

        svc = GoogleCalendarService(db=self.db, config=self.cfg)

        # 1) cancel_check가 즉시 True를 반환하면 블로킹 없이 취소 반환
        t0 = time.monotonic()
        ok_cancel, msg_cancel = svc.connect(
            account="personal",
            cancel_check=lambda: True,
            timeout_sec=30,
            flow_override=_FakeFlow(),
            open_browser=False,
        )
        elapsed_cancel = time.monotonic() - t0
        self.assertFalse(ok_cancel)
        self.assertIn("취소", msg_cancel)
        self.assertLess(elapsed_cancel, 1.5)

        # 2) timeout_sec=1 설정 시 약 1초 내 타임아웃 반환 및 서버 종료
        t1 = time.monotonic()
        ok_to, msg_to = svc.connect(
            account="workspace",
            cancel_check=lambda: False,
            timeout_sec=1,
            flow_override=_FakeFlow(),
            open_browser=False,
        )
        elapsed_to = time.monotonic() - t1
        self.assertFalse(ok_to)
        self.assertIn("초과", msg_to)
        self.assertLess(elapsed_to, 3.0)

    def test_convert_hwp_to_pdf_com_mock_and_fallback(self) -> None:
        from unittest.mock import patch
        from services.notice_parser_service import convert_hwp_to_pdf

        hwpx_path = Path(self.tmp_dir.name) / "notice_sample.hwpx"
        hwpx_path.write_bytes(b"fake-hwpx")
        out_dir = Path(self.tmp_dir.name) / "pdf_out"

        class _FakeHwp:
            def __init__(self):
                self.quit_called = False
                self.registered = False
                self.saved_args = None

            def RegisterModule(self, name, val):
                self.registered = (name, val)

            def SetMessageBoxMode(self, mode):
                pass

            def Open(self, path, fmt, arg):
                return True

            def SaveAs(self, out_path, fmt):
                self.saved_args = (out_path, fmt)
                Path(out_path).write_bytes(b"%PDF-1.4 converted")
                return True

            def Quit(self):
                self.quit_called = True

        fake_hwp = _FakeHwp()

        class _FakeWin32:
            class client:
                @staticmethod
                def Dispatch(prog_id):
                    return fake_hwp

        class _FakePyCom:
            @staticmethod
            def CoInitialize():
                pass

            @staticmethod
            def CoUninitialize():
                pass

        # 1) COM 성공 시 PDF 변환 및 Quit 호출 확인
        with patch("services.multi_doc_extractor_service.HAS_WIN32COM", True), patch(
            "services.multi_doc_extractor_service.win32com", _FakeWin32
        ), patch("services.multi_doc_extractor_service.pythoncom", _FakePyCom):
            pdf_out = convert_hwp_to_pdf(hwpx_path, output_dir=out_dir)

        self.assertIsNotNone(pdf_out)
        self.assertTrue(pdf_out.is_file())
        self.assertEqual(pdf_out.suffix.lower(), ".pdf")
        self.assertTrue(fake_hwp.quit_called)
        self.assertEqual(fake_hwp.registered, ("FilePathCheckDLL", "FilePathCheckerModule"))
        self.assertEqual(fake_hwp.saved_args[1], "PDF")

        # 2) win32com 미설치/예외 시 예외 없이 None 반환 (원본 업로드 fallback 가능)
        with patch("services.multi_doc_extractor_service.HAS_WIN32COM", False):
            res_none = convert_hwp_to_pdf(hwpx_path, output_dir=out_dir)
        self.assertIsNone(res_none)


if __name__ == "__main__":
    unittest.main(verbosity=2)
