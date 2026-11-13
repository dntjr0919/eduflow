"""
새로운 AI 과세특 및 채점 프롬프트를 작성, 수정, 템플릿 로드할 수 있는 모달 다이얼로그 (ui/dialogs/prompt_edit_dialog.py).
"""

import re
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
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


PROMPT_TEMPLATES_CATALOG = {
    "1. 기본 교과세특 작성 템플릿 (행동 중심 관찰 서술)": """[역할]
당신은 대한민국 중·고등학교 교사를 지원하는 NEIS 학교생활기록부 작성 전문 AI이다.
현재 작업은 교과 세부능력 및 특기사항 작성이며, 학생 응답과 교사의 관찰·평가 자료를 근거로 나이스에 입력 가능한 문장을 작성한다.

[기본 원칙]
1. 학생 응답과 교사가 제공한 관찰 기록에 있는 사실만 사용한다.
2. 없는 활동, 역할, 태도, 성취, 협업, 발표, 토론, 실험 과정, 결과를 추정하거나 추가하지 않는다.
3. 학생이 작성한 문장을 그대로 길게 옮기지 말고, 교사의 관찰자 시점에서 재구성한다.
4. 참고자료는 수업 맥락과 교과 용어를 정교하게 반영하는 용도로만 사용한다.
5. 참고자료의 문장을 그대로 베끼지 않는다.

[사용자 요청]
1. 바이트 제한 요청 : {max_bytes}바이트 이내로 작성

{ref_section}

[행동 중심 작성 원칙]
1. 학생의 내면 상태, 감정, 의도, 인지 수준을 추정·단정하는 표현을 사용하지 않는다.
2. '이해함, 파악함, 생각함, 깨달음, 느낌, 관심을 가짐, 노력함' 등의 표현 대신 교사가 확인 가능한 행동 중심으로 작성한다:
   발표함, 토론함, 비교함, 분류함, 분석함, 계산함, 해석함, 설명함, 해결 방안을 제안함, 자료를 수정·보완함, 역할을 분담함, 기록함
3. 위 행동 표현도 학생 응답에 실제 근거가 있을 때만 사용한다.

[문체 및 형식]
1. 실제 나이스 입력용 최종 문장만 작성한다.
2. 교사의 관찰자 시점으로 작성한다.
3. 학생 이름({student_name}), 학번, 1인칭 표현, 자기소개식 문체를 사용하지 않는다.
4. 모든 문장은 반드시 명사형 종결인 '~함', '~임'으로 끝낸다.
5. 결과는 줄바꿈 없는 하나의 완성된 문단으로 작성한다.
6. 제목, 설명, 따옴표 없이 결과 문장만 출력한다.
7. 나이스 기준 최대 {max_bytes}바이트 이내를 목표로 작성한다.

[학생 응답]
{answers_text}

[출력]
""",
    "2. 자유 주제 탐구 계획서 채점 템플릿": """[역할]
당신은 과학과 자유 주제 탐구 계획서를 공정하고 객관적으로 평가하는 교사 채점 보조 AI이다.
제시된 학생의 계획서 응답을 루브릭 기준에 대조하여 평가 피드백과 채점 의견을 작성한다.

[평가 기준]
1. 주제 선정의 타당성 및 동기 (10점)
2. 변인 설정(조작변인, 통제변인, 종속변인)의 구체성 및 타당성 (10점)
3. 탐구 설계 및 실험 과정의 실현 가능성 (10점)

{ref_section}

[출력 형식]
- 학생: {student_name}
- 총평: 행동 중심의 객관적 관찰 기록 (명사형 종결 '~함')
- 강점 및 보완점 2가지 서술

[학생 응답]
{answers_text}

[채점 및 평가 결과]
""",
    "3. 자유 주제 탐구 보고서 채점 템플릿": """[역할]
당신은 과학과 자유 주제 탐구 보고서를 심사하는 전문 평가 위원 AI이다.
학생의 탐구 수행 결과와 결론 도출, 고찰 과정을 면밀히 검토하여 최종 평가 문장을 작성한다.

[평가 주안점]
1. 수집된 자료 및 측정 결과의 체계적 분석
2. 가설 검증과 논리적 결론 도출 과정
3. 한계점 인식 및 후속 탐구 방향성 제시

{ref_section}

[작성 지침]
- 학생 이름({student_name})과 1인칭 표현 제외
- '~함', '~임' 명사형 종결
- 분량: {max_bytes}바이트 이내

[학생 응답]
{answers_text}

[출력]
""",
    "4. 세특 요약 및 다듬기 템플릿": """[역할]
기존에 작성된 학생 과세특 초안에서 중복 표현, 장황한 접속어, 추상적 평가를 걷어내고
핵심 탐구 역량과 관찰 행동만을 보존하여 정제하는 NEIS 에디터 AI이다.

[정제 규칙]
1. 반복 수식어 삭제
2. 원문에 없는 사실 임의 추가 절대 금지
3. 분량 상한: 최대 {max_bytes}바이트 이내
4. 문체: '~함', '~임' 명사형 종결 유지

[원문]
{answers_text}

[정제된 최종 과세특]
""",
}


class PromptEditDialog(QDialog):
    """새 프롬프트를 작성하거나 기존 템플릿을 불러와 저장하는 모달 다이얼로그."""

    def __init__(self, parent: Optional[QWidget] = None, default_title: str = ""):
        super().__init__(parent)
        apply_qss_role(self, obj_name="PromptEditModal")
        self.setWindowTitle("+ 새 AI 세특/채점 프롬프트 작성")
        self.resize(780, 680)
        self.setMinimumSize(640, 520)

        self.saved_prompt_path: Optional[str] = None
        self._init_ui(default_title)

    def _init_ui(self, default_title: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        # 상단 안내 배너 (치환 변수 가이드)
        lbl_banner = QLabel(
            "[!] <b>프롬프트 작성 및 동적 치환 변수 안내</b><br>"
            "AI가 학생별로 치환하여 처리할 변수들을 본문에 포함할 수 있습니다:<br>"
            "▪ <code>{answers_text}</code> : 학생의 문항별 응답 내용 (<b>필수</b>)<br>"
            "▪ <code>{student_name}</code> : 학생 이름<br>"
            "▪ <code>{reference_context}</code> : 참고자료 폴더 문서 내용<br>"
            "▪ <code>{max_bytes}</code> : 나이스 바이트 상한선 숫자"
        )
        lbl_banner.setWordWrap(True)
        apply_qss_role(lbl_banner, role="infoBanner", obj_name="PromptEditInfoBanner")
        layout.addWidget(lbl_banner)

        # 상단 설정 영역 (파일명 및 템플릿 불러오기)
        form = QFormLayout()
        form.setSpacing(10)

        # 1. 파일명 입력
        box_name = QHBoxLayout()
        self.edit_prompt_name = QLineEdit()
        self.edit_prompt_name.setPlaceholderText("예: 자유주제탐구_보고서_평가 (확장자 제외)")
        self.edit_prompt_name.setMinimumHeight(34)
        if default_title:
            self.edit_prompt_name.setText(default_title)
        apply_qss_role(self.edit_prompt_name)

        lbl_prefix = QLabel("저장 파일명: prompts_memory/prompt_")
        apply_qss_role(lbl_prefix, role="mutedText")
        lbl_ext = QLabel(".txt")
        apply_qss_role(lbl_ext, role="mutedText")

        box_name.addWidget(lbl_prefix)
        box_name.addWidget(self.edit_prompt_name, stretch=1)
        box_name.addWidget(lbl_ext)
        form.addRow("프롬프트 이름:", box_name)

        # 2. 템플릿 선택 및 외부 파일 불러오기
        box_tpl = QHBoxLayout()
        self.combo_tpl = QComboBox()
        self.combo_tpl.setMinimumHeight(34)
        self.combo_tpl.addItem("▸ 추천 템플릿 선택하여 채우기...", "")
        for tpl_name in PROMPT_TEMPLATES_CATALOG:
            self.combo_tpl.addItem(tpl_name, tpl_name)
        apply_qss_role(self.combo_tpl)
        self.combo_tpl.currentIndexChanged.connect(self._on_template_selected)

        btn_import = QPushButton("↗ 외부 .txt 가져오기")
        btn_import.setMinimumHeight(34)
        btn_import.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_qss_role(btn_import, role="secondary")
        btn_import.clicked.connect(self._on_import_txt)

        box_tpl.addWidget(self.combo_tpl, stretch=1)
        box_tpl.addWidget(btn_import)
        form.addRow("템플릿 로드:", box_tpl)

        layout.addLayout(form)

        # 프롬프트 에디터
        lbl_editor = QLabel("프롬프트 본문:")
        apply_qss_role(lbl_editor, role="fieldLabel")
        layout.addWidget(lbl_editor)

        self.txt_content = QTextEdit()
        self.txt_content.setPlaceholderText("여기에 프롬프트 내용을 작성하세요. 반드시 {answers_text}가 포함되어야 합니다.")
        apply_qss_role(self.txt_content, obj_name="PromptCodeBox")
        layout.addWidget(self.txt_content, stretch=1)

        # 기본 템플릿 하나 자동 로드
        default_tpl_key = list(PROMPT_TEMPLATES_CATALOG.keys())[0]
        self.txt_content.setText(PROMPT_TEMPLATES_CATALOG[default_tpl_key])

        # 하단 버튼
        box_bottom = QHBoxLayout()
        box_bottom.setSpacing(10)

        btn_save = QPushButton("▸ prompts_memory/에 저장")
        btn_save.setMinimumHeight(40)
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_qss_role(btn_save, role="primary")
        btn_save.clicked.connect(self._on_save_prompt)

        btn_cancel = QPushButton("취소")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_qss_role(btn_cancel, role="subtle")
        btn_cancel.clicked.connect(self.reject)

        box_bottom.addStretch()
        box_bottom.addWidget(btn_save)
        box_bottom.addWidget(btn_cancel)
        layout.addLayout(box_bottom)

    def _on_template_selected(self, index: int) -> None:
        key = self.combo_tpl.currentData()
        if key and key in PROMPT_TEMPLATES_CATALOG:
            # 기존 내용이 있을 때 확인
            if self.txt_content.toPlainText().strip():
                reply = QMessageBox.question(
                    self,
                    "템플릿 적용 확인",
                    "현재 작성 중인 본문을 선택한 템플릿 내용으로 덮어쓰시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            self.txt_content.setText(PROMPT_TEMPLATES_CATALOG[key])

    def _on_import_txt(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "외부 프롬프트 파일 가져오기", "", "텍스트 파일 (*.txt *.md);;모든 파일 (*)"
        )
        if path:
            try:
                content = Path(path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = Path(path).read_text(encoding="cp949", errors="replace")

            self.txt_content.setText(content)
            if not self.edit_prompt_name.text().strip():
                raw_stem = Path(path).stem
                clean_stem = re.sub(r"^prompt_", "", raw_stem)
                self.edit_prompt_name.setText(clean_stem)

    def _on_save_prompt(self) -> None:
        raw_name = self.edit_prompt_name.text().strip()
        if not raw_name:
            QMessageBox.warning(self, "이름 입력", "프롬프트 이름을 입력해 주세요.")
            self.edit_prompt_name.setFocus()
            return

        content = self.txt_content.toPlainText().strip()
        if not content:
            QMessageBox.warning(self, "내용 확인", "프롬프트 본문 내용이 비어 있습니다.")
            self.txt_content.setFocus()
            return

        if "{answers_text}" not in content:
            reply = QMessageBox.question(
                self,
                "필수 변수 누락 확인",
                "프롬프트에 '{answers_text}' 치환 변수가 없습니다.\n"
                "'{answers_text}'가 없으면 학생의 실제 과제 응답이 AI에 전달되지 않습니다.\n\n"
                "그래도 저장하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # 저장 파일명 결정
        clean_name = re.sub(r'[\\/*?:"<>|]', "_", raw_name)
        if not clean_name.startswith("prompt_"):
            filename = f"prompt_{clean_name}.txt"
        else:
            filename = f"{clean_name}.txt"

        prompts_dir = Path("prompts_memory")
        prompts_dir.mkdir(parents=True, exist_ok=True)
        target_path = prompts_dir / filename

        try:
            target_path.write_text(content, encoding="utf-8")
            self.saved_prompt_path = str(target_path.resolve())

            QMessageBox.information(
                self,
                "저장 완료",
                f"프롬프트 파일이 성공적으로 저장되었습니다:\n{filename}\n\n"
                f"경로: {target_path.resolve()}\n"
                f"프롬프트 선택 콤보박스에 자동으로 선택됩니다.",
            )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", f"파일 저장 중 오류가 발생했습니다:\n{exc}")
