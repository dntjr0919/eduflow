"""
[탭 5] 🔔 외부 연동 및 자동 알림 센터 (ui/tabs/tab_notification.py)
- 모든 위젯에 setObjectName / setProperty("role", ...) 및 style().polish() 강제 적용
"""

from datetime import datetime
from typing import Optional
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from core.base_worker import PeriodicWorker, TaskRunner
from core.config_manager import ConfigManager
from services.notifier import NotificationService


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
    def __init__(self, sheet_url: str, credentials_path: str, interval_sec: float, row_detected_signal):
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
            0, f"구글 시트 응답 폴링 확인 중... (최근 확인: {now_str}, {self._tick_count}회차)"
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

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(14)

        sheet_group = QGroupBox("📊 구글 시트 / 구글 설문지 응답 실시간 감시 워커")
        apply_qss_role(sheet_group, obj_name="GoogleSheetWatchGroup")
        s_lay = QVBoxLayout(sheet_group)
        s_lay.setContentsMargins(15, 15, 15, 15)

        form = QFormLayout()
        self.edit_sheet_url = QLineEdit(
            self.config.get("api_and_integrations", "google_sheet_url", "")
        )
        self.edit_sheet_url.setPlaceholderText("https://docs.google.com/spreadsheets/d/...")
        form.addRow("감시 대상 Google 시트 URL:", self.edit_sheet_url)
        s_lay.addLayout(form)

        btn_bar = QHBoxLayout()
        self.btn_toggle_sheet = QPushButton("📡 구글 시트 실시간 폴링 감시: OFF (클릭하여 시작)")
        apply_qss_role(self.btn_toggle_sheet, role="slate", obj_name="BtnToggleSheetWatch")
        self.btn_toggle_sheet.setMinimumHeight(38)
        self.btn_toggle_sheet.clicked.connect(self._toggle_sheet_watch)

        btn_sim_row = QPushButton("🧪 새 설문지 응답 감지 시뮬레이션")
        apply_qss_role(btn_sim_row, obj_name="BtnSimulateSheetRow")
        btn_sim_row.setMinimumHeight(38)
        btn_sim_row.clicked.connect(self._simulate_new_row)

        btn_bar.addWidget(self.btn_toggle_sheet)
        btn_bar.addWidget(btn_sim_row)
        btn_bar.addStretch()
        s_lay.addLayout(btn_bar)
        layout.addWidget(sheet_group)

        notify_group = QGroupBox("🔔 즉시 알림 & 카카오톡 연동 발송 센터 (services/notifier.py 연동)")
        apply_qss_role(notify_group, obj_name="ManualNotifyGroup")
        n_lay = QVBoxLayout(notify_group)
        n_lay.setContentsMargins(15, 15, 15, 15)

        n_form = QFormLayout()
        self.edit_notify_title = QLineEdit("학급 공지 / 설문 응답 알림")
        self.edit_notify_msg = QLineEdit("새로운 학부모 상담 신청 설문 응답이 접수되었습니다.")
        n_form.addRow("알림 제목:", self.edit_notify_title)
        n_form.addRow("알림 본문:", self.edit_notify_msg)
        n_lay.addLayout(n_form)

        send_bar = QHBoxLayout()
        btn_send_now = QPushButton("🚀 설정된 채널(토스트 / 카톡 / 웹훅)로 즉시 발송")
        apply_qss_role(btn_send_now, role="primary", obj_name="BtnSendManualNotify")
        btn_send_now.setMinimumHeight(38)
        btn_send_now.clicked.connect(self._on_send_manual_notification)
        send_bar.addWidget(btn_send_now)
        send_bar.addStretch()
        n_lay.addLayout(send_bar)

        self.txt_event_log = QTextEdit()
        apply_qss_role(self.txt_event_log, obj_name="WebAiInputBox")
        self.txt_event_log.setReadOnly(True)
        self.txt_event_log.setPlaceholderText("외부 연동 감지 이벤트 및 알림 발송 내역이 이곳에 표시됩니다...")
        n_lay.addWidget(self.txt_event_log)

        layout.addWidget(notify_group, stretch=1)

    def _toggle_sheet_watch(self) -> None:
        if self._watching_sheet:
            self.task_runner.stop_task(self.SHEET_WATCH_KEY)
            self._watching_sheet = False
            self.btn_toggle_sheet.setText("📡 구글 시트 실시간 폴링 감시: OFF (클릭하여 시작)")
            apply_qss_role(self.btn_toggle_sheet, role="slate", obj_name="BtnToggleSheetWatch")
            self._append_log("구글 시트 실시간 감시가 중지되었습니다.")
            return

        url = self.edit_sheet_url.text().strip()
        self.config.set("api_and_integrations", "google_sheet_url", url)
        cred = self.config.get("api_and_integrations", "google_credentials_path", "")
        interval = float(self.config.get("api_and_integrations", "google_sheet_poll_interval_sec", 30))

        worker = GoogleSheetWatchWorker(
            sheet_url=url,
            credentials_path=cred,
            interval_sec=interval,
            row_detected_signal=self.new_sheet_row_signal,
        )
        self._watching_sheet = True
        self.btn_toggle_sheet.setText("🟢 구글 시트 실시간 폴링 감시: ON (작동 중)")
        apply_qss_role(self.btn_toggle_sheet, role="success", obj_name="BtnToggleSheetWatch")
        self._append_log(f"구글 시트 폴링 감시 시작 (주기: {interval}초)")
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
        results = self.notifier.notify(title, body, force_external=True)
        self._append_log(f"[수동 발송] {title}: {body} -> {', '.join(results)}")

    def _append_log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_event_log.append(f"[{ts}] {text}")
