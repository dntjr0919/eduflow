"""
[탭 6] 캘린더 & 할일 (ui/tabs/tab_calendar_todo.py)
- 좌측(65%): Google Calendar 스타일 월간/주간 전환 캘린더, 카테고리별 색상 태그(학사일정/수행평가/업무/개인),
  일정 클릭 시 상세 조회·수정·삭제, .xlsx/.csv/.ics 학사일정 일괄 등록 미리보기 모달 및 .ics 내보내기
- 우측(35%): 할일 목록(체크박스 완료, 마감일, D-Day 배지, 우선순위) + 모바일 예약 작업(예정된 작업 목록)
  [지금 실행], [확인 완료], 상단 [모두 읽음] 버튼
- 1분 주기 QTimer 기반 임박 일정(10분 전, 1시간 전, 당일 오전 09:00) Windows 토스트 + 알림 센터 기록
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QDate, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import FunctionWorker, TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.google_sheets_sync_service import (
    GoogleSheetsSyncService,
    GoogleSheetsSyncWorker,
)
from services.notifier import NotificationService
from services.schedule_parser_service import (
    REMINDER_LABELS,
    ScheduleReminderChecker,
    VALID_SCHEDULE_CATEGORIES,
    calculate_dday_badge,
    export_schedules_to_ics,
    export_schedules_to_xlsx,
    parse_calendar_ai_response,
    parse_schedule_file,
)
from ui.components.file_drop_helper import enable_file_drop
from ui.dialogs.schedule_dialogs import ScheduleImportPreviewDialog, ScheduleItemDialog
from ui.utils import install_no_scroll_wheel_filter, load_svg_icon, load_svg_pixmap


CATEGORY_COLOR_MAP: Dict[str, Dict[str, str]] = {
    "학사일정": {"bg": "#EEF2FF", "fg": "#4338CA", "border": "#C7D2FE", "dot": "#4F46E5"},
    "수행평가": {"bg": "#ECFDF5", "fg": "#047857", "border": "#A7F3D0", "dot": "#059669"},
    "업무": {"bg": "#FFFBEB", "fg": "#B45309", "border": "#FDE68A", "dot": "#D97706"},
    "개인": {"bg": "#F1F5F9", "fg": "#334155", "border": "#CBD5E1", "dot": "#475569"},
}


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


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


class _ElidedEventButton(QPushButton):
    """달력 셀 폭에 맞춰 일정 제목을 말줄임(...) 처리하는 버튼."""

    def __init__(self, full_text: str, parent: Optional[QWidget] = None):
        super().__init__(full_text, parent)
        self._full_text = full_text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(0, 20)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 18)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        avail = max(16, self.width() - 12)
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, avail
        )
        if self.text() != elided:
            self.setText(elided)


class _ClickableDayCell(QFrame):
    """월간 캘린더의 개별 날짜 셀."""

    clicked = pyqtSignal(str)
    double_clicked = pyqtSignal(str)
    event_clicked = pyqtSignal(int)

    def __init__(
        self,
        day_date: date,
        in_current_month: bool,
        is_today: bool,
        is_selected: bool,
        events: List[Dict[str, Any]],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.date_str = day_date.isoformat()
        if is_selected:
            role = "calendarDayCellSelected"
        elif is_today:
            role = "calendarDayCellToday"
        elif not in_current_month:
            role = "calendarDayCellMuted"
        else:
            role = "calendarDayCell"
        _apply_role(self, role=role)
        self.setMinimumWidth(0)
        self.setMinimumHeight(74)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 4, 5, 4)
        lay.setSpacing(2)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(4)

        lbl_day = QLabel(str(day_date.day))
        dow = day_date.weekday()  # Mon=0 .. Sun=6
        if not in_current_month:
            day_color = "#94A3B8"
        elif dow == 6:
            day_color = "#DC2626"
        elif dow == 5:
            day_color = "#2563EB"
        else:
            day_color = "#0F172A"
        lbl_day.setStyleSheet(
            f"color: {day_color}; font-size: 12px; font-weight: {'700' if is_today or is_selected else '600'}; background: transparent;"
        )
        top_row.addWidget(lbl_day)

        if is_today:
            lbl_today = QLabel("오늘")
            lbl_today.setStyleSheet(
                "background-color: #2563EB; color: #FFFFFF; border-radius: 4px; "
                "padding: 1px 5px; font-size: 10px; font-weight: 700;"
            )
            top_row.addWidget(lbl_today)
        top_row.addStretch()
        lay.addLayout(top_row)

        max_visible = 2
        for ev in events[:max_visible]:
            cat = str(ev.get("category", "학사일정"))
            palette = CATEGORY_COLOR_MAP.get(cat, CATEGORY_COLOR_MAP["학사일정"])
            unread_prefix = "● " if int(ev.get("is_read", 1)) == 0 else ""
            time_prefix = f"{ev['start_time']} " if ev.get("start_time") else ""
            display_txt = f"{unread_prefix}{time_prefix}{ev.get('title', '')}"

            ev_btn = _ElidedEventButton(display_txt, parent=self)
            ev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            ev_btn.setMinimumHeight(18)
            ev_btn.setMaximumHeight(20)
            ev_btn.setToolTip(
                f"[{cat}] {ev.get('title', '')}\n"
                f"기간: {ev.get('start_date', '')} ~ {ev.get('end_date', '')}"
                + (f" ({ev['start_time']})" if ev.get("start_time") else "")
            )
            ev_btn.setStyleSheet(
                f"QPushButton {{ background-color: {palette['bg']}; color: {palette['fg']}; "
                f"border: 1px solid {palette['border']}; border-radius: 4px; "
                f"padding: 1px 5px; font-size: 10.5px; font-weight: 600; text-align: left; }}"
                f"QPushButton:hover {{ border-color: {palette['dot']}; }}"
            )
            ev_id = int(ev["id"])
            ev_btn.clicked.connect(lambda _chk=False, sid=ev_id: self.event_clicked.emit(sid))
            lay.addWidget(ev_btn)

        if len(events) > max_visible:
            more_lbl = QLabel(f"+{len(events) - max_visible}건")
            more_lbl.setMinimumWidth(0)
            more_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            more_lbl.setStyleSheet("color: #64748B; font-size: 10px; font-weight: 600; padding-left: 2px;")
            lay.addWidget(more_lbl)

        lay.addStretch()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.date_str)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.date_str)
        super().mouseDoubleClickEvent(event)


class CalendarTodoTab(QWidget):
    """[탭 6] 캘린더 & 할일 탭 위젯."""

    unread_changed = pyqtSignal()
    mobile_task_execute_requested = pyqtSignal(dict)
    sync_completed = pyqtSignal(dict)

    SYNC_TASK_KEY = "calendar_google_sheets_sync"

    def __init__(
        self,
        task_runner: TaskRunner,
        parent: Optional[QWidget] = None,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
        notifier: Optional[NotificationService] = None,
        start_timer: bool = True,
    ):
        super().__init__(parent)
        _apply_role(self, obj_name="CalendarTodoTabPage")
        self.task_runner = task_runner
        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()
        self.notifier = notifier or NotificationService()
        self.sync_service = GoogleSheetsSyncService(db=self.db, config=self.config)

        today = date.today()
        self._current_year = today.year
        self._current_month = today.month
        self._selected_date = today.isoformat()
        self._view_mode = "month"  # "month" | "week"
        self._active_category = "전체"
        self._banner_dismissed = False
        self._category_buttons: Dict[str, QPushButton] = {}

        self._init_ui()
        self.refresh_all()

        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(60_000)  # 1분 주기
        self.reminder_timer.timeout.connect(self.check_reminders_now)
        if start_timer:
            self.reminder_timer.start()

    # =========================================================================
    # UI 구성 (상단 액션 바 + 모바일 알림 배너 + 65:35 분할 워크스페이스)
    # =========================================================================
    def _init_ui(self) -> None:
        outer_lay = QVBoxLayout(self)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        self.main_scroll = QScrollArea(self)
        self.main_scroll.setWidgetResizable(True)
        self.main_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.main_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.main_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content_widget = QWidget()
        content_widget.setMinimumWidth(680)
        root = QVBoxLayout(content_widget)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # 1. 상단 액션 헤더 바
        action_bar = QHBoxLayout()
        action_bar.setSpacing(10)

        self.lbl_sync_status_pill = QLabel("Google Sheets 연동 대기")
        _apply_role(self.lbl_sync_status_pill, role="badgeNeutral", obj_name="CalendarSyncPill")

        self.btn_bulk_import = QPushButton("학사일정 일괄 등록 (.xlsx)")
        _apply_role(self.btn_bulk_import, role="secondary", obj_name="BtnBulkImportSchedule")
        self.btn_bulk_import.setIcon(load_svg_icon("file-spreadsheet", "#059669", 15))
        self.btn_bulk_import.setIconSize(QSize(15, 15))
        self.btn_bulk_import.setMinimumHeight(34)
        self.btn_bulk_import.setToolTip("학사일정 일괄 등록 (.xlsx / .csv / .ics 파일을 클릭하거나 드래그하세요)")
        self.btn_bulk_import.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_bulk_import.clicked.connect(self._on_bulk_import_clicked)
        enable_file_drop(
            self.btn_bulk_import,
            allowed_exts=[".xlsx", ".csv", ".ics"],
            allow_dirs=False,
            allow_files=True,
            multiple=False,
            on_files_dropped=lambda paths: self.import_schedule_file(paths[0]),
            context_label="학사일정 일괄 등록 파일",
        )

        self.btn_sync_sheets = QPushButton("Google Sheets 동기화")
        _apply_role(self.btn_sync_sheets, role="primary", obj_name="BtnCalendarSyncSheets")
        self.btn_sync_sheets.setIcon(load_svg_icon("refresh-cw", "#FFFFFF", 14))
        self.btn_sync_sheets.setIconSize(QSize(14, 14))
        self.btn_sync_sheets.setMinimumHeight(34)
        self.btn_sync_sheets.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sync_sheets.clicked.connect(lambda: self.sync_with_google_sheets(interactive=True))

        self.btn_new_item = QPushButton("새 일정/할일")
        _apply_role(self.btn_new_item, role="slate", obj_name="BtnNewCalendarItem")
        self.btn_new_item.setIcon(load_svg_icon("plus", "#FFFFFF", 14))
        self.btn_new_item.setIconSize(QSize(14, 14))
        self.btn_new_item.setMinimumHeight(34)
        self.btn_new_item.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_item.clicked.connect(self._on_new_item_clicked)

        action_bar.addWidget(self.lbl_sync_status_pill)
        action_bar.addStretch()
        action_bar.addWidget(self.btn_bulk_import)
        action_bar.addWidget(self.btn_sync_sheets)
        action_bar.addWidget(self.btn_new_item)
        root.addLayout(action_bar)

        # 2. 상단 모바일 예약 작업 알림 배너 (pending 항목 있을 때 표시)
        self.mobile_banner_frame = QFrame(self)
        _apply_role(self.mobile_banner_frame, obj_name="MobileTaskAlertBanner")
        banner_lay = QHBoxLayout(self.mobile_banner_frame)
        banner_lay.setContentsMargins(14, 10, 14, 10)
        banner_lay.setSpacing(10)

        self.lbl_banner_icon = QLabel()
        self.lbl_banner_icon.setObjectName("MobileBannerIconBadge")
        self.lbl_banner_icon.setFixedSize(32, 32)
        self.lbl_banner_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner_pix = load_svg_pixmap("smartphone", "#2563EB", 16)
        if not banner_pix.isNull():
            self.lbl_banner_icon.setPixmap(banner_pix)

        banner_txt_col = QVBoxLayout()
        banner_txt_col.setSpacing(2)
        self.lbl_banner_title = QLabel("모바일에서 새로 추가된 예약 작업이 대기 중입니다.")
        self.lbl_banner_title.setStyleSheet("color: #1E3A8A; font-size: 13px; font-weight: 700;")
        self.lbl_banner_sub = QLabel("자동 강제 실행되지 않으며, 우측 패널에서 [지금 실행] 또는 [확인 완료]를 선택할 수 있습니다.")
        self.lbl_banner_sub.setWordWrap(True)
        self.lbl_banner_sub.setStyleSheet("color: #3B82F6; font-size: 11.5px;")
        banner_txt_col.addWidget(self.lbl_banner_title)
        banner_txt_col.addWidget(self.lbl_banner_sub)

        btn_banner_focus = QPushButton("지금 확인")
        _apply_role(btn_banner_focus, role="primary")
        btn_banner_focus.setMinimumHeight(30)
        btn_banner_focus.clicked.connect(self._focus_mobile_tasks_panel)

        btn_banner_read_all = QPushButton("모두 읽음")
        _apply_role(btn_banner_read_all, role="secondary")
        btn_banner_read_all.setMinimumHeight(30)
        btn_banner_read_all.clicked.connect(self.mark_all_read)

        btn_banner_close = QPushButton("✕")
        _apply_role(btn_banner_close, role="outline")
        btn_banner_close.setFixedSize(28, 28)
        btn_banner_close.clicked.connect(self._dismiss_banner)

        banner_lay.addWidget(self.lbl_banner_icon)
        banner_lay.addLayout(banner_txt_col, stretch=1)
        banner_lay.addWidget(btn_banner_focus)
        banner_lay.addWidget(btn_banner_read_all)
        banner_lay.addWidget(btn_banner_close)
        self.mobile_banner_frame.hide()
        root.addWidget(self.mobile_banner_frame)

        # 3. 메인 65% 좌측 캘린더 + 35% 우측 할일·모바일 작업 패널
        split_row = QHBoxLayout()
        split_row.setContentsMargins(0, 0, 0, 0)
        split_row.setSpacing(14)

        self.left_calendar_card = self._build_left_calendar_card()
        self.right_tasks_card = self._build_right_tasks_panel()

        split_row.addWidget(self.left_calendar_card, stretch=65)
        split_row.addWidget(self.right_tasks_card, stretch=35)
        root.addLayout(split_row, stretch=1)

        for cal_drop_w in (self.left_calendar_card, self.month_grid_host):
            enable_file_drop(
                cal_drop_w,
                allowed_exts=[".xlsx", ".csv", ".ics"],
                allow_dirs=False,
                allow_files=True,
                multiple=False,
                on_files_dropped=lambda paths: self.import_schedule_file(paths[0]),
                context_label="학사일정 일괄 등록 파일",
            )

        self.main_scroll.setWidget(content_widget)
        outer_lay.addWidget(self.main_scroll)

    def _build_left_calendar_card(self) -> QFrame:
        card = QFrame(self)
        _apply_role(card, role="instrumentCard", obj_name="CalendarMainCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        # 캘린더 상단 컨트롤 바
        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)

        self.lbl_cal_period = QLabel("2026년 11월")
        self.lbl_cal_period.setStyleSheet("color: #0F172A; font-size: 18px; font-weight: 700;")

        btn_prev = QPushButton("‹")
        _apply_role(btn_prev, role="outline")
        btn_prev.setFixedSize(30, 30)
        btn_prev.clicked.connect(lambda: self._navigate_period(-1))

        btn_today = QPushButton("오늘")
        _apply_role(btn_today, role="secondary")
        btn_today.setMinimumHeight(30)
        btn_today.clicked.connect(self._jump_to_today)

        btn_next = QPushButton("›")
        _apply_role(btn_next, role="outline")
        btn_next.setFixedSize(30, 30)
        btn_next.clicked.connect(lambda: self._navigate_period(1))

        seg_frame = QFrame(card)
        _apply_role(seg_frame, obj_name="AiDockSegmentFrame")
        seg_lay = QHBoxLayout(seg_frame)
        seg_lay.setContentsMargins(3, 3, 3, 3)
        seg_lay.setSpacing(2)

        self.btn_view_month = QPushButton("월간")
        _apply_role(self.btn_view_month, role="segmentBtn")
        self.btn_view_month.setCheckable(True)
        self.btn_view_month.setChecked(True)
        self.btn_view_month.setMinimumWidth(56)
        self.btn_view_month.clicked.connect(lambda: self._set_view_mode("month"))

        self.btn_view_week = QPushButton("주간")
        _apply_role(self.btn_view_week, role="segmentBtn")
        self.btn_view_week.setCheckable(True)
        self.btn_view_week.setMinimumWidth(56)
        self.btn_view_week.clicked.connect(lambda: self._set_view_mode("week"))

        seg_lay.addWidget(self.btn_view_month)
        seg_lay.addWidget(self.btn_view_week)

        nav_row.addWidget(self.lbl_cal_period)
        nav_row.addSpacing(8)
        nav_row.addWidget(btn_prev)
        nav_row.addWidget(btn_today)
        nav_row.addWidget(btn_next)
        nav_row.addStretch()
        nav_row.addWidget(seg_frame)
        lay.addLayout(nav_row)

        # 카테고리 필터 칩 바
        cat_row = QHBoxLayout()
        cat_row.setSpacing(6)
        for cat in ("전체",) + VALID_SCHEDULE_CATEGORIES:
            btn = QPushButton(cat if cat == "전체" else f"● {cat}")
            btn.setCheckable(True)
            btn.setChecked(cat == "전체")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(26)
            btn.clicked.connect(lambda _chk=False, c=cat: self._set_category_filter(c))
            self._category_buttons[cat] = btn
            cat_row.addWidget(btn)
        cat_row.addStretch()
        self._refresh_category_chip_styles()
        lay.addLayout(cat_row)

        # 월간 / 주간 뷰 스택
        self.cal_view_stack = QStackedWidget(card)

        # [0] 월간 그리드 페이지
        month_page = QWidget()
        month_lay = QVBoxLayout(month_page)
        month_lay.setContentsMargins(0, 0, 0, 2)
        month_lay.setSpacing(0)

        dow_hdr = QFrame()
        dow_hdr.setStyleSheet(
            "background-color: #F8FAFC; border: 1px solid #E2E8F0; border-bottom: none; "
            "border-top-left-radius: 8px; border-top-right-radius: 8px;"
        )
        dow_grid = QGridLayout(dow_hdr)
        dow_grid.setContentsMargins(0, 6, 0, 6)
        dow_grid.setSpacing(0)
        dow_names = [
            ("일 (Sun)", "#DC2626"),
            ("월", "#475569"),
            ("화", "#475569"),
            ("수", "#475569"),
            ("목", "#475569"),
            ("금", "#475569"),
            ("토 (Sat)", "#2563EB"),
        ]
        for col_idx, (d_lbl, d_col) in enumerate(dow_names):
            lbl = QLabel(d_lbl)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color: {d_col}; font-size: 11.5px; font-weight: 700; border: none;")
            dow_grid.addWidget(lbl, 0, col_idx)
        month_lay.addWidget(dow_hdr)

        self.month_grid_host = QFrame()
        _apply_role(self.month_grid_host, obj_name="CalendarMonthGridFrame")
        self.month_grid_host.setMinimumHeight(6 * 66 + 2)
        self.month_grid_layout = QGridLayout(self.month_grid_host)
        self.month_grid_layout.setContentsMargins(0, 0, 0, 1)
        self.month_grid_layout.setSpacing(0)
        for col in range(7):
            self.month_grid_layout.setColumnStretch(col, 1)
        for row in range(6):
            self.month_grid_layout.setRowStretch(row, 1)
        month_lay.addWidget(self.month_grid_host, stretch=1)

        # [1] 주간 리스트 페이지
        week_scroll = QScrollArea()
        week_scroll.setWidgetResizable(True)
        week_scroll.setFrameShape(QFrame.Shape.NoFrame)
        week_host = QWidget()
        self.week_list_layout = QVBoxLayout(week_host)
        self.week_list_layout.setContentsMargins(2, 4, 2, 4)
        self.week_list_layout.setSpacing(8)
        week_scroll.setWidget(week_host)

        self.cal_view_stack.addWidget(month_page)
        self.cal_view_stack.addWidget(week_scroll)
        lay.addWidget(self.cal_view_stack, stretch=1)

        # 하단 캘린더 푸터 (안내 문구 + 버튼 2줄 분리로 AI 패널 열림 시 잘림 방지)
        footer_col = QVBoxLayout()
        footer_col.setContentsMargins(0, 2, 0, 0)
        footer_col.setSpacing(6)

        self.lbl_cal_footer = QLabel("일정을 클릭하면 상세 조회·수정·삭제할 수 있습니다. (빈 날짜 더블클릭 시 빠른 등록)")
        self.lbl_cal_footer.setWordWrap(True)
        self.lbl_cal_footer.setMinimumWidth(0)
        _apply_role(self.lbl_cal_footer, role="mutedText")

        footer_btn_row = QHBoxLayout()
        footer_btn_row.setContentsMargins(0, 0, 0, 0)
        footer_btn_row.setSpacing(6)

        btn_export_template = QPushButton("빈 양식 (.xlsx)")
        _apply_role(btn_export_template, role="outline", obj_name="BtnExportXlsxTemplate")
        btn_export_template.setMinimumHeight(28)
        btn_export_template.clicked.connect(self._on_export_xlsx_template_clicked)

        btn_export_xlsx = QPushButton("엑셀 내보내기")
        _apply_role(btn_export_xlsx, role="outline", obj_name="BtnExportXlsx")
        btn_export_xlsx.setMinimumHeight(28)
        btn_export_xlsx.clicked.connect(self._on_export_xlsx_clicked)

        btn_export_ics = QPushButton("달력 (.ics)")
        _apply_role(btn_export_ics, role="outline", obj_name="BtnExportIcs")
        btn_export_ics.setMinimumHeight(28)
        btn_export_ics.clicked.connect(self._on_export_ics_clicked)

        footer_btn_row.addStretch()
        footer_btn_row.addWidget(btn_export_template)
        footer_btn_row.addWidget(btn_export_xlsx)
        footer_btn_row.addWidget(btn_export_ics)

        footer_col.addWidget(self.lbl_cal_footer)
        footer_col.addLayout(footer_btn_row)
        lay.addLayout(footer_col)
        return card

    def _build_right_tasks_panel(self) -> QFrame:
        card = QFrame(self)
        _apply_role(card, role="instrumentCard", obj_name="RightTasksPanelCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(10)

        # 상단 헤더 (카운트 배지 + [모두 읽음] + 새로고침)
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        lbl_mob = QLabel("모바일 예약 작업")
        lbl_mob.setStyleSheet("color: #0F172A; font-size: 13px; font-weight: 700;")
        self.lbl_mobile_count = QLabel("0")
        _apply_role(self.lbl_mobile_count, role="badgeDanger")

        lbl_todo = QLabel("오늘의 할일")
        lbl_todo.setStyleSheet("color: #0F172A; font-size: 13px; font-weight: 700;")
        self.lbl_todo_count = QLabel("0")
        _apply_role(self.lbl_todo_count, role="badgePrimary")

        self.btn_mark_all_read = QPushButton("모두 읽음")
        _apply_role(self.btn_mark_all_read, role="outline", obj_name="BtnCalendarMarkAllRead")
        self.btn_mark_all_read.setMinimumHeight(26)
        self.btn_mark_all_read.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mark_all_read.clicked.connect(self.mark_all_read)

        self.btn_refresh_panel = QPushButton("")
        _apply_role(self.btn_refresh_panel, role="outline", obj_name="BtnRefreshTasksPanel")
        self.btn_refresh_panel.setIcon(load_svg_icon("rotate-cw", "#64748B", 14))
        self.btn_refresh_panel.setIconSize(QSize(14, 14))
        self.btn_refresh_panel.setFixedSize(28, 28)
        self.btn_refresh_panel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_panel.setToolTip("목록 새로고침")
        self.btn_refresh_panel.clicked.connect(self.refresh_all)

        hdr.addWidget(lbl_mob)
        hdr.addWidget(self.lbl_mobile_count)
        hdr.addSpacing(4)
        hdr.addWidget(lbl_todo)
        hdr.addWidget(self.lbl_todo_count)
        hdr.addStretch()
        hdr.addWidget(self.btn_mark_all_read)
        hdr.addWidget(self.btn_refresh_panel)
        lay.addLayout(hdr)

        # 스크롤 영역 (모바일 예약 작업 + 할일 목록)
        self.right_scroll = QScrollArea(card)
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_body = QWidget()
        scroll_lay = QVBoxLayout(scroll_body)
        scroll_lay.setContentsMargins(0, 0, 4, 0)
        scroll_lay.setSpacing(12)

        # 섹션 A: 모바일 예약 작업
        mob_hdr = QHBoxLayout()
        mob_hdr.setSpacing(6)
        self.lbl_mob_sec_icon = QLabel()
        self.lbl_mob_sec_icon.setFixedSize(16, 16)
        self.lbl_mob_sec_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zap_pix = load_svg_pixmap("zap", "#F59E0B", 14)
        if not zap_pix.isNull():
            self.lbl_mob_sec_icon.setPixmap(zap_pix)
        self.lbl_mob_sec = QLabel("대기 중인 원격 트리거")
        self.lbl_mob_sec.setStyleSheet("color: #334155; font-size: 12px; font-weight: 700;")
        self.lbl_mob_hint = QLabel("자동 백그라운드 대기")
        self.lbl_mob_hint.setStyleSheet(
            "color: #2563EB; font-size: 11px; font-weight: 600; background: transparent; border: none; padding: 0px;"
        )
        mob_hdr.addWidget(self.lbl_mob_sec_icon)
        mob_hdr.addWidget(self.lbl_mob_sec)
        mob_hdr.addStretch()
        mob_hdr.addWidget(self.lbl_mob_hint)
        scroll_lay.addLayout(mob_hdr)

        self.mobile_tasks_host = QWidget()
        self.mobile_tasks_layout = QVBoxLayout(self.mobile_tasks_host)
        self.mobile_tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.mobile_tasks_layout.setSpacing(8)
        scroll_lay.addWidget(self.mobile_tasks_host)

        # 구분선
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #E2E8F0; max-height: 1px;")
        scroll_lay.addWidget(sep)

        # 섹션 B: 할일 목록
        todo_hdr = QHBoxLayout()
        todo_hdr.setSpacing(6)
        self.lbl_todo_sec_icon = QLabel()
        self.lbl_todo_sec_icon.setFixedSize(16, 16)
        self.lbl_todo_sec_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        check_pix = load_svg_pixmap("check-square", "#2563EB", 14)
        if not check_pix.isNull():
            self.lbl_todo_sec_icon.setPixmap(check_pix)
        self.lbl_todo_sec = QLabel("오늘의 교무/학급 할일")
        self.lbl_todo_sec.setStyleSheet("color: #0F172A; font-size: 12px; font-weight: 700;")
        self.lbl_todo_progress = QLabel("0 / 0 완료")
        _apply_role(self.lbl_todo_progress, role="mutedText")
        todo_hdr.addWidget(self.lbl_todo_sec_icon)
        todo_hdr.addWidget(self.lbl_todo_sec)
        todo_hdr.addStretch()
        todo_hdr.addWidget(self.lbl_todo_progress)
        scroll_lay.addLayout(todo_hdr)

        self.todos_host = QWidget()
        self.todos_layout = QVBoxLayout(self.todos_host)
        self.todos_layout.setContentsMargins(0, 0, 0, 0)
        self.todos_layout.setSpacing(6)
        scroll_lay.addWidget(self.todos_host)
        scroll_lay.addStretch()

        self.right_scroll.setWidget(scroll_body)
        lay.addWidget(self.right_scroll, stretch=1)

        # 하단 빠른 할일 입력 바
        quick_box = QFrame(card)
        _apply_role(quick_box, role="subCard")
        q_lay = QVBoxLayout(quick_box)
        q_lay.setContentsMargins(10, 8, 10, 8)
        q_lay.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.edit_quick_todo = QLineEdit()
        self.edit_quick_todo.setMinimumHeight(32)
        self.edit_quick_todo.setPlaceholderText("+ 새 할일 입력 (Enter로 저장)...")
        self.edit_quick_todo.returnPressed.connect(self._on_quick_add_todo)

        self.btn_quick_add = QPushButton("추가")
        _apply_role(self.btn_quick_add, role="primary", obj_name="BtnQuickAddTodo")
        self.btn_quick_add.setMinimumHeight(32)
        self.btn_quick_add.clicked.connect(self._on_quick_add_todo)

        row1.addWidget(self.edit_quick_todo, stretch=1)
        row1.addWidget(self.btn_quick_add)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.date_quick_due = QDateEdit()
        self.date_quick_due.setMinimumHeight(28)
        self.date_quick_due.setCalendarPopup(True)
        self.date_quick_due.setDisplayFormat("yyyy-MM-dd")
        self.date_quick_due.setDate(QDate.currentDate())
        self.date_quick_due.setStyleSheet(
            "QDateEdit { background-color: #FFFFFF; color: #0F172A; border: 1px solid #CBD5E1; "
            "border-radius: 6px; padding: 3px 10px; } "
            "QDateEdit:focus { border: 1.5px solid #2563EB; } "
            "QDateEdit::drop-down { subcontrol-origin: padding; subcontrol-position: top right; "
            "width: 24px; border-left: 1px solid #E2E8F0; background-color: #F8FAFC; } "
            "QCalendarWidget QWidget { background-color: #FFFFFF; color: #0F172A; }"
        )
        install_no_scroll_wheel_filter(self.date_quick_due)

        self.combo_quick_prio = QComboBox()
        self.combo_quick_prio.setMinimumHeight(28)
        self.combo_quick_prio.addItem("보통", "normal")
        self.combo_quick_prio.addItem("높음", "high")
        self.combo_quick_prio.addItem("낮음", "low")
        install_no_scroll_wheel_filter(self.combo_quick_prio)

        row2.addWidget(QLabel("마감일:"))
        row2.addWidget(self.date_quick_due, stretch=1)
        row2.addWidget(QLabel("우선순위:"))
        row2.addWidget(self.combo_quick_prio)

        q_lay.addLayout(row1)
        q_lay.addLayout(row2)
        lay.addWidget(quick_box)
        return card

    # =========================================================================
    # 캘린더 렌더링 및 네비게이션
    # =========================================================================
    def _refresh_category_chip_styles(self) -> None:
        for cat, btn in self._category_buttons.items():
            is_sel = cat == self._active_category
            if cat == "전체":
                bg = "#004AC6" if is_sel else "#F1F5F9"
                fg = "#FFFFFF" if is_sel else "#334155"
                bd = "#003EA8" if is_sel else "#CBD5E1"
            else:
                pal = CATEGORY_COLOR_MAP[cat]
                bg = pal["dot"] if is_sel else pal["bg"]
                fg = "#FFFFFF" if is_sel else pal["fg"]
                bd = pal["dot"] if is_sel else pal["border"]
            btn.setChecked(is_sel)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; "
                f"border-radius: 13px; padding: 2px 10px; font-size: 11.5px; font-weight: 600; }}"
            )

    def _set_category_filter(self, category: str) -> None:
        self._active_category = category
        self._refresh_category_chip_styles()
        self.refresh_calendar()

    def _set_view_mode(self, mode: str) -> None:
        self._view_mode = "week" if mode == "week" else "month"
        self.btn_view_month.setChecked(self._view_mode == "month")
        self.btn_view_week.setChecked(self._view_mode == "week")
        self.cal_view_stack.setCurrentIndex(1 if self._view_mode == "week" else 0)
        self.refresh_calendar()

    def _navigate_period(self, step: int) -> None:
        if self._view_mode == "week":
            try:
                cur_d = date.fromisoformat(self._selected_date)
            except ValueError:
                cur_d = date.today()
            new_d = cur_d + timedelta(days=7 * step)
            self._selected_date = new_d.isoformat()
            self._current_year = new_d.year
            self._current_month = new_d.month
        else:
            m = self._current_month + step
            y = self._current_year
            if m < 1:
                m = 12
                y -= 1
            elif m > 12:
                m = 1
                y += 1
            self._current_year = y
            self._current_month = m
            self._selected_date = date(y, m, 1).isoformat()
        self.refresh_calendar()

    def _jump_to_today(self) -> None:
        today = date.today()
        self._current_year = today.year
        self._current_month = today.month
        self._selected_date = today.isoformat()
        self.refresh_calendar()

    def refresh_all(self) -> None:
        self.refresh_calendar()
        self.refresh_mobile_tasks()
        self.refresh_todos()
        self._update_sync_pill()

    def _update_sync_pill(self) -> None:
        gas_url = str(self.config.get("api_and_integrations", "google_apps_script_url", "") or "").strip()
        if gas_url:
            self.lbl_sync_status_pill.setText("Google Sheets 실시간 연동 활성화")
            _apply_role(self.lbl_sync_status_pill, role="badgeSuccess", obj_name="CalendarSyncPill")
        else:
            self.lbl_sync_status_pill.setText("로컬 캘린더 모드 (환경설정에서 시트 연동 가능)")
            _apply_role(self.lbl_sync_status_pill, role="badgeNeutral", obj_name="CalendarSyncPill")

    def refresh_calendar(self) -> None:
        if self._view_mode == "week":
            self._render_week_view()
        else:
            self._render_month_view()

    def _render_month_view(self) -> None:
        self.lbl_cal_period.setText(f"{self._current_year}년 {self._current_month:02d}월")
        cal = calendar.Calendar(firstweekday=6)  # 일요일 시작
        weeks = cal.monthdatescalendar(self._current_year, self._current_month)
        while len(weeks) < 6:
            last_day = weeks[-1][-1]
            weeks.append([last_day + timedelta(days=i + 1) for i in range(7)])

        start_range = weeks[0][0].isoformat()
        end_range = weeks[-1][-1].isoformat()
        schedules = self.db.get_schedules(
            start_date=start_range,
            end_date=end_range,
            category=self._active_category,
        )

        by_day: Dict[str, List[Dict[str, Any]]] = {}
        for sch in schedules:
            try:
                d_cur = date.fromisoformat(sch["start_date"][:10])
                d_end = date.fromisoformat(sch["end_date"][:10])
            except ValueError:
                continue
            d_cur = max(d_cur, weeks[0][0])
            d_end = min(d_end, weeks[-1][-1])
            while d_cur <= d_end:
                by_day.setdefault(d_cur.isoformat(), []).append(sch)
                d_cur += timedelta(days=1)

        _clear_layout(self.month_grid_layout)
        today_str = date.today().isoformat()

        for c_idx in range(7):
            self.month_grid_layout.setColumnStretch(c_idx, 1)

        for r_idx in range(6):
            self.month_grid_layout.setRowStretch(r_idx, 1)
            self.month_grid_layout.setRowMinimumHeight(r_idx, 74)
            for c_idx in range(7):
                day_d = weeks[r_idx][c_idx]
                d_str = day_d.isoformat()
                cell = _ClickableDayCell(
                    day_date=day_d,
                    in_current_month=(day_d.month == self._current_month),
                    is_today=(d_str == today_str),
                    is_selected=(d_str == self._selected_date),
                    events=by_day.get(d_str, []),
                    parent=self.month_grid_host,
                )
                cell.clicked.connect(self._on_day_cell_clicked)
                cell.double_clicked.connect(self._on_day_cell_double_clicked)
                cell.event_clicked.connect(self._on_schedule_clicked)
                self.month_grid_layout.addWidget(cell, r_idx, c_idx)

    def _render_week_view(self) -> None:
        try:
            anchor = date.fromisoformat(self._selected_date)
        except ValueError:
            anchor = date.today()
        days_since_sunday = (anchor.weekday() + 1) % 7
        week_start = anchor - timedelta(days=days_since_sunday)
        week_days = [week_start + timedelta(days=i) for i in range(7)]
        self.lbl_cal_period.setText(
            f"{week_days[0].strftime('%Y.%m.%d')} ~ {week_days[-1].strftime('%m.%d')} (주간)"
        )

        schedules = self.db.get_schedules(
            start_date=week_days[0].isoformat(),
            end_date=week_days[-1].isoformat(),
            category=self._active_category,
        )

        _clear_layout(self.week_list_layout)
        today_str = date.today().isoformat()
        dow_ko = ["월", "화", "수", "목", "금", "토", "일"]

        for day_d in week_days:
            d_str = day_d.isoformat()
            day_events = [
                sch for sch in schedules
                if sch["start_date"][:10] <= d_str <= sch["end_date"][:10]
            ]

            day_card = QFrame()
            _apply_role(
                day_card,
                role="calendarDayCellToday" if d_str == today_str else "subCard",
            )
            d_lay = QVBoxLayout(day_card)
            d_lay.setContentsMargins(12, 10, 12, 10)
            d_lay.setSpacing(6)

            hdr = QHBoxLayout()
            lbl_date = QLabel(f"{day_d.strftime('%Y-%m-%d')} ({dow_ko[day_d.weekday()]})")
            lbl_date.setStyleSheet(
                f"color: {'#2563EB' if d_str == today_str else '#0F172A'}; font-size: 13px; font-weight: 700;"
            )
            hdr.addWidget(lbl_date)
            if d_str == today_str:
                t_badge = QLabel("오늘")
                _apply_role(t_badge, role="stageBadgeActive")
                hdr.addWidget(t_badge)
            hdr.addStretch()

            btn_add_day = QPushButton("+ 일정 추가")
            _apply_role(btn_add_day, role="outline")
            btn_add_day.setMinimumHeight(24)
            btn_add_day.clicked.connect(lambda _chk=False, ds=d_str: self._open_create_schedule(ds))
            hdr.addWidget(btn_add_day)
            d_lay.addLayout(hdr)

            if not day_events:
                empty_lbl = QLabel("등록된 일정이 없습니다.")
                _apply_role(empty_lbl, role="mutedText")
                d_lay.addWidget(empty_lbl)
            else:
                for ev in day_events:
                    cat = str(ev.get("category", "학사일정"))
                    pal = CATEGORY_COLOR_MAP.get(cat, CATEGORY_COLOR_MAP["학사일정"])
                    unread_dot = "● " if int(ev.get("is_read", 1)) == 0 else ""
                    time_txt = ev.get("start_time") or "종일"
                    row_btn = QPushButton(
                        f"{unread_dot}[{cat}] {time_txt}  |  {ev.get('title', '')}"
                        + (f"  —  {ev['memo']}" if ev.get("memo") else "")
                    )
                    row_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    row_btn.setMinimumHeight(30)
                    row_btn.setStyleSheet(
                        f"QPushButton {{ background-color: {pal['bg']}; color: {pal['fg']}; "
                        f"border: 1px solid {pal['border']}; border-radius: 6px; "
                        f"padding: 4px 10px; font-size: 12px; font-weight: 600; text-align: left; }}"
                        f"QPushButton:hover {{ border-color: {pal['dot']}; }}"
                    )
                    ev_id = int(ev["id"])
                    row_btn.clicked.connect(lambda _chk=False, sid=ev_id: self._on_schedule_clicked(sid))
                    d_lay.addWidget(row_btn)

            self.week_list_layout.addWidget(day_card)

        self.week_list_layout.addStretch()

    def _on_day_cell_clicked(self, date_str: str) -> None:
        self._selected_date = date_str
        self.refresh_calendar()

    def _on_day_cell_double_clicked(self, date_str: str) -> None:
        self._selected_date = date_str
        self._open_create_schedule(date_str)

    def _open_create_schedule(self, default_date: str) -> None:
        dlg = ScheduleItemDialog(
            self,
            item_type="schedule",
            default_date=default_date,
            allow_type_switch=True,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_dialog_result(dlg.get_result())

    def _on_new_item_clicked(self) -> None:
        dlg = ScheduleItemDialog(
            self,
            item_type="schedule",
            default_date=self._selected_date,
            allow_type_switch=True,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_dialog_result(dlg.get_result())

    def _on_schedule_clicked(self, schedule_id: int) -> None:
        sch = self.db.get_schedule(schedule_id)
        if not sch:
            return
        if int(sch.get("is_read", 1)) == 0:
            self.db.mark_schedule_read(schedule_id)
            self.unread_changed.emit()

        dlg = ScheduleItemDialog(
            self,
            item_type="schedule",
            item_data=sch,
            default_date=sch.get("start_date"),
            allow_type_switch=False,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_dialog_result(dlg.get_result())
        else:
            self.refresh_calendar()

    def _apply_dialog_result(self, res: Dict[str, Any]) -> None:
        action = res.get("action")
        item_type = res.get("item_type")
        item_id = res.get("id")
        data = res.get("data") or {}

        if item_type == "schedule":
            if action == "delete" and item_id:
                self.db.delete_schedule(int(item_id))
            elif action == "save":
                if item_id:
                    self.db.update_schedule(int(item_id), **data)
                else:
                    new_id = self.db.add_schedule(**data, is_read=1)
                    if new_id == 0:
                        QMessageBox.information(
                            self,
                            "중복 일정 건너뜀",
                            "동일한 일정(제목·날짜·시간)이 이미 등록되어 있어 건너뛰었습니다.",
                        )
            self.refresh_calendar()
        elif item_type == "todo":
            if action == "delete" and item_id:
                self.db.delete_todo(int(item_id))
            elif action == "save":
                if item_id:
                    self.db.update_todo(int(item_id), **data)
                else:
                    self.db.add_todo(
                        title=data["title"],
                        due_date=data["due_date"],
                        priority=data["priority"],
                        memo=data["memo"],
                        is_read=1,
                    )
            self.refresh_todos()
        self.unread_changed.emit()

    # =========================================================================
    # 우측 패널: 모바일 예약 작업 + 할일 목록
    # =========================================================================
    def refresh_mobile_tasks(self) -> None:
        pending_tasks = self.db.get_mobile_tasks(pending_only=True)
        self.lbl_mobile_count.setText(str(len(pending_tasks)))
        _apply_role(
            self.lbl_mobile_count,
            role="badgeDanger" if pending_tasks else "badgeNeutral",
        )

        # 배너 표시 제어
        if pending_tasks and not self._banner_dismissed:
            unread_mob = sum(1 for t in pending_tasks if int(t.get("is_read", 0)) == 0)
            dot_mark = " ●" if unread_mob > 0 else ""
            self.lbl_banner_title.setText(
                f"모바일에서 새로 추가된 예약 작업 {len(pending_tasks)}건이 대기 중입니다.{dot_mark}"
            )
            self.mobile_banner_frame.show()
        else:
            self.mobile_banner_frame.hide()

        _clear_layout(self.mobile_tasks_layout)
        if not pending_tasks:
            empty_card = QFrame()
            _apply_role(empty_card, role="subCard")
            e_lay = QHBoxLayout(empty_card)
            e_lay.setContentsMargins(12, 12, 12, 12)
            lbl = QLabel("대기 중인 모바일 예약 작업이 없습니다.")
            _apply_role(lbl, role="mutedText")
            e_lay.addWidget(lbl)
            self.mobile_tasks_layout.addWidget(empty_card)
            return

        for task in pending_tasks:
            card = QFrame()
            _apply_role(card, role="mobileTaskCard")
            c_lay = QVBoxLayout(card)
            c_lay.setContentsMargins(12, 10, 12, 10)
            c_lay.setSpacing(6)

            top = QHBoxLayout()
            t_type = str(task.get("task_type", "general"))
            type_label = "모바일 원격 예약" if t_type != "file_sort" else "원격 파일 분류 예약"
            chip = QLabel(type_label)
            _apply_role(chip, role="badgePrimary")
            top.addWidget(chip)
            top.addStretch()

            if int(task.get("is_read", 0)) == 0:
                dot = QLabel("●")
                dot.setStyleSheet("color: #EF4444; font-size: 11px;")
                dot.setToolTip("미확인 항목")
                top.addWidget(dot)
            c_lay.addLayout(top)

            lbl_title = QLabel(str(task.get("title", "")))
            lbl_title.setWordWrap(True)
            lbl_title.setStyleSheet("color: #0F172A; font-size: 13px; font-weight: 700;")
            c_lay.addWidget(lbl_title)

            meta_parts: List[str] = []
            if task.get("scheduled_time"):
                meta_parts.append(f"예약: {task['scheduled_time']}")
            if task.get("target_info"):
                meta_parts.append(f"대상: {task['target_info']}")
            if task.get("memo"):
                meta_parts.append(str(task["memo"]))
            if meta_parts:
                lbl_meta = QLabel(" | ".join(meta_parts))
                lbl_meta.setWordWrap(True)
                _apply_role(lbl_meta, role="mutedText")
                c_lay.addWidget(lbl_meta)

            btn_row = QHBoxLayout()
            btn_row.setSpacing(6)
            tid = int(task["id"])

            btn_exec = QPushButton("지금 실행")
            _apply_role(btn_exec, role="primary", obj_name=f"BtnExecMobile_{tid}")
            btn_exec.setMinimumHeight(28)
            btn_exec.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_exec.clicked.connect(lambda _chk=False, task_id=tid: self.execute_mobile_task(task_id))

            btn_ack = QPushButton("확인 완료")
            _apply_role(btn_ack, role="secondary", obj_name=f"BtnAckMobile_{tid}")
            btn_ack.setMinimumHeight(28)
            btn_ack.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_ack.clicked.connect(lambda _chk=False, task_id=tid: self.acknowledge_mobile_task(task_id))

            btn_row.addWidget(btn_exec)
            btn_row.addWidget(btn_ack)
            btn_row.addStretch()
            c_lay.addLayout(btn_row)

            self.mobile_tasks_layout.addWidget(card)

    def acknowledge_mobile_task(self, task_id: int) -> None:
        rid = self.db.acknowledge_mobile_task(task_id)
        if rid:
            self._push_mobile_status_async(rid, "acknowledged", is_done=False)
        self.refresh_mobile_tasks()
        self.unread_changed.emit()

    def execute_mobile_task(self, task_id: int) -> None:
        task = self.db.get_mobile_task(task_id)
        if not task:
            return
        title = str(task.get("title", "모바일 예약 작업"))
        task_type = str(task.get("task_type", "general")).strip().lower()
        rid = str(task.get("remote_id") or "").strip()

        def _dispatch(_worker: FunctionWorker) -> Dict[str, Any]:
            from services.mobile_integration_worker import MobileRemoteTaskDispatcher

            dispatcher = MobileRemoteTaskDispatcher(
                db=self.db,
                config=self.config,
                sync_service=self.sync_service,
            )
            return dispatcher.dispatch_task(task)

        def _on_done(res: Dict[str, Any]) -> None:
            summary = str(res.get("summary") or f"'{title}' 작업 완료")
            self.db.add_notification(
                title="모바일 예약 작업 실행 완료",
                message=summary,
                channel="mobile_task",
                sender="EduFlow",
                is_read=1,
            )
            self.notifier.notify("모바일 예약 작업 실행", summary)
            self.mobile_task_execute_requested.emit(task)
            self.refresh_mobile_tasks()
            self.unread_changed.emit()

        def _on_err(err_msg: str) -> None:
            if rid:
                self._push_mobile_status_async(rid, "error", is_done=False)
            self.db.add_log("ERROR", "MobileRemoteTask", f"원격 작업 실패 ({title}): {err_msg}")
            self.notifier.notify("모바일 예약 작업 오류", f"'{title}' 실행 실패: {err_msg.splitlines()[0]}")

        # 로컬 실행 대상(file_sort/excel_merge/web_llm/notice_extract/signature_pdf/comcigan_sync)은 백그라운드 워커로 실행
        if task_type in ("file_sort", "excel_merge", "web_llm", "notice_extract", "signature_pdf", "comcigan_sync"):
            self.db.acknowledge_mobile_task(task_id)
            worker = FunctionWorker(f"모바일 원격 작업: {title}", _dispatch)
            self.task_runner.start_worker(
                worker,
                on_result=_on_done,
                on_error=_on_err,
            )
            self.refresh_mobile_tasks()
            return

        rid = self.db.complete_mobile_task(task_id)
        if rid:
            self._push_mobile_status_async(rid, "done", is_done=True)

        self.db.add_notification(
            title="모바일 예약 작업 실행 완료",
            message=f"'{title}' 작업을 즉시 실행 및 완료 처리했습니다.",
            channel="mobile_task",
            sender="EduFlow",
            is_read=1,
        )
        self.notifier.notify("모바일 예약 작업 실행", f"'{title}' 작업을 실행했습니다.")
        self.mobile_task_execute_requested.emit(task)
        self.refresh_mobile_tasks()
        self.unread_changed.emit()

    def _push_mobile_status_async(self, remote_id: str, exec_status: str, is_done: bool) -> None:
        gas_url = str(self.config.get("api_and_integrations", "google_apps_script_url", "") or "").strip()
        if not gas_url or not remote_id:
            return

        def _push(_worker: FunctionWorker) -> bool:
            return self.sync_service.push_task_status(
                remote_id=remote_id,
                exec_status=exec_status,
                is_done=is_done,
            )

        worker = FunctionWorker("Google Sheets 작업 상태 반영", _push)
        self.task_runner.start_worker(worker)

    def refresh_todos(self) -> None:
        todos = self.db.get_todos(include_done=True)
        undone_cnt = sum(1 for t in todos if not int(t.get("is_done", 0)))
        done_cnt = len(todos) - undone_cnt
        self.lbl_todo_count.setText(str(undone_cnt))
        self.lbl_todo_progress.setText(f"{done_cnt} / {len(todos)} 완료")

        _clear_layout(self.todos_layout)
        if not todos:
            empty_lbl = QLabel("등록된 할일이 없습니다. 아래 입력창에서 바로 추가하세요.")
            _apply_role(empty_lbl, role="mutedText")
            empty_lbl.setWordWrap(True)
            self.todos_layout.addWidget(empty_lbl)
            return

        today_obj = date.today()
        for todo in todos:
            tid = int(todo["id"])
            is_done = bool(int(todo.get("is_done", 0)))
            due_str = str(todo.get("due_date", "") or "")
            dday_text, dday_role = calculate_dday_badge(due_str, today=today_obj)
            is_urgent = (not is_done) and dday_role == "badgeDanger"

            card = QFrame()
            _apply_role(card, role="todoItemCardUrgent" if is_urgent else "todoItemCard")
            row = QHBoxLayout(card)
            row.setContentsMargins(10, 7, 10, 7)
            row.setSpacing(8)

            chk = QCheckBox()
            chk.setChecked(is_done)
            chk.setCursor(Qt.CursorShape.PointingHandCursor)
            chk.toggled.connect(lambda checked, todo_id=tid: self._on_todo_toggled(todo_id, checked))
            row.addWidget(chk)

            title_btn = QPushButton(str(todo.get("title", "")))
            title_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            title_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            title_style = (
                "color: #94A3B8; text-decoration: line-through;"
                if is_done
                else "color: #0F172A; font-weight: 600;"
            )
            title_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; padding: 2px 0px; "
                f"text-align: left; font-size: 12.5px; {title_style} }}"
                f"QPushButton:hover {{ color: #2563EB; }}"
            )
            title_btn.clicked.connect(lambda _chk=False, todo_id=tid: self._on_todo_clicked(todo_id))
            row.addWidget(title_btn, stretch=1)

            prio = str(todo.get("priority", "normal"))
            if prio == "high" and not is_done:
                p_lbl = QLabel("긴급")
                _apply_role(p_lbl, role="badgeDanger")
                row.addWidget(p_lbl)
            elif prio == "low":
                p_lbl = QLabel("낮음")
                _apply_role(p_lbl, role="badgeNeutral")
                row.addWidget(p_lbl)

            if dday_text and not is_done:
                d_lbl = QLabel(dday_text)
                _apply_role(d_lbl, role=dday_role)
                row.addWidget(d_lbl)

            if int(todo.get("is_read", 1)) == 0:
                dot = QLabel("●")
                dot.setStyleSheet("color: #EF4444; font-size: 10px;")
                row.addWidget(dot)

            btn_del = QPushButton("✕")
            btn_del.setFixedSize(22, 22)
            btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_del.setStyleSheet(
                "QPushButton { background: transparent; border: none; color: #94A3B8; font-weight: 700; }"
                "QPushButton:hover { color: #DC2626; }"
            )
            btn_del.clicked.connect(lambda _chk=False, todo_id=tid: self._on_delete_todo(todo_id))
            row.addWidget(btn_del)

            self.todos_layout.addWidget(card)

    def _on_quick_add_todo(self) -> None:
        title = self.edit_quick_todo.text().strip()
        if not title:
            return
        due_date = self.date_quick_due.date().toString("yyyy-MM-dd")
        prio = str(self.combo_quick_prio.currentData() or "normal")
        self.db.add_todo(title=title, due_date=due_date, priority=prio, is_read=1)
        self.edit_quick_todo.clear()
        self.refresh_todos()
        self.unread_changed.emit()

    def _on_todo_toggled(self, todo_id: int, checked: bool) -> None:
        self.db.set_todo_done(todo_id, checked)
        self.refresh_todos()
        self.unread_changed.emit()

    def _on_todo_clicked(self, todo_id: int) -> None:
        todo = self.db.get_todo(todo_id)
        if not todo:
            return
        if int(todo.get("is_read", 1)) == 0:
            self.db.mark_todo_read(todo_id)
            self.unread_changed.emit()

        dlg = ScheduleItemDialog(
            self,
            item_type="todo",
            item_data=todo,
            default_date=todo.get("due_date") or self._selected_date,
            allow_type_switch=False,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_dialog_result(dlg.get_result())
        else:
            self.refresh_todos()

    def _on_delete_todo(self, todo_id: int) -> None:
        self.db.delete_todo(todo_id)
        self.refresh_todos()
        self.unread_changed.emit()

    def mark_all_read(self) -> int:
        cnt = self.db.mark_all_calendar_todo_read()
        self.refresh_all()
        self.unread_changed.emit()
        return cnt

    def _focus_mobile_tasks_panel(self) -> None:
        if hasattr(self, "right_scroll"):
            self.right_scroll.verticalScrollBar().setValue(0)

    def _dismiss_banner(self) -> None:
        self._banner_dismissed = True
        self.mobile_banner_frame.hide()

    # =========================================================================
    # 학사일정 일괄 등록(.xlsx/.csv/.ics) 미리보기 후 확정 & ICS 내보내기
    # =========================================================================
    def _on_bulk_import_clicked(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "학사일정 파일 선택 (.xlsx, .csv, .ics)",
            "",
            "Schedule Files (*.xlsx *.csv *.ics);;Excel (*.xlsx);;CSV (*.csv);;iCalendar (*.ics)",
        )
        if not file_path:
            return
        self.import_schedule_file(file_path)

    def import_schedule_file(self, file_path: str) -> int:
        try:
            parsed = parse_schedule_file(file_path, default_year=self._current_year)
        except Exception as exc:
            QMessageBox.warning(self, "파싱 오류", f"일정 파일을 읽는 중 오류가 발생했습니다:\n{exc}")
            return 0

        if not parsed:
            QMessageBox.information(
                self,
                "인식된 일정 없음",
                "파일에서 유효한 날짜와 일정명을 찾지 못했습니다.\n(예: 날짜/일정명/카테고리 열 포함 확인)",
            )
            return 0

        dlg = ScheduleImportPreviewDialog(
            parsed_items=parsed,
            file_name=Path(file_path).name,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0

        selected = dlg.get_selected_schedules()
        added = self.db.bulk_add_schedules(selected, is_read=1)
        skipped = max(0, len(selected) - added)
        self.refresh_calendar()
        self.unread_changed.emit()
        msg = f"선택하신 {added}건의 학사일정이 캘린더에 등록되었습니다."
        if skipped > 0:
            msg += f"\n(동일 일정 중복 {skipped}건 건너뜀)"
        QMessageBox.information(self, "일괄 등록 완료", msg)
        return added

    def _on_export_ics_clicked(self) -> None:
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "캘린더 일정 내보내기 (.ics)",
            f"eduflow_calendar_{self._current_year}_{self._current_month:02d}.ics",
            "iCalendar (*.ics)",
        )
        if not save_path:
            return
        schedules = self.db.get_schedules()
        count = export_schedules_to_ics(schedules, save_path)
        QMessageBox.information(
            self,
            "ICS 내보내기 완료",
            f"총 {count}건의 일정을 '{Path(save_path).name}' 파일로 저장했습니다.",
        )

    def _on_export_xlsx_clicked(self) -> None:
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "학사일정 엑셀 내보내기 (.xlsx)",
            f"eduflow_schedules_{self._current_year}_{self._current_month:02d}.xlsx",
            "Excel (*.xlsx)",
        )
        if not save_path:
            return
        schedules = self.db.get_schedules()
        count = export_schedules_to_xlsx(schedules, save_path, include_sample_when_empty=True)
        msg = (
            f"총 {count}건의 일정을 '{Path(save_path).name}' 파일로 저장했습니다.\n"
            "(편집 후 [학사일정 일괄 등록 (.xlsx)]로 다시 불러올 수 있습니다.)"
            if count > 0
            else f"등록된 일정이 없어 예시가 포함된 빈 양식 '{Path(save_path).name}'을 저장했습니다."
        )
        QMessageBox.information(self, "엑셀 내보내기 완료", msg)

    def _on_export_xlsx_template_clicked(self) -> None:
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "학사일정 빈 양식 받기 (.xlsx)",
            "eduflow_schedule_template.xlsx",
            "Excel (*.xlsx)",
        )
        if not save_path:
            return
        export_schedules_to_xlsx([], save_path, include_sample_when_empty=True)
        QMessageBox.information(
            self,
            "빈 양식 저장 완료",
            f"표준 학사일정 양식 '{Path(save_path).name}'을 저장했습니다.\n"
            "엑셀에서 일정을 입력한 뒤 [학사일정 일괄 등록 (.xlsx)]로 바로 불러올 수 있습니다.",
        )

    # =========================================================================
    # 우측 AiExecutionDock 연동 (자연어/공문 → 일정·할일 JSON 추출)
    # =========================================================================
    def get_ai_context(self) -> Dict[str, Any]:
        return {
            "target_id": "calendar_todo",
            "kind": "calendar_todo",
            "task_label": "일정·할일 자동 추출",
            "selected_date": self._selected_date_str,
        }

    def apply_ai_dock_result(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        parsed_cal = payload.get("parsed_calendar")
        if not isinstance(parsed_cal, dict):
            raw_json = payload.get("parsed_json")
            raw_text = payload.get("raw_response") or payload.get("formatted_text") or ""
            try:
                if isinstance(raw_json, dict) and raw_json:
                    parsed_cal = parse_calendar_ai_response(raw_json)
                elif raw_text:
                    parsed_cal = parse_calendar_ai_response(str(raw_text))
                else:
                    return
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "AI 응답 파싱 오류",
                    f"일정·할일 JSON 응답을 해석하는 중 오류가 발생했습니다:\n{exc}",
                )
                return

        schedules = list(parsed_cal.get("schedules") or [])
        todos = list(parsed_cal.get("todos") or [])
        summary = str(parsed_cal.get("summary") or "").strip()

        if not schedules and not todos:
            QMessageBox.information(
                self,
                "추출 결과 없음",
                "AI 응답에서 등록할 일정이나 할일을 찾지 못했습니다.",
            )
            return

        dlg = ScheduleImportPreviewDialog(
            parsed_items=schedules,
            file_name="AI 자동 추출",
            parent=self,
            parsed_todos=todos,
            summary=summary,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        sel_schedules = dlg.get_selected_schedules()
        sel_todos = dlg.get_selected_todos()
        added_sch = self.db.bulk_add_schedules(sel_schedules, is_read=1) if sel_schedules else 0
        skipped_sch = max(0, len(sel_schedules) - added_sch)
        added_todo = 0
        for td in sel_todos:
            self.db.add_todo(
                title=td["title"],
                due_date=td.get("due_date", ""),
                priority=td.get("priority", "normal"),
                memo=td.get("memo", ""),
                is_read=1,
            )
            added_todo += 1

        self.refresh_all()
        self.unread_changed.emit()
        done_msg = f"선택하신 일정 {added_sch}건, 할일 {added_todo}건이 등록되었습니다."
        if skipped_sch > 0:
            done_msg += f"\n(동일 일정 중복 {skipped_sch}건 건너뜀)"
        QMessageBox.information(self, "AI 일정·할일 등록 완료", done_msg)

    # =========================================================================
    # Google Sheets 동기화 및 1분 주기 알림 타이머
    # =========================================================================
    def sync_with_google_sheets(self, interactive: bool = True) -> None:
        gas_url = str(self.config.get("api_and_integrations", "google_apps_script_url", "") or "").strip()
        if not gas_url:
            if interactive:
                QMessageBox.information(
                    self,
                    "Google Apps Script URL 미설정",
                    "먼저 [환경설정 & 명렬표] -> [외부 연동]에서\nGoogle Apps Script 웹 앱 URL을 입력하고 저장해 주세요.",
                )
            return

        self.btn_sync_sheets.setEnabled(False)
        self.btn_sync_sheets.setText("동기화 중...")
        worker = GoogleSheetsSyncWorker(sync_service=self.sync_service)
        self.task_runner.start_worker(
            worker,
            task_key=self.SYNC_TASK_KEY,
            on_result=lambda res: self._on_sync_finished(res, interactive=interactive),
            on_error=lambda err: self._on_sync_error(err, interactive=interactive),
            on_finished=self._restore_sync_button,
        )

    def _restore_sync_button(self) -> None:
        self.btn_sync_sheets.setEnabled(True)
        self.btn_sync_sheets.setText("Google Sheets 동기화")

    def _on_sync_finished(self, res: Dict[str, Any], interactive: bool = False) -> None:
        if not res.get("ok"):
            if interactive and not res.get("skipped"):
                QMessageBox.warning(self, "동기화 실패", str(res.get("error", "동기화에 실패했습니다.")))
            return

        if res.get("mobile_tasks_added", 0) > 0:
            self._banner_dismissed = False
        self.refresh_all()
        self.unread_changed.emit()
        self.sync_completed.emit(res)

        if interactive:
            QMessageBox.information(
                self,
                "Google Sheets 동기화 완료",
                (
                    f"일정 +{res.get('schedules_added', 0)}건 · "
                    f"할일 +{res.get('todos_added', 0)}건 · "
                    f"모바일 예약 +{res.get('mobile_tasks_added', 0)}건 · "
                    f"외부 알림 +{res.get('notifications_added', 0)}건 반영됨\n"
                    f"(현재 미처리 모바일 예약 작업: {len(res.get('pending_mobile_tasks', []))}건)"
                ),
            )

    def _on_sync_error(self, err_msg: str, interactive: bool = False) -> None:
        if interactive:
            QMessageBox.warning(self, "동기화 오류", err_msg.splitlines()[0])

    def check_reminders_now(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        triggered = ScheduleReminderChecker.check_and_trigger_reminders(
            db=self.db,
            notifier=self.notifier,
            now=now,
        )
        if triggered:
            self.unread_changed.emit()
        return triggered
