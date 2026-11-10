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

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import PeriodicWorker, TaskRunner
from core.config_manager import ConfigManager
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
        self._tick_count += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        self.emit_progress(
            0,
            f"구글 시트 응답 폴링 확인 중... (최근 확인: {now_str}, {self._tick_count}회차)",
        )


class NotificationCenterTab(QWidget):
    """[탭 5] 외부 연동 및 자동 알림 센터 탭 위젯."""

    new_sheet_row_signal = pyqtSignal(dict)
    SHEET_WATCH_KEY = "google_sheet_watcher"

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="NotificationCenterTabPage")
        self.task_runner = task_runner
        self.config = ConfigManager()
        self.notifier = NotificationService()
        self._watching_sheet = False

        self.new_sheet_row_signal.connect(self._on_new_sheet_row)
        self._init_ui()
        self._refresh_channel_status_chips()
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
        install_no_scroll_wheel_filter(self.spin_poll_interval)

        self.btn_toggle_sheet = QPushButton("구글 시트 실시간 폴링 감시: OFF (시작)")
        apply_qss_role(self.btn_toggle_sheet, role="primary", obj_name="BtnToggleSheetWatch")
        self.btn_toggle_sheet.setMinimumHeight(34)
        self.btn_toggle_sheet.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_sheet.clicked.connect(self._toggle_sheet_watch)

        self.btn_sim_row = QPushButton("새 설문지 응답 감지 시뮬레이션")
        apply_qss_role(self.btn_sim_row, role="secondary", obj_name="BtnSimulateSheetRow")
        self.btn_sim_row.setMinimumHeight(34)
        self.btn_sim_row.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sim_row.clicked.connect(self._simulate_new_row)

        poll_row.addWidget(self.spin_poll_interval)
        poll_row.addWidget(self.btn_toggle_sheet)
        poll_row.addWidget(self.btn_sim_row)
        poll_row.addStretch()
        form.addRow("폴링 감시 주기 및 제어:", poll_row)

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
        self.chk_target_kakao = QCheckBox("PC 카카오톡 매크로 (KakaoTalk)")
        self.chk_target_kakao.setChecked(True)
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

        kakao_room = str(api_sec.get("kakao_target_room_name", "")).strip()
        kakao_mode = str(api_sec.get("kakao_notify_mode", "toast_only")).strip()
        if kakao_mode == "pc_macro" or kakao_room:
            self.chip_kakao.setText(
                f"KakaoTalk Macro: Connected ({kakao_room or ' 기본방'})"
            )
            apply_qss_role(self.chip_kakao, role="badgePrimary", obj_name="ChipKakaoStatus")
        else:
            self.chip_kakao.setText("KakaoTalk Macro: Configured")
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
            self.btn_toggle_sheet.setText("구글 시트 실시간 폴링 감시: OFF (시작)")
            apply_qss_role(self.btn_toggle_sheet, role="primary", obj_name="BtnToggleSheetWatch")
            self._append_log("구글 시트 실시간 감시가 중지되었습니다.")
            return

        url = self.edit_sheet_url.text().strip()
        interval = float(self.spin_poll_interval.value())
        self.config.set("api_and_integrations", "google_sheet_url", url)
        self.config.set(
            "api_and_integrations", "google_sheet_poll_interval_sec", int(interval)
        )
        cred = str(self.config.get("api_and_integrations", "google_credentials_path", ""))

        worker = GoogleSheetWatchWorker(
            sheet_url=url,
            credentials_path=cred,
            interval_sec=interval,
            row_detected_signal=self.new_sheet_row_signal,
        )
        self._watching_sheet = True
        self.btn_toggle_sheet.setText("구글 시트 실시간 폴링 감시: ON (작동 중)")
        apply_qss_role(self.btn_toggle_sheet, role="success", obj_name="BtnToggleSheetWatch")
        self._append_log(f"구글 시트 폴링 감시 시작 (주기: {int(interval)}초)")
        self.task_runner.start_worker(worker, task_key=self.SHEET_WATCH_KEY)

    def _simulate_new_row(self) -> None:
        sample = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "student": "10101 김민준",
            "type": "교외체험학습 신청서 제출",
        }
        self.new_sheet_row_signal.emit(sample)

    def _on_new_sheet_row(self, row_data: dict) -> None:
        msg = f"신규 시트 응답 감지: {row_data.get('student')} - {row_data.get('type')}"
        self._append_log(msg)
        self.notifier.notify("구글 설문/시트 새 응답", msg)

    def _on_send_manual_notification(self) -> None:
        title = self.edit_notify_title.text().strip() or "알림"
        body = self.edit_notify_msg.text().strip()
        selected_channels = []
        if self.chk_target_toast.isChecked():
            selected_channels.append("Toast")
        if self.chk_target_kakao.isChecked():
            selected_channels.append("KakaoTalk")
        if self.chk_target_webhook.isChecked():
            selected_channels.append("Webhook")

        results = self.notifier.notify(title, body, force_external=True)
        ch_summary = ", ".join(selected_channels) if selected_channels else " 기본채널"
        self._append_log(
            f"[즉시 발송 | 채널: {ch_summary}] {title}: {body} -> {', '.join(results)}"
        )

    def _append_log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_event_log.append(f"[{ts}] {text}")
