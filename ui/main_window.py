"""
메인 윈도우 모듈 (ui/main_window.py).
- QTabWidget을 완전히 제거하고 QHBoxLayout 기반의
  [좌측 다크 사이드바(QListWidget)] + [우측 밝은 메인 컨텐츠(QStackedWidget)] 구조로 구현
- 모든 프레임/위젯에 setObjectName(), WA_StyledBackground, style().polish()를 명시적으로 적용하여
  ui/styles/theme.qss가 100% 렌더링되도록 보장
"""

from datetime import datetime
from typing import Dict, List

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PyQt6.QtGui import QAction, QCloseEvent
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
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.notifier import NotificationService

# 독립된 각 기능 탭(페이지) 위젯 import
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

    def __init__(self):
        super().__init__()
        self.setWindowTitle("교무·행정 AI 워크스테이션 v2.0")
        self.resize(1360, 860)
        self.setMinimumSize(1080, 720)

        self.config = ConfigManager()
        self.db = DatabaseManager()
        self.task_runner = TaskRunner(self)
        self.notifier = NotificationService()
        self._force_quit = False
        self._page_meta: List[Dict[str, str]] = []

        self._init_ui()
        self._init_tray_icon()
        self._connect_global_worker_signals()
        self._register_all_tabs()
        self._update_ai_mode_badge()
        self.config.add_listener(lambda _: self._update_ai_mode_badge())

        self.append_log("INFO", "교무·행정 AI 워크스테이션이 시작되었습니다.")

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
        # 1. 좌측 다크 사이드바 (SidebarContainer + SidebarNavList)
        # ---------------------------------------------------------------------
        self.sidebar_frame = QFrame(central)
        polish_widget(self.sidebar_frame, obj_name="SidebarContainer")
        self.sidebar_frame.setFixedWidth(255)

        sidebar_layout = QVBoxLayout(self.sidebar_frame)
        sidebar_layout.setContentsMargins(16, 22, 16, 18)
        sidebar_layout.setSpacing(8)

        brand_title = QLabel("✨ AI 교무 워크스테이션")
        polish_widget(brand_title, obj_name="SidebarBrandTitle")

        brand_sub = QLabel("Smart Teacher Desktop Hub")
        polish_widget(brand_sub, obj_name="SidebarBrandSubtitle")

        nav_section_lbl = QLabel("WORKSPACE MENU")
        polish_widget(nav_section_lbl, obj_name="SidebarSectionLabel")

        self.sidebar_nav = QListWidget(self.sidebar_frame)
        polish_widget(self.sidebar_nav, obj_name="SidebarNavList")
        self.sidebar_nav.setSpacing(4)
        self.sidebar_nav.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_nav.currentRowChanged.connect(self._on_sidebar_nav_changed)

        btn_hide_tray = QPushButton("🔽  시스템 트레이로 숨기기")
        polish_widget(btn_hide_tray, obj_name="SidebarTrayButton")
        btn_hide_tray.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_hide_tray.setToolTip("창을 숨기고 시스템 트레이에서 백그라운드 감시를 계속합니다.")
        btn_hide_tray.clicked.connect(self.hide_to_system_tray)

        sidebar_layout.addWidget(brand_title)
        sidebar_layout.addWidget(brand_sub)
        sidebar_layout.addWidget(nav_section_lbl)
        sidebar_layout.addWidget(self.sidebar_nav, stretch=1)
        sidebar_layout.addWidget(btn_hide_tray)

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
        top_header_layout.setContentsMargins(24, 16, 24, 16)

        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        self.lbl_page_title = QLabel("📁 스마트 파일 분류기")
        polish_widget(self.lbl_page_title, obj_name="PageHeaderTitle")

        self.lbl_page_desc = QLabel("원본 폴더 감시 및 키워드/확장자 기반 자동 폴더 생성·분류·실행취소")
        polish_widget(self.lbl_page_desc, obj_name="PageHeaderDesc")

        title_col.addWidget(self.lbl_page_title)
        title_col.addWidget(self.lbl_page_desc)

        self.lbl_ai_badge = QLabel("🤖 AI 모드: 수동 프롬프트 (No-API)")
        polish_widget(self.lbl_ai_badge, obj_name="AiModeBadge")

        top_header_layout.addLayout(title_col)
        top_header_layout.addStretch()
        top_header_layout.addWidget(self.lbl_ai_badge)

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
        drawer_title = QLabel("🖥️ 실시간 백그라운드 작업 로그 콘솔")
        drawer_title.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: 700;")
        btn_clear_log = QPushButton("로그 지우기")
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
        status_layout.setContentsMargins(20, 10, 20, 10)
        status_layout.setSpacing(12)

        self.lbl_current_task = QLabel("⚡ 대기 중")
        polish_widget(self.lbl_current_task, obj_name="StatusTaskBadge")

        self.lbl_single_line_log = QLabel("최근 로그가 이곳에 한 줄로 실시간 요약 표시됩니다.")
        polish_widget(self.lbl_single_line_log, obj_name="StatusSingleLineLog")

        self.progress_bar = QProgressBar(bottom_bar)
        polish_widget(self.progress_bar, obj_name="ModernProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(210)
        self.progress_bar.setTextVisible(False)

        self.btn_toggle_log = QPushButton("로그 보기 ▲")
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

    # =========================================================================
    # 모듈형 페이지 등록 (좌측 QListWidget + 우측 QStackedWidget 동시 등록)
    # =========================================================================
    def register_tab(self, widget: QWidget, title: str, tooltip: str = "") -> int:
        polish_widget(widget)
        idx = self.stacked_widget.addWidget(widget)
        item = QListWidgetItem(title)
        if tooltip:
            item.setToolTip(tooltip)
        self.sidebar_nav.addItem(item)
        self._page_meta.append({"title": title, "desc": tooltip})
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
            "📁  스마트 파일 분류기",
            "원본 폴더 실시간 감시 및 키워드/확장자 기반 자동 폴더 생성·분류·실행취소",
        )
        self.register_tab(
            self.tab_doc_ocr,
            "📄  문서 처리 & AI OCR",
            "학번/이름 기준 제출 파일 병합 · 미제출자 대조 · 멀티 AI OCR 및 수동 프롬프트 보정",
        )
        self.register_tab(
            self.tab_grade,
            "📊  성적·수업자료 관리",
            "학생 명렬표 DB 연동 지필/수행평가 성적 관리 및 수업 자료 버전 관리",
        )
        self.register_tab(
            self.tab_classroom,
            "🏫  학급 경영 도구함",
            "학급 출결 서류 체크, 청소 당번/1인1역 랜덤 추첨기, 자리 배치 등 미니 툴",
        )
        self.register_tab(
            self.tab_notify,
            "🔔  외부 연동·알림 센터",
            "구글 시트(설문지 응답) 실시간 폴링 감시 및 카카오톡/데스크톱 토스트 알림 센터",
        )
        self.register_tab(
            self.tab_settings,
            "⚙️  환경설정·명렬표 관리",
            "멀티 LLM(Gemini/OpenAI/Claude/Ollama) 설정, 트레이 동작, 학생 명렬표 마스터 데이터 등록",
        )

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
            self.lbl_ai_badge.setText("🤖 AI 모드: 수동 프롬프트 (No-API 복붙)")
        else:
            prov = str(sec.get("ai_provider", "gemini")).upper()
            self.lbl_ai_badge.setText(f"⚡ AI API 연동: {prov}")

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
        self.lbl_current_task.setText(f"🔄 {task_name}")
        self.progress_bar.setValue(0)
        self.append_log("INFO", f"작업 시작: {task_name}")

    def _on_worker_progress(self, percent: int, status_msg: str) -> None:
        self.progress_bar.setValue(percent)
        if status_msg:
            self.lbl_single_line_log.setText(status_msg)

    def _on_worker_error(self, err_msg: str) -> None:
        self.append_log("ERROR", err_msg)

    def _on_worker_finished(self, task_name: str) -> None:
        self.lbl_current_task.setText(f"✅ 완료: {task_name}")

    def _on_toggle_log_console(self, checked: bool) -> None:
        self._drawer_anim.stop()
        if checked:
            self._drawer_anim.setStartValue(self.log_drawer.height())
            self._drawer_anim.setEndValue(200)
            self.btn_toggle_log.setText("로그 접기 ▼")
        else:
            self._drawer_anim.setStartValue(self.log_drawer.height())
            self._drawer_anim.setEndValue(0)
            self.btn_toggle_log.setText("로그 보기 ▲")
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
