"""
수동 프롬프트 모드 (No-API Fallback Workflow) 범용 모달 다이얼로그 (ui/dialogs/manual_llm_dialog.py).
- ui/styles/theme.qss 기반 모던 웹 AI 입력창 및 카드 레이아웃 적용
- 모든 위젯에 setObjectName / setProperty("role", ...) 및 style().polish() 강제 적용
"""

import webbrowser
from typing import Any, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.llm_client import (
    PROMPT_TEMPLATES,
    build_structured_prompt,
    extract_json_from_llm_response,
    format_parsed_json_to_readable_text,
)
from ui.utils import install_no_scroll_wheel_filter


def apply_qss_role(widget: QWidget, role: str = "", obj_name: str = "") -> None:
    """위젯에 objectName 및 role 속성을 부여하고 style().polish()를 즉시 호출합니다."""
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    install_no_scroll_wheel_filter(widget)
    if obj_name:
        widget.setObjectName(obj_name)
    if role:
        widget.setProperty("role", role)
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


class ManualLlmDialog(QDialog):
    """외부 웹 LLM(ChatGPT / Claude / Gemini 등) 복사·붙여넣기 하이브리드 모달 다이얼로그."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        task_key: str = "ocr_correction",
        raw_content: str = "",
        custom_instruction: str = "",
    ):
        super().__init__(parent)
        apply_qss_role(self, obj_name="ManualLlmModal")
        self.setWindowTitle("수동 프롬프트 어시스턴트 (No-API 외부 웹 AI 연동)")
        self.resize(1120, 800)
        self.setMinimumSize(920, 660)

        self._initial_task_key = task_key if task_key in PROMPT_TEMPLATES else "ocr_correction"
        self._raw_content = raw_content
        self._custom_instruction = custom_instruction

        self.parsed_json: Dict[str, Any] = {}
        self.formatted_text: str = ""

        self._init_ui()
        self._regenerate_prompt()

    def _init_ui(self) -> None:
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(15, 15, 15, 15)
        root_lay.setSpacing(14)

        # 1. 상단 모던 헤더 카드
        header_card = QFrame()
        apply_qss_role(header_card, obj_name="ModalHeaderCard")
        header_lay = QVBoxLayout(header_card)
        header_lay.setContentsMargins(15, 15, 15, 15)
        header_lay.setSpacing(10)

        banner = QLabel(
            "[!] <b>API 키 없이 무료 웹 AI(ChatGPT, Claude, Gemini)를 활용하는 하이브리드 워크플로우</b><br>"
            "1. 좌측 <b>[클립보드에 프롬프트 복사]</b> 클릭 -> 2. 웹 AI에 붙여넣기 후 응답 복사 -> "
            "3. 우측 입력창에 붙여넣고 <b>[파싱 결과 워크스테이션에 적용]</b>을 클릭하세요."
        )
        apply_qss_role(banner, role="infoBanner", obj_name="ModalInfoBanner")
        header_lay.addWidget(banner)

        tpl_bar = QHBoxLayout()
        tpl_bar.setSpacing(10)
        tpl_bar.addWidget(QLabel("<b>작업 템플릿:</b>"))
        self.combo_task = QComboBox()
        install_no_scroll_wheel_filter(self.combo_task)
        for k, meta in PROMPT_TEMPLATES.items():
            self.combo_task.addItem(meta["title"], k)
        idx = self.combo_task.findData(self._initial_task_key)
        if idx >= 0:
            self.combo_task.setCurrentIndex(idx)
        self.combo_task.currentIndexChanged.connect(self._on_template_changed)

        tpl_bar.addWidget(self.combo_task)
        tpl_bar.addSpacing(12)
        tpl_bar.addWidget(QLabel("<b>커스텀 지시문:</b>"))
        self.edit_instruction = QLineEdit(self._custom_instruction)
        self.edit_instruction.setPlaceholderText("비워두면 선택된 템플릿의 표준 지시문이 적용됩니다.")
        self.edit_instruction.textChanged.connect(self._regenerate_prompt)
        tpl_bar.addWidget(self.edit_instruction, stretch=1)

        header_lay.addLayout(tpl_bar)
        root_lay.addWidget(header_card)

        # 2. 메인 좌우 스플리터: [1단계: 프롬프트 생성/복사] | [2~3단계: 응답 붙여넣기 및 JSON 파싱]
        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # =====================================================================
        # 좌측 [1단계: 프롬프트 생성 및 클립보드 복사]
        # =====================================================================
        step1_group = QGroupBox("1. 구조화 JSON 프롬프트 생성 및 복사")
        apply_qss_role(step1_group, obj_name="Step1PromptGroup")
        step1_lay = QVBoxLayout(step1_group)
        step1_lay.setContentsMargins(15, 15, 15, 15)
        step1_lay.setSpacing(10)

        step1_lay.addWidget(QLabel("<b>분석할 원문 데이터 (필요 시 자유롭게 수정):</b>"))
        self.txt_raw_input = QTextEdit()
        apply_qss_role(self.txt_raw_input, obj_name="WebAiInputBox")
        self.txt_raw_input.setPlainText(self._raw_content)
        self.txt_raw_input.setPlaceholderText("OCR 추출 텍스트, 보고서 내용, 또는 파일 목록을 이곳에 입력하세요...")
        self.txt_raw_input.setMaximumHeight(165)
        self.txt_raw_input.textChanged.connect(self._regenerate_prompt)
        step1_lay.addWidget(self.txt_raw_input)

        step1_lay.addWidget(
            QLabel("<b>생성된 전체 프롬프트 (System 지시문 + ```json 출력 포맷 강제 규칙 포함):</b>")
        )
        self.txt_generated_prompt = QTextEdit()
        apply_qss_role(self.txt_generated_prompt, obj_name="PromptCodeBox")
        step1_lay.addWidget(self.txt_generated_prompt, stretch=1)

        copy_btn_row = QHBoxLayout()
        copy_btn_row.setSpacing(8)
        self.btn_copy_prompt = QPushButton("▸ 클립보드에 프롬프트 전체 복사")
        apply_qss_role(self.btn_copy_prompt, role="primary", obj_name="BtnCopyPrompt")
        self.btn_copy_prompt.setMinimumHeight(40)
        self.btn_copy_prompt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_prompt.clicked.connect(self._on_copy_prompt)

        btn_open_chatgpt = QPushButton("↗ ChatGPT")
        btn_open_claude = QPushButton("↗ Claude")
        btn_open_gemini = QPushButton("↗ Gemini")
        for b in (btn_open_chatgpt, btn_open_claude, btn_open_gemini):
            apply_qss_role(b, role="subtle")
            b.setMinimumHeight(40)
            b.setCursor(Qt.CursorShape.PointingHandCursor)

        btn_open_chatgpt.clicked.connect(lambda: webbrowser.open("https://chatgpt.com/"))
        btn_open_claude.clicked.connect(lambda: webbrowser.open("https://claude.ai/"))
        btn_open_gemini.clicked.connect(lambda: webbrowser.open("https://gemini.google.com/"))

        copy_btn_row.addWidget(self.btn_copy_prompt, stretch=1)
        copy_btn_row.addWidget(btn_open_chatgpt)
        copy_btn_row.addWidget(btn_open_claude)
        copy_btn_row.addWidget(btn_open_gemini)
        step1_lay.addLayout(copy_btn_row)

        main_splitter.addWidget(step1_group)

        # =====================================================================
        # 우측 [2단계: 외부 LLM 응답 붙여넣기 & 3단계: 안전한 JSON 파싱]
        # =====================================================================
        step2_group = QGroupBox("2. 외부 LLM 응답 붙여넣기 · 순수 JSON 정규식 추출")
        apply_qss_role(step2_group, obj_name="Step2ResponseGroup")
        step2_lay = QVBoxLayout(step2_group)
        step2_lay.setContentsMargins(15, 15, 15, 15)
        step2_lay.setSpacing(10)

        paste_header = QHBoxLayout()
        paste_header.addWidget(QLabel("<b>외부 LLM 응답 붙여넣기 (인사말 자동 제거):</b>"))
        paste_header.addStretch()
        btn_paste_clip = QPushButton("↓ 클립보드 바로 붙여넣기")
        apply_qss_role(btn_paste_clip, role="secondary")
        btn_paste_clip.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_paste_clip.clicked.connect(self._on_paste_from_clipboard)

        btn_sample_resp = QPushButton("▪ 샘플 응답 채우기")
        apply_qss_role(btn_sample_resp, role="subtle")
        btn_sample_resp.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_sample_resp.clicked.connect(self._on_fill_sample_response)

        paste_header.addWidget(btn_paste_clip)
        paste_header.addWidget(btn_sample_resp)
        step2_lay.addLayout(paste_header)

        self.txt_llm_response = QTextEdit()
        apply_qss_role(self.txt_llm_response, obj_name="WebAiInputBox")
        self.txt_llm_response.setPlaceholderText(
            "ChatGPT, Claude, Gemini에서 복사한 응답을 이곳에 붙여넣으세요 (Ctrl+V).\n\n"
            "응답 앞뒤에 '네, 요청하신 문서 교정 결과입니다:' 같은 인사말이나 사족이 섞여 있어도\n"
            "정규표현식 엔진이 ```json ... ``` 코드 블록만 정확히 추출합니다."
        )
        step2_lay.addWidget(self.txt_llm_response, stretch=1)

        parse_bar = QHBoxLayout()
        btn_preview_parse = QPushButton("▸ JSON 추출 검증 및 미리보기")
        apply_qss_role(btn_preview_parse, role="secondary", obj_name="BtnPreviewJsonParse")
        btn_preview_parse.setMinimumHeight(36)
        btn_preview_parse.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_preview_parse.clicked.connect(self._on_preview_parse)

        self.lbl_parse_status = QLabel("▪ 대기 중")
        apply_qss_role(self.lbl_parse_status, role="statusBanner")
        parse_bar.addWidget(btn_preview_parse)
        parse_bar.addWidget(self.lbl_parse_status, stretch=1)
        step2_lay.addLayout(parse_bar)

        step2_lay.addWidget(QLabel("<b>3. 파싱 및 마크다운 변환 미리보기:</b>"))
        self.txt_parsed_preview = QTextEdit()
        apply_qss_role(self.txt_parsed_preview, obj_name="ParsedPreviewBox")
        self.txt_parsed_preview.setMaximumHeight(185)
        self.txt_parsed_preview.setPlaceholderText("JSON 파싱 검증 결과가 이곳에 표시됩니다...")
        step2_lay.addWidget(self.txt_parsed_preview)

        main_splitter.addWidget(step2_group)
        main_splitter.setSizes([540, 540])
        root_lay.addWidget(main_splitter, stretch=1)

        # 3. 하단 최종 적용 / 취소 버튼 바
        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch()

        btn_cancel = QPushButton("닫기")
        apply_qss_role(btn_cancel, role="subtle")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.setMinimumWidth(90)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)

        self.btn_apply = QPushButton("✓ 파싱 결과 워크스테이션에 적용")
        apply_qss_role(self.btn_apply, role="primary", obj_name="BtnApplyParsedJson")
        self.btn_apply.setMinimumHeight(40)
        self.btn_apply.setMinimumWidth(240)
        self.btn_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply.clicked.connect(self._on_apply_and_accept)

        bottom_bar.addWidget(btn_cancel)
        bottom_bar.addWidget(self.btn_apply)
        root_lay.addLayout(bottom_bar)

    def _on_template_changed(self) -> None:
        self._regenerate_prompt()

    def _regenerate_prompt(self) -> None:
        task_key = self.combo_task.currentData() or "ocr_correction"
        raw_text = self.txt_raw_input.toPlainText()
        custom_inst = self.edit_instruction.text().strip()

        sys_prompt, user_prompt = build_structured_prompt(
            task_key=task_key,
            raw_content=raw_text,
            custom_instruction=custom_inst,
        )
        combined = f"[System 지시문]\n{sys_prompt}\n\n{user_prompt}"
        self.txt_generated_prompt.setPlainText(combined)

    def _on_copy_prompt(self) -> None:
        prompt_text = self.txt_generated_prompt.toPlainText().strip()
        if not prompt_text:
            return
        QApplication.clipboard().setText(prompt_text)
        self.btn_copy_prompt.setText("✓ 클립보드에 복사 완료 (웹 AI에 Ctrl+V 하세요)")

    def _on_paste_from_clipboard(self) -> None:
        clip_text = QApplication.clipboard().text()
        if clip_text:
            self.txt_llm_response.setPlainText(clip_text)
            self._on_preview_parse()

    def _on_fill_sample_response(self) -> None:
        sample = (
            "네, 선생님! 요청하신 스캔 공문 OCR 교정 및 요약 결과를 JSON으로 전달해 드립니다.\n\n"
            "```json\n"
            "{\n"
            '  "document_title": "2026학년도 2학기 학부모 상담주간 운영 안내",\n'
            '  "corrected_text": "1. 관련: 교무기획부-1042(2026.11.03.)\\n2. 2026학년도 2학기 학부모 상담주간을 다음과 같이 운영하오니 각 학급 담임교사께서는 상담 일정을 취합하여 주시기 바랍니다.\\n  가. 운영 기간: 2026. 11. 09.(월) ~ 11. 13.(금)\\n  나. 대상: 전 학년 학부모 희망자",\n'
            '  "summary_3lines": [\n'
            '    "2026학년도 2학기 학부모 상담주간 운영 안내 공문",\n'
            '    "운영 기간은 11월 9일(월)부터 11월 13일(금)까지 5일간 진행",\n'
            '    "학급별 상담 희망 신청서 취합 후 교무기획부로 제출 필요"\n'
            "  ],\n"
            '  "action_items": [\n'
            '    "학부모 상담 신청 가정통신문 발송",\n'
            '    "학급별 상담 시간표 확정 및 제출"\n'
            "  ]\n"
            "}\n"
            "```\n"
            "추가로 수정할 부분이 있으면 언제든 말씀해 주세요!"
        )
        self.txt_llm_response.setPlainText(sample)
        self._on_preview_parse()

    def _on_preview_parse(self) -> bool:
        raw_resp = self.txt_llm_response.toPlainText().strip()
        if not raw_resp:
            self.lbl_parse_status.setText("[!] 응답 텍스트를 먼저 붙여넣어 주세요.")
            return False

        try:
            parsed = extract_json_from_llm_response(raw_resp)
            formatted = format_parsed_json_to_readable_text(parsed)
            self.parsed_json = parsed
            self.formatted_text = formatted
            self.txt_parsed_preview.setPlainText(formatted)
            self.lbl_parse_status.setText(
                f"✓ 순수 JSON 추출 및 파싱 성공 (추출된 키: {', '.join(parsed.keys())})"
            )
            return True
        except Exception as exc:
            self.lbl_parse_status.setText(f"✕ JSON 추출 오류: {exc}")
            self.txt_parsed_preview.setPlainText(str(exc))
            return False

    def _on_apply_and_accept(self) -> None:
        if not self._on_preview_parse():
            QMessageBox.warning(
                self,
                "JSON 파싱 확인",
                "외부 LLM 응답에서 유효한 JSON 코드 블록(```json ... ```)을 찾지 못했습니다.\n"
                "응답 텍스트를 확인해 주세요.",
            )
            return
        edited_preview = self.txt_parsed_preview.toPlainText().strip()
        if edited_preview:
            self.formatted_text = edited_preview
        self.accept()

    def get_result(self) -> Dict[str, Any]:
        return {
            "parsed_json": self.parsed_json,
            "formatted_text": self.formatted_text,
            "raw_response": self.txt_llm_response.toPlainText().strip(),
        }
