"""
캘린더 일정·할일 상세/등록/수정 모달 및 학사일정 일괄 등록(.xlsx/.csv/.ics) 미리보기 모달.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QDate, Qt, QTime
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from services.schedule_parser_service import (
    REMINDER_LABELS,
    VALID_SCHEDULE_CATEGORIES,
    normalize_date_str,
    normalize_time_str,
    normalize_todo_priority,
)
from ui.utils import install_no_scroll_wheel_filter


DATE_TIME_LIGHT_QSS = """
QDateEdit, QTimeEdit {
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    border-radius: 6px;
    padding: 4px 10px;
    selection-background-color: #EEF3FD;
    selection-color: #003EA8;
}
QDateEdit:focus, QTimeEdit:focus {
    border: 1.5px solid #2563EB;
}
QDateEdit:disabled, QTimeEdit:disabled {
    background-color: #F8FAFC;
    color: #64748B;
    border-color: #E2E8F0;
}
QDateEdit::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid #E2E8F0;
    background-color: #F8FAFC;
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
}
QTimeEdit::up-button, QTimeEdit::down-button {
    width: 20px;
    border-left: 1px solid #E2E8F0;
    background-color: #F8FAFC;
}
QCalendarWidget QWidget {
    background-color: #FFFFFF;
    color: #0F172A;
}
"""


def _apply_role(widget: QWidget, role: str = "", obj_name: str = "") -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    if obj_name:
        widget.setObjectName(obj_name)
    if role:
        widget.setProperty("role", role)
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


class ScheduleItemDialog(QDialog):
    """캘린더 일정 또는 할일을 등록·상세조회·수정·삭제하는 모달 다이얼로그."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        item_type: str = "schedule",
        item_data: Optional[Dict[str, Any]] = None,
        default_date: Optional[str] = None,
        allow_type_switch: bool = False,
    ):
        super().__init__(parent)
        _apply_role(self, obj_name="ScheduleItemModal")
        self.item_data = dict(item_data or {})
        self.is_edit = bool(self.item_data.get("id"))
        self.item_type = "todo" if item_type == "todo" else "schedule"
        self.allow_type_switch = bool(allow_type_switch and not self.is_edit)
        self.default_date = default_date or date.today().isoformat()
        self.deleted = False

        if self.is_edit:
            self.setWindowTitle("할일 상세 및 수정" if self.item_type == "todo" else "일정 상세 및 수정")
        else:
            self.setWindowTitle("새 일정 / 할일 추가" if self.allow_type_switch else ("새 할일 추가" if self.item_type == "todo" else "새 일정 추가"))

        self.setMinimumWidth(500)
        self._init_ui()
        self._load_initial_data()

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        # 상단 헤더 카드
        hdr_card = QFrame(self)
        _apply_role(hdr_card, obj_name="ModalHeaderCard")
        hdr_lay = QHBoxLayout(hdr_card)
        hdr_lay.setContentsMargins(14, 12, 14, 12)
        hdr_lay.setSpacing(10)

        lbl_title = QLabel(self.windowTitle())
        _apply_role(lbl_title, role="cardTitle")
        hdr_lay.addWidget(lbl_title)
        hdr_lay.addStretch()

        self.btn_mode_schedule = QPushButton("캘린더 일정")
        _apply_role(self.btn_mode_schedule, role="segmentBtn")
        self.btn_mode_schedule.setCheckable(True)
        self.btn_mode_schedule.setCursor(Qt.CursorShape.PointingHandCursor)

        self.btn_mode_todo = QPushButton("할일 (Task)")
        _apply_role(self.btn_mode_todo, role="segmentBtn")
        self.btn_mode_todo.setCheckable(True)
        self.btn_mode_todo.setCursor(Qt.CursorShape.PointingHandCursor)

        if self.allow_type_switch:
            self.btn_mode_schedule.clicked.connect(lambda: self._switch_mode("schedule"))
            self.btn_mode_todo.clicked.connect(lambda: self._switch_mode("todo"))
            hdr_lay.addWidget(self.btn_mode_schedule)
            hdr_lay.addWidget(self.btn_mode_todo)
        else:
            self.btn_mode_schedule.hide()
            self.btn_mode_todo.hide()

        root.addWidget(hdr_card)

        # 스택 폼 (0: Schedule, 1: Todo)
        self.form_stack = QStackedWidget(self)
        self.form_stack.addWidget(self._build_schedule_form())
        self.form_stack.addWidget(self._build_todo_form())
        root.addWidget(self.form_stack)

        # 하단 액션 버튼 바
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_delete = QPushButton("삭제")
        _apply_role(self.btn_delete, role="danger", obj_name="BtnDeleteScheduleItem")
        self.btn_delete.setMinimumHeight(36)
        self.btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete.setVisible(self.is_edit)
        self.btn_delete.clicked.connect(self._on_delete_clicked)

        btn_cancel = QPushButton("취소")
        _apply_role(btn_cancel, role="secondary")
        btn_cancel.setMinimumHeight(36)
        btn_cancel.clicked.connect(self.reject)

        self.btn_save = QPushButton("저장" if self.is_edit else "등록하기")
        _apply_role(self.btn_save, role="primary", obj_name="BtnSaveScheduleItem")
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setMinimumWidth(110)
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.clicked.connect(self._on_save_clicked)

        btn_row.addWidget(self.btn_delete)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self.btn_save)
        root.addLayout(btn_row)

    def _build_schedule_form(self) -> QWidget:
        panel = QFrame(self)
        _apply_role(panel, role="instrumentCard")
        form = QFormLayout(panel)
        form.setContentsMargins(16, 16, 16, 16)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(14)

        self.edit_sch_title = QLineEdit()
        self.edit_sch_title.setMinimumHeight(34)
        self.edit_sch_title.setPlaceholderText("예: 2학년 2차 지필고사 / 교무회의")

        self.combo_sch_category = QComboBox()
        self.combo_sch_category.setMinimumHeight(34)
        for cat in VALID_SCHEDULE_CATEGORIES:
            self.combo_sch_category.addItem(cat, cat)
        install_no_scroll_wheel_filter(self.combo_sch_category)

        date_row = QHBoxLayout()
        date_row.setSpacing(8)
        self.date_sch_start = QDateEdit()
        self.date_sch_start.setMinimumHeight(34)
        self.date_sch_start.setCalendarPopup(True)
        self.date_sch_start.setDisplayFormat("yyyy-MM-dd")
        self.date_sch_start.setStyleSheet(DATE_TIME_LIGHT_QSS)
        if self.date_sch_start.calendarWidget() is not None:
            self.date_sch_start.calendarWidget().setMinimumSize(320, 260)
        install_no_scroll_wheel_filter(self.date_sch_start)

        self.date_sch_end = QDateEdit()
        self.date_sch_end.setMinimumHeight(34)
        self.date_sch_end.setCalendarPopup(True)
        self.date_sch_end.setDisplayFormat("yyyy-MM-dd")
        self.date_sch_end.setStyleSheet(DATE_TIME_LIGHT_QSS)
        if self.date_sch_end.calendarWidget() is not None:
            self.date_sch_end.calendarWidget().setMinimumSize(320, 260)
        install_no_scroll_wheel_filter(self.date_sch_end)

        self.date_sch_start.dateChanged.connect(self._on_start_date_changed)
        date_row.addWidget(self.date_sch_start, stretch=1)
        date_row.addWidget(QLabel("~"))
        date_row.addWidget(self.date_sch_end, stretch=1)

        time_row = QHBoxLayout()
        time_row.setSpacing(10)
        self.chk_sch_all_day = QCheckBox("종일 일정")
        self.chk_sch_all_day.setChecked(True)
        self.time_sch_start = QTimeEdit()
        self.time_sch_start.setMinimumHeight(34)
        self.time_sch_start.setDisplayFormat("HH:mm")
        self.time_sch_start.setTime(QTime(9, 0))
        self.time_sch_start.setStyleSheet(DATE_TIME_LIGHT_QSS)
        self.time_sch_start.setEnabled(False)
        install_no_scroll_wheel_filter(self.time_sch_start)
        self.chk_sch_all_day.toggled.connect(
            lambda checked: self.time_sch_start.setEnabled(not checked)
        )
        time_row.addWidget(self.chk_sch_all_day)
        time_row.addWidget(self.time_sch_start, stretch=1)

        self.combo_sch_reminder = QComboBox()
        self.combo_sch_reminder.setMinimumHeight(34)
        for key, label in REMINDER_LABELS.items():
            self.combo_sch_reminder.addItem(label, key)
        install_no_scroll_wheel_filter(self.combo_sch_reminder)

        self.edit_sch_memo = QTextEdit()
        self.edit_sch_memo.setMinimumHeight(82)
        self.edit_sch_memo.setMaximumHeight(120)
        self.edit_sch_memo.setPlaceholderText("상세 장소, 준비물, 참고 사항 등을 입력하세요.")

        form.addRow("일정명:", self.edit_sch_title)
        form.addRow("카테고리:", self.combo_sch_category)
        form.addRow("기간:", date_row)
        form.addRow("시작 시간:", time_row)
        form.addRow("데스크톱 알림:", self.combo_sch_reminder)
        form.addRow("메모:", self.edit_sch_memo)
        return panel

    def _build_todo_form(self) -> QWidget:
        panel = QFrame(self)
        _apply_role(panel, role="instrumentCard")
        form = QFormLayout(panel)
        form.setContentsMargins(16, 16, 16, 16)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(14)

        self.edit_todo_title = QLineEdit()
        self.edit_todo_title.setMinimumHeight(34)
        self.edit_todo_title.setPlaceholderText("예: 나이스(NEIS) 동아리 특기사항 초안 검토")

        due_row = QHBoxLayout()
        due_row.setSpacing(10)
        self.chk_todo_has_due = QCheckBox("마감일 지정")
        self.chk_todo_has_due.setChecked(True)
        self.date_todo_due = QDateEdit()
        self.date_todo_due.setMinimumHeight(34)
        self.date_todo_due.setCalendarPopup(True)
        self.date_todo_due.setDisplayFormat("yyyy-MM-dd")
        self.date_todo_due.setStyleSheet(DATE_TIME_LIGHT_QSS)
        if self.date_todo_due.calendarWidget() is not None:
            self.date_todo_due.calendarWidget().setMinimumSize(320, 260)
        install_no_scroll_wheel_filter(self.date_todo_due)
        self.chk_todo_has_due.toggled.connect(self.date_todo_due.setEnabled)
        due_row.addWidget(self.chk_todo_has_due)
        due_row.addWidget(self.date_todo_due, stretch=1)

        self.combo_todo_priority = QComboBox()
        self.combo_todo_priority.setMinimumHeight(34)
        self.combo_todo_priority.addItem("높음 (긴급)", "high")
        self.combo_todo_priority.addItem("보통", "normal")
        self.combo_todo_priority.addItem("낮음", "low")
        self.combo_todo_priority.setCurrentIndex(1)
        install_no_scroll_wheel_filter(self.combo_todo_priority)

        self.chk_todo_done = QCheckBox("완료 처리됨")

        self.edit_todo_memo = QTextEdit()
        self.edit_todo_memo.setMinimumHeight(82)
        self.edit_todo_memo.setMaximumHeight(120)
        self.edit_todo_memo.setPlaceholderText("할일 세부 내용을 입력하세요.")

        form.addRow("할일 제목:", self.edit_todo_title)
        form.addRow("마감일:", due_row)
        form.addRow("우선순위:", self.combo_todo_priority)
        form.addRow("상태:", self.chk_todo_done)
        form.addRow("메모:", self.edit_todo_memo)
        return panel

    def _switch_mode(self, mode: str) -> None:
        self.item_type = "todo" if mode == "todo" else "schedule"
        self.form_stack.setCurrentIndex(1 if self.item_type == "todo" else 0)
        self.btn_mode_schedule.setChecked(self.item_type == "schedule")
        self.btn_mode_todo.setChecked(self.item_type == "todo")

    def _on_start_date_changed(self, new_date: QDate) -> None:
        if self.date_sch_end.date() < new_date:
            self.date_sch_end.setDate(new_date)

    def _load_initial_data(self) -> None:
        self._switch_mode(self.item_type)
        base_qdate = QDate.fromString(self.default_date[:10], "yyyy-MM-dd")
        if not base_qdate.isValid():
            base_qdate = QDate.currentDate()

        if self.item_type == "schedule":
            self.edit_sch_title.setText(str(self.item_data.get("title", "")))
            cat = str(self.item_data.get("category", "학사일정"))
            c_idx = self.combo_sch_category.findData(cat)
            if c_idx >= 0:
                self.combo_sch_category.setCurrentIndex(c_idx)

            s_str = str(self.item_data.get("start_date", "") or self.default_date)[:10]
            e_str = str(self.item_data.get("end_date", "") or s_str)[:10]
            s_qd = QDate.fromString(s_str, "yyyy-MM-dd")
            e_qd = QDate.fromString(e_str, "yyyy-MM-dd")
            self.date_sch_start.setDate(s_qd if s_qd.isValid() else base_qdate)
            self.date_sch_end.setDate(e_qd if e_qd.isValid() else (s_qd if s_qd.isValid() else base_qdate))

            t_str = normalize_time_str(self.item_data.get("start_time", ""))
            if t_str:
                hh, mm = map(int, t_str.split(":"))
                self.chk_sch_all_day.setChecked(False)
                self.time_sch_start.setTime(QTime(hh, mm))
            else:
                self.chk_sch_all_day.setChecked(True)

            r_type = str(self.item_data.get("reminder_type", "none") or "none")
            r_idx = self.combo_sch_reminder.findData(r_type)
            if r_idx >= 0:
                self.combo_sch_reminder.setCurrentIndex(r_idx)

            self.edit_sch_memo.setPlainText(str(self.item_data.get("memo", "")))
            self.date_todo_due.setDate(base_qdate)
        else:
            self.edit_todo_title.setText(str(self.item_data.get("title", "")))
            due_str = str(self.item_data.get("due_date", "")).strip()
            if due_str:
                dq = QDate.fromString(due_str[:10], "yyyy-MM-dd")
                self.chk_todo_has_due.setChecked(dq.isValid())
                self.date_todo_due.setDate(dq if dq.isValid() else base_qdate)
            else:
                self.chk_todo_has_due.setChecked(not self.is_edit)
                self.date_todo_due.setDate(base_qdate)

            prio = str(self.item_data.get("priority", "normal") or "normal")
            p_idx = self.combo_todo_priority.findData(prio)
            if p_idx >= 0:
                self.combo_todo_priority.setCurrentIndex(p_idx)
            self.chk_todo_done.setChecked(bool(self.item_data.get("is_done", 0)))
            self.edit_todo_memo.setPlainText(str(self.item_data.get("memo", "")))
            self.date_sch_start.setDate(base_qdate)
            self.date_sch_end.setDate(base_qdate)

    def _on_delete_clicked(self) -> None:
        ans = QMessageBox.question(
            self,
            "삭제 확인",
            "선택한 항목을 삭제하시겠습니까?",
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.deleted = True
            self.accept()

    def _on_save_clicked(self) -> None:
        if self.item_type == "schedule":
            if not self.edit_sch_title.text().strip():
                QMessageBox.warning(self, "입력 확인", "일정명을 입력해 주세요.")
                self.edit_sch_title.setFocus()
                return
        else:
            if not self.edit_todo_title.text().strip():
                QMessageBox.warning(self, "입력 확인", "할일 제목을 입력해 주세요.")
                self.edit_todo_title.setFocus()
                return
        self.deleted = False
        self.accept()

    def get_result(self) -> Dict[str, Any]:
        if self.deleted:
            return {
                "action": "delete",
                "item_type": self.item_type,
                "id": self.item_data.get("id"),
            }

        if self.item_type == "schedule":
            s_date = self.date_sch_start.date().toString("yyyy-MM-dd")
            e_date = self.date_sch_end.date().toString("yyyy-MM-dd")
            if e_date < s_date:
                e_date = s_date
            s_time = "" if self.chk_sch_all_day.isChecked() else self.time_sch_start.time().toString("HH:mm")
            return {
                "action": "save",
                "item_type": "schedule",
                "id": self.item_data.get("id"),
                "data": {
                    "title": self.edit_sch_title.text().strip(),
                    "category": str(self.combo_sch_category.currentData() or "학사일정"),
                    "start_date": s_date,
                    "end_date": e_date,
                    "start_time": s_time,
                    "reminder_type": str(self.combo_sch_reminder.currentData() or "none"),
                    "memo": self.edit_sch_memo.toPlainText().strip(),
                },
            }

        due_date = self.date_todo_due.date().toString("yyyy-MM-dd") if self.chk_todo_has_due.isChecked() else ""
        return {
            "action": "save",
            "item_type": "todo",
            "id": self.item_data.get("id"),
            "data": {
                "title": self.edit_todo_title.text().strip(),
                "due_date": due_date,
                "priority": str(self.combo_todo_priority.currentData() or "normal"),
                "is_done": self.chk_todo_done.isChecked(),
                "memo": self.edit_todo_memo.toPlainText().strip(),
            },
        }


class ScheduleImportPreviewDialog(QDialog):
    """학사일정(.xlsx / .csv / .ics) 및 AI 추출 일정·할일 미리보기/확정 모달."""

    def __init__(
        self,
        parsed_items: List[Dict[str, Any]],
        file_name: str = "",
        parent: Optional[QWidget] = None,
        *,
        parsed_todos: Optional[List[Dict[str, Any]]] = None,
        summary: str = "",
    ):
        super().__init__(parent)
        _apply_role(self, obj_name="ScheduleImportPreviewModal")
        self.parsed_items = [dict(x) for x in parsed_items]
        self._has_todo_section = parsed_todos is not None
        self.parsed_todos = [dict(x) for x in (parsed_todos or [])]
        self.summary = str(summary or "").strip()
        self.file_name = file_name
        self.setWindowTitle(
            "AI 추출 일정·할일 미리보기 및 확정"
            if self._has_todo_section
            else "학사일정 일괄 등록 미리보기 및 확정"
        )
        self.resize(920, 660 if self._has_todo_section else 580)
        self.setMinimumSize(760, 460)
        self._init_ui()
        self._populate_table()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        source_text = f" ({self.file_name})" if self.file_name else ""
        if self._has_todo_section:
            banner_msg = (
                f"일정 {len(self.parsed_items)}건 · 할일 {len(self.parsed_todos)}건 추출됨{source_text} — "
                "아직 DB에 저장되지 않았습니다. 항목을 체크·수정한 뒤 [선택 항목 일괄 등록 확정]을 누르세요."
            )
            if self.summary:
                banner_msg += f"\n요약: {self.summary}"
        else:
            banner_msg = (
                f"파싱된 일정 {len(self.parsed_items)}건{source_text} — 아직 DB에 저장되지 않았습니다. "
                "등록할 항목을 체크·검수한 뒤 [선택 일정 일괄 등록 확정]을 누르세요."
            )
        banner = QLabel(banner_msg)
        _apply_role(banner, role="infoBanner")
        banner.setWordWrap(True)
        layout.addWidget(banner)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        btn_sel_all = QPushButton("전체 선택")
        _apply_role(btn_sel_all, role="secondary")
        btn_sel_all.clicked.connect(lambda: self._set_all_checked(True))

        btn_desel_all = QPushButton("전체 해제")
        _apply_role(btn_desel_all, role="outline")
        btn_desel_all.clicked.connect(lambda: self._set_all_checked(False))

        self.combo_bulk_category = QComboBox()
        for cat in VALID_SCHEDULE_CATEGORIES:
            self.combo_bulk_category.addItem(cat, cat)
        install_no_scroll_wheel_filter(self.combo_bulk_category)

        btn_apply_cat = QPushButton("체크 항목 카테고리 일괄 변경")
        _apply_role(btn_apply_cat, role="outline")
        btn_apply_cat.clicked.connect(self._apply_bulk_category)

        total_init = len(self.parsed_items) + (len(self.parsed_todos) if self._has_todo_section else 0)
        self.lbl_checked_count = QLabel(f"선택됨: {total_init}건")
        _apply_role(self.lbl_checked_count, role="badgePrimary")

        toolbar.addWidget(btn_sel_all)
        toolbar.addWidget(btn_desel_all)
        toolbar.addSpacing(10)
        toolbar.addWidget(self.combo_bulk_category)
        toolbar.addWidget(btn_apply_cat)
        toolbar.addStretch()
        toolbar.addWidget(self.lbl_checked_count)
        layout.addLayout(toolbar)

        if self._has_todo_section:
            lbl_sch = QLabel(f"일정 ({len(self.parsed_items)}건)")
            _apply_role(lbl_sch, role="fieldLabel")
            layout.addWidget(lbl_sch)

        self.table = QTableWidget(0, 7, self)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setHorizontalHeaderLabels(
            ["선택", "시작일", "종료일", "시간", "일정명", "카테고리", "메모"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._update_checked_count)
        layout.addWidget(self.table, stretch=1)

        self.todo_table: Optional[QTableWidget] = None
        if self._has_todo_section:
            lbl_td = QLabel(f"할일 ({len(self.parsed_todos)}건)")
            _apply_role(lbl_td, role="fieldLabel")
            layout.addWidget(lbl_td)

            self.todo_table = QTableWidget(0, 5, self)
            self.todo_table.setAlternatingRowColors(True)
            self.todo_table.verticalHeader().setDefaultSectionSize(34)
            self.todo_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.todo_table.setHorizontalHeaderLabels(
                ["선택", "마감일", "우선순위", "할일 내용", "메모"]
            )
            thdr = self.todo_table.horizontalHeader()
            thdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            thdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            thdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            thdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            thdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
            self.todo_table.itemChanged.connect(self._update_checked_count)
            layout.addWidget(self.todo_table, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        btn_cancel = QPushButton("취소")
        _apply_role(btn_cancel, role="secondary")
        btn_cancel.setMinimumHeight(38)
        btn_cancel.clicked.connect(self.reject)

        self.btn_confirm = QPushButton(
            "선택 항목 일괄 등록 확정" if self._has_todo_section else "선택 일정 일괄 등록 확정"
        )
        _apply_role(self.btn_confirm, role="primary", obj_name="BtnConfirmImportSchedules")
        self.btn_confirm.setMinimumHeight(38)
        self.btn_confirm.setMinimumWidth(190)
        self.btn_confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_confirm.clicked.connect(self._on_confirm)

        footer.addStretch()
        footer.addWidget(btn_cancel)
        footer.addWidget(self.btn_confirm)
        layout.addLayout(footer)

    def _populate_table(self) -> None:
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for idx, item in enumerate(self.parsed_items):
                self.table.insertRow(idx)

                chk_item = QTableWidgetItem()
                chk_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                chk_item.setCheckState(Qt.CheckState.Checked)
                chk_item.setData(Qt.ItemDataRole.UserRole, item.get("remote_id", ""))
                chk_item.setData(
                    Qt.ItemDataRole.UserRole + 1,
                    str(item.get("reminder_type") or item.get("reminder") or "none"),
                )
                self.table.setItem(idx, 0, chk_item)

                self.table.setItem(idx, 1, QTableWidgetItem(str(item.get("start_date", ""))))
                self.table.setItem(idx, 2, QTableWidgetItem(str(item.get("end_date", ""))))
                self.table.setItem(idx, 3, QTableWidgetItem(str(item.get("start_time", ""))))
                self.table.setItem(idx, 4, QTableWidgetItem(str(item.get("title", ""))))

                cat_combo = QComboBox()
                cat_combo.setMinimumHeight(28)
                for cat in VALID_SCHEDULE_CATEGORIES:
                    cat_combo.addItem(cat, cat)
                cur_cat = str(item.get("category", "학사일정"))
                c_idx = cat_combo.findData(cur_cat)
                if c_idx >= 0:
                    cat_combo.setCurrentIndex(c_idx)
                install_no_scroll_wheel_filter(cat_combo)
                self.table.setCellWidget(idx, 5, cat_combo)

                self.table.setItem(idx, 6, QTableWidgetItem(str(item.get("memo", ""))))
        finally:
            self.table.blockSignals(False)

        if self.todo_table is not None:
            self.todo_table.blockSignals(True)
            try:
                self.todo_table.setRowCount(0)
                for idx, td in enumerate(self.parsed_todos):
                    self.todo_table.insertRow(idx)

                    chk_td = QTableWidgetItem()
                    chk_td.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    chk_td.setCheckState(Qt.CheckState.Checked)
                    self.todo_table.setItem(idx, 0, chk_td)

                    self.todo_table.setItem(idx, 1, QTableWidgetItem(str(td.get("due_date", ""))))

                    prio_combo = QComboBox()
                    prio_combo.setMinimumHeight(28)
                    prio_combo.addItem("높음", "high")
                    prio_combo.addItem("보통", "normal")
                    prio_combo.addItem("낮음", "low")
                    cur_prio = normalize_todo_priority(td.get("priority", "normal"))
                    p_idx = prio_combo.findData(cur_prio)
                    if p_idx >= 0:
                        prio_combo.setCurrentIndex(p_idx)
                    install_no_scroll_wheel_filter(prio_combo)
                    self.todo_table.setCellWidget(idx, 2, prio_combo)

                    self.todo_table.setItem(idx, 3, QTableWidgetItem(str(td.get("title", ""))))
                    self.todo_table.setItem(idx, 4, QTableWidgetItem(str(td.get("memo", ""))))
            finally:
                self.todo_table.blockSignals(False)

        self._update_checked_count()

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                it = self.table.item(row, 0)
                if it is not None:
                    it.setCheckState(state)
        finally:
            self.table.blockSignals(False)

        if self.todo_table is not None:
            self.todo_table.blockSignals(True)
            try:
                for row in range(self.todo_table.rowCount()):
                    it = self.todo_table.item(row, 0)
                    if it is not None:
                        it.setCheckState(state)
            finally:
                self.todo_table.blockSignals(False)

        self._update_checked_count()

    def _apply_bulk_category(self) -> None:
        target_cat = str(self.combo_bulk_category.currentData() or "학사일정")
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if chk is not None and chk.checkState() == Qt.CheckState.Checked:
                combo = self.table.cellWidget(row, 5)
                if isinstance(combo, QComboBox):
                    idx = combo.findData(target_cat)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)

    def _update_checked_count(self, *_args: Any) -> None:
        cnt = 0
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if chk is not None and chk.checkState() == Qt.CheckState.Checked:
                cnt += 1
        if self.todo_table is not None:
            for row in range(self.todo_table.rowCount()):
                chk = self.todo_table.item(row, 0)
                if chk is not None and chk.checkState() == Qt.CheckState.Checked:
                    cnt += 1
        self.lbl_checked_count.setText(f"선택됨: {cnt}건")

    def _on_confirm(self) -> None:
        selected = self.get_selected_schedules()
        selected_todos = self.get_selected_todos()
        if not selected and not selected_todos:
            msg = (
                "등록할 일정 또는 할일을 최소 1건 이상 선택해 주세요."
                if self._has_todo_section
                else "등록할 일정을 최소 1건 이상 선택해 주세요."
            )
            QMessageBox.warning(self, "선택 확인", msg)
            return
        self.accept()

    def get_selected_schedules(self) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if chk is None or chk.checkState() != Qt.CheckState.Checked:
                continue

            raw_s = self.table.item(row, 1).text().strip() if self.table.item(row, 1) else ""
            raw_e = self.table.item(row, 2).text().strip() if self.table.item(row, 2) else ""
            raw_t = self.table.item(row, 3).text().strip() if self.table.item(row, 3) else ""
            title = self.table.item(row, 4).text().strip() if self.table.item(row, 4) else ""
            memo = self.table.item(row, 6).text().strip() if self.table.item(row, 6) else ""
            combo = self.table.cellWidget(row, 5)
            cat = str(combo.currentData() if isinstance(combo, QComboBox) else "학사일정")

            s_date, _ = normalize_date_str(raw_s)
            e_date, _ = normalize_date_str(raw_e or s_date)
            if not s_date or not title:
                continue
            if not e_date or e_date < s_date:
                e_date = s_date

            selected.append(
                {
                    "remote_id": str(chk.data(Qt.ItemDataRole.UserRole) or "").strip() or None,
                    "title": title,
                    "category": cat,
                    "start_date": s_date,
                    "end_date": e_date,
                    "start_time": normalize_time_str(raw_t),
                    "reminder_type": str(chk.data(Qt.ItemDataRole.UserRole + 1) or "none").strip() or "none",
                    "memo": memo,
                }
            )
        return selected

    def get_selected_todos(self) -> List[Dict[str, Any]]:
        if self.todo_table is None:
            return []
        selected: List[Dict[str, Any]] = []
        for row in range(self.todo_table.rowCount()):
            chk = self.todo_table.item(row, 0)
            if chk is None or chk.checkState() != Qt.CheckState.Checked:
                continue
            raw_due = self.todo_table.item(row, 1).text().strip() if self.todo_table.item(row, 1) else ""
            title = self.todo_table.item(row, 3).text().strip() if self.todo_table.item(row, 3) else ""
            memo = self.todo_table.item(row, 4).text().strip() if self.todo_table.item(row, 4) else ""
            if not title:
                continue
            combo = self.todo_table.cellWidget(row, 2)
            prio = normalize_todo_priority(combo.currentData() if isinstance(combo, QComboBox) else "normal")
            due_date, _ = normalize_date_str(raw_due)
            selected.append(
                {
                    "title": title,
                    "due_date": due_date,
                    "priority": prio,
                    "memo": memo,
                }
            )
        return selected
