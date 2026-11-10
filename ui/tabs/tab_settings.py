"""
[탭 6] ⚙️ 환경설정 및 마스터 데이터 관리 (ui/tabs/tab_settings.py)
- [개편] 호버 확대(HoverZoomCard) 방식을 폐지하고, 좌측 설정 카드를 클릭형 아코디언(AccordionCard) 리스트로 교체
  (헤더 클릭 시에만 220ms InOutQuad 슬라이딩으로 펼침/접힘, 기본 단일 모드: 한 번에 하나만 펼침)
- INPUT_FIELD_STYLE 명시 적용 및 모든 QLineEdit, QComboBox, QSpinBox에 높이 34px,
  QFormLayout에 setVerticalSpacing(14), setHorizontalSpacing(16), setContentsMargins(16, 16, 16, 16) 적용하여
  입력칸 높이 찌그러짐 및 텍스트 겹침 현상을 완벽히 해결
- 부모 컨테이너의 강제 고정 높이를 제거하고 QSizePolicy.Policy.MinimumExpanding 기반 자동 계산 및 QScrollArea 적용
"""

import csv
import html
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import FunctionWorker, TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.llm_client import (
    MultiLlmClient,
    fetch_available_models,
    is_ollama_base_url,
    pick_recommended_model,
)
from services.notifier import NotificationService
from ui.components.collapsible_card import AccordionCard, AccordionGroup
from ui.components.hover_zoom_card import HoverZoomCard
from ui.dialogs.manual_llm_dialog import ManualLlmDialog
from ui.utils import (
    COMBOBOX_STYLE,
    NoScrollWheelFilter,
    SearchableModelComboBox,
    install_no_scroll_wheel_filter,
)


# 좌측 환경설정 아코디언: True = 한 번에 하나만 펼침(단일 모드), False = 여러 개 동시 펼침(다중 모드)
SETTINGS_ACCORDION_EXCLUSIVE = True

INPUT_FIELD_STYLE = """
QLineEdit, QComboBox, QSpinBox {
    min-height: 34px;
    height: 34px;
    font-size: 13px;
    padding: 4px 10px;
    border: 1px solid #D0D5DD;
    border-radius: 6px;
    background-color: #FFFFFF;
    color: #0F172A;
}
QComboBox {
    padding: 2px 30px 2px 10px;
    color: #101828;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1.5px solid #4F46E5;
    background-color: #FFFFFF;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border-left: 1px solid #E4E7EC;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
    background-color: #F8FAFC;
}
QComboBox::drop-down:hover {
    background-color: #EEF2F6;
}
QComboBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #475467;
    margin-right: 2px;
}
QComboBox QAbstractItemView {
    border: 1px solid #CBD5E1;
    border-radius: 6px;
    background-color: #FFFFFF;
    selection-background-color: #EEF2F6;
    selection-color: #1D2939;
    padding: 4px;
    outline: none;
    min-height: 120px;
}
QComboBox QAbstractItemView::item {
    min-height: 28px;
    padding: 4px 8px;
}
"""

PROVIDER_TEXT_TO_CODE: Dict[str, str] = {
    "Google Gemini": "gemini",
    "OpenAI (GPT)": "openai",
    "OpenAI": "openai",
    "Anthropic Claude": "claude",
    "OpenRouter": "openrouter",
    "Ollama (로컬)": "ollama",
    "OpenRouter/Ollama": "ollama",
}

PROVIDER_CODE_TO_TEXT: Dict[str, str] = {
    "gemini": "Google Gemini",
    "openai": "OpenAI (GPT)",
    "claude": "Anthropic Claude",
    "openrouter": "OpenRouter",
    "ollama": "Ollama (로컬)",
    "custom": "Ollama (로컬)",
}

PROVIDER_DEFAULT_MODELS: Dict[str, str] = {
    "gemini": "gemini-1.5-flash",
    "openai": "gpt-4o-mini",
    "claude": "claude-3-7-sonnet-latest",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "ollama": "llama3.2",
    "custom": "llama3.2",
}

PROVIDER_DEFAULT_BASE_URLS: Dict[str, str] = {
    "gemini": "",
    "openai": "",
    "claude": "",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434",
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


def configure_input_widget(widget: QWidget) -> None:
    """
    입력 위젯이 세로로 눌리거나 잘리지 않도록 최소 높이(34px)와 확장형 SizePolicy를 설정합니다.
    QComboBox 및 QSpinBox에는 드롭다운 스타일과 마우스 휠 오조작 방지 필터(NoScrollWheelFilter)를 자동 적용합니다.
    """
    widget.setMinimumHeight(34)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    if isinstance(widget, (QComboBox, QSpinBox)):
        install_no_scroll_wheel_filter(widget)
        if isinstance(widget, QComboBox):
            if not isinstance(widget.view(), QListView):
                widget.setView(QListView())
            widget.setStyleSheet(COMBOBOX_STYLE)


def build_settings_form() -> QFormLayout:
    """아코디언 카드 내부에서 사용하는 공통 규격의 QFormLayout (행 겹침 방지 간격 포함)."""
    form = QFormLayout()
    form.setVerticalSpacing(14)
    form.setHorizontalSpacing(16)
    form.setContentsMargins(16, 16, 16, 16)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    return form


def build_floating_tooltip(title: str, lines: List[str]) -> str:
    """테이블 항목 위에 마우스를 올렸을 때 플로팅 팝업 카드 형태로 전체 정보를 보여주는 HTML 툴팁."""
    escaped_title = html.escape(title)
    body_html = "<br/>".join(lines)
    return (
        f"<div style='min-width: 240px; padding: 2px 4px;'>"
        f"<div style='color: #60a5fa; font-weight: 700; margin-bottom: 4px;'>{escaped_title}</div>"
        f"<div style='color: #f8fafc; line-height: 1.45;'>{body_html}</div>"
        f"</div>"
    )


class StudentAddDialog(QDialog):
    """학생 개별 추가/수정 다이얼로그."""

    def __init__(self, parent: Optional[QWidget] = None, data: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("학생 정보 등록/수정")
        self.setMinimumWidth(440)
        apply_qss_role(self, obj_name="StudentAddModal")
        self.setStyleSheet(INPUT_FIELD_STYLE)
        self.data = data or {}
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(6, 6, 6, 6)

        self.edit_student_id = QLineEdit(str(self.data.get("student_id", "")))
        configure_input_widget(self.edit_student_id)
        self.edit_student_id.setPlaceholderText("예: 10101 (학년반번호 5자리)")

        self.spin_grade = QSpinBox()
        configure_input_widget(self.spin_grade)
        self.spin_grade.setRange(1, 6)
        self.spin_grade.setValue(int(self.data.get("grade", 1)))

        self.spin_class = QSpinBox()
        configure_input_widget(self.spin_class)
        self.spin_class.setRange(1, 30)
        self.spin_class.setValue(int(self.data.get("class_no", 1)))

        self.spin_number = QSpinBox()
        configure_input_widget(self.spin_number)
        self.spin_number.setRange(1, 99)
        self.spin_number.setValue(int(self.data.get("number", 1)))

        self.edit_name = QLineEdit(str(self.data.get("name", "")))
        configure_input_widget(self.edit_name)
        self.edit_name.setPlaceholderText("학생 성명 (예: 홍길동)")

        self.edit_contact = QLineEdit(str(self.data.get("contact", "")))
        configure_input_widget(self.edit_contact)
        self.edit_contact.setPlaceholderText("010-0000-0000")

        self.edit_parent = QLineEdit(str(self.data.get("parent_contact", "")))
        configure_input_widget(self.edit_parent)
        self.edit_parent.setPlaceholderText("010-0000-0000")

        self.edit_memo = QLineEdit(str(self.data.get("memo", "")))
        configure_input_widget(self.edit_memo)

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
        self.setStyleSheet(INPUT_FIELD_STYLE)
        self.config = ConfigManager()
        self.db = DatabaseManager()
        self.notifier = NotificationService()
        self._loading_ui: bool = False
        self._focused_card: Optional[str] = None
        # 모델 목록 동적 조회 상태 (공급자 코드별 세션 캐시 / 백그라운드 실행 플래그)
        self.task_runner = TaskRunner(self)
        self._model_cache: Dict[str, List[str]] = {}
        self._model_fetch_running: bool = False
        self._last_auto_fetch_key: Optional[tuple] = None

        self._init_ui()
        self.load_settings_to_ui()
        self.refresh_students_table()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(12)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)

        # =====================================================================
        # 좌측 패널: QScrollArea + AccordionCard 3개 (AI 설정 / 폴더 감시 / 외부 연동)
        # - 평소엔 헤더 바 리스트로 정돈, 헤더 클릭 시에만 아래로 슬라이딩 펼침
        # =====================================================================
        self.left_scroll = QScrollArea()
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.left_scroll.setMinimumWidth(460)

        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(10)

        self.settings_accordion = AccordionGroup(exclusive=SETTINGS_ACCORDION_EXCLUSIVE)

        # ---------------------------------------------------------------------
        # 1. AI 및 프롬프트 설정 (기본 펼침)
        # ---------------------------------------------------------------------
        self.ai_group = AccordionCard(
            "AI 및 프롬프트 설정", parent=left_container, is_expanded=True
        )
        self.ai_group.setObjectName("AiPromptSettingsGroup")

        self.btn_focus_left = QPushButton("전체화면")
        apply_qss_role(self.btn_focus_left, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_left.setCheckable(True)
        self.btn_focus_left.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_left.setToolTip("환경설정 패널을 전체화면으로 확대하거나 원래 크기로 복원합니다.")
        self.btn_focus_left.clicked.connect(lambda: self._toggle_card_focus("left"))
        self.ai_group.add_header_widget(self.btn_focus_left)

        ai_vbox = self.ai_group.body_layout()

        self.chk_manual_prompt_mode = QCheckBox("수동 프롬프트 모드 사용 (API 무시)")
        self.chk_manual_prompt_mode.setChecked(True)
        self.chk_manual_prompt_mode.setMinimumHeight(28)
        self.chk_manual_prompt_mode.setToolTip(
            "체크 시 API 키 없이도 ChatGPT, Claude, Gemini 웹 화면에 복사/붙여넣기하여 결과를 처리할 수 있습니다."
        )
        manual_row = QHBoxLayout()
        manual_row.setContentsMargins(16, 8, 16, 0)
        manual_row.addWidget(self.chk_manual_prompt_mode)
        ai_vbox.addLayout(manual_row)

        ai_form = build_settings_form()

        # (2) AI 공급자 선택: QComboBox
        self.combo_ai_provider = QComboBox()
        configure_input_widget(self.combo_ai_provider)
        self.combo_ai_provider.addItems(
            ["Google Gemini", "OpenAI (GPT)", "Anthropic Claude", "OpenRouter", "Ollama (로컬)"]
        )
        ai_form.addRow("AI 공급자 선택:", self.combo_ai_provider)

        # (3) API Key: QLineEdit + 표시 토글 버튼
        api_key_container = QWidget()
        api_key_container.setMinimumHeight(38)
        api_key_row = QHBoxLayout(api_key_container)
        api_key_row.setContentsMargins(0, 0, 0, 0)
        api_key_row.setSpacing(8)

        self.edit_api_key = QLineEdit()
        configure_input_widget(self.edit_api_key)
        self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_api_key.setPlaceholderText("선택한 AI 공급자의 API Key를 입력하세요")

        self.btn_toggle_api_key = QPushButton("표시")
        apply_qss_role(self.btn_toggle_api_key)
        self.btn_toggle_api_key.setCheckable(True)
        self.btn_toggle_api_key.setMinimumHeight(36)
        self.btn_toggle_api_key.setFixedWidth(64)
        self.btn_toggle_api_key.toggled.connect(
            lambda checked: self.edit_api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        api_key_row.addWidget(self.edit_api_key, stretch=1)
        api_key_row.addWidget(self.btn_toggle_api_key)
        ai_form.addRow("API Key:", api_key_container)

        # (4) Custom Base URL: QLineEdit
        self.edit_custom_base_url = QLineEdit("")
        configure_input_widget(self.edit_custom_base_url)
        self.edit_custom_base_url.setPlaceholderText(
            "OpenRouter: https://openrouter.ai/api/v1 / Ollama: http://localhost:11434"
        )
        ai_form.addRow("Custom Base URL:", self.edit_custom_base_url)

        # (5) 사용할 모델명: 실시간 검색 가능한 SearchableModelComboBox + [🔄 모델 목록 갱신] 버튼
        model_container = QWidget()
        model_container.setMinimumHeight(38)
        model_row = QHBoxLayout(model_container)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)

        self.combo_model = SearchableModelComboBox()
        configure_input_widget(self.combo_model)
        self.combo_model.setMaxVisibleItems(12)
        self.combo_model.set_model_list(["gemini-1.5-flash"], "gemini-1.5-flash")
        if self.combo_model.lineEdit() is not None:
            self.combo_model.lineEdit().setPlaceholderText(
                "모델명 검색 또는 직접 입력 (예: gemini-1.5-flash, gpt-4o-mini, llama3.2)"
            )

        self.btn_refresh_models = QPushButton("모델 목록 갱신")
        apply_qss_role(self.btn_refresh_models, role="secondary", obj_name="BtnRefreshModels")
        self.btn_refresh_models.setMinimumHeight(36)
        self.btn_refresh_models.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_models.setToolTip("선택한 공급자에서 현재 사용 가능한 모델 목록을 실시간으로 조회합니다.")
        self.btn_refresh_models.clicked.connect(self.on_refresh_models_clicked)

        model_row.addWidget(self.combo_model, stretch=1)
        model_row.addWidget(self.btn_refresh_models)
        ai_form.addRow("사용할 모델명:", model_container)

        self.lbl_model_status = QLabel("")
        apply_qss_role(self.lbl_model_status, role="mutedText")
        self.lbl_model_status.setWordWrap(True)
        self.lbl_model_status.setVisible(False)
        ai_form.addRow("", self.lbl_model_status)

        ai_vbox.addLayout(ai_form)

        # (6) [연결 테스트] 및 [수동 복붙 모달 미리보기] 버튼
        ai_btn_row = QHBoxLayout()
        ai_btn_row.setContentsMargins(16, 0, 16, 4)
        ai_btn_row.setSpacing(10)
        self.btn_test_connection = QPushButton("연결 테스트")
        apply_qss_role(self.btn_test_connection, role="teal", obj_name="BtnTestAiConnection")
        self.btn_test_connection.setMinimumHeight(38)
        self.btn_test_connection.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_test_connection.clicked.connect(self._on_test_llm_connection)

        self.btn_preview_manual = QPushButton("수동 복붙 모달 미리보기")
        apply_qss_role(self.btn_preview_manual, role="secondary", obj_name="BtnPreviewManualDialog")
        self.btn_preview_manual.setMinimumHeight(38)
        self.btn_preview_manual.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_preview_manual.clicked.connect(self._on_open_manual_dialog_preview)

        ai_btn_row.addWidget(self.btn_test_connection)
        ai_btn_row.addWidget(self.btn_preview_manual)
        ai_btn_row.addStretch()
        ai_vbox.addLayout(ai_btn_row)

        # 실시간 즉시 저장 시그널 연결
        self.chk_manual_prompt_mode.stateChanged.connect(self._on_ai_setting_changed)
        self.combo_ai_provider.currentTextChanged.connect(self._on_provider_text_changed)
        self.edit_api_key.textChanged.connect(self._on_ai_setting_changed)
        self.edit_api_key.editingFinished.connect(self._on_api_key_editing_finished)
        self.edit_custom_base_url.textChanged.connect(self._on_ai_setting_changed)
        self.edit_custom_base_url.textChanged.connect(self._update_provider_field_states)
        self.combo_model.currentTextChanged.connect(self._on_ai_setting_changed)

        left_layout.addWidget(self.ai_group)
        self.settings_accordion.add_card(self.ai_group)

        # ---------------------------------------------------------------------
        # 2. 기본 폴더 및 백그라운드 감시 설정 (기본 접힘)
        # ---------------------------------------------------------------------
        self.folder_group = AccordionCard(
            "기본 폴더 및 백그라운드 감시 설정", parent=left_container, is_expanded=False
        )
        self.folder_group.setObjectName("FolderSettingsGroup")

        folder_form = build_settings_form()

        target_container = QWidget()
        target_container.setMinimumHeight(38)
        target_row = QHBoxLayout(target_container)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(8)
        self.edit_default_target = QLineEdit()
        configure_input_widget(self.edit_default_target)
        btn_target_browse = QPushButton("변경...")
        apply_qss_role(btn_target_browse)
        btn_target_browse.setMinimumHeight(36)
        btn_target_browse.clicked.connect(self._browse_target_dir)
        target_row.addWidget(self.edit_default_target, stretch=1)
        target_row.addWidget(btn_target_browse)

        self.spin_watch_interval = QSpinBox()
        configure_input_widget(self.spin_watch_interval)
        self.spin_watch_interval.setRange(1, 3600)
        self.spin_watch_interval.setSuffix(" 초")

        self.chk_auto_watch = QCheckBox("프로그램 시작 시 실시간 폴더 감시 자동 시작")
        self.chk_auto_watch.setMinimumHeight(28)
        self.chk_ignore_temp = QCheckBox("브라우저 다운로드 임시파일(.crdownload, .part, ~$) 자동 무시")
        self.chk_ignore_temp.setMinimumHeight(28)

        folder_form.addRow("분류 기준 상위 폴더:", target_container)
        folder_form.addRow("폴더 폴링 감시 주기:", self.spin_watch_interval)
        folder_form.addRow("", self.chk_auto_watch)
        folder_form.addRow("", self.chk_ignore_temp)
        self.folder_group.add_layout(folder_form)
        left_layout.addWidget(self.folder_group)
        self.settings_accordion.add_card(self.folder_group)

        # ---------------------------------------------------------------------
        # 3. 외부 연동(Google 시트/카카오톡) 및 시스템 트레이 설정 (기본 접힘)
        # ---------------------------------------------------------------------
        self.ext_group = AccordionCard(
            "외부 연동(Google 시트/카카오톡) 및 시스템 트레이 설정",
            parent=left_container,
            is_expanded=False,
        )
        self.ext_group.setObjectName("ExtSettingsGroup")

        ext_form = build_settings_form()

        cred_container = QWidget()
        cred_container.setMinimumHeight(38)
        cred_row = QHBoxLayout(cred_container)
        cred_row.setContentsMargins(0, 0, 0, 0)
        cred_row.setSpacing(8)
        self.edit_google_cred = QLineEdit()
        configure_input_widget(self.edit_google_cred)
        self.edit_google_cred.setPlaceholderText("Google API credentials.json 파일 경로")
        btn_cred_browse = QPushButton("찾아보기...")
        apply_qss_role(btn_cred_browse)
        btn_cred_browse.setMinimumHeight(36)
        btn_cred_browse.clicked.connect(self._browse_credentials_json)
        cred_row.addWidget(self.edit_google_cred, stretch=1)
        cred_row.addWidget(btn_cred_browse)

        self.edit_sheet_url = QLineEdit()
        configure_input_widget(self.edit_sheet_url)
        self.edit_sheet_url.setPlaceholderText("감시할 Google 스프레드시트 URL 또는 문서 ID")

        self.spin_sheet_interval = QSpinBox()
        configure_input_widget(self.spin_sheet_interval)
        self.spin_sheet_interval.setRange(5, 3600)
        self.spin_sheet_interval.setSuffix(" 초")

        self.combo_kakao_mode = QComboBox()
        configure_input_widget(self.combo_kakao_mode)
        self.combo_kakao_mode.addItem("윈도우 데스크톱 토스트 알림만 사용", "toast_only")
        self.combo_kakao_mode.addItem("PC 카카오톡 열려있는 채팅방 매크로 전송", "pc_macro")
        self.combo_kakao_mode.addItem("웹훅(Webhook) URL로 메시지 전송", "webhook")

        self.edit_kakao_room = QLineEdit()
        configure_input_widget(self.edit_kakao_room)
        self.edit_kakao_room.setPlaceholderText("PC 카카오톡 채팅방 이름 (예: 2학년 교무부 단톡방)")

        self.edit_kakao_webhook = QLineEdit()
        configure_input_widget(self.edit_kakao_webhook)
        self.edit_kakao_webhook.setPlaceholderText("https://... (카카오워크/슬랙/디스코드 웹훅 URL)")

        self.chk_minimize_to_tray = QCheckBox(
            "창 닫기(X) 클릭 시 종료하지 않고 시스템 트레이로 숨기기 (백그라운드 유지)"
        )
        self.chk_minimize_to_tray.setMinimumHeight(28)
        self.chk_tray_notify = QCheckBox("작업 완료 시 윈도우 트레이 토스트 알림 표시")
        self.chk_tray_notify.setMinimumHeight(28)

        ext_form.addRow("credentials.json:", cred_container)
        ext_form.addRow("Google 시트 URL:", self.edit_sheet_url)
        ext_form.addRow("구글 시트 감시 주기:", self.spin_sheet_interval)
        ext_form.addRow("알림/카톡 연동 방식:", self.combo_kakao_mode)
        ext_form.addRow("PC 카톡 채팅방명:", self.edit_kakao_room)
        ext_form.addRow("Webhook URL:", self.edit_kakao_webhook)
        ext_form.addRow("", self.chk_minimize_to_tray)
        ext_form.addRow("", self.chk_tray_notify)
        self.ext_group.add_layout(ext_form)
        left_layout.addWidget(self.ext_group)
        self.settings_accordion.add_card(self.ext_group)

        # 펼침 애니메이션 완료 시 해당 카드가 스크롤 영역 안에 보이도록 자동 스크롤
        for card in self.settings_accordion.cards():
            card.animation_finished.connect(
                lambda expanded, c=card: expanded and self.left_scroll.ensureWidgetVisible(c, 0, 10)
            )

        # 하단 전체 저장 및 알림 테스트 버튼 바
        save_bar = QHBoxLayout()
        save_bar.setSpacing(10)
        btn_save_cfg = QPushButton("환경설정 모두 저장")
        apply_qss_role(btn_save_cfg, role="primary", obj_name="BtnSaveAllSettings")
        btn_save_cfg.setMinimumHeight(40)
        btn_save_cfg.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save_cfg.clicked.connect(self.save_ui_to_settings)

        btn_test_notify = QPushButton("알림 테스트 발송")
        apply_qss_role(btn_test_notify, role="secondary", obj_name="BtnTestNotify")
        btn_test_notify.setMinimumHeight(40)
        btn_test_notify.clicked.connect(self._on_test_notification)

        btn_reset_cfg = QPushButton("초기값 복원")
        apply_qss_role(btn_reset_cfg, role="secondary", obj_name="BtnResetSettings")
        btn_reset_cfg.setMinimumHeight(40)
        btn_reset_cfg.clicked.connect(self._on_reset_config)

        save_bar.addWidget(btn_save_cfg)
        save_bar.addWidget(btn_test_notify)
        save_bar.addStretch()
        save_bar.addWidget(btn_reset_cfg)
        left_layout.addLayout(save_bar)
        left_layout.addStretch()

        self.left_scroll.setWidget(left_container)
        self.main_splitter.addWidget(self.left_scroll)

        # =====================================================================
        # 우측 패널 (55%): 학생 명렬표 (마스터 데이터) 정적 카드 + 전체화면 버튼
        # =====================================================================
        self.right_widget = QWidget()
        self.right_widget.setMinimumWidth(420)
        right_layout = QVBoxLayout(self.right_widget)
        right_layout.setContentsMargins(8, 8, 18, 18)

        self.stu_group = HoverZoomCard(
            title="학생 명렬표 마스터 데이터 관리 (파일 취합·성적·출결 탭 공통 연동)",
            parent=self.right_widget,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.stu_group, role="hoverZoomCard", obj_name="StudentMasterGroup")

        self.btn_focus_students = QPushButton("전체화면")
        apply_qss_role(self.btn_focus_students, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_students.setCheckable(True)
        self.btn_focus_students.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_students.setToolTip("학생 명렬표 테이블 카드를 전체화면으로 크게 보거나 원래 크기로 복원합니다.")
        self.btn_focus_students.clicked.connect(lambda: self._toggle_card_focus("right"))
        self.stu_group.add_header_widget(self.btn_focus_students)

        stu_layout = self.stu_group.body_layout()

        stu_btn_bar = QHBoxLayout()
        stu_btn_bar.setSpacing(6)
        btn_add_stu = QPushButton("+ 학생 추가")
        apply_qss_role(btn_add_stu, role="primary", obj_name="BtnAddStudent")
        btn_import_file = QPushButton("CSV/엑셀 일괄 등록")
        apply_qss_role(btn_import_file, role="secondary", obj_name="BtnImportStudents")
        btn_export_sample = QPushButton("양식 내보내기")
        apply_qss_role(btn_export_sample, role="secondary", obj_name="BtnExportStudents")
        btn_del_stu = QPushButton("- 선택 삭제")
        apply_qss_role(btn_del_stu, role="secondary", obj_name="BtnDeleteStudent")
        btn_clear_stu = QPushButton("전체 삭제")
        apply_qss_role(btn_clear_stu, role="warningOutline", obj_name="BtnClearStudents")

        btn_import_file.clicked.connect(self._on_import_students_file)
        btn_export_sample.clicked.connect(self._on_export_students_file)
        btn_add_stu.clicked.connect(self._on_add_student)
        btn_del_stu.clicked.connect(self._on_delete_student)
        btn_clear_stu.clicked.connect(self._on_clear_students)

        stu_btn_bar.addWidget(btn_add_stu)
        stu_btn_bar.addWidget(btn_import_file)
        stu_btn_bar.addWidget(btn_export_sample)
        stu_btn_bar.addWidget(btn_del_stu)
        stu_btn_bar.addStretch()
        stu_btn_bar.addWidget(btn_clear_stu)
        stu_layout.addLayout(stu_btn_bar)

        self.lbl_student_count = QLabel(
            "등록된 학생: 0명 (행에 마우스를 올리면 전체 상세 정보가 플로팅 툴팁으로 표시되며, 더블클릭 시 수정됩니다)"
        )
        apply_qss_role(self.lbl_student_count, role="mutedText")
        stu_layout.addWidget(self.lbl_student_count)

        self.table_students = QTableWidget(0, 7)
        self.table_students.setAlternatingRowColors(True)
        self.table_students.setMouseTracking(True)
        self.table_students.setStyleSheet(
            "QTableWidget { background-color: #FFFFFF; alternate-background-color: #F8F9FF; "
            "selection-background-color: #EEF3FD; selection-color: #003EA8; "
            "border: 1px solid #E2E8F0; border-radius: 8px; }"
        )
        self.table_students.setHorizontalHeaderLabels(
            ["학번", "학년", "반", "번호", "이름", "학생 연락처", "학부모 연락처 / 비고"]
        )
        self.table_students.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_students.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_students.verticalHeader().setDefaultSectionSize(34)
        self.table_students.verticalHeader().setVisible(False)
        self.table_students.doubleClicked.connect(self._on_edit_student)
        s_header = self.table_students.horizontalHeader()
        s_header.setMinimumHeight(32)
        s_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        s_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        s_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        stu_layout.addWidget(self.table_students, stretch=1)
        right_layout.addWidget(self.stu_group)

        self.main_splitter.addWidget(self.right_widget)
        self.main_splitter.setSizes([495, 605])
        self.main_splitter.setStretchFactor(0, 45)
        self.main_splitter.setStretchFactor(1, 55)
        main_layout.addWidget(self.main_splitter, stretch=1)

    # =========================================================================
    # 섹션 카드 전체화면 포커스 토글 ([ 전체화면 ] <-> [ 원래 크기로 ])
    # =========================================================================
    def _toggle_card_focus(self, target: str) -> None:
        if self._focused_card == target:
            self._focused_card = None
            self.stu_group.set_zoom_enabled(True)
            self.left_scroll.show()
            self.right_widget.show()
            self.btn_focus_left.setChecked(False)
            self.btn_focus_left.setText("전체화면")
            self.btn_focus_students.setChecked(False)
            self.btn_focus_students.setText("전체화면")
            self.main_splitter.setSizes([495, 605])
            return

        self._focused_card = target
        if target == "right":
            self.stu_group.set_zoom_enabled(False)
            self.left_scroll.hide()
            self.right_widget.show()
            self.btn_focus_students.setChecked(True)
            self.btn_focus_students.setText("원래 크기로")
            self.btn_focus_left.setChecked(False)
            self.btn_focus_left.setText("전체화면")
        elif target == "left":
            self.right_widget.hide()
            self.left_scroll.show()
            self.btn_focus_left.setChecked(True)
            self.btn_focus_left.setText("원래 크기로")
            self.btn_focus_students.setChecked(False)
            self.btn_focus_students.setText("전체화면")

    # =========================================================================
    # AI 및 프롬프트 설정 실시간 즉시 저장 핸들러 (stateChanged / currentTextChanged / textChanged)
    # =========================================================================
    def _get_selected_provider_code(self) -> str:
        text = self.combo_ai_provider.currentText().strip()
        return PROVIDER_TEXT_TO_CODE.get(text, "gemini")

    def _resolve_provider_fields_from_config(
        self, api_sec: Dict[str, Any], prov_code: str
    ) -> tuple[str, str, str]:
        """선택된 공급자 코드에 맞춰 저장된 (api_key, model_name, base_url)을 반환합니다."""
        key_field_map = {
            "gemini": "gemini_api_key",
            "openai": "openai_api_key",
            "claude": "claude_api_key",
            "openrouter": "openrouter_api_key",
            "ollama": "",
        }
        model_field_map = {
            "gemini": "gemini_model",
            "openai": "openai_model",
            "claude": "claude_model",
            "openrouter": "openrouter_model",
            "ollama": "ollama_model",
        }
        key_field = key_field_map.get(prov_code, "")
        saved_key = str(api_sec.get(key_field, "")) if key_field else ""
        if prov_code == "openrouter" and not saved_key:
            saved_key = str(api_sec.get("custom_api_key", ""))

        model_field = model_field_map.get(prov_code, "gemini_model")
        default_model = PROVIDER_DEFAULT_MODELS.get(prov_code, "gemini-1.5-flash")
        saved_model = str(api_sec.get(model_field, "") or "")
        if prov_code == "ollama" and not saved_model:
            saved_model = str(api_sec.get("custom_model", "") or default_model)
        if not saved_model or "2.5-flash" in saved_model:
            saved_model = default_model

        if prov_code == "openrouter":
            saved_url = str(api_sec.get("openrouter_base_url", "") or "").strip()
            if not saved_url or "11434" in saved_url or "localhost" in saved_url:
                saved_url = PROVIDER_DEFAULT_BASE_URLS["openrouter"]
        elif prov_code == "ollama":
            saved_url = str(
                api_sec.get("ollama_base_url", "") or api_sec.get("custom_base_url", "") or ""
            ).strip()
            if not saved_url or "openrouter.ai" in saved_url:
                saved_url = PROVIDER_DEFAULT_BASE_URLS["ollama"]
            else:
                saved_url = saved_url.replace("/v1", "").rstrip("/") or PROVIDER_DEFAULT_BASE_URLS["ollama"]
        else:
            saved_url = ""

        return saved_key, saved_model, saved_url

    def _on_provider_text_changed(self, provider_text: str) -> None:
        if self._loading_ui:
            return
        prov_code = PROVIDER_TEXT_TO_CODE.get(provider_text, "gemini")
        api_sec = self.config.get_section("api_and_integrations")

        self._loading_ui = True
        try:
            saved_key, saved_model, saved_url = self._resolve_provider_fields_from_config(
                api_sec, prov_code
            )
            self.edit_api_key.setText(saved_key)
            self.edit_custom_base_url.setText(saved_url)
            # 이번 세션에 이미 조회한 목록이 있으면 재사용, 없으면 저장된 모델만 표시
            cached = self._model_cache.get(prov_code, [])
            self._populate_model_combo(cached, saved_model)
        finally:
            self._loading_ui = False

        self._update_provider_field_states()
        self.lbl_model_status.setVisible(False)
        self._on_ai_setting_changed()

        # 공급자 전환 시 모델 목록이 아직 캐시되지 않았고 조회 조건이 충족되면 자동 조회
        if prov_code not in self._model_cache:
            if prov_code == "ollama" or bool(self.edit_api_key.text().strip()):
                self._start_model_fetch(interactive=False)

    # =========================================================================
    # 공급자별 입력칸 활성화 제어 및 모델 목록 동적 조회
    # =========================================================================
    def _current_model_name(self) -> str:
        return self.combo_model.currentText().strip()

    def _populate_model_combo(self, models: List[str], selected: str) -> None:
        """모델 콤보박스를 목록으로 채우고 selected 모델을 선택합니다."""
        self.combo_model.blockSignals(True)
        try:
            self.combo_model.set_model_list(list(models), selected)
        finally:
            self.combo_model.blockSignals(False)

    def _is_ollama_mode(self) -> bool:
        return self._get_selected_provider_code() == "ollama"

    def _update_provider_field_states(self, *_args: Any) -> None:
        """
        - Ollama (로컬): API Key 비활성화(setEnabled(False)), Base URL 활성화(기본값: http://localhost:11434)
        - OpenRouter: API Key 활성화, Base URL 활성화(기본값: https://openrouter.ai/api/v1)
        - Gemini / OpenAI (GPT) / Anthropic Claude: API Key 활성화, Base URL 비활성화
        """
        prov_code = self._get_selected_provider_code()

        if prov_code == "ollama":
            self.edit_api_key.setEnabled(False)
            self.btn_toggle_api_key.setEnabled(False)
            self.edit_api_key.setPlaceholderText("로컬 Ollama는 API Key가 필요 없습니다")
            self.edit_custom_base_url.setEnabled(True)
            self.edit_custom_base_url.setPlaceholderText("http://localhost:11434")
            cur_url = self.edit_custom_base_url.text().strip()
            if not cur_url or "openrouter.ai" in cur_url:
                self._loading_ui = True
                try:
                    self.edit_custom_base_url.setText("http://localhost:11434")
                finally:
                    self._loading_ui = False
        elif prov_code == "openrouter":
            self.edit_api_key.setEnabled(True)
            self.btn_toggle_api_key.setEnabled(True)
            self.edit_api_key.setPlaceholderText("OpenRouter API Key를 입력하세요 (필수, sk-or-...)")
            self.edit_custom_base_url.setEnabled(True)
            self.edit_custom_base_url.setPlaceholderText("https://openrouter.ai/api/v1")
            cur_url = self.edit_custom_base_url.text().strip()
            if not cur_url or "11434" in cur_url or "localhost" in cur_url:
                self._loading_ui = True
                try:
                    self.edit_custom_base_url.setText("https://openrouter.ai/api/v1")
                finally:
                    self._loading_ui = False
        else:
            self.edit_api_key.setEnabled(True)
            self.btn_toggle_api_key.setEnabled(True)
            self.edit_api_key.setPlaceholderText(
                f"{self.combo_ai_provider.currentText()} API Key를 입력하세요 (필수)"
            )
            self.edit_custom_base_url.setEnabled(False)
            self.edit_custom_base_url.setPlaceholderText("공식 기본 엔드포인트가 자동으로 사용됩니다")

    def _on_api_key_editing_finished(self) -> None:
        """API Key 입력을 마쳤을 때(포커스 아웃/Enter) 키가 바뀌었으면 모델 목록을 조용히 자동 갱신합니다."""
        if self._loading_ui or self._is_ollama_mode():
            return
        api_key = self.edit_api_key.text().strip()
        if not api_key:
            return
        fetch_key = (self._get_selected_provider_code(), api_key)
        if fetch_key == self._last_auto_fetch_key:
            return
        self._last_auto_fetch_key = fetch_key
        self._start_model_fetch(interactive=False)

    def on_refresh_models_clicked(self) -> None:
        """[🔄 모델 목록 갱신] 버튼 클릭 핸들러."""
        provider = self.combo_ai_provider.currentText()
        if not self._is_ollama_mode():
            if not self.edit_api_key.text().strip():
                QMessageBox.warning(self, "안내", f"{provider} API Key를 먼저 입력해주세요.")
                return
        self._start_model_fetch(interactive=True)

    def _start_model_fetch(self, interactive: bool) -> None:
        """UI 멈춤 방지를 위해 TaskRunner(QThread) 백그라운드에서 모델 목록을 조회합니다."""
        prov_code = self._get_selected_provider_code()
        provider_text = self.combo_ai_provider.currentText()
        api_key = "" if self._is_ollama_mode() else self.edit_api_key.text().strip()
        base_url = (
            self.edit_custom_base_url.text().strip()
            if prov_code in ("openrouter", "ollama")
            else ""
        )

        self._model_fetch_running = True
        self.btn_refresh_models.setEnabled(False)
        self.btn_refresh_models.setText("탐지 중...")
        self.lbl_model_status.setText(f"[{provider_text}] 사용 가능한 모델 목록을 조회하고 있습니다...")
        self.lbl_model_status.setVisible(True)

        def _job(_worker: FunctionWorker) -> Dict[str, Any]:
            # 워커 스레드 내부: 어떤 예외도 dict로 변환하여 UI 스레드에서 안전하게 처리
            try:
                models, recommended = fetch_available_models(provider_text, api_key, base_url)
                return {"ok": True, "models": models, "recommended": recommended}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        worker = FunctionWorker("AI 모델 목록 조회", _job)
        self.task_runner.start_worker(
            worker,
            task_key=f"settings_model_list_fetch_{prov_code}",
            on_result=lambda res: self._on_models_fetched(res, prov_code, provider_text, interactive),
            on_finished=self._on_model_fetch_finished,
        )

    def _on_model_fetch_finished(self) -> None:
        self._model_fetch_running = False
        self.btn_refresh_models.setEnabled(True)
        self.btn_refresh_models.setText("모델 목록 갱신")

    def _on_models_fetched(
        self, res: Dict[str, Any], prov_code: str, provider_text: str, interactive: bool
    ) -> None:
        # 조회 도중 사용자가 공급자를 바꿨다면 결과를 캐시에만 반영하고 화면은 건드리지 않음
        still_same_provider = self._get_selected_provider_code() == prov_code

        if not res.get("ok"):
            err = str(res.get("error", "알 수 없는 오류"))
            if still_same_provider:
                self.lbl_model_status.setText(f"[오류] 모델 목록 조회 실패: {err.splitlines()[0]}")
                self.lbl_model_status.setVisible(True)
            if interactive:
                QMessageBox.warning(
                    self,
                    "조회 실패",
                    f"[{provider_text}] 모델 목록을 불러오지 못했습니다.\n"
                    f"API 키나 서버 주소를 확인해주세요.\n\n상세: {err}",
                )
            return

        models: List[str] = list(res.get("models") or [])
        recommended: str = str(res.get("recommended") or "")

        if not models:
            hint_msg = (
                recommended
                if recommended
                else "조회는 성공했지만 사용 가능한 텍스트 모델이 없습니다."
            )
            if still_same_provider:
                self.lbl_model_status.setText(f"[안내] {hint_msg}")
                self.lbl_model_status.setVisible(True)
            if interactive:
                QMessageBox.warning(
                    self,
                    "조회 결과 없음",
                    f"[{provider_text}] {hint_msg}\n"
                    "Ollama의 경우 'ollama pull llama3.2' 등으로 모델을 먼저 설치해 주세요.",
                )
            return

        self._model_cache[prov_code] = models
        if not still_same_provider:
            return

        # 수동 갱신: 추천 모델 자동 선택 / 자동 갱신: 기존 선택이 목록에 있으면 유지 (단, 2.5 폐기 모델은 자동 전환)
        current = self._current_model_name()
        if interactive or current not in models or "2.5" in current:
            selected = recommended or pick_recommended_model(prov_code, models)
        else:
            selected = current

        self._populate_model_combo(models, selected)
        self._on_ai_setting_changed()
        self.lbl_model_status.setText(
            f"[완료] {len(models)}개 모델을 불러왔습니다. (선택: {selected})"
        )
        self.lbl_model_status.setVisible(True)

        if interactive:
            QMessageBox.information(
                self,
                "조회 완료",
                f"[{provider_text}] 사용 가능한 {len(models)}개 모델을 불러왔습니다.\n기본 선택: {selected}",
            )

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
        model_name = self._current_model_name() or PROVIDER_DEFAULT_MODELS.get(prov_code, "gemini-1.5-flash")

        updates: Dict[str, Any] = {
            "use_manual_prompt_mode": self.chk_manual_prompt_mode.isChecked(),
            "ai_provider": prov_code,
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
        elif prov_code == "openrouter":
            updates["openrouter_api_key"] = api_key
            updates["openrouter_model"] = model_name
            updates["openrouter_base_url"] = custom_url or PROVIDER_DEFAULT_BASE_URLS["openrouter"]
        elif prov_code == "ollama":
            updates["ollama_model"] = model_name
            updates["ollama_base_url"] = custom_url or PROVIDER_DEFAULT_BASE_URLS["ollama"]
            updates["custom_model"] = model_name
            updates["custom_base_url"] = custom_url or PROVIDER_DEFAULT_BASE_URLS["ollama"]

        self.config.update_section("api_and_integrations", updates, auto_save=True)

    def _on_test_llm_connection(self) -> None:
        """[🔌 연결 테스트] 버튼 클릭 시 현재 입력된 AI 설정으로 즉시 연결을 테스트합니다."""
        self._on_ai_setting_changed()

        prov_code = self._get_selected_provider_code()
        api_key = self.edit_api_key.text().strip()
        custom_url = (
            self.edit_custom_base_url.text().strip()
            if prov_code in ("openrouter", "ollama")
            else None
        )
        model_name = self._current_model_name() or PROVIDER_DEFAULT_MODELS.get(prov_code, "gemini-1.5-flash")

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
        prov_code = "gemini"
        try:
            fs = self.config.get_section("file_sorter")
            api = self.config.get_section("api_and_integrations")
            sys_sec = self.config.get_section("system")

            # 1) AI 및 프롬프트 설정 로드
            self.chk_manual_prompt_mode.setChecked(
                bool(api.get("use_manual_prompt_mode", True))
            )

            raw_prov = str(api.get("ai_provider", "gemini")).lower().strip()
            if raw_prov == "custom":
                raw_url = str(api.get("custom_base_url", "")).lower()
                prov_code = "openrouter" if "openrouter" in raw_url else "ollama"
            else:
                prov_code = raw_prov if raw_prov in PROVIDER_CODE_TO_TEXT else "gemini"

            prov_text = PROVIDER_CODE_TO_TEXT.get(prov_code, "Google Gemini")
            idx = self.combo_ai_provider.findText(prov_text)
            if idx >= 0:
                self.combo_ai_provider.setCurrentIndex(idx)

            saved_key, saved_model, saved_url = self._resolve_provider_fields_from_config(
                api, prov_code
            )
            self.edit_api_key.setText(saved_key)
            self.edit_custom_base_url.setText(saved_url)
            self._populate_model_combo(
                self._model_cache.get(prov_code, []),
                saved_model,
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
        self._update_provider_field_states()

        # 초기 로드 시 현재 공급자 모델 목록 자동 조회 (Ollama 또는 API Key가 설정된 공급자)
        if prov_code == "ollama" or bool(self.edit_api_key.text().strip()):
            self._start_model_fetch(interactive=False)

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
    # 학생 명렬표 마스터 데이터 관리 (CSV / Excel 입출력 및 플로팅 툴팁)
    # =========================================================================
    def refresh_students_table(self) -> None:
        students = self.db.get_students()
        self.table_students.setRowCount(0)
        self.lbl_student_count.setText(
            f"등록된 학생: 총 {len(students)}명 (행 위에 마우스를 올리면 상세 정보가 플로팅 툴팁으로 표시됩니다)"
        )

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

            tooltip_html = build_floating_tooltip(
                f"[{s['student_id']}] {s['name']}",
                [
                    f"<b>학급 정보:</b> {s['grade']}학년 {s['class_no']}반 {s['number']}번",
                    f"<b>학생 연락처:</b> {html.escape(s['contact'] or '-')}",
                    f"<b>학부모 연락처:</b> {html.escape(s['parent_contact'] or '-')}",
                    f"<b>비고/특기사항:</b> {html.escape(s['memo'] or '-')}",
                ],
            )

            items = [sid_item, g_item, c_item, n_item, name_item, contact_item, pm_item]
            for col_idx, it in enumerate(items):
                it.setToolTip(tooltip_html)
                self.table_students.setItem(row_idx, col_idx, it)

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
