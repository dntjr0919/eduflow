"""
[탭 6] ⚙️ 환경설정 및 마스터 데이터 관리 (ui/tabs/tab_settings.py)
- "🤖 AI 및 프롬프트 설정" QGroupBox 명시적 배치:
  1) 수동 프롬프트 모드 사용 (API 무시): QCheckBox (기본값 Checked, stateChanged 즉시 저장)
  2) AI 공급자 선택: QComboBox ("Google Gemini", "OpenAI", "Anthropic Claude", "OpenRouter/Ollama", currentTextChanged 즉시 저장)
  3) API Key: QLineEdit (EchoMode.Password, textChanged 즉시 저장)
  4) Custom Base URL: QLineEdit (기본값 빈 문자열, textChanged 즉시 저장)
  5) [🔌 연결 테스트] 버튼
- 모든 위젯에 setObjectName / setProperty("role", ...) 및 style().polish() 강제 적용
"""

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.llm_client import MultiLlmClient
from services.notifier import NotificationService
from ui.dialogs.manual_llm_dialog import ManualLlmDialog


PROVIDER_TEXT_TO_CODE: Dict[str, str] = {
    "Google Gemini": "gemini",
    "OpenAI": "openai",
    "Anthropic Claude": "claude",
    "OpenRouter/Ollama": "custom",
}

PROVIDER_CODE_TO_TEXT: Dict[str, str] = {
    v: k for k, v in PROVIDER_TEXT_TO_CODE.items()
}

PROVIDER_DEFAULT_MODELS: Dict[str, str] = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o",
    "claude": "claude-3-7-sonnet-latest",
    "custom": "llama3.2",
}


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


class StudentAddDialog(QDialog):
    """학생 개별 추가/수정 다이얼로그."""

    def __init__(self, parent: Optional[QWidget] = None, data: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("학생 정보 등록/수정")
        self.setMinimumWidth(400)
        apply_qss_role(self, obj_name="StudentAddModal")
        self.data = data or {}
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self.edit_student_id = QLineEdit(str(self.data.get("student_id", "")))
        self.edit_student_id.setPlaceholderText("예: 10101 (학년반번호 5자리)")

        self.spin_grade = QSpinBox()
        self.spin_grade.setRange(1, 6)
        self.spin_grade.setValue(int(self.data.get("grade", 1)))

        self.spin_class = QSpinBox()
        self.spin_class.setRange(1, 30)
        self.spin_class.setValue(int(self.data.get("class_no", 1)))

        self.spin_number = QSpinBox()
        self.spin_number.setRange(1, 99)
        self.spin_number.setValue(int(self.data.get("number", 1)))

        self.edit_name = QLineEdit(str(self.data.get("name", "")))
        self.edit_name.setPlaceholderText("학생 성명 (예: 홍길동)")

        self.edit_contact = QLineEdit(str(self.data.get("contact", "")))
        self.edit_contact.setPlaceholderText("010-0000-0000")

        self.edit_parent = QLineEdit(str(self.data.get("parent_contact", "")))
        self.edit_parent.setPlaceholderText("010-0000-0000")

        self.edit_memo = QLineEdit(str(self.data.get("memo", "")))

        form.addRow("학번 (고유코드):", self.edit_student_id)
        form.addRow("학년:", self.spin_grade)
        form.addRow("반:", self.spin_class)
        form.addRow("번호:", self.spin_number)
        form.addRow("이름:", self.edit_name)
        form.addRow("학생 연락처:", self.edit_contact)
        form.addRow("학부모 연락처:", self.edit_parent)
        form.addRow("비고/특기사항:", self.edit_memo)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        sid = self.edit_student_id.text().strip()
        name = self.edit_name.text().strip()
        if not name:
            QMessageBox.warning(self, "입력 확인", "학생 이름을 입력해 주세요.")
            return
        if not sid:
            sid = f"{self.spin_grade.value()}{self.spin_class.value():02d}{self.spin_number.value():02d}"
            self.edit_student_id.setText(sid)
        self.accept()

    def get_values(self) -> Dict[str, Any]:
        return {
            "student_id": self.edit_student_id.text().strip(),
            "grade": self.spin_grade.value(),
            "class_no": self.spin_class.value(),
            "number": self.spin_number.value(),
            "name": self.edit_name.text().strip(),
            "contact": self.edit_contact.text().strip(),
            "parent_contact": self.edit_parent.text().strip(),
            "memo": self.edit_memo.text().strip(),
        }


class SettingsTab(QWidget):
    """[탭 6] 환경설정 및 마스터 데이터 관리 탭 위젯."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="SettingsTabPage")
        self.config = ConfigManager()
        self.db = DatabaseManager()
        self.notifier = NotificationService()
        self._loading_ui: bool = False

        self._init_ui()
        self.load_settings_to_ui()
        self.refresh_students_table()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # =====================================================================
        # 좌측 패널: [1. 🤖 AI 및 프롬프트 설정] + [2. 폴더/감시] + [3. 외부 연동/트레이]
        # =====================================================================
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(12)

        # ---------------------------------------------------------------------
        # 1. 🤖 AI 및 프롬프트 설정 그룹박스 (명시적 배치 및 즉시 저장 연결)
        # ---------------------------------------------------------------------
        self.ai_group = QGroupBox("🤖 AI 및 프롬프트 설정")
        apply_qss_role(self.ai_group, obj_name="AiPromptSettingsGroup")
        ai_vbox = QVBoxLayout(self.ai_group)
        ai_vbox.setContentsMargins(15, 15, 15, 15)
        ai_vbox.setSpacing(10)

        # (1) 수동 프롬프트 모드 사용 (API 무시): QCheckBox (기본값 Checked)
        self.chk_manual_prompt_mode = QCheckBox("수동 프롬프트 모드 사용 (API 무시)")
        self.chk_manual_prompt_mode.setChecked(True)
        self.chk_manual_prompt_mode.setToolTip(
            "체크 시 API 키 없이도 ChatGPT, Claude, Gemini 웹 화면에 복사/붙여넣기하여 결과를 처리할 수 있습니다."
        )
        ai_vbox.addWidget(self.chk_manual_prompt_mode)

        ai_form = QFormLayout()
        ai_form.setSpacing(10)

        # (2) AI 공급자 선택: QComboBox ("Google Gemini", "OpenAI", "Anthropic Claude", "OpenRouter/Ollama")
        self.combo_ai_provider = QComboBox()
        self.combo_ai_provider.addItems(
            ["Google Gemini", "OpenAI", "Anthropic Claude", "OpenRouter/Ollama"]
        )
        ai_form.addRow("AI 공급자 선택:", self.combo_ai_provider)

        # (3) API Key: QLineEdit (EchoMode.Password)
        api_key_row = QHBoxLayout()
        self.edit_api_key = QLineEdit()
        self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_api_key.setPlaceholderText("선택한 AI 공급자의 API Key를 입력하세요")

        self.btn_toggle_api_key = QPushButton("표시")
        apply_qss_role(self.btn_toggle_api_key)
        self.btn_toggle_api_key.setCheckable(True)
        self.btn_toggle_api_key.setFixedWidth(60)
        self.btn_toggle_api_key.toggled.connect(
            lambda checked: self.edit_api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        api_key_row.addWidget(self.edit_api_key, stretch=1)
        api_key_row.addWidget(self.btn_toggle_api_key)
        ai_form.addRow("API Key:", api_key_row)

        # (4) Custom Base URL: QLineEdit (기본값은 비워둠)
        self.edit_custom_base_url = QLineEdit("")
        self.edit_custom_base_url.setPlaceholderText(
            "비워두면 기본 엔드포인트 사용 (예: https://openrouter.ai/api/v1 또는 http://localhost:11434/v1)"
        )
        ai_form.addRow("Custom Base URL:", self.edit_custom_base_url)

        # (보조) 사용할 모델명
        self.edit_model_name = QLineEdit("gemini-2.5-flash")
        self.edit_model_name.setPlaceholderText("예: gemini-2.5-flash, gpt-4o, claude-3-7-sonnet-latest, llama3.2")
        ai_form.addRow("사용할 모델명:", self.edit_model_name)

        ai_vbox.addLayout(ai_form)

        # (5) [🔌 연결 테스트] 버튼
        ai_btn_row = QHBoxLayout()
        self.btn_test_connection = QPushButton("🔌 연결 테스트")
        apply_qss_role(self.btn_test_connection, role="teal", obj_name="BtnTestAiConnection")
        self.btn_test_connection.setMinimumHeight(38)
        self.btn_test_connection.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_test_connection.clicked.connect(self._on_test_llm_connection)

        self.btn_preview_manual = QPushButton("📋 수동 복붙 모달 미리보기")
        apply_qss_role(self.btn_preview_manual, obj_name="BtnPreviewManualDialog")
        self.btn_preview_manual.setMinimumHeight(38)
        self.btn_preview_manual.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_preview_manual.clicked.connect(self._on_open_manual_dialog_preview)

        ai_btn_row.addWidget(self.btn_test_connection)
        ai_btn_row.addWidget(self.btn_preview_manual)
        ai_btn_row.addStretch()
        ai_vbox.addLayout(ai_btn_row)

        # 실시간 즉시 저장 시그널 연결 (stateChanged, currentTextChanged, textChanged)
        self.chk_manual_prompt_mode.stateChanged.connect(self._on_ai_setting_changed)
        self.combo_ai_provider.currentTextChanged.connect(self._on_provider_text_changed)
        self.edit_api_key.textChanged.connect(self._on_ai_setting_changed)
        self.edit_custom_base_url.textChanged.connect(self._on_ai_setting_changed)
        self.edit_model_name.textChanged.connect(self._on_ai_setting_changed)

        left_layout.addWidget(self.ai_group)

        # ---------------------------------------------------------------------
        # 2. 📁 기본 폴더 및 백그라운드 감시 설정
        # ---------------------------------------------------------------------
        folder_group = QGroupBox("📁 기본 폴더 및 백그라운드 감시 설정")
        apply_qss_role(folder_group, obj_name="FolderSettingsGroup")
        folder_form = QFormLayout(folder_group)
        folder_form.setContentsMargins(15, 15, 15, 15)
        folder_form.setSpacing(10)

        target_row = QHBoxLayout()
        self.edit_default_target = QLineEdit()
        btn_target_browse = QPushButton("변경...")
        apply_qss_role(btn_target_browse)
        btn_target_browse.clicked.connect(self._browse_target_dir)
        target_row.addWidget(self.edit_default_target)
        target_row.addWidget(btn_target_browse)

        self.spin_watch_interval = QSpinBox()
        self.spin_watch_interval.setRange(1, 3600)
        self.spin_watch_interval.setSuffix(" 초")

        self.chk_auto_watch = QCheckBox("프로그램 시작 시 실시간 폴더 감시 자동 시작")
        self.chk_ignore_temp = QCheckBox("브라우저 다운로드 임시파일(.crdownload, .part, ~$) 자동 무시")

        folder_form.addRow("분류 기준 상위 폴더:", target_row)
        folder_form.addRow("폴더 폴링 감시 주기:", self.spin_watch_interval)
        folder_form.addRow("", self.chk_auto_watch)
        folder_form.addRow("", self.chk_ignore_temp)
        left_layout.addWidget(folder_group)

        # ---------------------------------------------------------------------
        # 3. 🔔 외부 연동(Google 시트/카카오톡) 및 시스템 트레이 설정
        # ---------------------------------------------------------------------
        ext_group = QGroupBox("🔔 외부 연동(Google 시트/카카오톡) 및 시스템 트레이 설정")
        apply_qss_role(ext_group, obj_name="ExtSettingsGroup")
        ext_form = QFormLayout(ext_group)
        ext_form.setContentsMargins(15, 15, 15, 15)
        ext_form.setSpacing(10)

        cred_row = QHBoxLayout()
        self.edit_google_cred = QLineEdit()
        self.edit_google_cred.setPlaceholderText("Google API credentials.json 파일 경로")
        btn_cred_browse = QPushButton("찾아보기...")
        apply_qss_role(btn_cred_browse)
        btn_cred_browse.clicked.connect(self._browse_credentials_json)
        cred_row.addWidget(self.edit_google_cred)
        cred_row.addWidget(btn_cred_browse)

        self.edit_sheet_url = QLineEdit()
        self.edit_sheet_url.setPlaceholderText("감시할 Google 스프레드시트 URL 또는 문서 ID")

        self.spin_sheet_interval = QSpinBox()
        self.spin_sheet_interval.setRange(5, 3600)
        self.spin_sheet_interval.setSuffix(" 초")

        self.combo_kakao_mode = QComboBox()
        self.combo_kakao_mode.addItem("윈도우 데스크톱 토스트 알림만 사용", "toast_only")
        self.combo_kakao_mode.addItem("PC 카카오톡 열려있는 채팅방 매크로 전송", "pc_macro")
        self.combo_kakao_mode.addItem("웹훅(Webhook) URL로 메시지 전송", "webhook")

        self.edit_kakao_room = QLineEdit()
        self.edit_kakao_room.setPlaceholderText("PC 카카오톡 채팅방 이름 (예: 2학년 교무부 단톡방)")

        self.edit_kakao_webhook = QLineEdit()
        self.edit_kakao_webhook.setPlaceholderText("https://... (카카오워크/슬랙/디스코드 웹훅 URL)")

        self.chk_minimize_to_tray = QCheckBox(
            "창 닫기(X) 클릭 시 종료하지 않고 시스템 트레이로 숨기기 (백그라운드 유지)"
        )
        self.chk_tray_notify = QCheckBox("작업 완료 시 윈도우 트레이 토스트 알림 표시")

        ext_form.addRow("credentials.json:", cred_row)
        ext_form.addRow("Google 시트 URL:", self.edit_sheet_url)
        ext_form.addRow("구글 시트 감시 주기:", self.spin_sheet_interval)
        ext_form.addRow("알림/카톡 연동 방식:", self.combo_kakao_mode)
        ext_form.addRow("PC 카톡 채팅방명:", self.edit_kakao_room)
        ext_form.addRow("Webhook URL:", self.edit_kakao_webhook)
        ext_form.addRow("", self.chk_minimize_to_tray)
        ext_form.addRow("", self.chk_tray_notify)
        left_layout.addWidget(ext_group)

        # 하단 전체 저장 및 알림 테스트 버튼 바
        save_bar = QHBoxLayout()
        btn_save_cfg = QPushButton("💾 환경설정 모두 저장")
        apply_qss_role(btn_save_cfg, role="primary", obj_name="BtnSaveAllSettings")
        btn_save_cfg.setMinimumHeight(38)
        btn_save_cfg.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save_cfg.clicked.connect(self.save_ui_to_settings)

        btn_test_notify = QPushButton("🔔 알림 테스트 발송")
        apply_qss_role(btn_test_notify, obj_name="BtnTestNotify")
        btn_test_notify.setMinimumHeight(38)
        btn_test_notify.clicked.connect(self._on_test_notification)

        btn_reset_cfg = QPushButton("초기값 복원")
        apply_qss_role(btn_reset_cfg, obj_name="BtnResetSettings")
        btn_reset_cfg.setMinimumHeight(38)
        btn_reset_cfg.clicked.connect(self._on_reset_config)

        save_bar.addWidget(btn_save_cfg)
        save_bar.addWidget(btn_test_notify)
        save_bar.addStretch()
        save_bar.addWidget(btn_reset_cfg)
        left_layout.addLayout(save_bar)
        left_layout.addStretch()

        splitter.addWidget(left_widget)

        # =====================================================================
        # 우측 패널: 학생 명렬표 (마스터 데이터) CSV/엑셀 등록 및 관리
        # =====================================================================
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 0, 0, 0)

        stu_group = QGroupBox("🎓 학생 명렬표 마스터 데이터 관리 (파일 취합·성적·출결 탭 공통 연동)")
        apply_qss_role(stu_group, obj_name="StudentMasterGroup")
        stu_layout = QVBoxLayout(stu_group)
        stu_layout.setContentsMargins(15, 15, 15, 15)
        stu_layout.setSpacing(10)

        stu_btn_bar = QHBoxLayout()
        btn_import_file = QPushButton("📥 CSV / 엑셀(.xlsx) 일괄 등록")
        apply_qss_role(btn_import_file, role="primary", obj_name="BtnImportStudents")
        btn_export_sample = QPushButton("📤 양식/명렬표 내보내기")
        apply_qss_role(btn_export_sample)
        btn_add_stu = QPushButton("+ 학생 추가")
        apply_qss_role(btn_add_stu)
        btn_del_stu = QPushButton("- 선택 삭제")
        apply_qss_role(btn_del_stu)
        btn_clear_stu = QPushButton("🧹 전체 삭제")
        apply_qss_role(btn_clear_stu, role="warningOutline")

        btn_import_file.clicked.connect(self._on_import_students_file)
        btn_export_sample.clicked.connect(self._on_export_students_file)
        btn_add_stu.clicked.connect(self._on_add_student)
        btn_del_stu.clicked.connect(self._on_delete_student)
        btn_clear_stu.clicked.connect(self._on_clear_students)

        stu_btn_bar.addWidget(btn_import_file)
        stu_btn_bar.addWidget(btn_export_sample)
        stu_btn_bar.addWidget(btn_add_stu)
        stu_btn_bar.addWidget(btn_del_stu)
        stu_btn_bar.addStretch()
        stu_btn_bar.addWidget(btn_clear_stu)
        stu_layout.addLayout(stu_btn_bar)

        self.lbl_student_count = QLabel("등록된 학생: 0명")
        apply_qss_role(self.lbl_student_count, role="mutedText")
        stu_layout.addWidget(self.lbl_student_count)

        self.table_students = QTableWidget(0, 7)
        self.table_students.setAlternatingRowColors(True)
        self.table_students.setHorizontalHeaderLabels(
            ["학번", "학년", "반", "번호", "이름", "학생 연락처", "학부모 연락처 / 비고"]
        )
        self.table_students.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_students.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_students.doubleClicked.connect(self._on_edit_student)
        s_header = self.table_students.horizontalHeader()
        s_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        s_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        stu_layout.addWidget(self.table_students)
        right_layout.addWidget(stu_group)

        splitter.addWidget(right_widget)
        splitter.setSizes([560, 540])
        main_layout.addWidget(splitter)

    # =========================================================================
    # AI 및 프롬프트 설정 실시간 즉시 저장 핸들러 (stateChanged / currentTextChanged / textChanged)
    # =========================================================================
    def _get_selected_provider_code(self) -> str:
        text = self.combo_ai_provider.currentText().strip()
        return PROVIDER_TEXT_TO_CODE.get(text, "gemini")

    def _on_provider_text_changed(self, provider_text: str) -> None:
        if self._loading_ui:
            return
        prov_code = PROVIDER_TEXT_TO_CODE.get(provider_text, "gemini")
        api_sec = self.config.get_section("api_and_integrations")

        self._loading_ui = True
        try:
            key_field_map = {
                "gemini": "gemini_api_key",
                "openai": "openai_api_key",
                "claude": "claude_api_key",
                "custom": "custom_api_key",
            }
            model_field_map = {
                "gemini": "gemini_model",
                "openai": "openai_model",
                "claude": "claude_model",
                "custom": "custom_model",
            }
            saved_key = api_sec.get(key_field_map[prov_code], "")
            saved_model = api_sec.get(
                model_field_map[prov_code], PROVIDER_DEFAULT_MODELS[prov_code]
            )
            self.edit_api_key.setText(str(saved_key))
            self.edit_model_name.setText(str(saved_model))
        finally:
            self._loading_ui = False

        self._on_ai_setting_changed()

    def _on_ai_setting_changed(self, *_args: Any) -> None:
        """
        AI 설정 위젯(수동 모드 체크박스, 공급자 콤보박스, API Key, Custom Base URL, 모델명) 값이
        바뀔 때마다 config_manager에 즉시 저장합니다.
        """
        if self._loading_ui:
            return

        prov_code = self._get_selected_provider_code()
        api_key = self.edit_api_key.text().strip()
        custom_url = self.edit_custom_base_url.text().strip()
        model_name = self.edit_model_name.text().strip() or PROVIDER_DEFAULT_MODELS.get(prov_code, "gemini-2.5-flash")

        updates: Dict[str, Any] = {
            "use_manual_prompt_mode": self.chk_manual_prompt_mode.isChecked(),
            "ai_provider": prov_code,
            "custom_base_url": custom_url,
        }
        if prov_code == "gemini":
            updates["gemini_api_key"] = api_key
            updates["gemini_model"] = model_name
        elif prov_code == "openai":
            updates["openai_api_key"] = api_key
            updates["openai_model"] = model_name
        elif prov_code == "claude":
            updates["claude_api_key"] = api_key
            updates["claude_model"] = model_name
        elif prov_code == "custom":
            updates["custom_api_key"] = api_key
            updates["custom_model"] = model_name

        self.config.update_section("api_and_integrations", updates, auto_save=True)

    def _on_test_llm_connection(self) -> None:
        """[🔌 연결 테스트] 버튼 클릭 시 현재 입력된 AI 설정으로 즉시 연결을 테스트합니다."""
        self._on_ai_setting_changed()

        prov_code = self._get_selected_provider_code()
        api_key = self.edit_api_key.text().strip()
        custom_url = self.edit_custom_base_url.text().strip() or None
        model_name = self.edit_model_name.text().strip() or PROVIDER_DEFAULT_MODELS.get(prov_code, "gemini-2.5-flash")

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            client = MultiLlmClient(
                provider=prov_code,
                api_key=api_key,
                model_name=model_name,
                base_url=custom_url,
            )
            ok, msg = client.test_connection()
        finally:
            QApplication.restoreOverrideCursor()

        if ok:
            QMessageBox.information(self, "AI API 연결 테스트 성공", msg)
        else:
            QMessageBox.warning(self, "AI API 연결 테스트 실패", msg)

    def _on_open_manual_dialog_preview(self) -> None:
        dlg = ManualLlmDialog(
            self,
            task_key="ocr_correction",
            raw_content="2026학년도 교내 과학탐구대회 운영 계획 안내\n일시: 2026. 11. 20.(금) 5~6교시\n대상: 전교생 희망자",
        )
        dlg.exec()

    # =========================================================================
    # 설정 불러오기 및 전체 저장
    # =========================================================================
    def load_settings_to_ui(self) -> None:
        self._loading_ui = True
        try:
            fs = self.config.get_section("file_sorter")
            api = self.config.get_section("api_and_integrations")
            sys_sec = self.config.get_section("system")

            # 1) AI 및 프롬프트 설정 로드
            self.chk_manual_prompt_mode.setChecked(
                bool(api.get("use_manual_prompt_mode", True))
            )

            prov_code = str(api.get("ai_provider", "gemini")).lower().strip()
            prov_text = PROVIDER_CODE_TO_TEXT.get(prov_code, "Google Gemini")
            idx = self.combo_ai_provider.findText(prov_text)
            if idx >= 0:
                self.combo_ai_provider.setCurrentIndex(idx)

            key_field_map = {
                "gemini": "gemini_api_key",
                "openai": "openai_api_key",
                "claude": "claude_api_key",
                "custom": "custom_api_key",
            }
            model_field_map = {
                "gemini": "gemini_model",
                "openai": "openai_model",
                "claude": "claude_model",
                "custom": "custom_model",
            }
            self.edit_api_key.setText(str(api.get(key_field_map.get(prov_code, "gemini_api_key"), "")))
            self.edit_custom_base_url.setText(str(api.get("custom_base_url", "")))
            self.edit_model_name.setText(
                str(api.get(model_field_map.get(prov_code, "gemini_model"), PROVIDER_DEFAULT_MODELS.get(prov_code, "gemini-2.5-flash")))
            )

            # 2) 폴더 및 감시 설정 로드
            self.edit_default_target.setText(fs.get("target_base_dir", ""))
            self.spin_watch_interval.setValue(int(fs.get("watch_interval_sec", 3)))
            self.chk_auto_watch.setChecked(bool(fs.get("auto_watch_on_startup", False)))
            self.chk_ignore_temp.setChecked(bool(fs.get("ignore_temp_files", True)))

            # 3) 외부 연동 및 트레이 설정 로드
            self.edit_google_cred.setText(api.get("google_credentials_path", ""))
            self.edit_sheet_url.setText(api.get("google_sheet_url", ""))
            self.spin_sheet_interval.setValue(
                int(api.get("google_sheet_poll_interval_sec", 30))
            )

            k_idx = self.combo_kakao_mode.findData(
                api.get("kakao_notify_mode", "toast_only")
            )
            if k_idx >= 0:
                self.combo_kakao_mode.setCurrentIndex(k_idx)
            self.edit_kakao_room.setText(api.get("kakao_target_room_name", ""))
            self.edit_kakao_webhook.setText(api.get("kakao_webhook_url", ""))

            self.chk_minimize_to_tray.setChecked(
                bool(sys_sec.get("minimize_to_tray_on_close", True))
            )
            self.chk_tray_notify.setChecked(
                bool(sys_sec.get("show_tray_notifications", True))
            )
        finally:
            self._loading_ui = False

    def _persist_settings_silently(self) -> None:
        self._on_ai_setting_changed()
        self.config.update_section(
            "file_sorter",
            {
                "target_base_dir": self.edit_default_target.text().strip(),
                "watch_interval_sec": self.spin_watch_interval.value(),
                "auto_watch_on_startup": self.chk_auto_watch.isChecked(),
                "ignore_temp_files": self.chk_ignore_temp.isChecked(),
            },
            auto_save=False,
        )
        self.config.update_section(
            "api_and_integrations",
            {
                "google_credentials_path": self.edit_google_cred.text().strip(),
                "google_sheet_url": self.edit_sheet_url.text().strip(),
                "google_sheet_poll_interval_sec": self.spin_sheet_interval.value(),
                "kakao_notify_mode": self.combo_kakao_mode.currentData(),
                "kakao_target_room_name": self.edit_kakao_room.text().strip(),
                "kakao_webhook_url": self.edit_kakao_webhook.text().strip(),
            },
            auto_save=False,
        )
        self.config.update_section(
            "system",
            {
                "minimize_to_tray_on_close": self.chk_minimize_to_tray.isChecked(),
                "show_tray_notifications": self.chk_tray_notify.isChecked(),
            },
            auto_save=True,
        )

    def save_ui_to_settings(self) -> None:
        self._persist_settings_silently()
        QMessageBox.information(self, "저장 완료", "환경설정이 config.json에 안전하게 저장되었습니다.")

    def _browse_target_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "분류 기준 상위 폴더 선택")
        if folder:
            self.edit_default_target.setText(str(Path(folder).resolve()))

    def _browse_credentials_json(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Google API credentials.json 선택", "", "JSON Files (*.json);;All Files (*)"
        )
        if file_path:
            self.edit_google_cred.setText(str(Path(file_path).resolve()))

    def _on_test_notification(self) -> None:
        self._persist_settings_silently()
        results = self.notifier.notify(
            "환경설정 알림 테스트",
            "교무/업무 자동화 워크스테이션 알림 연동이 정상적으로 작동합니다.",
            force_external=True,
        )
        QMessageBox.information(
            self,
            "알림 테스트 결과",
            "발송 결과:\n- " + "\n- ".join(results if results else ["알림 채널 처리 완료"]),
        )

    def _on_reset_config(self) -> None:
        ans = QMessageBox.question(self, "설정 초기화", "모든 환경설정을 초기 기본값으로 되돌리시겠습니까?")
        if ans == QMessageBox.StandardButton.Yes:
            self.config.reset_to_defaults()
            self.load_settings_to_ui()

    # =========================================================================
    # 학생 명렬표 마스터 데이터 관리 (CSV / Excel 입출력)
    # =========================================================================
    def refresh_students_table(self) -> None:
        students = self.db.get_students()
        self.table_students.setRowCount(0)
        self.lbl_student_count.setText(f"등록된 학생: 총 {len(students)}명")

        for row_idx, s in enumerate(students):
            self.table_students.insertRow(row_idx)
            sid_item = QTableWidgetItem(str(s["student_id"]))
            sid_item.setData(Qt.ItemDataRole.UserRole, s)
            sid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            g_item = QTableWidgetItem(str(s["grade"]))
            g_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            c_item = QTableWidgetItem(str(s["class_no"]))
            c_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            n_item = QTableWidgetItem(str(s["number"]))
            n_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            name_item = QTableWidgetItem(s["name"])
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            contact_item = QTableWidgetItem(s["contact"])

            parent_memo = s["parent_contact"]
            if s["memo"]:
                parent_memo = f"{parent_memo} ({s['memo']})" if parent_memo else s["memo"]
            pm_item = QTableWidgetItem(parent_memo)

            self.table_students.setItem(row_idx, 0, sid_item)
            self.table_students.setItem(row_idx, 1, g_item)
            self.table_students.setItem(row_idx, 2, c_item)
            self.table_students.setItem(row_idx, 3, n_item)
            self.table_students.setItem(row_idx, 4, name_item)
            self.table_students.setItem(row_idx, 5, contact_item)
            self.table_students.setItem(row_idx, 6, pm_item)

    def _on_add_student(self) -> None:
        dlg = StudentAddDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            self.db.upsert_student(**vals)
            self.refresh_students_table()

    def _on_edit_student(self) -> None:
        row = self.table_students.currentRow()
        if row < 0:
            return
        sid_item = self.table_students.item(row, 0)
        if not sid_item:
            return
        s_data = sid_item.data(Qt.ItemDataRole.UserRole)
        dlg = StudentAddDialog(self, data=s_data)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            self.db.upsert_student(**vals)
            self.refresh_students_table()

    def _on_delete_student(self) -> None:
        row = self.table_students.currentRow()
        if row < 0:
            QMessageBox.information(self, "안내", "삭제할 학생을 선택해 주세요.")
            return
        sid_item = self.table_students.item(row, 0)
        if sid_item:
            self.db.delete_student(sid_item.text())
            self.refresh_students_table()

    def _on_clear_students(self) -> None:
        ans = QMessageBox.question(self, "전체 삭제", "등록된 학생 명렬표 데이터를 모두 삭제하시겠습니까?")
        if ans == QMessageBox.StandardButton.Yes:
            self.db.clear_students()
            self.refresh_students_table()

    def _on_import_students_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "학생 명렬표 CSV 또는 Excel(.xlsx) 파일 선택",
            "",
            "명렬표 파일 (*.csv *.xlsx);;CSV Files (*.csv);;Excel Files (*.xlsx)",
        )
        if not file_path:
            return

        path_obj = Path(file_path)
        rows_loaded: List[List[Any]] = []

        try:
            if path_obj.suffix.lower() == ".xlsx":
                import openpyxl
                wb = openpyxl.load_workbook(file_path, data_only=True)
                ws = wb.active
                for row in ws.iter_rows(values_only=True):
                    if any(cell is not None for cell in row):
                        rows_loaded.append([str(c).strip() if c is not None else "" for c in row])
            else:
                content = None
                for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
                    try:
                        with open(file_path, "r", encoding=enc, newline="") as f:
                            content = list(csv.reader(f))
                        break
                    except UnicodeDecodeError:
                        continue
                if content is None:
                    raise ValueError("CSV 파일 인코딩을 읽을 수 없습니다.")
                rows_loaded = content

            count = 0
            for idx, r in enumerate(rows_loaded):
                if not r or len(r) < 2:
                    continue
                first_col = str(r[0]).strip()
                if idx == 0 and any(h in first_col for h in ("학번", "학년", "student", "번호", "이름")):
                    continue

                if len(r) >= 5:
                    sid = str(r[0]).strip()
                    grade = int(r[1]) if str(r[1]).isdigit() else 1
                    class_no = int(r[2]) if str(r[2]).isdigit() else 1
                    number = int(r[3]) if str(r[3]).isdigit() else (idx + 1)
                    name = str(r[4]).strip()
                    contact = str(r[5]).strip() if len(r) > 5 else ""
                    parent = str(r[6]).strip() if len(r) > 6 else ""
                    memo = str(r[7]).strip() if len(r) > 7 else ""
                else:
                    sid = str(r[0]).strip()
                    name = str(r[1]).strip()
                    grade = int(sid[0]) if len(sid) == 5 and sid.isdigit() else 1
                    class_no = int(sid[1:3]) if len(sid) == 5 and sid.isdigit() else 1
                    number = int(sid[3:5]) if len(sid) == 5 and sid.isdigit() else (idx + 1)
                    contact = str(r[2]).strip() if len(r) > 2 else ""
                    parent = str(r[3]).strip() if len(r) > 3 else ""
                    memo = ""

                if not sid:
                    sid = f"{grade}{class_no:02d}{number:02d}"
                if name:
                    self.db.upsert_student(
                        student_id=sid,
                        grade=grade,
                        class_no=class_no,
                        number=number,
                        name=name,
                        contact=contact,
                        parent_contact=parent,
                        memo=memo,
                    )
                    count += 1

            self.refresh_students_table()
            QMessageBox.information(self, "일괄 등록 완료", f"총 {count}명의 학생 명렬표를 성공적으로 등록했습니다.")
        except Exception as exc:
            QMessageBox.critical(self, "파일 가져오기 오류", f"명렬표 파일을 읽는 중 오류가 발생했습니다:\n{exc}")

    def _on_export_students_file(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "학생 명렬표/양식 내보내기",
            "학생명렬표_양식.csv",
            "CSV 파일 (*.csv);;Excel 파일 (*.xlsx)",
        )
        if not file_path:
            return

        students = self.db.get_students()
        headers = ["학번", "학년", "반", "번호", "이름", "학생연락처", "학부모연락처", "비고"]

        rows_to_write = []
        if students:
            for s in students:
                rows_to_write.append(
                    [
                        s["student_id"],
                        s["grade"],
                        s["class_no"],
                        s["number"],
                        s["name"],
                        s["contact"],
                        s["parent_contact"],
                        s["memo"],
                    ]
                )
        else:
            rows_to_write = [
                ["10101", 1, 1, 1, "김민준", "010-1111-2222", "010-3333-4444", "학급회장"],
                ["10102", 1, 1, 2, "이서연", "010-2222-3333", "010-4444-5555", ""],
                ["10103", 1, 1, 3, "박지호", "010-3333-4444", "010-5555-6666", ""],
            ]

        try:
            if file_path.lower().endswith(".xlsx"):
                import openpyxl
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "학생명렬표"
                ws.append(headers)
                for r in rows_to_write:
                    ws.append(r)
                wb.save(file_path)
            else:
                with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    writer.writerows(rows_to_write)
            QMessageBox.information(self, "내보내기 완료", f"명렬표 파일이 저장되었습니다:\n{file_path}")
        except Exception as exc:
            QMessageBox.critical(self, "내보내기 실패", f"파일 저장 중 오류가 발생했습니다:\n{exc}")
