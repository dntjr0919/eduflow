"""
메인 윈도우 모듈 (ui/main_window.py).
- QTabWidget을 완전히 제거하고 QHBoxLayout 기반의
  [좌측 다크 사이드바(QListWidget)] + [우측 밝은 메인 컨텐츠(QStackedWidget)] 구조로 구현
- 사이드바 상단 햄버거 토글 버튼([ ☰ ]) 및 QPropertyAnimation 기반 부드러운 접기/펼치기 지원
- 모든 프레임/위젯에 setObjectName(), WA_StyledBackground, style().polish()를 명시적으로 적용하여
  ui/styles/theme.qss가 100% 렌더링되도록 보장
"""

from datetime import datetime
from typing import Dict, List, Optional

from PyQt6.QtCore import QEasingCurve, QParallelAnimationGroup, QPropertyAnimation, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
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
from services.notifier import NotificationService
from services.record_generator_service import get_active_ai_info

# 독립된 각 기능 탭(페이지) 위젯 및 우측 AI 도크 import
from ui.components.ai_execution_dock import AiExecutionDock
from ui.tabs.tab_classroom_tools import ClassroomToolsTab
from ui.tabs.tab_doc_ocr import DocOcrTab
from ui.tabs.tab_file_sorter import FileSorterTab
from ui.tabs.tab_grade_material import GradeMaterialTab
from ui.tabs.tab_notification import NotificationCenterTab
from ui.tabs.tab_settings import SettingsTab


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

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EduFlow — Teacher Workstation")
        self.resize(1380, 860)
        self.setMinimumSize(1040, 680)

        self.config = ConfigManager()
        self.db = DatabaseManager()
        self.task_runner = TaskRunner(self)
        self.notifier = NotificationService()
        self._force_quit = False
        self._sidebar_collapsed = False
        self._ai_dock_expanded = False
        self._page_meta: List[Dict[str, str]] = []
        self._shortcuts: Dict[str, QShortcut] = {}

        self._init_ui()
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
        self.sidebar_layout.setContentsMargins(14, 18, 14, 16)
        self.sidebar_layout.setSpacing(6)

        # 사이드바 상단: 브랜드 타이틀 + [ = ] 햄버거 토글 버튼
        brand_top_row = QHBoxLayout()
        brand_top_row.setContentsMargins(0, 0, 0, 0)
        brand_top_row.setSpacing(6)

        self.brand_title = QLabel("EduFlow")
        polish_widget(self.brand_title, obj_name="SidebarBrandTitle")

        self.btn_sidebar_toggle = QPushButton("≡")
        polish_widget(self.btn_sidebar_toggle, obj_name="SidebarToggleBtn")
        self.btn_sidebar_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sidebar_toggle.setToolTip("사이드바 메뉴 접기 / 펼치기")
        self.btn_sidebar_toggle.clicked.connect(self.toggle_sidebar)

        brand_top_row.addWidget(self.brand_title, stretch=1)
        brand_top_row.addWidget(self.btn_sidebar_toggle, alignment=Qt.AlignmentFlag.AlignCenter)

        self.brand_sub = QLabel("Teacher Workstation")
        polish_widget(self.brand_sub, obj_name="SidebarBrandSubtitle")

        self.nav_section_lbl = QLabel("NAVIGATION")
        polish_widget(self.nav_section_lbl, obj_name="SidebarSectionLabel")

        self.sidebar_nav = QListWidget(self.sidebar_frame)
        polish_widget(self.sidebar_nav, obj_name="SidebarNavList")
        self.sidebar_nav.setSpacing(4)
        self.sidebar_nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_nav.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_nav.currentRowChanged.connect(self._on_sidebar_nav_changed)

        self.btn_hide_tray = QPushButton("시스템 트레이로 최소화")
        polish_widget(self.btn_hide_tray, obj_name="SidebarTrayButton")
        self.btn_hide_tray.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hide_tray.setToolTip("창을 숨기고 시스템 트레이에서 백그라운드 감시를 계속합니다.")
        self.btn_hide_tray.clicked.connect(self.hide_to_system_tray)

        self.sidebar_layout.addLayout(brand_top_row)
        self.sidebar_layout.addWidget(self.brand_sub)
        self.sidebar_layout.addWidget(self.nav_section_lbl)
        self.sidebar_layout.addWidget(self.sidebar_nav, stretch=1)
        self.sidebar_layout.addWidget(self.btn_hide_tray)

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

        workspace_layout = QVBoxLayout(self.workspace_frame)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)

        # 2-1) 상단 헤더 바 (TopHeaderBar)
        top_header = QFrame(self.workspace_frame)
        polish_widget(top_header, obj_name="TopHeaderBar")
        top_header_layout = QHBoxLayout(top_header)
        top_header_layout.setContentsMargins(24, 14, 24, 14)

        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        self.lbl_page_title = QLabel("스마트 파일 분류기")
        polish_widget(self.lbl_page_title, obj_name="PageHeaderTitle")

        self.lbl_page_desc = QLabel("원본 폴더 감시 및 키워드/확장자 기반 자동 폴더 생성·분류·실행취소")
        polish_widget(self.lbl_page_desc, obj_name="PageHeaderDesc")

        title_col.addWidget(self.lbl_page_title)
        title_col.addWidget(self.lbl_page_desc)

        self.lbl_ai_badge = QLabel("AI: 수동 프롬프트 모드")
        polish_widget(self.lbl_ai_badge, obj_name="AiModeBadge")

        self.btn_toggle_ai_dock = QPushButton("AI 실행 패널")
        polish_widget(self.btn_toggle_ai_dock, obj_name="AiDockToggleBtn")
        self.btn_toggle_ai_dock.setCheckable(True)
        self.btn_toggle_ai_dock.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_ai_dock.setToolTip("우측 AI 실행 사이드바 열기/닫기")
        self.btn_toggle_ai_dock.clicked.connect(lambda checked: self.toggle_ai_dock(checked))

        top_header_layout.addLayout(title_col)
        top_header_layout.addStretch()
        top_header_layout.addWidget(self.lbl_ai_badge)
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

        self.progress_bar = QProgressBar(bottom_bar)
        polish_widget(self.progress_bar, obj_name="ModernProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(210)
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
    # 좌측 다크 사이드바 접기/펼치기 토글 (Collapsible Sidebar)
    # =========================================================================
    def toggle_sidebar(self) -> None:
        self._sidebar_collapsed = not self._sidebar_collapsed
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
        self.btn_hide_tray.setProperty("collapsed", collapsed_str)

        if self._sidebar_collapsed:
            self.sidebar_layout.setContentsMargins(8, 18, 8, 16)
            self.brand_title.hide()
            self.brand_sub.hide()
            self.nav_section_lbl.hide()
            self.btn_sidebar_toggle.setToolTip("사이드바 메뉴 펼치기")
            self.btn_hide_tray.setText("▼")
            self.btn_hide_tray.setToolTip(
                "<b>시스템 트레이로 숨기기</b><br/>창을 숨기고 시스템 트레이에서 백그라운드 감시를 계속합니다."
            )
            for idx, meta in enumerate(self._page_meta):
                item = self.sidebar_nav.item(idx)
                if item is not None:
                    item.setText(meta["short"])
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(
                        f"<div style='padding:2px 4px;'><b>{meta['title']}</b><br/>"
                        f"<span style='color:#cbd5e1;'>{meta['desc']}</span></div>"
                    )
        else:
            self.sidebar_layout.setContentsMargins(14, 18, 14, 16)
            self.brand_title.show()
            self.brand_sub.show()
            self.nav_section_lbl.show()
            self.btn_sidebar_toggle.setToolTip("사이드바 메뉴 접기")
            self.btn_hide_tray.setText("시스템 트레이로 최소화")
            self.btn_hide_tray.setToolTip("창을 숨기고 시스템 트레이에서 백그라운드 감시를 계속합니다.")
            for idx, meta in enumerate(self._page_meta):
                item = self.sidebar_nav.item(idx)
                if item is not None:
                    item.setText(meta["title"])
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                    item.setToolTip(
                        f"<div style='padding:2px 4px;'><b>{meta['title']}</b><br/>"
                        f"<span style='color:#cbd5e1;'>{meta['desc']}</span></div>"
                    )

        for w in (self.sidebar_frame, self.sidebar_nav, self.btn_hide_tray):
            st = w.style()
            if st is not None:
                st.unpolish(w)
                st.polish(w)

        if hasattr(self, "_shortcuts") and self._shortcuts:
            self._update_shortcut_tooltips()

        self._sidebar_anim_group.start()

    # =========================================================================
    # 우측 AI 실행 사이드바 접기/펼치기 및 포커스·컨텍스트 연동
    # =========================================================================
    def toggle_ai_dock(self, expanded: Optional[bool] = None) -> None:
        target_expanded = (not self._ai_dock_expanded) if expanded is None else bool(expanded)
        self._ai_dock_expanded = target_expanded

        self.btn_toggle_ai_dock.blockSignals(True)
        self.btn_toggle_ai_dock.setChecked(target_expanded)
        self.btn_toggle_ai_dock.blockSignals(False)

        if target_expanded:
            if not self._sidebar_collapsed:
                self.toggle_sidebar()
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

    def _on_config_changed(self) -> None:
        self._update_ai_mode_badge()
        self.ai_dock.refresh_ai_info()
        self._refresh_shortcuts_from_config()
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
        self.btn_sidebar_toggle.setToolTip(self._format_tooltip(sidebar_base, sidebar_seq))

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
    def register_tab(self, widget: QWidget, title: str, tooltip: str = "", short_code: str = "") -> int:
        polish_widget(widget)
        idx = self.stacked_widget.addWidget(widget)
        short = short_code or (title[:2] if len(title) >= 2 else title)

        item = QListWidgetItem(title)
        rich_tip = (
            f"<div style='padding:2px 4px;'><b>{title.strip()}</b><br/>"
            f"<span style='color:#cbd5e1;'>{tooltip}</span></div>"
            if tooltip
            else title.strip()
        )
        item.setToolTip(rich_tip)
        self.sidebar_nav.addItem(item)
        self._page_meta.append(
            {"title": title, "short": short, "desc": tooltip}
        )
        return idx

    def _register_all_tabs(self) -> None:
        self.tab_sorter = FileSorterTab(task_runner=self.task_runner, parent=self)
        self.tab_doc_ocr = DocOcrTab(task_runner=self.task_runner, parent=self)
        self.tab_grade = GradeMaterialTab(task_runner=self.task_runner, parent=self)
        self.tab_classroom = ClassroomToolsTab(task_runner=self.task_runner, parent=self)
        self.tab_notify = NotificationCenterTab(task_runner=self.task_runner, parent=self)
        self.tab_settings = SettingsTab(parent=self)

        self.register_tab(
            self.tab_sorter,
            "스마트 파일 자동 분류",
            "원본 폴더 실시간 감시 및 키워드/확장자 기반 자동 폴더 생성·분류·실행취소",
            short_code="FS",
        )
        self.register_tab(
            self.tab_doc_ocr,
            "문서 처리 & AI 과제 파이프라인",
            "학번/이름 기준 제출 파일 병합 · 미제출자 대조 · 멀티 AI OCR 및 DOCX 과제 평가",
            short_code="DOC",
        )
        self.register_tab(
            self.tab_grade,
            "성적·수업자료 관리",
            "학생 명렬표 DB 연동 지필/수행평가 성적 관리 및 수업 자료 버전 관리",
            short_code="GRD",
        )
        self.register_tab(
            self.tab_classroom,
            "학급 경영 도구함",
            "학급 출결 서류 체크, 청소 당번/1인1역 랜덤 추첨기, 자리 배치 등 미니 툴",
            short_code="CLS",
        )
        self.register_tab(
            self.tab_notify,
            "외부 연동·알림 센터",
            "구글 시트(설문지 응답) 실시간 폴링 감시 및 카카오톡/데스크톱 토스트 알림 센터",
            short_code="NOT",
        )
        self.register_tab(
            self.tab_settings,
            "환경설정 & 명렬표",
            "멀티 LLM(Gemini/OpenAI/Claude/Ollama) 설정, 트레이 동작, 학생 명렬표 마스터 데이터 등록",
            short_code="SET",
        )

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
        self.ai_dock.target_changed.connect(self.tab_doc_ocr.set_active_ai_target)
        self.ai_dock.go_to_settings_requested.connect(self.switch_to_settings_tab)
        self.tab_doc_ocr.ai_target_requested.connect(self._on_central_ai_target_requested)
        self.tab_doc_ocr.ai_context_changed.connect(self.ai_dock.sync_from_context)
        self.ai_dock.set_target("agg_step2", force=True)

        self.sidebar_nav.setCurrentRow(0)

    def _on_sidebar_nav_changed(self, row: int) -> None:
        if 0 <= row < self.stacked_widget.count():
            self.stacked_widget.setCurrentIndex(row)
            meta = self._page_meta[row]
            self.lbl_page_title.setText(meta["title"].strip())
            self.lbl_page_desc.setText(meta["desc"].strip())

    def _update_ai_mode_badge(self) -> None:
        sec = self.config.get_section("api_and_integrations")
        if bool(sec.get("use_manual_prompt_mode", True)):
            self.lbl_ai_badge.setText("AI: 수동 프롬프트 모드")
            self.lbl_ai_badge.setToolTip("수동 프롬프트 복사/붙여넣기 모드가 활성화되어 있습니다.")
            return

        info = get_active_ai_info()
        prov = str(info.get("provider", "gemini")).upper()
        active_model = str(info.get("model", "")).strip()
        responded_model = str(info.get("responded_model", "")).strip()
        verified = bool(info.get("connection_verified", False))
        failed = bool(info.get("connection_failed", False))

        if verified and responded_model:
            self.lbl_ai_badge.setText(f"AI: {prov} · {responded_model} (연결됨)")
            if responded_model != active_model:
                self.lbl_ai_badge.setToolTip(
                    f"연결 테스트 응답 확인 완료\n공급자: {prov}\n설정 모델: {active_model}\n실제 응답 모델: {responded_model}"
                )
            else:
                self.lbl_ai_badge.setToolTip(
                    f"연결 테스트 응답 확인 완료\n공급자: {prov}\n응답 모델: {responded_model}"
                )
        elif failed:
            self.lbl_ai_badge.setText(f"AI: {prov} · {active_model} (연결 실패)")
            self.lbl_ai_badge.setToolTip(
                f"최근 연결 테스트 실패\n공급자: {prov}\n설정 모델: {active_model}\n환경설정에서 API Key 또는 모델을 확인해 주세요."
            )
        else:
            self.lbl_ai_badge.setText(f"AI: {prov} · {active_model}")
            self.lbl_ai_badge.setToolTip(
                f"현재 설정된 모델: {active_model}\n(환경설정에서 [연결 테스트]를 실행하면 실제 응답 모델이 표시됩니다.)"
            )

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

    def _on_worker_started(self, task_name: str) -> None:
        self.lbl_current_task.setText(f"실행 중: {task_name}")
        self.progress_bar.setValue(0)
        self.append_log("INFO", f"작업 시작: {task_name}")

    def _on_worker_progress(self, percent: int, status_msg: str) -> None:
        self.progress_bar.setValue(percent)
        if status_msg:
            self.lbl_single_line_log.setText(status_msg)
            self.lbl_single_line_log.setToolTip(status_msg)

    def _on_worker_error(self, err_msg: str) -> None:
        self.append_log("ERROR", err_msg)

    def _on_worker_finished(self, task_name: str) -> None:
        self.lbl_current_task.setText(f"완료: {task_name}")

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
        app_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(app_icon)

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
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def quit_application(self) -> None:
        self._force_quit = True
        self.task_runner.stop_all(wait_ms=2500)
        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force_quit:
            self.task_runner.stop_all(wait_ms=2500)
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
