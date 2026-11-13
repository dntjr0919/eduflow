"""
새 DOCX 서식 파일에서 질문 및 표 구조를 자동 분석하여
JSON 스키마로 저장하는 모달 다이얼로그 (ui/dialogs/docx_schema_create_dialog.py).
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
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

from services.docx_extractor_service import analyze_docx_schema, save_schema_json
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


class DocxSchemaCreateDialog(QDialog):
    """빈 DOCX 서식을 분석하여 JSON 스키마 초안을 생성·저장하는 다이얼로그."""

    def __init__(self, parent: Optional[QWidget] = None, default_docx: str = ""):
        super().__init__(parent)
        apply_qss_role(self, obj_name="DocxSchemaCreateModal")
        self.setWindowTitle("+ 새 과제 양식(DOCX)에서 스키마 자동 생성")
        self.resize(720, 600)
        self.setMinimumSize(600, 500)

        self.analyzed_report: Optional[Dict[str, Any]] = None
        self.created_schema_path: Optional[str] = None
        self.created_form_name: Optional[str] = None

        self._init_ui(default_docx)

    def _init_ui(self, default_docx: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        # 상단 안내 배너
        lbl_banner = QLabel(
            "[!] <b>서식 DOCX 파일 자동 분석기</b><br>"
            "질문 번호(Q1, Q2, ...)와 답변 표(셀)의 레이아웃을 스캔하여 엑셀 변환용 JSON 스키마를 생성합니다."
        )
        lbl_banner.setWordWrap(True)
        apply_qss_role(lbl_banner, role="infoBanner", obj_name="SchemaCreateInfoBanner")
        layout.addWidget(lbl_banner)

        # 폼 레이아웃
        form = QFormLayout()
        form.setSpacing(10)

        # 1. 파일 선택
        box_file = QHBoxLayout()
        self.edit_docx_path = QLineEdit()
        self.edit_docx_path.setPlaceholderText("빈 양식 .docx 파일을 선택하세요.")
        self.edit_docx_path.setMinimumHeight(34)
        if default_docx:
            self.edit_docx_path.setText(default_docx)
            self.edit_docx_path.setToolTip(default_docx)
        apply_qss_role(self.edit_docx_path)

        btn_browse = QPushButton("찾아보기...")
        btn_browse.setMinimumHeight(34)
        btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_qss_role(btn_browse, role="secondary")
        btn_browse.clicked.connect(self._on_browse_docx)

        box_file.addWidget(self.edit_docx_path, stretch=1)
        box_file.addWidget(btn_browse)
        form.addRow("DOCX 서식 파일:", box_file)

        # 2. 양식 이름
        self.edit_form_name = QLineEdit()
        self.edit_form_name.setPlaceholderText("예: 진로탐구_활동지 (미입력 시 파일명 사용)")
        self.edit_form_name.setMinimumHeight(34)
        if default_docx:
            self.edit_form_name.setText(Path(default_docx).stem)
        apply_qss_role(self.edit_form_name)
        form.addRow("양식 이름:", self.edit_form_name)

        layout.addLayout(form)

        # 분석 실행 버튼
        btn_analyze = QPushButton("▸ 양식 구조 분석 및 미리보기")
        btn_analyze.setMinimumHeight(38)
        btn_analyze.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_qss_role(btn_analyze, role="secondary")
        btn_analyze.clicked.connect(self._on_analyze)
        layout.addWidget(btn_analyze)

        # 분석 결과 미리보기 영역
        lbl_preview = QLabel("분석 결과 미리보기:")
        apply_qss_role(lbl_preview, role="fieldLabel")
        layout.addWidget(lbl_preview)

        self.txt_preview = QTextEdit()
        self.txt_preview.setReadOnly(True)
        self.txt_preview.setPlaceholderText(
            "위의 '양식 구조 분석 및 미리보기' 버튼을 누르면 탐지된 필드 목록과 스키마 규칙이 표시됩니다."
        )
        apply_qss_role(self.txt_preview, obj_name="PromptCodeBox")
        layout.addWidget(self.txt_preview, stretch=1)

        # 하단 버튼 영역
        box_bottom = QHBoxLayout()
        box_bottom.setSpacing(10)

        self.btn_save = QPushButton("▸ schemas/ 폴더에 스키마 저장")
        self.btn_save.setMinimumHeight(40)
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.setEnabled(False)
        apply_qss_role(self.btn_save, role="primary")
        self.btn_save.clicked.connect(self._on_save_schema)

        btn_cancel = QPushButton("닫기")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_qss_role(btn_cancel, role="subtle")
        btn_cancel.clicked.connect(self.reject)

        box_bottom.addStretch()
        box_bottom.addWidget(self.btn_save)
        box_bottom.addWidget(btn_cancel)
        layout.addLayout(box_bottom)

    def _on_browse_docx(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "서식 DOCX 파일 선택", "", "Word 문서 (*.docx)"
        )
        if path:
            norm = str(Path(path).resolve())
            self.edit_docx_path.setText(norm)
            self.edit_docx_path.setToolTip(norm)
            if not self.edit_form_name.text().strip():
                self.edit_form_name.setText(Path(norm).stem)

    def _on_analyze(self) -> None:
        p_str = self.edit_docx_path.text().strip()
        if not p_str or not Path(p_str).is_file():
            QMessageBox.warning(self, "파일 확인", "분석할 .docx 파일을 먼저 선택해 주세요.")
            return

        form_name = self.edit_form_name.text().strip() or Path(p_str).stem

        try:
            report = analyze_docx_schema(p_str, form_name=form_name)
            self.analyzed_report = report

            # 보기 좋은 텍스트 리포트 생성
            fields = report["schema"].get("fields", {})
            keywords = report["schema"].get("detect_keywords", [])
            warnings = report.get("warnings", [])
            locations = report["schema"].get("extraction", [])

            lines = [
                f"=== [양식 분석 보고서: {form_name}] ===",
                f"▪ 소스 파일: {report['source_file']}",
                f"▪ 판별 키워드: {', '.join(keywords) if keywords else '(없음)'}",
                f"▪ 추출 필드 수: 총 {len(fields)}개",
                "",
                "[감지된 필드 및 별칭 목록]",
            ]
            for f_name, aliases in fields.items():
                lines.append(f"  ▪ {f_name}: {aliases}")

            lines.append("")
            lines.append("[추출 규칙 (Extraction Rules)]")
            for loc in locations:
                r_type = loc.get("reader", "unknown")
                f_target = loc.get("field", "")
                if r_type == "question_then_table":
                    lines.append(f"  ▪ {f_target} -> [질문 다음 첫 표 전체 읽기] (질문 블록 #{loc.get('question_block')})")
                else:
                    lines.append(f"  ▪ {f_target} -> [표 행 내 인접 셀 읽기] (블록 #{loc.get('table_block')}, 행 #{loc.get('row_index')})")

            if warnings:
                lines.append("")
                lines.append("[확인 및 유의 사항]")
                for w in warnings:
                    lines.append(f"  [!] {w}")

            self.txt_preview.setText("\n".join(lines))
            self.btn_save.setEnabled(True)

        except Exception as exc:
            self.btn_save.setEnabled(False)
            QMessageBox.critical(self, "분석 실패", f"DOCX 분석 중 오류가 발생했습니다:\n{exc}")

    def _on_save_schema(self) -> None:
        if not self.analyzed_report:
            return

        form_name = self.edit_form_name.text().strip()
        if form_name:
            self.analyzed_report["form_name"] = form_name

        try:
            schemas_dir = Path("schemas")
            saved_file = save_schema_json(self.analyzed_report, schemas_dir=schemas_dir)
            self.created_schema_path = str(saved_file.resolve())
            self.created_form_name = self.analyzed_report.get("form_name")

            QMessageBox.information(
                self,
                "저장 완료",
                f"양식 스키마가 성공적으로 저장되었습니다:\n{saved_file.name}\n\n"
                f"저장 위치: {saved_file.resolve()}\n"
                f"과제 양식 콤보박스에 자동으로 선택됩니다.",
            )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "저장 오류", f"스키마 JSON 저장 중 오류가 발생했습니다:\n{exc}")
