"""
EduFlow 전 탭 드래그앤 드롭(Drag & Drop) 헬퍼 및 통합 연결 단위 테스트.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import openpyxl
from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QLineEdit, QListWidget, QWidget

from core.base_worker import TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.hwpx_agent_service import create_minimal_hwpx_document
from ui.components.file_drop_helper import enable_file_drop
from ui.tabs.tab_calendar_todo import CalendarTodoTab
from ui.tabs.tab_classroom_tools import ClassroomToolsTab
from ui.tabs.tab_doc_ocr import DocOcrTab
from ui.tabs.tab_file_sorter import FileSorterTab
from ui.tabs.tab_grade_material import GradeMaterialTab
from ui.tabs.tab_settings import SettingsTab


_QAPP: Optional[QApplication] = None


def _get_or_create_qapp() -> QApplication:
    global _QAPP
    app = QApplication.instance()
    if app is None:
        app = QApplication(["eduflow-test"])
    _QAPP = app
    return app


def _build_mime(paths: List[Path | str]) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def _drop_target_for(widget: QWidget) -> QWidget:
    vp_fn = getattr(widget, "viewport", None)
    if callable(vp_fn):
        vp = vp_fn()
        if isinstance(vp, QWidget):
            return vp
    return widget


def _send_drag_enter(widget: QWidget, paths: List[Path | str]) -> QDragEnterEvent:
    mime = _build_mime(paths)
    ev = QDragEnterEvent(
        QPoint(8, 8),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    setattr(ev, "_mime", mime)
    ev.ignore()
    QApplication.sendEvent(_drop_target_for(widget), ev)
    return ev


def _send_drag_leave(widget: QWidget) -> QDragLeaveEvent:
    ev = QDragLeaveEvent()
    QApplication.sendEvent(_drop_target_for(widget), ev)
    return ev


def _send_drop(widget: QWidget, paths: List[Path | str]) -> QDropEvent:
    target = _drop_target_for(widget)
    ev_enter = _send_drag_enter(widget, paths)
    mime = _build_mime(paths)
    ev = QDropEvent(
        QPointF(8, 8),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    setattr(ev, "_mime", mime)
    ev.ignore()
    if ev_enter.isAccepted():
        QApplication.sendEvent(target, ev)
    else:
        filt = getattr(widget, "_eduflow_file_drop_filter", None)
        if filt is not None:
            filt.eventFilter(target, ev)
        else:
            QApplication.sendEvent(target, ev)
    return ev


class _IsolatedEnvTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _get_or_create_qapp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db_path = self.tmp_path / "test_eduflow.db"
        self.cfg_path = self.tmp_path / "config.json"

        self._prev_db_inst = DatabaseManager._instance
        self._prev_cfg_inst = ConfigManager._instance
        DatabaseManager._instance = None
        ConfigManager._instance = None

        self.db = DatabaseManager(db_rel_path=str(self.db_path))
        DatabaseManager._instance = self.db
        self.config = ConfigManager(config_filename=str(self.cfg_path))
        self.config.config_path = str(self.cfg_path)
        self.config.set(
            "api_and_integrations",
            "output_base_dir",
            str(self.tmp_path / "output"),
            auto_save=True,
        )
        self.task_runner = TaskRunner()

    def tearDown(self) -> None:
        self.task_runner.stop_all()
        DatabaseManager._instance = self._prev_db_inst
        ConfigManager._instance = self._prev_cfg_inst
        self.tmp_dir.cleanup()


class TestFileDropHelperCore(_IsolatedEnvTestCase):
    def test_valid_extension_highlight_and_leave_restore(self) -> None:
        edit = QLineEdit()
        edit.setStyleSheet("QLineEdit { border: 1px solid #CBD5E1; }")
        orig_css = edit.styleSheet()
        dropped: List[List[str]] = []

        filt = enable_file_drop(
            edit,
            allowed_exts=[".xlsx", ".csv"],
            allow_dirs=False,
            allow_files=True,
            on_files_dropped=dropped.append,
            context_label="테스트 입력",
        )
        sample_xlsx = self.tmp_path / "sample.xlsx"
        sample_xlsx.write_bytes(b"dummy")

        ev_enter = _send_drag_enter(edit, [sample_xlsx])
        self.assertTrue(ev_enter.isAccepted())
        self.assertEqual(edit.property("fileDragActive"), "true")
        self.assertIn("#2563EB", edit.styleSheet())

        _send_drag_leave(edit)
        self.assertEqual(edit.property("fileDragActive"), "false")
        self.assertEqual(edit.styleSheet(), orig_css)

        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn:
            ev_drop = _send_drop(edit, [sample_xlsx])
        self.assertTrue(ev_drop.isAccepted())
        self.assertEqual(dropped, [[str(sample_xlsx.resolve())]])
        self.assertEqual(filt.last_rejected_paths, [])
        mock_warn.assert_not_called()

    def test_invalid_and_mixed_drops_warn_and_filter(self) -> None:
        lst = QListWidget()
        dropped: List[List[str]] = []
        filt = enable_file_drop(
            lst,
            allowed_exts=[".pdf"],
            allow_dirs=False,
            allow_files=True,
            multiple=True,
            on_files_dropped=dropped.append,
            context_label="PDF 목록",
        )
        bad_file = self.tmp_path / "script.exe"
        bad_file.write_bytes(b"mz")
        good_file = self.tmp_path / "report.pdf"
        good_file.write_bytes(b"%PDF-1.4")

        ev_bad_enter = _send_drag_enter(lst, [bad_file])
        self.assertFalse(ev_bad_enter.isAccepted())
        self.assertNotEqual(lst.property("fileDragActive"), "true")

        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn:
            ev_bad_drop = _send_drop(lst, [bad_file])
        self.assertFalse(ev_bad_drop.isAccepted())
        self.assertEqual(dropped, [])
        self.assertEqual(len(filt.last_rejected_paths), 1)
        mock_warn.assert_called_once()

        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn_mixed:
            ev_mixed_enter = _send_drag_enter(lst, [good_file, bad_file])
            self.assertTrue(ev_mixed_enter.isAccepted())
            ev_mixed_drop = _send_drop(lst, [good_file, bad_file])
        self.assertTrue(ev_mixed_drop.isAccepted())
        self.assertEqual(dropped, [[str(good_file.resolve())]])
        self.assertEqual(filt.last_rejected_paths, [str(bad_file.resolve())])
        mock_warn_mixed.assert_called_once()

    def test_directory_only_mode_rejects_files_and_accepts_dirs(self) -> None:
        edit = QLineEdit()
        dropped: List[List[str]] = []
        enable_file_drop(
            edit,
            allow_dirs=True,
            allow_files=False,
            multiple=False,
            on_files_dropped=dropped.append,
            context_label="폴더 입력",
        )
        f = self.tmp_path / "note.txt"
        f.write_text("hi", encoding="utf-8")
        sub_dir = self.tmp_path / "watch_folder"
        sub_dir.mkdir()

        self.assertFalse(_send_drag_enter(edit, [f]).isAccepted())
        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn:
            self.assertFalse(_send_drop(edit, [f]).isAccepted())
        mock_warn.assert_called_once()
        self.assertEqual(dropped, [])

        self.assertTrue(_send_drag_enter(edit, [sub_dir]).isAccepted())
        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn_ok:
            self.assertTrue(_send_drop(edit, [sub_dir]).isAccepted())
        mock_warn_ok.assert_not_called()
        self.assertEqual(dropped, [[str(sub_dir.resolve())]])


class TestTabDragDropIntegrations(_IsolatedEnvTestCase):
    def test_file_sorter_tab_drops(self) -> None:
        tab = FileSorterTab(task_runner=self.task_runner)
        src_dir = self.tmp_path / "Downloads_Test"
        src_dir.mkdir()
        target_dir = self.tmp_path / "Sorted_Target"
        target_dir.mkdir()
        not_a_dir = self.tmp_path / "random.txt"
        not_a_dir.write_text("x", encoding="utf-8")

        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn:
            _send_drop(tab.list_source_dirs, [not_a_dir])
        mock_warn.assert_called_once()

        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn_ok:
            ev = _send_drop(tab.list_source_dirs, [src_dir])
        self.assertTrue(ev.isAccepted())
        mock_warn_ok.assert_not_called()
        items = [tab.list_source_dirs.item(i).text() for i in range(tab.list_source_dirs.count())]
        self.assertIn(str(src_dir.resolve()), items)

        ev_t = _send_drop(tab.edit_target_dir, [target_dir])
        self.assertTrue(ev_t.isAccepted())
        self.assertEqual(tab.edit_target_dir.text(), str(target_dir.resolve()))

    def test_doc_ocr_tab_drops(self) -> None:
        tab = DocOcrTab(task_runner=self.task_runner)

        # 1) 제출물 폴더
        sub_dir = self.tmp_path / "3반_과제"
        sub_dir.mkdir()
        with patch.object(tab, "run_submission_analysis") as mock_analyze:
            ev1 = _send_drop(tab.edit_submission_dir, [sub_dir])
        self.assertTrue(ev1.isAccepted())
        self.assertEqual(tab.edit_submission_dir.text(), str(sub_dir.resolve()))
        mock_analyze.assert_called_once()

        # 2) AI OCR 파일 목록 (혼합 드롭 필터링)
        ocr_img = self.tmp_path / "scan1.png"
        ocr_img.write_bytes(b"png")
        bad_exe = self.tmp_path / "bad.exe"
        bad_exe.write_bytes(b"exe")
        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn:
            ev2 = _send_drop(tab.list_ocr_files, [ocr_img, bad_exe])
        self.assertTrue(ev2.isAccepted())
        mock_warn.assert_called_once()
        ocr_items = [tab.list_ocr_files.item(i).text() for i in range(tab.list_ocr_files.count())]
        self.assertEqual(ocr_items, [str(ocr_img.resolve())])

        # 3) 스캔 PDF & 외부 명렬표 CSV
        scan_pdf = self.tmp_path / "batch.pdf"
        scan_pdf.write_bytes(b"%PDF")
        with patch("ui.tabs.tab_doc_ocr.get_pdf_page_count", return_value=12):
            ev_pdf = _send_drop(tab.edit_split_pdf_path, [scan_pdf])
        self.assertTrue(ev_pdf.isAccepted())
        self.assertEqual(tab.edit_split_pdf_path.text(), str(scan_pdf.resolve()))
        self.assertEqual(tab._split_pdf_total_pages, 12)

        roster_csv = self.tmp_path / "roster.csv"
        roster_csv.write_text("학번,이름\n20101,김철수\n", encoding="utf-8-sig")
        ev_roster = _send_drop(tab.table_split_roster, [roster_csv])
        self.assertTrue(ev_roster.isAccepted())
        self.assertEqual(tab.table_split_roster.rowCount(), 1)

        # 4) 과제 파이프라인 & 가로 병합
        agg_dir = self.tmp_path / "hw_docx"
        agg_dir.mkdir()
        agg_xlsx = self.tmp_path / "extracted.xlsx"
        agg_xlsx.write_bytes(b"xlsx")
        rubric_dir = self.tmp_path / "rubrics"
        rubric_dir.mkdir()
        rubric_pdf = rubric_dir / "rubric.pdf"
        rubric_pdf.write_bytes(b"%PDF")

        self.assertTrue(_send_drop(tab.edit_agg_docx_dir, [agg_dir]).isAccepted())
        self.assertEqual(tab.edit_agg_docx_dir.text(), str(agg_dir.resolve()))
        self.assertTrue(_send_drop(tab.edit_agg_excel_file, [agg_xlsx]).isAccepted())
        self.assertEqual(tab.edit_agg_excel_file.text(), str(agg_xlsx.resolve()))
        self.assertTrue(_send_drop(tab.edit_agg_ref_dir, [rubric_pdf]).isAccepted())
        self.assertEqual(tab.edit_agg_ref_dir.text(), str(rubric_dir.resolve()))

        self.assertEqual(tab.list_merge_excels.dragDropMode(), QAbstractItemView.DragDropMode.InternalMove)
        self.assertTrue(_send_drop(tab.list_merge_excels, [agg_xlsx]).isAccepted())
        merge_items = [tab.list_merge_excels.item(i).text() for i in range(tab.list_merge_excels.count())]
        self.assertEqual(merge_items, [str(agg_xlsx.resolve())])

        # 5) HWPX AI 에이전트 미리보기(.hwpx) & 참고 파일(.docx)
        hwpx_sample = self.tmp_path / "sample_notice.hwpx"
        create_minimal_hwpx_document(title="테스트 안내", empty=False).save(hwpx_sample)
        with patch.object(tab, "_load_hwpx_document_into_agent") as mock_load_hwpx:
            ev_hwpx = _send_drop(tab.browser_hwpx_preview, [hwpx_sample])
        self.assertTrue(ev_hwpx.isAccepted())
        mock_load_hwpx.assert_called_once()
        self.assertEqual(Path(mock_load_hwpx.call_args[0][0]).resolve(), hwpx_sample.resolve())

        ref_docx = self.tmp_path / "guide.docx"
        ref_docx.write_bytes(b"docx")
        with patch.object(tab, "_on_hwpx_create_new"):
            ev_ref = _send_drop(tab.btn_hwpx_add_ref, [ref_docx])
        self.assertTrue(ev_ref.isAccepted())
        self.assertIn(str(ref_docx.resolve()), tab._hwpx_reference_files)

    def test_grade_material_tab_drops(self) -> None:
        tab = GradeMaterialTab(task_runner=self.task_runner)
        course_dir = self.tmp_path / "통합과학_수업자료"
        course_dir.mkdir()
        unit_dir = course_dir / "01_단원"
        unit_dir.mkdir()

        ev_folder = _send_drop(tab.folder_card, [course_dir])
        self.assertTrue(ev_folder.isAccepted())
        self.assertEqual(tab._current_material_root.resolve(), course_dir.resolve())

        slide_pdf = self.tmp_path / "lesson_01.pdf"
        slide_pdf.write_bytes(b"%PDF")
        with patch("ui.tabs.tab_grade_material.QMessageBox.information"):
            ev_mat = _send_drop(tab.tree_materials, [slide_pdf])
        self.assertTrue(ev_mat.isAccepted())
        self.assertTrue((unit_dir / "lesson_01.pdf").is_file())

    def test_classroom_tools_and_calendar_drops(self) -> None:
        # ClassroomToolsTab
        tools_tab = ClassroomToolsTab(task_runner=self.task_runner)
        hr_xlsx = self.tmp_path / "homeroom.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["학년", "반", "번호", "성명"])
        ws.append([2, 3, 1, "홍길동"])
        wb.save(hr_xlsx)

        with patch.object(tools_tab, "import_homeroom_roster_file") as mock_imp:
            ev_hr = _send_drop(tools_tab.btn_sync_roster, [hr_xlsx])
        self.assertTrue(ev_hr.isAccepted())
        mock_imp.assert_called_once_with(str(hr_xlsx.resolve()))

        tmpl_hwpx = self.tmp_path / "duty_tmpl.hwpx"
        tmpl_hwpx.write_bytes(b"zip")
        with patch.object(tools_tab, "export_duty_hwpx_with_template") as mock_duty:
            ev_duty = _send_drop(tools_tab.btn_assign, [tmpl_hwpx])
        self.assertTrue(ev_duty.isAccepted())
        mock_duty.assert_called_once_with(str(tmpl_hwpx.resolve()))

        # CalendarTodoTab
        cal_tab = CalendarTodoTab(db=self.db, config=self.config, task_runner=self.task_runner)
        cal_csv = self.tmp_path / "events.csv"
        cal_csv.write_text("날짜,일정명\n2026-11-20,공개수업\n", encoding="utf-8-sig")
        with patch.object(cal_tab, "import_schedule_file") as mock_cal:
            ev_cal = _send_drop(cal_tab.month_grid_host, [cal_csv])
            ev_btn = _send_drop(cal_tab.btn_bulk_import, [cal_csv])
        self.assertTrue(ev_cal.isAccepted())
        self.assertTrue(ev_btn.isAccepted())
        self.assertEqual(mock_cal.call_count, 2)

        bad_img = self.tmp_path / "photo.png"
        bad_img.write_bytes(b"png")
        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn_cal:
            ev_cal_bad = _send_drop(cal_tab.btn_bulk_import, [bad_img])
        self.assertFalse(ev_cal_bad.isAccepted())
        mock_warn_cal.assert_called_once()

    def test_settings_tab_drops(self) -> None:
        tab = SettingsTab(db=self.db, config=self.config)
        xlsx_path = self.tmp_path / "roster.xlsx"
        xlsx_path.write_bytes(b"xlsx")
        cred_json = self.tmp_path / "credentials.json"
        cred_json.write_text("{}", encoding="utf-8")
        base_dir = self.tmp_path / "EduRoot"
        base_dir.mkdir()

        with patch.object(tab, "import_homeroom_roster_file") as mock_hr:
            ev1 = _send_drop(tab.table_homeroom, [xlsx_path])
        self.assertTrue(ev1.isAccepted())
        mock_hr.assert_called_once_with(str(xlsx_path.resolve()))

        with patch.object(tab, "import_subject_roster_file") as mock_sub:
            ev2 = _send_drop(tab.table_subject, [xlsx_path])
        self.assertTrue(ev2.isAccepted())
        mock_sub.assert_called_once_with(str(xlsx_path.resolve()))

        with patch.object(tab, "import_club_roster_file") as mock_club:
            ev3 = _send_drop(tab.table_club, [xlsx_path])
        self.assertTrue(ev3.isAccepted())
        mock_club.assert_called_once_with(str(xlsx_path.resolve()))

        with patch("ui.components.file_drop_helper.QMessageBox.warning") as mock_warn:
            ev_bad = _send_drop(tab.table_homeroom, [cred_json])
        self.assertFalse(ev_bad.isAccepted())
        mock_warn.assert_called_once()

        self.assertTrue(_send_drop(tab.edit_default_target, [base_dir]).isAccepted())
        self.assertEqual(tab.edit_default_target.text(), str(base_dir.resolve()))
        self.assertTrue(_send_drop(tab.edit_google_cred, [cred_json]).isAccepted())
        self.assertEqual(tab.edit_google_cred.text(), str(cred_json.resolve()))


if __name__ == "__main__":
    unittest.main()
