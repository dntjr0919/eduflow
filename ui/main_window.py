"""
메인 윈도우 모듈 (ui/main_window.py).
- QTabWidget을 완전히 제거하고 QHBoxLayout 기반의
  [좌측 다크 사이드바(QListWidget)] + [우측 밝은 메인 컨텐츠(QStackedWidget)] 구조로 구현
- 사이드바 상단 햄버거 토글 버튼([ ☰ ]) 및 QPropertyAnimation 기반 부드러운 접기/펼치기 지원
- 모든 프레임/위젯에 setObjectName(), WA_StyledBackground, style().polish()를 명시적으로 적용하여
  ui/styles/theme.qss가 100% 렌더링되도록 보장
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import (
    QByteArray,
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRectF,
    QSize,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import TaskRunner
from core.config_manager import ConfigManager, DEFAULT_CONFIG
from core.database import DatabaseManager
from services.firestore_sync_service import (
    FirestoreSyncService,
    FirestoreSyncWorker,
)
from services.notifier import NotificationService
from services.record_generator_service import get_active_ai_info

# 독립된 각 기능 탭(페이지) 위젯 및 우측 AI 도크 import
from ui.components.ai_execution_dock import AiExecutionDock
from ui.components.sidebar_nav_delegate import SidebarNavDelegate
from ui.tabs.tab_calendar_todo import CalendarTodoTab
from ui.tabs.tab_classroom_tools import ClassroomToolsTab
from ui.tabs.tab_db_manager import DbManagerTab
from ui.tabs.tab_doc_ocr import DocOcrTab
from ui.tabs.tab_file_sorter import FileSorterTab
from ui.tabs.tab_grade_material import GradeMaterialTab
from ui.tabs.tab_notification import NotificationCenterTab
from PyQt6.QtGui import QGuiApplication

from ui.tabs.tab_settings import SettingsTab
from ui.utils import create_eduflow_app_icon, load_svg_icon, load_svg_pixmap
from utils.path_helper import get_active_workspace_dir, get_resource_path


def polish_widget(widget: QWidget, role: str = "", obj_name: str = "") -> None:
    """위젯에 objectName 및 role 프로퍼티를 부여하고 즉시 QSS 스타일을 강제 재렌더링합니다."""
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    if obj_name:
        widget.setObjectName(obj_name)
    if role:
        widget.setProperty("role", role)
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


class MainWindow(QMainWindow):
    """확장형 교무/업무 자동화 데스크톱 워크스테이션 메인 윈도우."""

    SIDEBAR_EXPANDED_WIDTH = 248
    SIDEBAR_COLLAPSED_WIDTH = 64

    _sig_config_changed = pyqtSignal()

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
        enable_startup_sync: bool = True,
    ):
        super().__init__()
        self.setWindowTitle("EduFlow — Teacher Workstation")
        app_icon = create_eduflow_app_icon()
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)
            if QGuiApplication.instance() is not None:
                QGuiApplication.setWindowIcon(app_icon)
        self.resize(1380, 860)
        self.setMinimumSize(1040, 680)

        self.config = config or ConfigManager()
        self.db = db or DatabaseManager()
        self.task_runner = TaskRunner(self)
        self._active_worker_count: int = 0
        self.notifier = NotificationService()
        self._force_quit = False
        self._sidebar_collapsed = False
        self._ai_dock_expanded = False
        self._page_meta: List[Dict[str, str]] = []
        self._shortcuts: Dict[str, QShortcut] = {}
        self._notify_nav_row: int = 4
        self._calendar_nav_row: int = 5
        self._enable_startup_sync = bool(enable_startup_sync)
        self._last_cfg_manual_mode: bool = bool(
            self.config.get_section("api_and_integrations").get("use_manual_prompt_mode", True)
        )
        self._ai_status_dot_color: str = "#3B82F6"

        self._init_ui()
        if not self._last_cfg_manual_mode:
            self.ai_dock.set_mode_index(0)
        self.ai_dock.ai_mode_changed.connect(self._update_ai_mode_badge)
        self.ai_dock.combo_pw_exec_mode.currentIndexChanged.connect(
            lambda _idx: self._update_ai_mode_badge()
        )
        self.ai_dock.edit_pw_custom_url.textChanged.connect(
            lambda _text: self._update_ai_mode_badge()
        )
        self._init_tray_icon()
        self._connect_global_worker_signals()
        self._register_all_tabs()
        self._init_shortcuts()
        self._refresh_shortcuts_from_config()
        self._update_ai_mode_badge()

        self._sig_config_changed.connect(self._on_config_changed)
        self.config.add_listener(lambda _: self._sig_config_changed.emit())

        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_app_focus_changed)

        self.append_log("INFO", "EduFlow Workstation initialized.")
        if self._enable_startup_sync:
            QTimer.singleShot(600, self.run_startup_sheets_sync)

    # =========================================================================
    # UI 레이아웃 초기화 (QHBoxLayout: 좌측 다크 사이드바 + 우측 QStackedWidget)
    # =========================================================================
    def _init_ui(self) -> None:
        central = QWidget(self)
        polish_widget(central, obj_name="CentralRoot")
        self.setCentralWidget(central)

        outer_layout = QHBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # ---------------------------------------------------------------------
        # 1. 좌측 다크 사이드바 (SidebarContainer + 접기/펼치기 토글 애니메이션)
        # ---------------------------------------------------------------------
        self.sidebar_frame = QFrame(central)
        polish_widget(self.sidebar_frame, obj_name="SidebarContainer")
        self.sidebar_frame.setMinimumWidth(self.SIDEBAR_EXPANDED_WIDTH)
        self.sidebar_frame.setMaximumWidth(self.SIDEBAR_EXPANDED_WIDTH)

        self.sidebar_layout = QVBoxLayout(self.sidebar_frame)
        self.sidebar_layout.setContentsMargins(10, 18, 10, 16)
        self.sidebar_layout.setSpacing(6)

        # 사이드바 상단: 32x32 학사모 로고 토글 버튼 + EduFlow/Teacher Workstation
        self.brand_header_frame = QFrame()
        polish_widget(self.brand_header_frame, obj_name="SidebarBrandHeader")
        self.brand_top_row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.brand_header_frame)
        self.brand_top_row.setContentsMargins(0, 0, 0, 0)
        self.brand_top_row.setSpacing(10)

        self.brand_logo_badge = QPushButton()
        polish_widget(self.brand_logo_badge, obj_name="SidebarBrandLogoBadge")
        self.brand_logo_badge.setFixedSize(32, 32)
        self.brand_logo_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.brand_logo_badge.setToolTip("사이드바 접기 / 펼치기")
        cap_pix = load_svg_pixmap("graduation-cap", "#FFFFFF", 18)
        if not cap_pix.isNull():
            self.brand_logo_badge.setIcon(QIcon(cap_pix))
            self.brand_logo_badge.setIconSize(QSize(18, 18))
        self.brand_logo_badge.clicked.connect(self.toggle_sidebar)

        self.brand_text_container = QWidget()
        self.brand_text_layout = QVBoxLayout(self.brand_text_container)
        self.brand_text_layout.setContentsMargins(0, 0, 0, 0)
        self.brand_text_layout.setSpacing(1)

        self.brand_title = QLabel("EduFlow")
        polish_widget(self.brand_title, obj_name="SidebarBrandTitle")

        self.brand_sub = QLabel("Teacher Workstation")
        polish_widget(self.brand_sub, obj_name="SidebarBrandSubtitle")

        self.brand_text_layout.addWidget(self.brand_title)
        self.brand_text_layout.addWidget(self.brand_sub)

        self.brand_top_row.addWidget(
            self.brand_logo_badge, alignment=Qt.AlignmentFlag.AlignVCenter
        )
        self.brand_top_row.addWidget(self.brand_text_container, stretch=1)

        self.nav_section_lbl = QLabel("NAVIGATION")
        polish_widget(self.nav_section_lbl, obj_name="SidebarSectionLabel")

        self.sidebar_nav = QListWidget(self.sidebar_frame)
        polish_widget(self.sidebar_nav, obj_name="SidebarNavList")
        self.sidebar_nav.setSpacing(4)
        self.sidebar_nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_nav.setMouseTracking(True)
        self.sidebar_nav.setUniformItemSizes(True)
        self._sidebar_nav_delegate = SidebarNavDelegate(self.sidebar_nav)
        self.sidebar_nav.setItemDelegate(self._sidebar_nav_delegate)
        self.sidebar_nav.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_nav.currentRowChanged.connect(self._on_sidebar_nav_changed)

        self.sidebar_util_container = QWidget()
        self.sidebar_util_container.setObjectName("SidebarUtilContainer")
        self.sidebar_util_layout = QBoxLayout(
            QBoxLayout.Direction.LeftToRight, self.sidebar_util_container
        )
        self.sidebar_util_layout.setContentsMargins(0, 4, 0, 0)
        self.sidebar_util_layout.setSpacing(6)
        self.sidebar_util_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.btn_open_folder = QPushButton("EduFlow 폴더 열기")
        polish_widget(self.btn_open_folder, obj_name="SidebarFolderButton")
        self.btn_open_folder.setIcon(load_svg_icon("folder", "#60A5FA", 14))
        self.btn_open_folder.setIconSize(QSize(14, 14))
        self.btn_open_folder.setFixedHeight(36)
        self.btn_open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_folder.setToolTip("EduFlow 작업 루트(또는 출력) 폴더를 탐색기로 엽니다.")
        self.btn_open_folder.clicked.connect(self.open_eduflow_folder)

        self.btn_hide_tray = QPushButton("")
        polish_widget(self.btn_hide_tray, obj_name="SidebarTrayIconBtn")
        self.btn_hide_tray.setIcon(load_svg_icon("chevron-down", "#94A3B8", 16))
        self.btn_hide_tray.setIconSize(QSize(16, 16))
        self.btn_hide_tray.setFixedSize(36, 36)
        self.btn_hide_tray.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hide_tray.setToolTip("시스템 트레이로 최소화 (백그라운드 알림 유지)")
        self.btn_hide_tray.clicked.connect(self.hide_to_system_tray)

        self.sidebar_util_layout.addWidget(self.btn_open_folder, stretch=1)
        self.sidebar_util_layout.addWidget(self.btn_hide_tray)

        self.sidebar_ai_badge = QPushButton("")
        polish_widget(self.sidebar_ai_badge, obj_name="SidebarAiStatusBadge")
        self.sidebar_ai_badge.setFixedHeight(36)
        self.sidebar_ai_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_ai_badge.clicked.connect(lambda: self.toggle_ai_dock())

        self.sidebar_ai_badge_layout = QHBoxLayout(self.sidebar_ai_badge)
        self.sidebar_ai_badge_layout.setContentsMargins(10, 0, 10, 0)
        self.sidebar_ai_badge_layout.setSpacing(8)
        self.sidebar_ai_badge_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.ai_status_dot = QLabel()
        polish_widget(self.ai_status_dot, obj_name="SidebarAiStatusDot")
        self.ai_status_dot.setFixedSize(8, 8)
        self.ai_status_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.lbl_ai_badge = QLabel("AI: 수동 복붙 · Gemini")
        polish_widget(self.lbl_ai_badge, obj_name="SidebarAiStatusText")
        self.lbl_ai_badge.setMinimumWidth(0)
        self.lbl_ai_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.sidebar_ai_badge_layout.addWidget(
            self.ai_status_dot, alignment=Qt.AlignmentFlag.AlignVCenter
        )
        self.sidebar_ai_badge_layout.addWidget(self.lbl_ai_badge, stretch=1)

        self.sidebar_layout.addWidget(self.brand_header_frame)
        self.sidebar_layout.addSpacing(8)
        self.sidebar_layout.addWidget(self.nav_section_lbl)
        self.sidebar_layout.addWidget(self.sidebar_nav, stretch=1)
        self.sidebar_layout.addWidget(self.sidebar_ai_badge)
        self.sidebar_layout.addWidget(self.sidebar_util_container)

        # 사이드바 너비 애니메이션 그룹 (minimumWidth & maximumWidth 동시 제어)
        self._sidebar_anim_group = QParallelAnimationGroup(self)
        self._anim_min_w = QPropertyAnimation(self.sidebar_frame, b"minimumWidth", self)
        self._anim_max_w = QPropertyAnimation(self.sidebar_frame, b"maximumWidth", self)
        for anim in (self._anim_min_w, self._anim_max_w):
            anim.setDuration(220)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._sidebar_anim_group.addAnimation(anim)

        outer_layout.addWidget(self.sidebar_frame)

        # ---------------------------------------------------------------------
        # 2. 우측 밝은 메인 컨텐츠 영역 (WorkspaceContainer + QStackedWidget)
        # ---------------------------------------------------------------------
        self.workspace_frame = QFrame(central)
        polish_widget(self.workspace_frame, obj_name="WorkspaceContainer")
        self.workspace_frame.setMinimumWidth(420)

        workspace_layout = QVBoxLayout(self.workspace_frame)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)

        # 2-1) 상단 헤더 바 (TopHeaderBar)
        top_header = QFrame(self.workspace_frame)
        polish_widget(top_header, obj_name="TopHeaderBar")
        top_header_layout = QHBoxLayout(top_header)
        top_header_layout.setContentsMargins(24, 14, 24, 14)
        top_header_layout.setSpacing(12)

        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        self.lbl_page_title = QLabel("스마트 파일 분류기")
        polish_widget(self.lbl_page_title, obj_name="PageHeaderTitle")
        self.lbl_page_title.setMinimumWidth(0)

        self.lbl_page_desc = QLabel("원본 폴더 감시 및 키워드/확장자 기반 자동 폴더 생성·분류·실행취소")
        polish_widget(self.lbl_page_desc, obj_name="PageHeaderDesc")
        self.lbl_page_desc.setMinimumWidth(0)
        self.lbl_page_desc.setWordWrap(True)

        title_col.addWidget(self.lbl_page_title)
        title_col.addWidget(self.lbl_page_desc)

        self.btn_toggle_ai_dock = QPushButton("AI 실행 패널")
        polish_widget(self.btn_toggle_ai_dock, obj_name="AiDockToggleBtn")
        self.btn_toggle_ai_dock.setCheckable(True)
        self.btn_toggle_ai_dock.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_ai_dock.setToolTip("우측 AI 실행 사이드바 열기/닫기")
        self.btn_toggle_ai_dock.clicked.connect(lambda checked: self.toggle_ai_dock(checked))

        top_header_layout.addLayout(title_col, stretch=1)
        top_header_layout.addWidget(self.btn_toggle_ai_dock)

        workspace_layout.addWidget(top_header)

        # 2-2) 중앙 메인 페이지 스택 (MainStackedWidget)
        self.stacked_widget = QStackedWidget(self.workspace_frame)
        polish_widget(self.stacked_widget, obj_name="MainStackedWidget")
        workspace_layout.addWidget(self.stacked_widget, stretch=1)

        # 2-3) 접이식 다크 실시간 로그 콘솔 서랍 (LogConsoleDrawer)
        self.log_drawer = QFrame(self.workspace_frame)
        polish_widget(self.log_drawer, obj_name="LogConsoleDrawer")
        self.log_drawer.setMaximumHeight(0)  # 기본 상태: 닫혀 있음

        drawer_layout = QVBoxLayout(self.log_drawer)
        drawer_layout.setContentsMargins(18, 10, 18, 10)
        drawer_layout.setSpacing(6)

        drawer_header = QHBoxLayout()
        drawer_title = QLabel("실시간 백그라운드 작업 로그 콘솔")
        drawer_title.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: 600;")
        btn_clear_log = QPushButton("로그 비우기")
        polish_widget(btn_clear_log, role="slate")
        btn_clear_log.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear_log.clicked.connect(self._on_clear_logs)
        drawer_header.addWidget(drawer_title)
        drawer_header.addStretch()
        drawer_header.addWidget(btn_clear_log)

        self.log_console = QTextEdit(self.log_drawer)
        polish_widget(self.log_console, obj_name="DarkLogConsole")
        self.log_console.setReadOnly(True)

        drawer_layout.addLayout(drawer_header)
        drawer_layout.addWidget(self.log_console)
        workspace_layout.addWidget(self.log_drawer)

        # 서랍 슬라이드 애니메이션
        self._drawer_anim = QPropertyAnimation(self.log_drawer, b"maximumHeight", self)
        self._drawer_anim.setDuration(220)
        self._drawer_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # 2-4) 최하단 슬림 상태 표시줄 (BottomStatusContainer)
        bottom_bar = QFrame(self.workspace_frame)
        polish_widget(bottom_bar, obj_name="BottomStatusContainer")
        status_layout = QHBoxLayout(bottom_bar)
        status_layout.setContentsMargins(20, 8, 20, 8)
        status_layout.setSpacing(12)

        self.lbl_current_task = QLabel("대기 중")
        polish_widget(self.lbl_current_task, obj_name="StatusTaskBadge")

        self.lbl_single_line_log = QLabel("최근 작업 상태가 실시간으로 표시됩니다.")
        polish_widget(self.lbl_single_line_log, obj_name="StatusSingleLineLog")
        self.lbl_single_line_log.setMinimumWidth(0)
        self.lbl_single_line_log.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self.progress_bar = QProgressBar(bottom_bar)
        polish_widget(self.progress_bar, obj_name="ModernProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMinimumWidth(120)
        self.progress_bar.setMaximumWidth(210)
        self.progress_bar.setTextVisible(False)

        self.btn_toggle_log = QPushButton("로그 콘솔 열기")
        polish_widget(self.btn_toggle_log)
        self.btn_toggle_log.setCheckable(True)
        self.btn_toggle_log.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_log.toggled.connect(self._on_toggle_log_console)

        status_layout.addWidget(self.lbl_current_task)
        status_layout.addWidget(self.lbl_single_line_log, stretch=1)
        status_layout.addWidget(self.progress_bar)
        status_layout.addWidget(self.btn_toggle_log)

        workspace_layout.addWidget(bottom_bar)
        outer_layout.addWidget(self.workspace_frame, stretch=1)

        # ---------------------------------------------------------------------
        # 3. 우측 접이식 AI 실행 사이드바 (AiExecutionDock)
        # ---------------------------------------------------------------------
        self.ai_dock = AiExecutionDock(task_runner=self.task_runner, parent=central)
        self.ai_dock.setMinimumWidth(0)
        self.ai_dock.setMaximumWidth(0)
        self.ai_dock.hide()
        outer_layout.addWidget(self.ai_dock)

        self._ai_dock_anim_group = QParallelAnimationGroup(self)
        self._ai_dock_anim_min_w = QPropertyAnimation(self.ai_dock, b"minimumWidth", self)
        self._ai_dock_anim_max_w = QPropertyAnimation(self.ai_dock, b"maximumWidth", self)
        for anim in (self._ai_dock_anim_min_w, self._ai_dock_anim_max_w):
            anim.setDuration(220)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._ai_dock_anim_group.addAnimation(anim)
        self._ai_dock_anim_group.finished.connect(self._on_ai_dock_anim_finished)

    # =========================================================================
    # 좌측 다크 사이드바 접기/펼치기 토글 (Collapsible Sidebar - 독립 제어)
    # =========================================================================
    def toggle_sidebar(self) -> None:
        self.set_sidebar_collapsed(not self._sidebar_collapsed)

    def set_sidebar_collapsed(self, collapsed: bool) -> None:
        target_collapsed = bool(collapsed)
        if target_collapsed == self._sidebar_collapsed:
            return
        self._sidebar_collapsed = target_collapsed
        current_w = self.sidebar_frame.width()
        target_w = (
            self.SIDEBAR_COLLAPSED_WIDTH
            if self._sidebar_collapsed
            else self.SIDEBAR_EXPANDED_WIDTH
        )

        self._sidebar_anim_group.stop()
        self._anim_min_w.setStartValue(current_w)
        self._anim_min_w.setEndValue(target_w)
        self._anim_max_w.setStartValue(current_w)
        self._anim_max_w.setEndValue(target_w)

        collapsed_str = "true" if self._sidebar_collapsed else "false"
        self.sidebar_frame.setProperty("collapsed", collapsed_str)
        self.sidebar_nav.setProperty("collapsed", collapsed_str)
        self.sidebar_ai_badge.setProperty("collapsed", collapsed_str)
        self.btn_open_folder.setProperty("collapsed", collapsed_str)
        self.btn_hide_tray.setProperty("collapsed", collapsed_str)
        self._sidebar_nav_delegate.set_collapsed(self._sidebar_collapsed)

        if self._sidebar_collapsed:
            self.sidebar_layout.setContentsMargins(8, 18, 8, 16)
            self.brand_top_row.setDirection(QBoxLayout.Direction.TopToBottom)
            self.brand_top_row.setSpacing(6)
            self.brand_top_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.sidebar_util_layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self.sidebar_util_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.brand_text_container.hide()
            self.brand_title.hide()
            self.brand_sub.hide()
            self.nav_section_lbl.hide()
            self.brand_logo_badge.setToolTip("사이드바 메뉴 펼치기")
            self.lbl_ai_badge.hide()
            self.sidebar_ai_badge_layout.setContentsMargins(0, 0, 0, 0)
            self.sidebar_ai_badge_layout.setSpacing(0)
            self.sidebar_ai_badge_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.sidebar_ai_badge_layout.setAlignment(
                self.ai_status_dot, Qt.AlignmentFlag.AlignCenter
            )
            self.sidebar_ai_badge.setFixedSize(36, 36)
            self.sidebar_layout.setAlignment(self.sidebar_ai_badge, Qt.AlignmentFlag.AlignHCenter)
            self.btn_open_folder.setText("")
            self.btn_open_folder.setFixedSize(36, 36)
            self.btn_open_folder.setToolTip("EduFlow 폴더 열기")
            self.btn_hide_tray.setText("")
            self.btn_hide_tray.setFixedSize(36, 36)
            self.btn_hide_tray.setToolTip("시스템 트레이로 최소화 (백그라운드 알림 유지)")
            for idx, meta in enumerate(self._page_meta):
                item = self.sidebar_nav.item(idx)
                if item is not None:
                    item.setText(meta["title"])
                    item.setToolTip(
                        f"<div style='padding:2px 4px;'><b>{meta['title']}</b><br/>"
                        f"<span style='color:#cbd5e1;'>{meta['desc']}</span></div>"
                    )
        else:
            self.sidebar_layout.setContentsMargins(10, 18, 10, 16)
            self.brand_top_row.setDirection(QBoxLayout.Direction.LeftToRight)
            self.brand_top_row.setSpacing(10)
            self.brand_top_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.sidebar_util_layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self.sidebar_util_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.brand_text_container.show()
            self.brand_title.show()
            self.brand_sub.show()
            self.nav_section_lbl.show()
            self.brand_logo_badge.setToolTip("사이드바 메뉴 접기")
            self.lbl_ai_badge.show()
            self.sidebar_ai_badge_layout.setContentsMargins(10, 0, 10, 0)
            self.sidebar_ai_badge_layout.setSpacing(8)
            self.sidebar_ai_badge_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.sidebar_ai_badge_layout.setAlignment(
                self.ai_status_dot, Qt.AlignmentFlag.AlignVCenter
            )
            self.sidebar_ai_badge.setMinimumWidth(0)
            self.sidebar_ai_badge.setMaximumWidth(16777215)
            self.sidebar_ai_badge.setFixedHeight(36)
            self.sidebar_layout.setAlignment(self.sidebar_ai_badge, Qt.AlignmentFlag(0))
            self.btn_open_folder.setMinimumWidth(0)
            self.btn_open_folder.setMaximumWidth(16777215)
            self.btn_open_folder.setFixedHeight(36)
            self.btn_open_folder.setText("EduFlow 폴더 열기")
            self.btn_open_folder.setToolTip("EduFlow 작업 루트(또는 출력) 폴더를 탐색기로 엽니다.")
            self.btn_hide_tray.setText("")
            self.btn_hide_tray.setFixedSize(36, 36)
            self.btn_hide_tray.setToolTip("시스템 트레이로 최소화 (백그라운드 알림 유지)")
            for idx, meta in enumerate(self._page_meta):
                item = self.sidebar_nav.item(idx)
                if item is not None:
                    item.setText(meta["title"])
                    item.setToolTip(
                        f"<div style='padding:2px 4px;'><b>{meta['title']}</b><br/>"
                        f"<span style='color:#cbd5e1;'>{meta['desc']}</span></div>"
                    )

        for w in (
            self.sidebar_frame,
            self.sidebar_nav,
            self.sidebar_ai_badge,
            self.btn_open_folder,
            self.btn_hide_tray,
        ):
            st = w.style()
            if st is not None:
                st.unpolish(w)
                st.polish(w)

        self.sidebar_nav.viewport().update()

        if hasattr(self, "_shortcuts") and self._shortcuts:
            self._update_shortcut_tooltips()

        self._sidebar_anim_group.start()

    # =========================================================================
    # 우측 AI 실행 사이드바 접기/펼치기 (좌측 메인 사이드바와 완전 독립 동작)
    # =========================================================================
    def toggle_ai_dock(self, expanded: Optional[bool] = None) -> None:
        target_expanded = (not self._ai_dock_expanded) if expanded is None else bool(expanded)
        self._ai_dock_expanded = target_expanded

        self.btn_toggle_ai_dock.blockSignals(True)
        self.btn_toggle_ai_dock.setChecked(target_expanded)
        self.btn_toggle_ai_dock.blockSignals(False)

        if target_expanded:
            self.ai_dock.show()
            self.ai_dock.sync_from_context()

        current_w = self.ai_dock.width() if self.ai_dock.isVisible() else 0
        target_w = AiExecutionDock.EXPANDED_WIDTH if target_expanded else 0

        self._ai_dock_anim_group.stop()
        self._ai_dock_anim_min_w.setStartValue(current_w)
        self._ai_dock_anim_min_w.setEndValue(target_w)
        self._ai_dock_anim_max_w.setStartValue(current_w)
        self._ai_dock_anim_max_w.setEndValue(target_w)
        self._ai_dock_anim_group.start()

    def _on_ai_dock_anim_finished(self) -> None:
        if not self._ai_dock_expanded:
            self.ai_dock.hide()

    def switch_to_settings_tab(self) -> None:
        idx = self.stacked_widget.indexOf(self.tab_settings)
        if idx >= 0:
            self.sidebar_nav.setCurrentRow(idx)

    def _create_sidebar_svg_icon(
        self, icon_name: str, color_hex: str = "#CBD5E1", size: int = 16
    ) -> QIcon:
        """단색 SVG 아이콘을 HiDPI QPixmap으로 렌더링해 QIcon으로 반환합니다."""
        candidates = [
            Path(get_resource_path(os.path.join("ui", "assets", "icons", f"{icon_name}.svg"))),
            Path(__file__).resolve().parent / "assets" / "icons" / f"{icon_name}.svg",
        ]
        svg_path = next((p for p in candidates if p.is_file()), None)
        if svg_path is None:
            return QIcon()
        try:
            svg_text = svg_path.read_text(encoding="utf-8").replace("currentColor", color_hex)
            renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
            if not renderer.isValid():
                return QIcon()
            scale = 2
            pixmap = QPixmap(size * scale, size * scale)
            pixmap.fill(QColor(0, 0, 0, 0))
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
            painter.end()
            pixmap.setDevicePixelRatio(float(scale))
            return QIcon(pixmap)
        except Exception:
            return QIcon()

    def _on_config_changed(self) -> None:
        sec = self.config.get_section("api_and_integrations")
        cfg_manual = bool(sec.get("use_manual_prompt_mode", True))
        if cfg_manual != self._last_cfg_manual_mode:
            self._last_cfg_manual_mode = cfg_manual
            self.ai_dock.set_mode_index(1 if cfg_manual else 0)
        self.ai_dock.refresh_ai_info()
        self._refresh_shortcuts_from_config()
        self._update_ai_mode_badge()
        if hasattr(self, "tab_doc_ocr"):
            self.tab_doc_ocr._update_active_ai_display()

    # =========================================================================
    # 전역 단축키(WindowShortcut) 등록 및 실시간 동기화
    # =========================================================================
    def _init_shortcuts(self) -> None:
        action_map = {
            "toggle_ai_dock": lambda: self.toggle_ai_dock(),
            "toggle_left_sidebar": self.toggle_sidebar,
            "toggle_ai_target_lock": self._on_shortcut_toggle_ai_lock,
            "switch_ai_mode_api": lambda: self._on_shortcut_switch_ai_mode(0),
            "switch_ai_mode_manual": lambda: self._on_shortcut_switch_ai_mode(1),
            "switch_ai_mode_playwright": lambda: self._on_shortcut_switch_ai_mode(2),
            "toggle_log_drawer": self.btn_toggle_log.toggle,
        }
        for key, handler in action_map.items():
            sc = QShortcut(QKeySequence(), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(handler)
            self._shortcuts[key] = sc
        self._shortcut_ai_dock = self._shortcuts["toggle_ai_dock"]

    def _on_shortcut_toggle_ai_lock(self) -> None:
        self.ai_dock.btn_lock_target.toggle()

    def _on_shortcut_switch_ai_mode(self, mode_index: int) -> None:
        if not self._ai_dock_expanded:
            self.toggle_ai_dock(True)
        self.ai_dock.set_mode_index(mode_index)

    def _get_shortcut_str(self, key: str) -> str:
        sec = self.config.get_section("shortcuts")
        if isinstance(sec, dict) and key in sec:
            return str(sec.get(key) or "").strip()
        return str(DEFAULT_CONFIG.get("shortcuts", {}).get(key, "")).strip()

    @staticmethod
    def _format_tooltip(base: str, seq_str: str) -> str:
        return f"{base} ({seq_str})" if seq_str else base

    def _update_shortcut_tooltips(self) -> None:
        dock_seq = self._get_shortcut_str("toggle_ai_dock")
        self.btn_toggle_ai_dock.setToolTip(
            self._format_tooltip("우측 AI 실행 사이드바 열기/닫기", dock_seq)
        )

        sidebar_seq = self._get_shortcut_str("toggle_left_sidebar")
        sidebar_base = "사이드바 메뉴 펼치기" if self._sidebar_collapsed else "사이드바 메뉴 접기"
        self.brand_logo_badge.setToolTip(self._format_tooltip(sidebar_base, sidebar_seq))

        lock_seq = self._get_shortcut_str("toggle_ai_target_lock")
        self.ai_dock.btn_lock_target.setToolTip(
            self._format_tooltip(
                "켜면 중앙 카드를 클릭해도 활성 AI 타겟이 전환되지 않습니다.", lock_seq
            )
        )

        self.ai_dock.btn_mode_api.setToolTip(
            self._format_tooltip("API 모드로 전환", self._get_shortcut_str("switch_ai_mode_api"))
        )
        self.ai_dock.btn_mode_manual.setToolTip(
            self._format_tooltip("수동 복붙 모드로 전환", self._get_shortcut_str("switch_ai_mode_manual"))
        )
        self.ai_dock.btn_mode_playwright.setToolTip(
            self._format_tooltip(
                "Playwright 자동 모드로 전환", self._get_shortcut_str("switch_ai_mode_playwright")
            )
        )

        log_seq = self._get_shortcut_str("toggle_log_drawer")
        self.btn_toggle_log.setToolTip(
            self._format_tooltip("하단 로그 콘솔 서랍 열기/닫기", log_seq)
        )

    def _refresh_shortcuts_from_config(self) -> None:
        for key, sc in self._shortcuts.items():
            seq_str = self._get_shortcut_str(key)
            sc.setKey(QKeySequence(seq_str))
            sc.setEnabled(bool(seq_str))
        self._update_shortcut_tooltips()

    def _on_central_ai_target_requested(self, target_id: str) -> None:
        if self.ai_dock.is_target_locked():
            return
        self.ai_dock.on_central_target_clicked(target_id)

    def _on_calendar_hwp_ai_requested(self, prompt_text: str, _file_name: str = "") -> None:
        self.ai_dock.set_target("calendar_todo", force=True)
        self.ai_dock.txt_calendar_instruction.setPlainText(prompt_text)
        if not self._ai_dock_expanded:
            self.toggle_ai_dock(True)
        if self.ai_dock.current_mode_index() == 1:
            self.ai_dock.prepare_manual_prompt_for_target("calendar_todo")
        elif self.ai_dock.current_mode_index() == 2:
            self.ai_dock._on_pw_start_clicked()

    def _on_app_focus_changed(self, _old: Optional[QWidget], new: Optional[QWidget]) -> None:
        if new is None:
            return
        if new is self.ai_dock or self.ai_dock.isAncestorOf(new):
            return
        if self.ai_dock.is_target_locked():
            return
        curr: Optional[QWidget] = new
        while curr is not None:
            tid = curr.property("_ai_target_id")
            if isinstance(tid, str) and tid:
                self.ai_dock.on_central_target_clicked(tid)
                break
            curr = curr.parentWidget()

    # =========================================================================
    # 모듈형 페이지 등록 (좌측 QListWidget + 우측 QStackedWidget 동시 등록)
    # =========================================================================
    def register_tab(
        self,
        widget: QWidget,
        title: str,
        tooltip: str = "",
        short_code: str = "",
        icon_name: str = "",
    ) -> int:
        polish_widget(widget)
        idx = self.stacked_widget.addWidget(widget)
        short = short_code or (title[:2] if len(title) >= 2 else title)

        item = QListWidgetItem(title)
        item.setData(SidebarNavDelegate.ICON_NAME_ROLE, icon_name)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, title.strip())
        rich_tip = (
            f"<div style='padding:2px 4px;'><b>{title.strip()}</b><br/>"
            f"<span style='color:#cbd5e1;'>{tooltip}</span></div>"
            if tooltip
            else title.strip()
        )
        item.setToolTip(rich_tip)
        self.sidebar_nav.addItem(item)
        self._page_meta.append(
            {"title": title, "short": short, "desc": tooltip, "icon": icon_name}
        )
        return idx

    def _register_all_tabs(self) -> None:
        self.tab_sorter = FileSorterTab(task_runner=self.task_runner, parent=self)
        self.tab_doc_ocr = DocOcrTab(task_runner=self.task_runner, parent=self)
        self.tab_grade = GradeMaterialTab(task_runner=self.task_runner, parent=self)
        self.tab_classroom = ClassroomToolsTab(task_runner=self.task_runner, parent=self)
        self.tab_notify = NotificationCenterTab(
            task_runner=self.task_runner,
            parent=self,
            db=self.db,
            config=self.config,
            notifier=self.notifier,
        )
        self.tab_calendar = CalendarTodoTab(
            task_runner=self.task_runner,
            parent=self,
            db=self.db,
            config=self.config,
            notifier=self.notifier,
        )
        self.tab_db_manager = DbManagerTab(
            task_runner=self.task_runner,
            parent=self,
            db=self.db,
            config=self.config,
        )
        self.tab_settings = SettingsTab(parent=self, db=self.db, config=self.config)

        self.register_tab(
            self.tab_sorter,
            "스마트 파일 자동 분류",
            "원본 폴더 실시간 감시 및 키워드/확장자 기반 자동 폴더 생성·분류·실행취소",
            short_code="FS",
            icon_name="folder-input",
        )
        self.register_tab(
            self.tab_doc_ocr,
            "문서 처리 & AI 과제 파이프라인",
            "학번/이름 기준 제출 파일 병합 · 미제출자 대조 · 멀티 AI OCR 및 DOCX 과제 평가",
            short_code="DOC",
            icon_name="file-text",
        )
        self.register_tab(
            self.tab_grade,
            "성적·수업자료 관리",
            "학생 명렬표 DB 연동 지필/수행평가 성적 관리 및 수업 자료 버전 관리",
            short_code="GRD",
            icon_name="chart-column",
        )
        self.register_tab(
            self.tab_classroom,
            "학급 경영 도구함",
            "학급 출결 서류 체크, 청소 당번/1인1역 랜덤 추첨기, 자리 배치 등 미니 툴",
            short_code="CLS",
            icon_name="users",
        )
        self._notify_nav_row = self.register_tab(
            self.tab_notify,
            "외부 연동·알림 센터",
            "구글 시트(설문지 응답) 실시간 폴링 감시 및 카카오톡/데스크톱 토스트 알림 센터",
            short_code="NOT",
            icon_name="bell",
        )
        self._calendar_nav_row = self.register_tab(
            self.tab_calendar,
            "캘린더 & 할일",
            "학사일정 캘린더, 할일(Tasks), 모바일 예약 작업 및 Firebase 클라우드 동기화",
            short_code="CAL",
            icon_name="calendar",
        )
        self.register_tab(
            self.tab_db_manager,
            "통합 DB 관리",
            "Cloud Firestore 17개 컬렉션 로컬 증분 캐시 조회·수정·일괄 저장 및 CSV 내보내기",
            short_code="DB",
            icon_name="file-spreadsheet",
        )
        self.register_tab(
            self.tab_settings,
            "환경설정 & 명렬표",
            "멀티 LLM(Gemini/OpenAI/Claude/Ollama) 설정, 트레이 동작, 학생 명렬표 마스터 데이터 등록",
            short_code="SET",
            icon_name="settings",
        )

        # 미확인 빨간 점 배지 · 모바일 작업 실행 · 동기화 완료 시그널 연결
        self.tab_notify.unread_changed.connect(self.refresh_sidebar_unread_badges)
        self.tab_calendar.unread_changed.connect(self._on_calendar_unread_changed)
        self.tab_calendar.mobile_task_execute_requested.connect(self._on_execute_mobile_task)
        self.tab_calendar.sync_completed.connect(self._on_sheets_sync_completed)
        self.tab_calendar.ai_hwp_schedule_requested.connect(self._on_calendar_hwp_ai_requested)
        self.tab_settings.sync_completed.connect(self._on_sheets_sync_completed)

        # 우측 AiExecutionDock AI 대상 등록 및 양방향 동기화 연결
        self.ai_dock.register_target(
            target_id="agg_step2",
            label="과제 파이프라인 > 2단계 세특 생성",
            context_getter=lambda: self.tab_doc_ocr.get_ai_context("agg_step2"),
            on_result=self.tab_doc_ocr.apply_ai_dock_result,
        )
        self.ai_dock.register_target(
            target_id="ocr",
            label="2. AI OCR 및 텍스트 보정",
            context_getter=lambda: self.tab_doc_ocr.get_ai_context("ocr"),
            on_result=self.tab_doc_ocr.apply_ai_dock_result,
        )
        self.ai_dock.register_target(
            target_id="hwpx_agent",
            label="4. HWPX AI 문서 에이전트",
            context_getter=lambda: self.tab_doc_ocr.get_ai_context("hwpx_agent"),
            on_result=self.tab_doc_ocr.apply_ai_dock_result,
        )
        self.ai_dock.register_target(
            target_id="calendar_todo",
            label="캘린더 & 할일 (일정·할일 자동 추출)",
            context_getter=self.tab_calendar.get_ai_context,
            on_result=self.tab_calendar.apply_ai_dock_result,
        )
        self.ai_dock.register_target(
            target_id="notice_ocr",
            label="학급 도구함 > 가정통신문 PDF OCR",
            context_getter=self.tab_classroom.get_ai_context,
            on_result=self.tab_classroom.apply_ai_dock_result,
        )
        self.ai_dock.register_target(
            target_id="class_notice_digest",
            label="학급 도구함 > 카톡·학교종이 AI 공지 요약",
            context_getter=self.tab_classroom.get_class_notice_ai_context,
            on_result=self.tab_classroom.apply_class_notice_ai_result,
        )
        self.ai_dock.target_changed.connect(self.tab_doc_ocr.set_active_ai_target)
        self.ai_dock.target_changed.connect(self._on_ai_dock_target_changed_nav)
        self.ai_dock.go_to_settings_requested.connect(self.switch_to_settings_tab)
        self.tab_doc_ocr.ai_target_requested.connect(self._on_central_ai_target_requested)
        self.tab_doc_ocr.ai_context_changed.connect(self.ai_dock.sync_from_context)
        self.tab_doc_ocr.ai_quote_selected.connect(self.ai_dock.set_quoted_selection)
        self.tab_classroom.ai_target_requested.connect(self._on_central_ai_target_requested)
        self.tab_classroom.ai_context_changed.connect(self.ai_dock.sync_from_context)
        self.tab_classroom.schoolbell_login_required.connect(
            lambda worker, msg: self.ai_dock.show_external_login_waiting(
                worker, msg, provider_name="학교종이"
            )
        )
        self.tab_classroom.schoolbell_login_cleared.connect(self.ai_dock.clear_external_login_waiting)
        self.tab_grade.ai_reference_requested.connect(self._on_grade_material_send_to_ai)
        self.ai_dock.set_target("general_chat", force=True)

        self.sidebar_nav.setCurrentRow(0)
        self.refresh_sidebar_unread_badges()

    def open_eduflow_folder(self) -> str:
        """설정된 기본 출력 폴더(미지정 시 Documents/EduFlow)를 탐색기로 엽니다."""
        target_dir = str(get_active_workspace_dir(self.config))

        try:
            if hasattr(os, "startfile"):
                os.startfile(target_dir)
            self.append_log("INFO", f"EduFlow 폴더 열기: {target_dir}")
        except Exception as exc:
            self.append_log("WARN", f"폴더 열기 실패 ({target_dir}): {exc}")
            QMessageBox.warning(
                self, "폴더 열기 실패", f"폴더를 열 수 없습니다:\n{target_dir}\n({exc})"
            )
        return target_dir

    def refresh_sidebar_unread_badges(self) -> None:
        """SQLite 미확인 건수를 읽어 사이드바 메뉴 빨간 점(UNREAD_ROLE)을 갱신합니다."""
        notify_unread = self.db.get_notification_unread_count() > 0
        calendar_unread = self.db.get_calendar_todo_unread_count() > 0

        if 0 <= self._notify_nav_row < self.sidebar_nav.count():
            item_notify = self.sidebar_nav.item(self._notify_nav_row)
            if item_notify is not None:
                item_notify.setData(SidebarNavDelegate.UNREAD_ROLE, notify_unread)

        if 0 <= self._calendar_nav_row < self.sidebar_nav.count():
            item_cal = self.sidebar_nav.item(self._calendar_nav_row)
            if item_cal is not None:
                item_cal.setData(SidebarNavDelegate.UNREAD_ROLE, calendar_unread)

        self.sidebar_nav.viewport().update()

    def _on_calendar_unread_changed(self) -> None:
        self.tab_notify.refresh_notifications_inbox()
        self.refresh_sidebar_unread_badges()

    def _on_execute_mobile_task(self, task: dict) -> None:
        """모바일 예약 작업 [지금 실행] 요청을 해당 탭의 실행 루틴으로 라우팅합니다."""
        task_type = str(task.get("task_type", "general") or "general").strip().lower()
        title = str(task.get("title", "모바일 예약 작업"))
        self.append_log("INFO", f"모바일 예약 작업 실행: [{task_type}] {title}")
        if task_type == "file_sort":
            sorter_idx = self.stacked_widget.indexOf(self.tab_sorter)
            if sorter_idx >= 0:
                self.sidebar_nav.setCurrentRow(sorter_idx)
            self.tab_sorter.run_batch_sort_once()
        elif task_type == "ocr_batch":
            doc_idx = self.stacked_widget.indexOf(self.tab_doc_ocr)
            if doc_idx >= 0:
                self.sidebar_nav.setCurrentRow(doc_idx)

    def run_startup_sheets_sync(self) -> None:
        """앱 시작 시 백그라운드 QThread로 Cloud Firestore 데이터를 동기화합니다."""
        api = self.config.get_section("api_and_integrations")
        if not bool(api.get("sync_on_startup", True)):
            return
        svc = FirestoreSyncService(db=self.db, config=self.config)
        worker = FirestoreSyncWorker(
            sync_service=svc,
            task_name="시작 시 Cloud Firestore 자동 동기화",
        )
        self.task_runner.start_worker(
            worker,
            task_key="startup_google_sheets_sync",
            on_result=lambda res: isinstance(res, dict) and self._on_sheets_sync_completed(res),
            on_error=lambda err: self.append_log("WARN", f"시작 시 Firestore 동기화 실패: {err}"),
        )

    def _on_sheets_sync_completed(self, result: dict) -> None:
        if not result.get("ok"):
            if result.get("error"):
                self.append_log("WARN", f"Firebase 클라우드 동기화 실패: {result.get('error')}")
            return

        if hasattr(self, "tab_calendar"):
            self.tab_calendar.refresh_all()
        if hasattr(self, "tab_notify"):
            self.tab_notify.refresh_notifications_inbox()
        self.refresh_sidebar_unread_badges()

        pending_mobile = result.get("pending_mobile_tasks") or []
        msg = (
            f"Firebase 클라우드 동기화 완료 — 일정 +{result.get('schedules_added', 0)}, "
            f"할일 +{result.get('todos_added', 0)}, "
            f"예약작업 +{result.get('mobile_tasks_added', 0)}, "
            f"알림 +{result.get('notifications_added', 0)}"
        )
        self.append_log("SUCCESS", msg)
        if pending_mobile and result.get("mobile_tasks_added", 0) > 0:
            self.show_tray_toast(
                "모바일 예약 작업 대기 중",
                f"확인되지 않은 모바일 예약 작업 {len(pending_mobile)}건이 캘린더 & 할일 탭에 대기 중입니다.",
            )

    def _on_grade_material_send_to_ai(self, path_str: str) -> None:
        target_id, summary_msg = self.tab_doc_ocr.register_external_material_for_ai(path_str)
        if not target_id:
            return
        self.ai_dock.set_target(target_id, force=True)
        if not self._ai_dock_expanded:
            self.toggle_ai_dock(True)
        else:
            self.ai_dock.sync_from_context()
        self.append_log("INFO", summary_msg)

    def _on_ai_dock_target_changed_nav(self, target_id: str) -> None:
        tid = str(target_id or "").strip()
        if not tid or tid == "general_chat":
            return
        if tid in ("agg_step2", "ocr", "hwpx_agent"):
            doc_idx = self.stacked_widget.indexOf(self.tab_doc_ocr)
            if doc_idx >= 0 and self.sidebar_nav.currentRow() != doc_idx:
                self.sidebar_nav.setCurrentRow(doc_idx)
            sub_idx = {"agg_step2": 0, "ocr": 1, "hwpx_agent": 3}.get(tid, 0)
            if hasattr(self.tab_doc_ocr, "sub_tabs") and self.tab_doc_ocr.sub_tabs.currentIndex() != sub_idx:
                self.tab_doc_ocr.sub_tabs.setCurrentIndex(sub_idx)
        elif tid == "calendar_todo":
            cal_idx = self.stacked_widget.indexOf(self.tab_calendar)
            if cal_idx >= 0 and self.sidebar_nav.currentRow() != cal_idx:
                self.sidebar_nav.setCurrentRow(cal_idx)
        elif tid == "notice_ocr":
            cls_idx = self.stacked_widget.indexOf(self.tab_classroom)
            if cls_idx >= 0 and self.sidebar_nav.currentRow() != cls_idx:
                self.sidebar_nav.setCurrentRow(cls_idx)
            if hasattr(self.tab_classroom, "tool_stack"):
                self.tab_classroom.tool_stack.setCurrentIndex(2)
            if hasattr(self.tab_classroom, "tool_pills") and len(self.tab_classroom.tool_pills) > 2:
                self.tab_classroom.tool_pills[2].setChecked(True)
        elif tid == "class_notice_digest":
            cls_idx = self.stacked_widget.indexOf(self.tab_classroom)
            if cls_idx >= 0 and self.sidebar_nav.currentRow() != cls_idx:
                self.sidebar_nav.setCurrentRow(cls_idx)
            if hasattr(self.tab_classroom, "tool_stack"):
                self.tab_classroom.tool_stack.setCurrentIndex(3)
            if hasattr(self.tab_classroom, "tool_pills") and len(self.tab_classroom.tool_pills) > 3:
                self.tab_classroom.tool_pills[3].setChecked(True)

    def _on_sidebar_nav_changed(self, row: int) -> None:
        if 0 <= row < self.stacked_widget.count():
            self.stacked_widget.setCurrentIndex(row)
            meta = self._page_meta[row]
            self.lbl_page_title.setText(meta["title"].strip())
            self.lbl_page_desc.setText(meta["desc"].strip())
            if self.stacked_widget.widget(row) is self.tab_calendar:
                self.ai_dock.set_target("calendar_todo", force=False)

    def _update_ai_mode_badge(self) -> None:
        if not hasattr(self, "sidebar_ai_badge"):
            return

        info = get_active_ai_info()
        api_prov_disp = str(
            info.get("provider_display") or info.get("provider") or "Gemini"
        ).strip()
        active_model = str(info.get("model", "")).strip()
        responded_model = str(info.get("responded_model", "")).strip()
        verified = bool(info.get("connection_verified", False))
        failed = bool(info.get("connection_failed", False))
        has_key = bool(info.get("has_key", False))
        model_disp = responded_model if (verified and responded_model) else active_model

        dock_seq = self._get_shortcut_str("toggle_ai_dock") if self._shortcuts else ""
        click_hint = self._format_tooltip(
            "클릭하여 우측 AI 실행 패널 열기/닫기", dock_seq
        )

        mode_idx = self.ai_dock.current_mode_index()
        if mode_idx == 1:
            dot_color = "#3B82F6"
            web_name = self.ai_dock.manual_provider_display()
            badge_text = f"AI: 수동 복붙 · {web_name}"
            tooltip_text = (
                f"현재 AI 모드: 수동 프롬프트 복붙 모드\n"
                f"연결 대상 웹 AI: {web_name}\n"
                f"기본 API LLM: {api_prov_disp} · {model_disp or active_model}\n"
                f"{click_hint}"
            )
        elif mode_idx == 2:
            dot_color = "#38BDF8"
            pw_name = self.ai_dock.playwright_provider_display()
            pw_exec = self.ai_dock.playwright_exec_mode_display()
            badge_text = f"AI: Playwright · {pw_name}"
            tooltip_text = (
                f"현재 AI 모드: Playwright 웹 자동화 ({pw_exec})\n"
                f"연결 대상 웹 AI: {pw_name}\n"
                f"기본 API LLM: {api_prov_disp} · {model_disp or active_model}\n"
                f"{click_hint}"
            )
        else:
            dot_color = "#22C55E"
            badge_text = f"AI: {api_prov_disp} · {model_disp or active_model}"
            if verified and responded_model:
                if responded_model != active_model:
                    conn_desc = f"연결 상태: 연결 확인됨 (설정 {active_model} -> 응답 {responded_model})"
                else:
                    conn_desc = "연결 상태: 연결 테스트 응답 확인 완료"
            elif failed:
                conn_desc = "연결 상태: 최근 연결 테스트 실패 (환경설정 확인 필요)"
            elif has_key:
                conn_desc = "연결 상태: API Key 등록됨"
            else:
                conn_desc = "연결 상태: API Key 미등록 (환경설정에서 등록 필요)"
            tooltip_text = (
                f"현재 AI 모드: API 직접 호출 모드\n"
                f"연결된 LLM: {api_prov_disp}\n"
                f"모델: {model_disp or active_model}\n"
                f"{conn_desc}\n"
                f"{click_hint}"
            )

        self._ai_status_dot_color = dot_color
        self.ai_status_dot.setStyleSheet(
            f"background-color: {dot_color}; "
            "border-radius: 4px; "
            "border: none; "
            "min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px;"
        )
        self.lbl_ai_badge.setText(badge_text)
        self.sidebar_ai_badge.setToolTip(tooltip_text)
        self.lbl_ai_badge.setToolTip(tooltip_text)

    # =========================================================================
    # 공통 백그라운드 워커 시그널 핸들러 & 부드러운 로그 서랍 확장
    # =========================================================================
    def _connect_global_worker_signals(self) -> None:
        sig = self.task_runner.global_signals
        sig.task_started.connect(self._on_worker_started)
        sig.progress.connect(self._on_worker_progress)
        sig.log.connect(self.append_log)
        sig.error.connect(self._on_worker_error)
        sig.task_finished.connect(self._on_worker_finished)

    _SILENT_BACKGROUND_TASKS = frozenset({
        "PC 에이전트 온라인 하트비트",
        "모바일 원격 작업 큐 실행",
    })

    def _on_worker_started(self, task_name: str) -> None:
        if task_name in self._SILENT_BACKGROUND_TASKS:
            return
        self._active_worker_count += 1
        self.lbl_current_task.setText(f"실행 중: {task_name}")
        self.progress_bar.setRange(0, 0)
        self.append_log("INFO", f"작업 시작: {task_name}")

    def _on_worker_progress(self, percent: int, status_msg: str) -> None:
        if 0 < int(percent) <= 100:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(int(percent))
        if status_msg:
            self.lbl_single_line_log.setText(status_msg)
            self.lbl_single_line_log.setToolTip(status_msg)

    def _on_worker_error(self, err_msg: str) -> None:
        self.append_log("ERROR", err_msg)

    def _on_worker_finished(self, task_name: str) -> None:
        if task_name in self._SILENT_BACKGROUND_TASKS:
            return
        self._active_worker_count = max(0, self._active_worker_count - 1)
        self.lbl_current_task.setText(f"완료: {task_name}")
        if self._active_worker_count == 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)

    def _on_toggle_log_console(self, checked: bool) -> None:
        self._drawer_anim.stop()
        if checked:
            self._drawer_anim.setStartValue(self.log_drawer.height())
            self._drawer_anim.setEndValue(200)
            self.btn_toggle_log.setText("로그 콘솔 닫기")
        else:
            self._drawer_anim.setStartValue(self.log_drawer.height())
            self._drawer_anim.setEndValue(0)
            self.btn_toggle_log.setText("로그 콘솔 열기")
        self._drawer_anim.start()

    def _on_clear_logs(self) -> None:
        self.log_console.clear()
        self.lbl_single_line_log.setText("로그가 초기화되었습니다.")

    def append_log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        lvl = level.upper()
        color_map = {
            "INFO": "#93c5fd",
            "SUCCESS": "#4ade80",
            "WARN": "#fde047",
            "ERROR": "#f87171",
        }
        color = color_map.get(lvl, "#e2e8f0")
        html = f'<span style="color:#64748b;">[{ts}]</span> <b style="color:{color};">[{lvl}]</b> {message}'
        self.log_console.append(html)

        one_line = message.replace("\n", " ").strip()
        self.lbl_single_line_log.setToolTip(f"[{ts}] [{lvl}] {one_line}")
        if len(one_line) > 95:
            one_line = one_line[:95] + "..."
        self.lbl_single_line_log.setText(f"[{ts}] {one_line}")

        self.db.add_log(lvl, "MainWindow", message)

    # =========================================================================
    # 시스템 트레이 (QSystemTrayIcon) 및 백그라운드 상주 제어
    # =========================================================================
    def _init_tray_icon(self) -> None:
        app_icon = create_eduflow_app_icon()
        if app_icon.isNull():
            app_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(app_icon)
        if QGuiApplication.instance() is not None:
            QGuiApplication.setWindowIcon(app_icon)

        self.tray_icon = QSystemTrayIcon(app_icon, self)
        self.tray_icon.setToolTip("교무·행정 AI 워크스테이션 (백그라운드 실행 지원)")

        tray_menu = QMenu(self)
        act_open = QAction("🖥️ 워크스테이션 창 열기", self)
        act_sort_once = QAction("🚀 즉시 1회 파일 자동 분류 실행", self)
        act_toggle_watch = QAction("👁️ 실시간 폴더 감시 ON/OFF 토글", self)
        act_quit = QAction("❌ 프로그램 완전 종료", self)

        act_open.triggered.connect(self.restore_from_tray)
        act_sort_once.triggered.connect(lambda: self.tab_sorter.run_batch_sort_once())
        act_toggle_watch.triggered.connect(lambda: self.tab_sorter.toggle_realtime_watch())
        act_quit.triggered.connect(self.quit_application)

        tray_menu.addAction(act_open)
        tray_menu.addSeparator()
        tray_menu.addAction(act_sort_once)
        tray_menu.addAction(act_toggle_watch)
        tray_menu.addSeparator()
        tray_menu.addAction(act_quit)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

        self.notifier.register_tray_notifier(self.show_tray_toast)

    def show_tray_toast(self, title: str, message: str) -> None:
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                title,
                message,
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.restore_from_tray()

    def hide_to_system_tray(self) -> None:
        self.hide()
        if self.config.get("system", "show_tray_notifications", True):
            self.show_tray_toast(
                "백그라운드 실행 중",
                "워크스테이션이 시스템 트레이로 숨겨졌습니다. 실시간 파일/시트 감시는 계속 동작합니다.",
            )

    def restore_from_tray(self) -> None:
        if self.isMinimized():
            self.showNormal()
        if not self.isVisible():
            self.show()

        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        self.raise_()
        self.activateWindow()

    def cleanup_on_quit(self) -> None:
        if getattr(self, "_cleaned_up_on_quit", False):
            return
        self._cleaned_up_on_quit = True
        if hasattr(self, "tab_classroom") and self.tab_classroom is not None:
            try:
                self.tab_classroom.notify_pc_agent_offline()
            except Exception:
                pass
        try:
            from services.hwpx_ref_session_service import clear_hwpx_ref_session_cache

            clear_hwpx_ref_session_cache()
        except Exception:
            pass
        for timer in self.findChildren(QTimer):
            try:
                timer.stop()
            except Exception:
                pass
        if hasattr(self, "ai_dock") and self.ai_dock is not None:
            try:
                self.ai_dock.shutdown_workers()
            except Exception:
                pass
        self.task_runner.stop_all(wait_ms=3000)
        for runner in self.findChildren(TaskRunner):
            if runner is not self.task_runner:
                try:
                    runner.stop_all(wait_ms=3000)
                except Exception:
                    pass
        if hasattr(self, "tray_icon") and self.tray_icon is not None:
            self.tray_icon.hide()

    def quit_application(self) -> None:
        self._force_quit = True
        self.cleanup_on_quit()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force_quit:
            self.cleanup_on_quit()
            event.accept()
            return

        minimize_to_tray = bool(self.config.get("system", "minimize_to_tray_on_close", True))
        if minimize_to_tray and self.tray_icon.isVisible():
            event.ignore()
            self.hide_to_system_tray()
        else:
            ans = QMessageBox.question(
                self,
                "종료 확인",
                "모든 백그라운드 감시 작업을 중지하고 프로그램을 완전히 종료하시겠습니까?",
            )
            if ans == QMessageBox.StandardButton.Yes:
                self.quit_application()
                event.accept()
            else:
                event.ignore()
