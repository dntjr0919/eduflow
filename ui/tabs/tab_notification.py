"""
[탭 5] 외부 연동 및 자동 알림 센터 (ui/tabs/tab_notification.py)
- EduFlow Slate Workstation 디자인 시스템 적용 (Zero-Emoji, Instrument Card, 1px #E2E8F0 보더)
- 상단 채널 상태 표시줄: Desktop Toast / KakaoTalk Macro / Webhook 상태 칩 3종
- 구글 시트 폴링 감시 카드: URL 입력(34px), 감시 주기 스핀박스, ON/OFF 토글 버튼 (GoogleSheetWatchWorker 연동)
- 즉시 알림 발송 카드: 제목/본문 입력, 대상 채널 체크박스, "즉시 발송" CTA 버튼 (_on_send_manual_notification 연동)
- 하단 실시간 터미널 이벤트 로그 드로어: #0F172A 다크 배경, #E2E8F0 텍스트, JetBrains Mono 폰트
"""

from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QDate, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import FunctionWorker, PeriodicWorker, TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.mobile_integration_worker import push_comcigan_changes, sync_comcigan_from_web
from services.notifier import NotificationService
from ui.components.hover_zoom_card import HoverZoomCard
from ui.utils import install_no_scroll_wheel_filter


def apply_qss_role(widget: QWidget, role: str = "", obj_name: str = "") -> None:
    """위젯에 objectName 및 role 속성을 부여하고 style().polish()를 즉시 호출합니다."""
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    if obj_name:
        widget.setObjectName(obj_name)
    if role:
        widget.setProperty("role", role)
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


class GoogleSheetWatchWorker(PeriodicWorker):
    def __init__(
        self,
        sheet_url: str,
        credentials_path: str,
        interval_sec: float,
        row_detected_signal,
    ):
        super().__init__(
            task_name="구글 시트 실시간 감시",
            interval_sec=interval_sec,
            tick_func=self._poll_once,
        )
        self.sheet_url = sheet_url
        self.credentials_path = credentials_path
        self.row_detected_signal = row_detected_signal
        self._tick_count = 0

    def _poll_once(self, worker: PeriodicWorker) -> None:
        raise NotImplementedError(
            "Google Sheets API 실시간 폴링 연동이 아직 구현되지 않았습니다."
        )


class NotificationCenterTab(QWidget):
    """[탭 5] 외부 연동 및 자동 알림 센터 탭 위젯."""

    new_sheet_row_signal = pyqtSignal(dict)
    unread_changed = pyqtSignal()
    SHEET_WATCH_KEY = "google_sheet_watcher"

    def __init__(
        self,
        task_runner: TaskRunner,
        parent: Optional[QWidget] = None,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
        notifier: Optional[NotificationService] = None,
    ):
        super().__init__(parent)
        apply_qss_role(self, obj_name="NotificationCenterTabPage")
        self.task_runner = task_runner
        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()
        self.notifier = notifier or NotificationService()
        self._watching_sheet = False
        self._comcigan_sync_running = False
        self._last_comcigan_snapshot_hash = ""
        self._comcigan_timer = QTimer(self)
        self._comcigan_timer.timeout.connect(self._on_comcigan_auto_timer_tick)

        self.new_sheet_row_signal.connect(self._on_new_sheet_row)
        self._init_ui()
        self._sync_comcigan_timer_state()
        self._refresh_channel_status_chips()
        self.refresh_notifications_inbox()
        self.config.add_listener(lambda _cfg: self._refresh_channel_status_chips())

    def _init_ui(self) -> None:
        outer_lay = QVBoxLayout(self)
        outer_lay.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        container.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 20, 20)
        layout.setSpacing(14)

        # -----------------------------------------------------------------
        # 1. 상단 채널 상태 표시줄 (Top Channel Status Bar: 3 Status Chips)
        # -----------------------------------------------------------------
        status_bar_card = QFrame()
        apply_qss_role(status_bar_card, role="instrumentCard", obj_name="ChannelStatusBarCard")
        sb_lay = QHBoxLayout(status_bar_card)
        sb_lay.setContentsMargins(16, 12, 16, 12)
        sb_lay.setSpacing(10)

        lbl_sb_title = QLabel("알림 채널 연결 상태 (CHANNEL STATUS)")
        apply_qss_role(lbl_sb_title, role="kpiLabel")

        self.chip_toast = QLabel("Desktop Toast: Active")
        apply_qss_role(self.chip_toast, role="badgeSuccess", obj_name="ChipToastStatus")

        self.chip_kakao = QLabel("KakaoTalk Macro: Configured")
        apply_qss_role(self.chip_kakao, role="badgePrimary", obj_name="ChipKakaoStatus")

        self.chip_webhook = QLabel("Webhook: Ready")
        apply_qss_role(self.chip_webhook, role="badgeNeutral", obj_name="ChipWebhookStatus")

        btn_refresh_status = QPushButton("상태 새로고침")
        apply_qss_role(btn_refresh_status, role="secondary", obj_name="BtnRefreshChannelStatus")
        btn_refresh_status.setMinimumHeight(28)
        btn_refresh_status.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_refresh_status.clicked.connect(self._refresh_channel_status_chips)

        sb_lay.addWidget(lbl_sb_title)
        sb_lay.addSpacing(8)
        sb_lay.addWidget(self.chip_toast)
        sb_lay.addWidget(self.chip_kakao)
        sb_lay.addWidget(self.chip_webhook)
        sb_lay.addStretch()
        sb_lay.addWidget(btn_refresh_status)
        layout.addWidget(status_bar_card)

        # -----------------------------------------------------------------
        # 2. 구글 시트 / 설문지 응답 실시간 폴링 감시 카드
        # -----------------------------------------------------------------
        self.sheet_group = HoverZoomCard(
            title="구글 시트 및 설문지 응답 실시간 감시 워커 (Google Sheet Polling)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.sheet_group, role="hoverZoomCard", obj_name="GoogleSheetWatchGroup")
        s_lay = self.sheet_group.body_layout()

        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(4, 4, 4, 4)

        self.edit_sheet_url = QLineEdit(
            str(self.config.get("api_and_integrations", "google_sheet_url", ""))
        )
        self.edit_sheet_url.setMinimumHeight(34)
        self.edit_sheet_url.setPlaceholderText("https://docs.google.com/spreadsheets/d/...")
        self.edit_sheet_url.setEnabled(False)
        form.addRow("감시 대상 Google 시트 URL:", self.edit_sheet_url)

        poll_row = QHBoxLayout()
        poll_row.setSpacing(10)

        self.spin_poll_interval = QSpinBox()
        self.spin_poll_interval.setMinimumHeight(34)
        self.spin_poll_interval.setRange(5, 3600)
        self.spin_poll_interval.setSuffix(" 초")
        self.spin_poll_interval.setValue(
            int(self.config.get("api_and_integrations", "google_sheet_poll_interval_sec", 30))
        )
        self.spin_poll_interval.setEnabled(False)
        install_no_scroll_wheel_filter(self.spin_poll_interval)

        self.btn_toggle_sheet = QPushButton("구글 시트 실시간 감시 (미지원 - 추후 연동 예정)")
        apply_qss_role(self.btn_toggle_sheet, role="secondary", obj_name="BtnToggleSheetWatch")
        self.btn_toggle_sheet.setMinimumHeight(34)
        self.btn_toggle_sheet.setEnabled(False)
        self.btn_toggle_sheet.setToolTip("Google Sheets API 연동이 아직 구현되지 않았습니다.")
        self.btn_toggle_sheet.clicked.connect(self._toggle_sheet_watch)

        poll_row.addWidget(self.spin_poll_interval)
        poll_row.addWidget(self.btn_toggle_sheet)
        poll_row.addStretch()
        form.addRow("폴링 감시 주기 및 제어:", poll_row)

        lbl_sheet_notice = QLabel(
            "안내: Google Sheets API 연동이 아직 구현되지 않아 실시간 폴링 감시 기능이 비활성화되어 있습니다."
        )
        apply_qss_role(lbl_sheet_notice, role="mutedText")
        lbl_sheet_notice.setWordWrap(True)
        form.addRow("", lbl_sheet_notice)

        s_lay.addLayout(form)
        layout.addWidget(self.sheet_group)

        # -----------------------------------------------------------------
        # 3. 즉시 브로드캐스트 알림 발송 카드 (Instant Broadcast Card)
        # -----------------------------------------------------------------
        self.notify_group = HoverZoomCard(
            title="즉시 알림 브로드캐스트 및 멀티 채널 발송 (Instant Broadcast)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.notify_group, role="hoverZoomCard", obj_name="ManualNotifyGroup")
        n_lay = self.notify_group.body_layout()

        n_form = QFormLayout()
        n_form.setVerticalSpacing(12)
        n_form.setHorizontalSpacing(16)
        n_form.setContentsMargins(4, 4, 4, 4)

        self.edit_notify_title = QLineEdit("학급 공지 / 설문 응답 알림")
        self.edit_notify_title.setMinimumHeight(34)

        self.edit_notify_msg = QLineEdit("새로운 학부모 상담 신청 설문 응답이 접수되었습니다.")
        self.edit_notify_msg.setMinimumHeight(34)

        ch_row = QHBoxLayout()
        ch_row.setSpacing(16)
        self.chk_target_toast = QCheckBox("데스크톱 토스트 (Desktop Toast)")
        self.chk_target_toast.setChecked(True)
        self.chk_target_kakao = QCheckBox("PC 카카오톡 매크로 (미지원)")
        self.chk_target_kakao.setChecked(False)
        self.chk_target_kakao.setToolTip("PC 카카오톡 자동 전송 매크로 기능은 현재 지원되지 않습니다.")
        self.chk_target_webhook = QCheckBox("웹훅 연동 (Webhook)")
        self.chk_target_webhook.setChecked(True)
        ch_row.addWidget(self.chk_target_toast)
        ch_row.addWidget(self.chk_target_kakao)
        ch_row.addWidget(self.chk_target_webhook)
        ch_row.addStretch()

        n_form.addRow("알림 제목:", self.edit_notify_title)
        n_form.addRow("알림 본문:", self.edit_notify_msg)
        n_form.addRow("발송 대상 채널:", ch_row)
        n_lay.addLayout(n_form)

        send_bar = QHBoxLayout()
        send_bar.setSpacing(10)
        self.btn_send_now = QPushButton("즉시 발송")
        apply_qss_role(self.btn_send_now, role="primary", obj_name="BtnSendManualNotify")
        self.btn_send_now.setMinimumHeight(36)
        self.btn_send_now.setMinimumWidth(140)
        self.btn_send_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send_now.clicked.connect(self._on_send_manual_notification)

        lbl_send_hint = QLabel("설정된 외부 알림 서비스(services/notifier.py)를 통해 즉시 전송합니다.")
        apply_qss_role(lbl_send_hint, role="mutedText")

        send_bar.addWidget(self.btn_send_now)
        send_bar.addWidget(lbl_send_hint)
        send_bar.addStretch()
        n_lay.addLayout(send_bar)
        layout.addWidget(self.notify_group)

        # -----------------------------------------------------------------
        # 3.2 컴시간 알리미 시간표 변동 반영 (Firebase Firestore 실시간 반영) 카드
        # -----------------------------------------------------------------
        self.comcigan_group = HoverZoomCard(
            title="컴시간 알리미 시간표 변동 반영 (Firebase Firestore 실시간 반영)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.comcigan_group, role="hoverZoomCard", obj_name="ComciganPushGroup")
        c_lay = self.comcigan_group.body_layout()

        # 자동 연동 설정 영역 (Playwright 웹 파싱)
        auto_form = QFormLayout()
        auto_form.setVerticalSpacing(10)
        auto_form.setHorizontalSpacing(16)
        auto_form.setContentsMargins(4, 4, 4, 4)

        self.edit_comcigan_url = QLineEdit(
            str(self.config.get("api_and_integrations", "comcigan_url", ""))
        )
        self.edit_comcigan_url.setMinimumHeight(34)
        self.edit_comcigan_url.setPlaceholderText("예: http://comci.net:4082/st 또는 교사용/학생용 컴시간 URL")
        self.edit_comcigan_url.textChanged.connect(self._on_comcigan_config_changed)
        auto_form.addRow("컴시간 접속 URL:", self.edit_comcigan_url)

        code_row = QHBoxLayout()
        code_row.setSpacing(10)
        self.edit_comcigan_school_code = QLineEdit(
            str(self.config.get("api_and_integrations", "comcigan_school_code", ""))
        )
        self.edit_comcigan_school_code.setMinimumHeight(34)
        self.edit_comcigan_school_code.setPlaceholderText("컴시간 5자리 학교코드 또는 학교명 (예: 31234)")
        self.edit_comcigan_school_code.textChanged.connect(self._on_comcigan_config_changed)

        self.btn_auto_sync_comcigan = QPushButton("🔄 컴시간 웹 자동 파싱 & Firebase Firestore 실시간 반영")
        apply_qss_role(self.btn_auto_sync_comcigan, role="primary", obj_name="BtnAutoSyncComcigan")
        self.btn_auto_sync_comcigan.setMinimumHeight(34)
        self.btn_auto_sync_comcigan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_sync_comcigan.clicked.connect(lambda: self._on_auto_sync_comcigan_clicked(force_full=False))

        self.btn_force_sync_comcigan = QPushButton("전체 강제 재동기화")
        apply_qss_role(self.btn_force_sync_comcigan, role="warningOutline", obj_name="BtnForceSyncComcigan")
        self.btn_force_sync_comcigan.setMinimumHeight(34)
        self.btn_force_sync_comcigan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_force_sync_comcigan.setToolTip("로컬 캐시를 무시하고 전체 시간표를 Firestore에 강제로 덮어씁니다.")
        self.btn_force_sync_comcigan.clicked.connect(lambda: self._on_auto_sync_comcigan_clicked(force_full=True))

        code_row.addWidget(self.edit_comcigan_school_code, stretch=1)
        code_row.addWidget(self.btn_auto_sync_comcigan)
        code_row.addWidget(self.btn_force_sync_comcigan)
        auto_form.addRow("컴시간 학교코드(5자리)/학교명:", code_row)

        teacher_scope_row = QHBoxLayout()
        teacher_scope_row.setSpacing(10)
        self.edit_comcigan_teacher_name = QLineEdit(
            str(self.config.get("api_and_integrations", "comcigan_teacher_name", ""))
        )
        self.edit_comcigan_teacher_name.setMinimumHeight(34)
        self.edit_comcigan_teacher_name.setPlaceholderText("내 교사명 (교사 개인 시간표 추출용, 예: 홍길동)")
        self.edit_comcigan_teacher_name.textChanged.connect(self._on_comcigan_config_changed)

        self.combo_comcigan_scope = QComboBox()
        self.combo_comcigan_scope.setMinimumHeight(34)
        self.combo_comcigan_scope.addItem("담임반 + 내 교사 시간표", "homeroom_and_teacher")
        self.combo_comcigan_scope.addItem("전체 학급 + 내 교사 시간표", "all_classes_and_teacher")

        teacher_scope_row.addWidget(self.edit_comcigan_teacher_name, stretch=1)
        teacher_scope_row.addWidget(QLabel("수집 범위:"))
        teacher_scope_row.addWidget(self.combo_comcigan_scope)
        auto_form.addRow("교사명 및 수집 범위:", teacher_scope_row)

        auto_timer_row = QHBoxLayout()
        auto_timer_row.setSpacing(10)
        self.chk_comcigan_auto_sync = QCheckBox("자동 주기 동기화")
        self.chk_comcigan_auto_sync.setChecked(
            bool(self.config.get("api_and_integrations", "comcigan_auto_sync_enabled", False))
        )

        self.combo_comcigan_interval = QComboBox()
        self.combo_comcigan_interval.setMinimumHeight(34)
        self.combo_comcigan_interval.addItem("10분마다", 10)
        self.combo_comcigan_interval.addItem("30분마다", 30)
        self.combo_comcigan_interval.addItem("1시간마다", 60)
        self.combo_comcigan_interval.addItem("2시간마다", 120)
        saved_interval = int(
            self.config.get("api_and_integrations", "comcigan_auto_sync_interval_min", 30) or 30
        )
        i_idx = self.combo_comcigan_interval.findData(saved_interval)
        self.combo_comcigan_interval.setCurrentIndex(i_idx if i_idx >= 0 else 1)

        self.lbl_comcigan_auto_status = QLabel("마지막 확인: 대기 중")
        apply_qss_role(self.lbl_comcigan_auto_status, role="mutedText")

        self.chk_comcigan_auto_sync.stateChanged.connect(self._on_comcigan_config_changed)
        self.combo_comcigan_interval.currentIndexChanged.connect(self._on_comcigan_config_changed)

        auto_timer_row.addWidget(self.chk_comcigan_auto_sync)
        auto_timer_row.addWidget(self.combo_comcigan_interval)
        auto_timer_row.addWidget(self.lbl_comcigan_auto_status, stretch=1)
        auto_form.addRow("백그라운드 주기 실행:", auto_timer_row)

        c_lay.addLayout(auto_form)

        c_divider = QFrame()
        c_divider.setFrameShape(QFrame.Shape.HLine)
        c_divider.setStyleSheet("color: #E2E8F0; margin: 4px 0;")
        c_lay.addWidget(c_divider)

        c_form = QFormLayout()
        c_form.setVerticalSpacing(12)
        c_form.setHorizontalSpacing(16)
        c_form.setContentsMargins(4, 4, 4, 4)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(10)

        self.date_comcigan = QDateEdit()
        self.date_comcigan.setCalendarPopup(True)
        self.date_comcigan.setDisplayFormat("yyyy-MM-dd")
        self.date_comcigan.setDate(QDate.currentDate())
        self.date_comcigan.setMinimumHeight(34)
        self.date_comcigan.setFixedWidth(140)
        if self.date_comcigan.calendarWidget() is not None:
            self.date_comcigan.calendarWidget().setMinimumSize(320, 260)

        self.edit_comcigan_grade = QLineEdit(
            str(self.config.get("api_and_integrations", "comcigan_target_grade", ""))
        )
        self.edit_comcigan_grade.setMinimumHeight(34)
        self.edit_comcigan_grade.setFixedWidth(90)
        self.edit_comcigan_grade.setPlaceholderText("학년 (예: 1)")
        self.edit_comcigan_grade.textChanged.connect(self._on_comcigan_config_changed)

        self.edit_comcigan_class = QLineEdit(
            str(self.config.get("api_and_integrations", "comcigan_target_class", ""))
        )
        self.edit_comcigan_class.setMinimumHeight(34)
        self.edit_comcigan_class.setFixedWidth(90)
        self.edit_comcigan_class.setPlaceholderText("반 (예: 3)")
        self.edit_comcigan_class.textChanged.connect(self._on_comcigan_config_changed)

        meta_row.addWidget(self.date_comcigan)
        meta_row.addWidget(QLabel("기본 학년:"))
        meta_row.addWidget(self.edit_comcigan_grade)
        meta_row.addWidget(QLabel("기본 반:"))
        meta_row.addWidget(self.edit_comcigan_class)
        meta_row.addStretch()
        c_form.addRow("대상 날짜 및 학급:", meta_row)

        self.txt_comcigan_payload = QTextEdit()
        self.txt_comcigan_payload.setMinimumHeight(88)
        self.txt_comcigan_payload.setPlaceholderText(
            "변동 내역 텍스트 또는 JSON을 입력하세요.\n"
            "예: 1-3 2교시 수학Ⅰ -> 영어 (한가인)\n"
            '또는 [{"grade": "1", "class_no": "3", "period": 2, "original_subject": "수학Ⅰ", "changed_subject": "영어", "changed_teacher": "한가인"}]'
        )
        c_form.addRow("변동 내역 (텍스트/JSON):", self.txt_comcigan_payload)
        c_lay.addLayout(c_form)

        c_btn_row = QHBoxLayout()
        c_btn_row.setSpacing(10)
        self.btn_push_comcigan = QPushButton("시간표 변동 시트에 반영")
        apply_qss_role(self.btn_push_comcigan, role="primary", obj_name="BtnPushComcigan")
        self.btn_push_comcigan.setMinimumHeight(34)
        self.btn_push_comcigan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_push_comcigan.clicked.connect(self._on_push_comcigan_clicked)

        lbl_comcigan_hint = QLabel(
            "Firebase Firestore '시간표_변동' 컬렉션에 즉시 반영하며, 결과를 로그와 알림으로 기록합니다."
        )
        apply_qss_role(lbl_comcigan_hint, role="mutedText")

        c_btn_row.addWidget(self.btn_push_comcigan)
        c_btn_row.addWidget(lbl_comcigan_hint)
        c_btn_row.addStretch()
        c_lay.addLayout(c_btn_row)
        layout.addWidget(self.comcigan_group)

        # -----------------------------------------------------------------
        # 3.5 외부 연동 및 알림 수신함 (Incoming Notifications & History)
        # -----------------------------------------------------------------
        self.inbox_group = HoverZoomCard(
            title="외부 연동 및 알림 수신함 (Incoming Notifications & History)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.inbox_group, role="hoverZoomCard", obj_name="NotificationInboxGroup")
        in_lay = self.inbox_group.body_layout()

        inbox_hdr = QHBoxLayout()
        inbox_hdr.setSpacing(8)
        self.lbl_inbox_unread = QLabel("미확인 알림 0건")
        apply_qss_role(self.lbl_inbox_unread, role="badgeNeutral", obj_name="InboxUnreadBadge")

        lbl_inbox_hint = QLabel("항목을 클릭하거나 [확인]을 누르면 읽음 처리됩니다. (탭 열기만으로는 읽음 처리되지 않음)")
        apply_qss_role(lbl_inbox_hint, role="mutedText")

        self.btn_refresh_inbox = QPushButton("새로고침")
        apply_qss_role(self.btn_refresh_inbox, role="secondary", obj_name="BtnRefreshInbox")
        self.btn_refresh_inbox.setMinimumHeight(28)
        self.btn_refresh_inbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_inbox.clicked.connect(self.refresh_notifications_inbox)

        self.btn_mark_all_noti_read = QPushButton("모두 읽음")
        apply_qss_role(self.btn_mark_all_noti_read, role="secondary", obj_name="BtnMarkAllNotiRead")
        self.btn_mark_all_noti_read.setMinimumHeight(28)
        self.btn_mark_all_noti_read.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mark_all_noti_read.clicked.connect(self.mark_all_read)

        inbox_hdr.addWidget(self.lbl_inbox_unread)
        inbox_hdr.addWidget(lbl_inbox_hint)
        inbox_hdr.addStretch()
        inbox_hdr.addWidget(self.btn_refresh_inbox)
        inbox_hdr.addWidget(self.btn_mark_all_noti_read)
        in_lay.addLayout(inbox_hdr)

        self.table_notifications = QTableWidget(0, 6)
        self.table_notifications.setHorizontalHeaderLabels(
            ["상태", "채널", "제목", "내용", "수신 시각", "작업"]
        )
        self.table_notifications.verticalHeader().setVisible(False)
        self.table_notifications.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_notifications.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_notifications.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_notifications.setAlternatingRowColors(True)
        self.table_notifications.setMinimumHeight(190)
        hdr = self.table_notifications.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table_notifications.setColumnWidth(2, 180)
        self.table_notifications.setColumnWidth(5, 84)
        self.table_notifications.cellClicked.connect(self._on_notification_cell_clicked)
        in_lay.addWidget(self.table_notifications)

        layout.addWidget(self.inbox_group)

        # -----------------------------------------------------------------
        # 4. 실시간 터미널 이벤트 로그 드로어 (Live Terminal Log Drawer)
        # -----------------------------------------------------------------
        log_card = QFrame()
        apply_qss_role(log_card, role="instrumentCard", obj_name="TerminalLogCard")
        log_lay = QVBoxLayout(log_card)
        log_lay.setContentsMargins(16, 12, 16, 14)
        log_lay.setSpacing(8)

        log_hdr = QHBoxLayout()
        lbl_log_title = QLabel("실시간 이벤트 및 알림 발송 터미널 로그 (LIVE STREAM LOG)")
        apply_qss_role(lbl_log_title, role="cardTitle")

        btn_clear_log = QPushButton("로그 지우기")
        apply_qss_role(btn_clear_log, role="secondary", obj_name="BtnClearNotifyLog")
        btn_clear_log.setMinimumHeight(28)
        btn_clear_log.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear_log.clicked.connect(lambda: self.txt_event_log.clear())

        log_hdr.addWidget(lbl_log_title)
        log_hdr.addStretch()
        log_hdr.addWidget(btn_clear_log)
        log_lay.addLayout(log_hdr)

        self.txt_event_log = QTextEdit()
        apply_qss_role(self.txt_event_log, role="terminalLog", obj_name="DarkLogConsole")
        self.txt_event_log.setStyleSheet(
            "QTextEdit { background-color: #0F172A; color: #E2E8F0; border: 1px solid #1E293B; "
            "border-radius: 8px; padding: 8px 10px; "
            "font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 11px; }"
        )
        self.txt_event_log.setReadOnly(True)
        self.txt_event_log.setMinimumHeight(160)
        self.txt_event_log.setPlaceholderText(
            "외부 연동 감지 이벤트 및 알림 발송 내역이 이곳에 실시간 스트리밍됩니다..."
        )
        log_lay.addWidget(self.txt_event_log, stretch=1)

        layout.addWidget(log_card, stretch=1)
        scroll.setWidget(container)
        outer_lay.addWidget(scroll)

    def _refresh_channel_status_chips(self) -> None:
        api_sec = self.config.get_section("api_and_integrations")
        sys_sec = self.config.get_section("system")

        toast_on = bool(sys_sec.get("show_tray_notifications", True))
        self.chip_toast.setText(
            f"Desktop Toast: {'Active' if toast_on else 'Muted'}"
        )
        apply_qss_role(
            self.chip_toast,
            role="badgeSuccess" if toast_on else "badgeNeutral",
            obj_name="ChipToastStatus",
        )

        self.chip_kakao.setText("KakaoTalk Macro: 미지원 (준비 중)")
        apply_qss_role(self.chip_kakao, role="badgeNeutral", obj_name="ChipKakaoStatus")

        webhook_url = str(api_sec.get("kakao_webhook_url", "")).strip()
        if webhook_url:
            self.chip_webhook.setText("Webhook: Ready (URL Bound)")
            apply_qss_role(self.chip_webhook, role="badgeSuccess", obj_name="ChipWebhookStatus")
        else:
            self.chip_webhook.setText("Webhook: Ready")
            apply_qss_role(self.chip_webhook, role="badgePrimary", obj_name="ChipWebhookStatus")

    def _toggle_sheet_watch(self) -> None:
        if self._watching_sheet:
            self.task_runner.stop_task(self.SHEET_WATCH_KEY)
            self._watching_sheet = False
            self.btn_toggle_sheet.setText("구글 시트 실시간 감시 (미지원 - 추후 연동 예정)")
            apply_qss_role(self.btn_toggle_sheet, role="secondary", obj_name="BtnToggleSheetWatch")
            self._append_log("구글 시트 실시간 감시가 중지되었습니다.")
            return

        self._append_log("구글 시트 실시간 감시는 현재 미지원 상태입니다. (Google Sheets API 미연동)")
        QMessageBox.information(
            self,
            "미지원 기능 안내",
            "Google Sheets API 실시간 폴링 연동이 아직 구현되지 않았습니다.",
        )

    def _on_new_sheet_row(self, row_data: dict) -> None:
        title = "구글 설문/시트 새 응답"
        msg = f"신규 시트 응답 감지: {row_data.get('student', '')} - {row_data.get('type', '')}"
        remote_id = str(row_data.get("remote_id", "")).strip() or None
        self.db.add_notification(
            title=title,
            message=msg,
            channel="google_sheets",
            sender=str(row_data.get("student", "")).strip(),
            remote_id=remote_id,
            is_read=0,
        )
        self._append_log(msg)
        self.notifier.notify(title, msg)
        self.refresh_notifications_inbox()
        self.unread_changed.emit()

    def _on_send_manual_notification(self) -> None:
        if not self.btn_send_now.isEnabled():
            return
        title = self.edit_notify_title.text().strip() or "알림"
        body = self.edit_notify_msg.text().strip()
        selected_channels: List[str] = []
        channel_labels: List[str] = []
        if self.chk_target_toast.isChecked():
            selected_channels.append("toast")
            channel_labels.append("Toast")
        if self.chk_target_kakao.isChecked():
            selected_channels.append("pc_macro")
            channel_labels.append("KakaoTalk")
        if self.chk_target_webhook.isChecked():
            selected_channels.append("webhook")
            channel_labels.append("Webhook")

        if not selected_channels:
            QMessageBox.warning(self, "채널 선택", "발송할 알림 채널을 1개 이상 선택해 주세요.")
            return

        ch_summary = ", ".join(channel_labels)
        orig_text = self.btn_send_now.text()
        self.btn_send_now.setEnabled(False)
        self.btn_send_now.setText("발송 중...")

        def _work(_worker: Any) -> List[str]:
            return self.notifier.notify(title, body, channels=selected_channels)

        def _on_result(results: Any) -> None:
            res_list = list(results or [])
            self.db.add_notification(
                title=title,
                message=body,
                channel=ch_summary,
                sender="manual",
                is_read=1,
            )
            self._append_log(
                f"[즉시 발송 | 채널: {ch_summary}] {title}: {body} -> {', '.join(res_list)}"
            )
            self.refresh_notifications_inbox()

        def _on_error(err: str) -> None:
            first_line = str(err or "").splitlines()[0] if err else "알 수 없는 오류"
            self._append_log(f"[즉시 발송 오류 | 채널: {ch_summary}] {title}: {first_line}")

        def _on_finished() -> None:
            try:
                self.btn_send_now.setText(orig_text)
                self.btn_send_now.setEnabled(True)
            except RuntimeError:
                pass

        worker = FunctionWorker("즉시 알림 발송", _work)
        self.task_runner.start_worker(
            worker,
            task_key="notify_send_manual",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=_on_finished,
        )

    def _on_push_comcigan_clicked(self) -> None:
        raw_data = self.txt_comcigan_payload.toPlainText().strip()
        if not raw_data:
            QMessageBox.warning(
                self,
                "입력 확인",
                "반영할 컴시간 시간표 변동 내역(텍스트 또는 JSON)을 입력해 주세요.",
            )
            self.txt_comcigan_payload.setFocus()
            return

        target_date = self.date_comcigan.date().toString("yyyy-MM-dd")
        grade = self.edit_comcigan_grade.text().strip()
        class_no = self.edit_comcigan_class.text().strip()

        self.btn_push_comcigan.setEnabled(False)
        self._append_log(
            f"[컴시간 Push 시작] 날짜={target_date}, 기본={grade or '-'}학년 {class_no or '-'}반"
        )

        def _run_push(_worker: FunctionWorker) -> dict:
            return push_comcigan_changes(
                raw_data=raw_data,
                target_date=target_date,
                grade=grade,
                class_no=class_no,
            )

        def _on_result(res: dict) -> None:
            ok = bool((res or {}).get("ok"))
            pushed = int((res or {}).get("pushed", 0) or 0)
            if ok:
                msg = f"{target_date} 시간표 변동 {pushed}건이 Firebase Firestore에 실시간 반영되었습니다."
                self._append_log(f"[컴시간 Push 완료] {msg}")
                self.notifier.notify("컴시간 시간표 변동 반영 완료", msg)
                self.db.add_notification(
                    title="컴시간 시간표 변동 반영 완료",
                    message=msg,
                    channel="google_sheets",
                    sender="comcigan",
                    is_read=1,
                )
                self.refresh_notifications_inbox()
            else:
                err_txt = str((res or {}).get("error") or "Firebase Firestore 반영에 실패했습니다.")
                self._append_log(f"[컴시간 Push 실패] {err_txt}")
                self.notifier.notify("컴시간 시간표 변동 반영 실패", err_txt)

        def _on_error(err: str) -> None:
            first_line = (err or "").strip().splitlines()[0] if err else "알 수 없는 오류"
            self._append_log(f"[컴시간 Push 오류] {first_line}")
            self.notifier.notify("컴시간 시간표 변동 반영 오류", first_line)

        worker = FunctionWorker("컴시간 시간표 변동 Firebase Firestore 실시간 반영", _run_push)
        self.task_runner.start_worker(
            worker=worker,
            task_key="comcigan_timetable_push",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=lambda: self.btn_push_comcigan.setEnabled(True),
        )

    def _on_comcigan_config_changed(self) -> None:
        self.config.update_section(
            "api_and_integrations",
            {
                "comcigan_url": self.edit_comcigan_url.text().strip(),
                "comcigan_school_code": self.edit_comcigan_school_code.text().strip(),
                "comcigan_teacher_name": self.edit_comcigan_teacher_name.text().strip(),
                "comcigan_target_grade": self.edit_comcigan_grade.text().strip(),
                "comcigan_target_class": self.edit_comcigan_class.text().strip(),
                "comcigan_auto_sync_enabled": self.chk_comcigan_auto_sync.isChecked(),
                "comcigan_auto_sync_interval_min": int(
                    self.combo_comcigan_interval.currentData() or 30
                ),
            },
            auto_save=True,
        )
        self._sync_comcigan_timer_state()

    def _sync_comcigan_timer_state(self) -> None:
        enabled = self.chk_comcigan_auto_sync.isChecked()
        interval_min = int(self.combo_comcigan_interval.currentData() or 30)
        if enabled:
            self._comcigan_timer.start(interval_min * 60 * 1000)
            self.lbl_comcigan_auto_status.setText(f"자동 주기 동기화 활성 ({interval_min}분마다)")
        else:
            self._comcigan_timer.stop()
            self.lbl_comcigan_auto_status.setText("마지막 확인: 자동 동기화 꺼짐")

    def _on_comcigan_auto_timer_tick(self) -> None:
        if self._comcigan_sync_running:
            return
        url = self.edit_comcigan_url.text().strip()
        if not url:
            self.lbl_comcigan_auto_status.setText("마지막 확인: URL 미입력으로 건너뜀")
            return

        school_code = self.edit_comcigan_school_code.text().strip()
        teacher_name = self.edit_comcigan_teacher_name.text().strip()
        grade = self.edit_comcigan_grade.text().strip()
        class_no = self.edit_comcigan_class.text().strip()
        scope = str(self.combo_comcigan_scope.currentData() or "homeroom_and_teacher")
        today_str = QDate.currentDate().toString("yyyy-MM-dd")

        self._comcigan_sync_running = True
        self.btn_auto_sync_comcigan.setEnabled(False)
        if hasattr(self, "btn_force_sync_comcigan"):
            self.btn_force_sync_comcigan.setEnabled(False)

        def _run_periodic(_worker: FunctionWorker) -> dict:
            return sync_comcigan_from_web(
                url=url,
                school_code=school_code,
                target_date=today_str,
                teacher_name=teacher_name,
                target_grade=grade,
                target_class=class_no,
                scope=scope,
                headless=True,
                prev_snapshot_hash=self._last_comcigan_snapshot_hash,
                skip_if_unchanged=True,
                progress_callback=lambda pct, msg: _worker.emit_progress(pct, msg),
            )

        def _on_result(res: dict) -> None:
            now_str = datetime.now().strftime("%H:%M:%S")
            snap_hash = str((res or {}).get("snapshot_hash", "") or "")
            if snap_hash:
                self._last_comcigan_snapshot_hash = snap_hash

            if (res or {}).get("skipped"):
                self._append_log("[INFO] 컴시간 주기 확인: 변경사항 없음")
                self.lbl_comcigan_auto_status.setText(f"마지막 확인: {now_str} (변경사항 없음)")
                return

            ok = bool((res or {}).get("ok"))
            b_cnt = int((res or {}).get("base_count", 0) or 0)
            cls_cnt = int((res or {}).get("class_base_count", b_cnt) or 0)
            tch_cnt = int((res or {}).get("teacher_base_count", 0) or 0)
            c_cnt = int((res or {}).get("change_count", 0) or 0)
            if ok:
                msg = (
                    f"{today_str} 기본 시간표 {b_cnt}건(학급 {cls_cnt}건 + 교사 {tch_cnt}건), "
                    f"변동 {c_cnt}건이 자동 반영되었습니다."
                )
                self._append_log(f"[컴시간 주기 자동 반영 완료] {msg}")
                self.lbl_comcigan_auto_status.setText(f"마지막 확인: {now_str} (변경 반영 완료)")
                self.notifier.notify("컴시간 시간표 변경 감지 및 반영", msg)
                self.db.add_notification(
                    title="컴시간 시간표 변경 감지 및 반영",
                    message=msg,
                    channel="google_sheets",
                    sender="comcigan_auto_timer",
                    is_read=0,
                )
                self.refresh_notifications_inbox()
                self.unread_changed.emit()
            else:
                err_txt = str((res or {}).get("error") or "컴시간 주기 동기화 실패")
                self._append_log(f"[컴시간 주기 동기화 실패] {err_txt}")
                self.lbl_comcigan_auto_status.setText(f"마지막 확인: {now_str} (오류)")

        def _on_error(err: str) -> None:
            now_str = datetime.now().strftime("%H:%M:%S")
            first_line = (err or "").strip().splitlines()[0] if err else "알 수 없는 오류"
            self._append_log(f"[컴시간 주기 동기화 오류] {first_line}")
            self.lbl_comcigan_auto_status.setText(f"마지막 확인: {now_str} (오류)")

        def _on_finished() -> None:
            self._comcigan_sync_running = False
            self.btn_auto_sync_comcigan.setEnabled(True)
            if hasattr(self, "btn_force_sync_comcigan"):
                self.btn_force_sync_comcigan.setEnabled(True)

        worker = FunctionWorker("컴시간 주기 자동 동기화", _run_periodic)
        worker.signals.progress.connect(
            lambda pct, msg: self.lbl_comcigan_auto_status.setText(f"진행 중 ({pct}%): {msg}")
        )
        self.task_runner.start_worker(
            worker=worker,
            task_key="comcigan_auto_sync",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=_on_finished,
        )

    def _on_auto_sync_comcigan_clicked(self, checked: bool = False, force_full: bool = False) -> None:
        if self._comcigan_sync_running:
            return
        url = self.edit_comcigan_url.text().strip()
        school_code = self.edit_comcigan_school_code.text().strip()
        teacher_name = self.edit_comcigan_teacher_name.text().strip()
        grade = self.edit_comcigan_grade.text().strip()
        class_no = self.edit_comcigan_class.text().strip()
        scope = str(self.combo_comcigan_scope.currentData() or "homeroom_and_teacher")
        if not url:
            QMessageBox.warning(
                self,
                "URL 확인",
                "컴시간 접속 URL을 먼저 입력해 주세요.",
            )
            self.edit_comcigan_url.setFocus()
            return

        self._on_comcigan_config_changed()
        target_date = self.date_comcigan.date().toString("yyyy-MM-dd")
        self._comcigan_sync_running = True
        self.btn_auto_sync_comcigan.setEnabled(False)
        if hasattr(self, "btn_force_sync_comcigan"):
            self.btn_force_sync_comcigan.setEnabled(False)
        self._append_log(
            f"[컴시간 웹 자동 파싱 시작] URL={url}, 학교={school_code or '(미지정)'}, "
            f"학년={grade or '(미지정)'}, 반={class_no or '(미지정)'}, "
            f"교사={teacher_name or '(미지정)'}, 범위={scope}, 날짜={target_date}"
        )

        def _run_auto(_worker: FunctionWorker) -> dict:
            return sync_comcigan_from_web(
                url=url,
                school_code=school_code,
                target_date=target_date,
                teacher_name=teacher_name,
                target_grade=grade,
                target_class=class_no,
                scope=scope,
                headless=True,
                force_full=force_full,
                progress_callback=lambda pct, msg: _worker.emit_progress(pct, msg),
            )

        def _on_result(res: dict) -> None:
            now_str = datetime.now().strftime("%H:%M:%S")
            snap_hash = str((res or {}).get("snapshot_hash", "") or "")
            if snap_hash:
                self._last_comcigan_snapshot_hash = snap_hash
            ok = bool((res or {}).get("ok"))
            b_cnt = int((res or {}).get("base_count", 0) or 0)
            cls_cnt = int((res or {}).get("class_base_count", b_cnt) or 0)
            tch_cnt = int((res or {}).get("teacher_base_count", 0) or 0)
            c_cnt = int((res or {}).get("change_count", 0) or 0)
            if ok:
                msg = (
                    f"{target_date} 기본 시간표 {b_cnt}건(학급 {cls_cnt}건 + 교사 {tch_cnt}건), "
                    f"변동 {c_cnt}건이 Firebase Firestore에 실시간 반영되었습니다."
                )
                self._append_log(f"[컴시간 웹 자동 파싱 완료] {msg}")
                self.lbl_comcigan_auto_status.setText(f"마지막 확인: {now_str} (수동 반영 완료)")
                self.notifier.notify("컴시간 웹 자동 파싱 완료", msg)
                self.db.add_notification(
                    title="컴시간 웹 자동 파싱 완료",
                    message=msg,
                    channel="google_sheets",
                    sender="comcigan_playwright",
                    is_read=1,
                )
                self.refresh_notifications_inbox()
            else:
                err_txt = str((res or {}).get("error") or "컴시간 웹 자동 반영에 실패했습니다.")
                self._append_log(f"[컴시간 웹 자동 파싱 실패] {err_txt}")
                self.lbl_comcigan_auto_status.setText(f"마지막 확인: {now_str} (실패)")
                self.notifier.notify("컴시간 웹 자동 파싱 실패", err_txt)

        def _on_error(err: str) -> None:
            now_str = datetime.now().strftime("%H:%M:%S")
            first_line = (err or "").strip().splitlines()[0] if err else "알 수 없는 오류"
            self._append_log(f"[컴시간 웹 자동 파싱 오류] {first_line} (디버그: logs/comcigan_debug.html 확인)")
            self.lbl_comcigan_auto_status.setText(f"마지막 확인: {now_str} (오류)")
            self.notifier.notify("컴시간 웹 자동 파싱 오류", first_line)

        def _on_finished() -> None:
            self._comcigan_sync_running = False
            self.btn_auto_sync_comcigan.setEnabled(True)
            if hasattr(self, "btn_force_sync_comcigan"):
                self.btn_force_sync_comcigan.setEnabled(True)

        def _on_progress(pct: int, msg: str) -> None:
            self.lbl_comcigan_auto_status.setText(f"진행 중 ({pct}%): {msg}")
            self._append_log(f"[진행 {pct}%] {msg}")

        worker = FunctionWorker("컴시간 웹 자동 파싱 및 Firebase Firestore 실시간 반영", _run_auto)
        worker.signals.progress.connect(_on_progress)
        self.task_runner.start_worker(
            worker=worker,
            task_key="comcigan_auto_sync",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=_on_finished,
        )

    def refresh_notifications_inbox(self) -> None:
        """SQLite notifications 테이블을 읽어 수신함 테이블과 미확인 배지를 갱신합니다."""
        unread_cnt = self.db.get_notification_unread_count()
        self.lbl_inbox_unread.setText(f"미확인 알림 {unread_cnt}건")
        role = "badgeWarning" if unread_cnt > 0 else "badgeNeutral"
        apply_qss_role(self.lbl_inbox_unread, role=role, obj_name="InboxUnreadBadge")
        self.btn_mark_all_noti_read.setEnabled(unread_cnt > 0)

        rows = self.db.get_notifications(limit=100, unread_only=False)
        self.table_notifications.setRowCount(0)
        for row_idx, item in enumerate(rows):
            self.table_notifications.insertRow(row_idx)
            noti_id = int(item["id"])
            is_read = bool(item.get("is_read", 0))

            status_item = QTableWidgetItem("읽음" if is_read else "● 미확인")
            status_item.setData(Qt.ItemDataRole.UserRole, noti_id)
            status_item.setData(Qt.ItemDataRole.UserRole + 1, 1 if is_read else 0)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if not is_read:
                status_item.setForeground(QColor("#EF4444"))
                f = status_item.font()
                f.setBold(True)
                status_item.setFont(f)
            else:
                status_item.setForeground(QColor("#64748B"))

            channel_item = QTableWidgetItem(str(item.get("channel") or "system"))
            channel_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            title_item = QTableWidgetItem(str(item.get("title") or ""))
            msg_item = QTableWidgetItem(str(item.get("message") or ""))
            ts_item = QTableWidgetItem(str(item.get("created_at") or ""))
            ts_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            if not is_read:
                for cell in (title_item, msg_item):
                    f = cell.font()
                    f.setBold(True)
                    cell.setFont(f)
                    cell.setBackground(QColor("#FEF2F2"))
                status_item.setBackground(QColor("#FEF2F2"))
                channel_item.setBackground(QColor("#FEF2F2"))
                ts_item.setBackground(QColor("#FEF2F2"))

            self.table_notifications.setItem(row_idx, 0, status_item)
            self.table_notifications.setItem(row_idx, 1, channel_item)
            self.table_notifications.setItem(row_idx, 2, title_item)
            self.table_notifications.setItem(row_idx, 3, msg_item)
            self.table_notifications.setItem(row_idx, 4, ts_item)

            if not is_read:
                btn_ack = QPushButton("확인")
                apply_qss_role(btn_ack, role="primary", obj_name="BtnAckNotification")
                btn_ack.setMinimumHeight(24)
                btn_ack.setCursor(Qt.CursorShape.PointingHandCursor)
                btn_ack.clicked.connect(
                    lambda _checked=False, nid=noti_id: self.mark_notification_read(nid)
                )
                self.table_notifications.setCellWidget(row_idx, 5, btn_ack)
            else:
                done_lbl = QLabel("확인됨")
                done_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                apply_qss_role(done_lbl, role="mutedText")
                self.table_notifications.setCellWidget(row_idx, 5, done_lbl)

    def _on_notification_cell_clicked(self, row: int, _col: int) -> None:
        item = self.table_notifications.item(row, 0)
        if item is None:
            return
        noti_id = item.data(Qt.ItemDataRole.UserRole)
        is_read = item.data(Qt.ItemDataRole.UserRole + 1)
        if noti_id is not None and not bool(is_read):
            self.mark_notification_read(int(noti_id))

    def mark_notification_read(self, notification_id: int) -> None:
        """개별 알림 항목을 읽음(is_read=1) 처리하고 사이드바 배지 갱신을 알립니다."""
        self.db.mark_notification_read(int(notification_id))
        self.refresh_notifications_inbox()
        self.unread_changed.emit()

    def mark_all_read(self) -> None:
        """수신함의 모든 미확인 알림을 일괄 읽음 처리합니다."""
        changed = self.db.mark_all_notifications_read()
        self._append_log(f"알림 수신함 모두 읽음 처리 완료 ({changed}건)")
        self.refresh_notifications_inbox()
        self.unread_changed.emit()

    def _append_log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_event_log.append(f"[{ts}] {text}")
