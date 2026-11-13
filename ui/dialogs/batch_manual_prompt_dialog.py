"""
수동 프롬프트 배치 모드 (웹 AI 복사·붙여넣기 일괄 처리) 모달 다이얼로그 (ui/dialogs/batch_manual_prompt_dialog.py).
- 컨텍스트(토큰) 및 인원 상한에 따라 미완료 학생을 배치 단위로 분할
- 좌측: 조립된 배치 프롬프트 미리보기, 복사 및 웹 AI 열기
- 우측: 웹 AI JSON 응답 붙여넣기, 매핑 검증 테이블, 즉시 엑셀 저장 및 다음 배치 자동 복사
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
import webbrowser

import pandas as pd
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import TaskRunner
from services.batch_prompt_service import (
    apply_batch_results_to_excel,
    build_student_batches,
    extract_batch_items_from_response,
    normalize_student_id_str,
    resolve_batch_output_path,
)
from services.record_generator_service import (
    calculate_neis_bytes,
    load_all_references,
    load_prompt_text,
)
from services.web_gemini_auto_worker import PROVIDER_CONFIGS, WebGeminiAutoWorker
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


class BatchManualPromptDialog(QDialog):
    """엑셀 학생 데이터를 토큰 단위로 배치 분할해 웹 AI 복붙으로 일괄 처리하는 모달 다이얼로그."""

    TASK_AUTO_GEMINI = "batch_manual_auto_gemini"

    def __init__(
        self,
        excel_path: Path | str,
        prompt_file: Optional[Path | str] = None,
        reference_dir: Optional[Path | str] = None,
        max_bytes: int = 1500,
        parent: Optional[QWidget] = None,
        task_runner: Optional[TaskRunner] = None,
    ):
        super().__init__(parent)
        apply_qss_role(self, obj_name="BatchManualPromptModal")
        self.setWindowTitle("수동 프롬프트 배치 모드 (웹 AI 일괄 처리)")
        self.resize(1240, 860)
        self.setMinimumSize(980, 700)

        self.excel_path = Path(excel_path)
        self.prompt_file = Path(prompt_file) if prompt_file else None
        self.reference_dir = Path(reference_dir) if reference_dir else None
        self.max_bytes = int(max_bytes)
        self._task_runner = task_runner if task_runner is not None else TaskRunner(self)
        self._active_worker: Optional[WebGeminiAutoWorker] = None

        self.is_grading = bool(
            self.prompt_file is not None and "채점" in self.prompt_file.name
        )
        self.output_path = resolve_batch_output_path(
            self.excel_path, None, is_grading=self.is_grading
        )
        self.last_saved_excel_path: Optional[str] = None
        self.total_saved_count: int = 0

        self._prompt_template = load_prompt_text(self.prompt_file)
        self._reference_context = (
            load_all_references(self.reference_dir)
            if self.reference_dir and self.reference_dir.exists()
            else ""
        )

        self._batches: List[Dict[str, Any]] = []
        self._parsed_items: List[Dict[str, str]] = []

        self._init_ui()
        self._rebuild_batches(copy_first=False)

    def _init_ui(self) -> None:
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(16, 16, 16, 16)
        root_lay.setSpacing(12)

        # ---------------------------------------------------------------------
        # 1. 상단 컨트롤 바 카드
        # ---------------------------------------------------------------------
        header_card = QFrame()
        apply_qss_role(header_card, obj_name="ModalHeaderCard")
        header_lay = QVBoxLayout(header_card)
        header_lay.setContentsMargins(14, 12, 14, 12)
        header_lay.setSpacing(10)

        mode_label = "AI 채점 모드" if self.is_grading else "과세특 생성 모드"
        banner = QLabel(
            f"[!] <b>수동 프롬프트 배치 모드 · {mode_label}</b> (출력 대상: <code>{self.output_path.name}</code>)<br>"
            "미완료 학생만 컨텍스트 예산에 맞춰 자동 분할합니다. 좌측 프롬프트를 복사해 웹 AI에 실행하고, "
            "응답 JSON을 우측에 붙여넣어 엑셀에 즉시 반영하세요."
        )
        banner.setWordWrap(True)
        apply_qss_role(banner, role="infoBanner", obj_name="BatchModalInfoBanner")
        header_lay.addWidget(banner)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)

        lbl_batch = QLabel("<b>배치 선택:</b>")
        self.combo_batch = QComboBox()
        self.combo_batch.setMinimumHeight(34)
        install_no_scroll_wheel_filter(self.combo_batch)
        self.combo_batch.currentIndexChanged.connect(self._on_batch_selection_changed)

        lbl_max_stu = QLabel("<b>묶음당 최대 인원:</b>")
        self.spin_max_students = QSpinBox()
        self.spin_max_students.setRange(1, 50)
        self.spin_max_students.setValue(15)
        self.spin_max_students.setSuffix(" 명")
        self.spin_max_students.setMinimumHeight(34)
        install_no_scroll_wheel_filter(self.spin_max_students)

        lbl_max_tok = QLabel("<b>최대 입력 토큰:</b>")
        self.spin_max_tokens = QSpinBox()
        self.spin_max_tokens.setRange(3000, 100000)
        self.spin_max_tokens.setSingleStep(1000)
        self.spin_max_tokens.setValue(15000)
        self.spin_max_tokens.setSuffix(" 토큰")
        self.spin_max_tokens.setMinimumHeight(34)
        install_no_scroll_wheel_filter(self.spin_max_tokens)

        self.btn_resplit = QPushButton("↻ 다시 분할")
        apply_qss_role(self.btn_resplit, role="secondary", obj_name="BtnResplitBatches")
        self.btn_resplit.setMinimumHeight(34)
        self.btn_resplit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_resplit.clicked.connect(lambda: self._rebuild_batches(copy_first=False))

        ctrl_row.addWidget(lbl_batch)
        ctrl_row.addWidget(self.combo_batch, stretch=3)
        ctrl_row.addWidget(lbl_max_stu)
        ctrl_row.addWidget(self.spin_max_students)
        ctrl_row.addWidget(lbl_max_tok)
        ctrl_row.addWidget(self.spin_max_tokens)
        ctrl_row.addWidget(self.btn_resplit)
        header_lay.addLayout(ctrl_row)

        root_lay.addWidget(header_card)

        # ---------------------------------------------------------------------
        # 2. 좌우 스플리터 (좌: 프롬프트 장전/복사 | 우: 응답 붙여넣기/검증/저장)
        # ---------------------------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 좌측 패널
        left_group = QGroupBox("1. 묶음 프롬프트 장전 및 복사")
        apply_qss_role(left_group, obj_name="BatchLeftPromptGroup")
        left_lay = QVBoxLayout(left_group)
        left_lay.setContentsMargins(14, 14, 14, 14)
        left_lay.setSpacing(10)

        self.lbl_prompt_meta = QLabel("▪ 대상 학생 및 예상 토큰 정보가 여기에 표시됩니다.")
        apply_qss_role(self.lbl_prompt_meta, role="statusBanner", obj_name="BatchPromptMeta")
        left_lay.addWidget(self.lbl_prompt_meta)

        self.txt_final_prompt = QTextEdit()
        self.txt_final_prompt.setReadOnly(True)
        apply_qss_role(self.txt_final_prompt, obj_name="PromptCodeBox")
        self.txt_final_prompt.setPlaceholderText("미완료 학생 배치가 생성되면 이곳에 최종 프롬프트가 표시됩니다.")
        left_lay.addWidget(self.txt_final_prompt, stretch=1)

        left_btn_row = QHBoxLayout()
        left_btn_row.setSpacing(8)

        self.btn_copy_prompt = QPushButton("▸ 현재 묶음 프롬프트 복사")
        apply_qss_role(self.btn_copy_prompt, role="primary", obj_name="BtnCopyBatchPrompt")
        self.btn_copy_prompt.setMinimumHeight(40)
        self.btn_copy_prompt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_prompt.clicked.connect(self._on_copy_current_prompt)

        self.btn_open_web_ai = QPushButton("↗ 웹 AI 열기")
        apply_qss_role(self.btn_open_web_ai, role="subtle", obj_name="BtnOpenWebAi")
        self.btn_open_web_ai.setMinimumHeight(40)
        self.btn_open_web_ai.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_web_ai.clicked.connect(self._on_open_selected_web_ai)
        self.btn_open_gemini = self.btn_open_web_ai

        left_btn_row.addWidget(self.btn_copy_prompt, stretch=1)
        left_btn_row.addWidget(self.btn_open_web_ai)
        left_lay.addLayout(left_btn_row)

        auto_opt_row = QHBoxLayout()
        auto_opt_row.setSpacing(8)

        lbl_provider = QLabel("<b>대상 웹 AI:</b>")
        self.combo_web_provider = QComboBox()
        self.combo_web_provider.setMinimumHeight(34)
        install_no_scroll_wheel_filter(self.combo_web_provider)
        self.combo_web_provider.addItem("Google Gemini (웹)", "gemini")
        self.combo_web_provider.addItem("ChatGPT (웹)", "chatgpt")
        self.combo_web_provider.addItem("Claude (웹)", "claude")

        self.chk_require_login = QCheckBox("로그인 상태 확인 후 실행 (구독 모델 사용)")
        self.chk_require_login.setChecked(True)
        install_no_scroll_wheel_filter(self.chk_require_login)

        auto_opt_row.addWidget(lbl_provider)
        auto_opt_row.addWidget(self.combo_web_provider, stretch=1)
        auto_opt_row.addWidget(self.chk_require_login)
        left_lay.addLayout(auto_opt_row)

        auto_btn_row = QHBoxLayout()
        auto_btn_row.setSpacing(8)

        self.btn_auto_gemini = QPushButton("▸ 웹 AI 완전 자동 실행 (Playwright)")
        apply_qss_role(self.btn_auto_gemini, role="secondary", obj_name="BtnAutoGeminiPlaywright")
        self.btn_auto_gemini.setMinimumHeight(38)
        self.btn_auto_gemini.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_gemini.clicked.connect(self._on_start_auto_gemini)

        self.btn_stop_auto_gemini = QPushButton("■ 자동 실행 중지")
        apply_qss_role(self.btn_stop_auto_gemini, role="danger", obj_name="BtnStopAutoGemini")
        self.btn_stop_auto_gemini.setMinimumHeight(38)
        self.btn_stop_auto_gemini.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop_auto_gemini.setEnabled(False)
        self.btn_stop_auto_gemini.clicked.connect(self._on_stop_auto_gemini)

        auto_btn_row.addWidget(self.btn_auto_gemini, stretch=1)
        auto_btn_row.addWidget(self.btn_stop_auto_gemini)
        left_lay.addLayout(auto_btn_row)

        splitter.addWidget(left_group)

        # 우측 패널
        right_group = QGroupBox("2. 웹 AI 응답 붙여넣기 및 엑셀 반영")
        apply_qss_role(right_group, obj_name="BatchRightVerifyGroup")
        right_lay = QVBoxLayout(right_group)
        right_lay.setContentsMargins(14, 14, 14, 14)
        right_lay.setSpacing(10)

        paste_bar = QHBoxLayout()
        paste_bar.addWidget(QLabel("<b>웹 AI JSON 응답 입력:</b>"))
        paste_bar.addStretch()

        self.btn_paste_verify = QPushButton("↓ 클립보드 붙여넣기 · 검증")
        apply_qss_role(self.btn_paste_verify, role="secondary", obj_name="BtnPasteVerifyBatch")
        self.btn_paste_verify.setMinimumHeight(32)
        self.btn_paste_verify.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_paste_verify.clicked.connect(self._on_paste_and_verify)
        paste_bar.addWidget(self.btn_paste_verify)
        right_lay.addLayout(paste_bar)

        self.txt_llm_response = QTextEdit()
        apply_qss_role(self.txt_llm_response, obj_name="WebAiInputBox")
        self.txt_llm_response.setMaximumHeight(180)
        self.txt_llm_response.setPlaceholderText(
            "웹 AI(Gemini / ChatGPT / Claude)에서 복사한 응답을 붙여넣으세요 (Ctrl+V).\n"
            "응답 앞뒤 인사말이 포함되어 있어도 ```json ... ``` 배열만 자동으로 추출해 매핑합니다."
        )
        self.txt_llm_response.textChanged.connect(self._on_response_text_changed)
        right_lay.addWidget(self.txt_llm_response)

        self.lbl_verify_status = QLabel("▪ 대기 중: 웹 AI 응답을 붙여넣으면 학생별 매핑 상태가 표시됩니다.")
        self.lbl_verify_status.setWordWrap(True)
        apply_qss_role(self.lbl_verify_status, role="statusBanner", obj_name="BatchVerifyStatus")
        right_lay.addWidget(self.lbl_verify_status)

        self.table_preview = QTableWidget(0, 4)
        self.table_preview.setAlternatingRowColors(True)
        self.table_preview.verticalHeader().setDefaultSectionSize(30)
        self.table_preview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_preview.setHorizontalHeaderLabels(["학번", "이름", "결과 미리보기", "상태"])
        hdr = self.table_preview.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        right_lay.addWidget(self.table_preview, stretch=1)

        right_btn_row = QHBoxLayout()
        right_btn_row.setSpacing(8)

        self.btn_close = QPushButton("닫기")
        apply_qss_role(self.btn_close, role="subtle", obj_name="BtnCloseBatchModal")
        self.btn_close.setMinimumHeight(40)
        self.btn_close.setMinimumWidth(88)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.accept)

        self.btn_save_and_next = QPushButton("▸ 엑셀 저장 후 다음 묶음 복사")
        apply_qss_role(self.btn_save_and_next, role="primary", obj_name="BtnSaveBatchAndNext")
        self.btn_save_and_next.setMinimumHeight(40)
        self.btn_save_and_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save_and_next.clicked.connect(self._on_save_and_next_batch)

        right_btn_row.addStretch()
        right_btn_row.addWidget(self.btn_close)
        right_btn_row.addWidget(self.btn_save_and_next, stretch=1)
        right_lay.addLayout(right_btn_row)

        splitter.addWidget(right_group)
        splitter.setSizes([580, 620])
        root_lay.addWidget(splitter, stretch=1)

        # ---------------------------------------------------------------------
        # 3. 하단 Playwright 자동화 실시간 진행률 및 상태바
        # ---------------------------------------------------------------------
        status_card = QFrame()
        apply_qss_role(status_card, obj_name="ModalHeaderCard")
        status_lay = QHBoxLayout(status_card)
        status_lay.setContentsMargins(12, 8, 12, 8)
        status_lay.setSpacing(10)

        self.lbl_auto_status = QLabel("▪ 대기 중: 수동 복사·붙여넣기 또는 Playwright 웹 AI 자동 실행을 선택하세요.")
        self.lbl_auto_status.setWordWrap(True)
        apply_qss_role(self.lbl_auto_status, role="statusBanner", obj_name="BatchAutoStatusLabel")

        self.progress_auto = QProgressBar()
        self.progress_auto.setObjectName("ModernProgressBar")
        self.progress_auto.setRange(0, 100)
        self.progress_auto.setValue(0)
        self.progress_auto.setTextVisible(False)
        self.progress_auto.setFixedWidth(180)

        status_lay.addWidget(self.lbl_auto_status, stretch=1)
        status_lay.addWidget(self.progress_auto)
        root_lay.addWidget(status_card)

    def _load_current_dataframe(self) -> pd.DataFrame:
        target = self.output_path if self.output_path.exists() else self.excel_path
        return pd.read_excel(target)

    def _format_batch_label(self, batch: Dict[str, Any], total_batches: int) -> str:
        students = batch.get("students", [])
        idx = batch.get("batch_index", 1)
        tokens = batch.get("estimated_tokens", 0)
        if not students:
            return f"Batch {idx} / {total_batches} (0명 | 예상 {tokens:,} 토큰)"
        first = students[0]
        first_str = f"{first.get('학번', '')} {first.get('이름', '')}".strip()
        if len(students) > 1:
            stu_summary = f"{first_str} 외 {len(students) - 1}명"
        else:
            stu_summary = first_str
        return f"Batch {idx} / {total_batches} ({stu_summary} | 예상 {tokens:,} 토큰)"

    def _rebuild_batches(self, copy_first: bool = False) -> Optional[str]:
        try:
            df = self._load_current_dataframe()
        except Exception as exc:
            QMessageBox.critical(self, "엑셀 읽기 오류", f"엑셀 파일을 읽는 중 오류가 발생했습니다:\n{exc}")
            return None

        self._batches = build_student_batches(
            df=df,
            prompt_template=self._prompt_template,
            reference_context=self._reference_context,
            max_students=self.spin_max_students.value(),
            max_input_tokens=self.spin_max_tokens.value(),
            is_grading=self.is_grading,
            max_bytes=self.max_bytes,
        )

        self.combo_batch.blockSignals(True)
        self.combo_batch.clear()
        total = len(self._batches)
        for batch in self._batches:
            self.combo_batch.addItem(self._format_batch_label(batch, total), batch)
        self.combo_batch.blockSignals(False)

        if total == 0:
            self.txt_final_prompt.clear()
            self.lbl_prompt_meta.setText("✓ 처리할 미완료 학생이 없습니다. 모든 학생의 결과가 기록되어 있습니다.")
            self.btn_copy_prompt.setEnabled(False)
            self.btn_save_and_next.setEnabled(False)
            if hasattr(self, "btn_auto_gemini"):
                self.btn_auto_gemini.setEnabled(False)
            self.table_preview.setRowCount(0)
            self.lbl_verify_status.setText("✓ 모든 배치 작업이 완료되었습니다.")
            return None

        self.btn_copy_prompt.setEnabled(True)
        self.btn_save_and_next.setEnabled(True)
        if hasattr(self, "btn_auto_gemini") and self._active_worker is None:
            self.btn_auto_gemini.setEnabled(True)
        self.combo_batch.setCurrentIndex(0)
        self._display_current_batch()

        if copy_first:
            first_prompt = self._batches[0]["final_prompt"]
            QApplication.clipboard().setText(first_prompt)
            self.btn_copy_prompt.setText("✓ 다음 묶음 자동 복사 완료 (Ctrl+V 하세요)")
            return first_prompt
        return None

    def _get_current_batch(self) -> Optional[Dict[str, Any]]:
        idx = self.combo_batch.currentIndex()
        if 0 <= idx < len(self._batches):
            return self._batches[idx]
        return None

    def _display_current_batch(self) -> None:
        batch = self._get_current_batch()
        self.btn_copy_prompt.setText("▸ 현재 묶음 프롬프트 복사")
        if batch is None:
            self.txt_final_prompt.clear()
            self.table_preview.setRowCount(0)
            return

        students = batch.get("students", [])
        tokens = batch.get("estimated_tokens", 0)
        self.lbl_prompt_meta.setText(
            f"▪ 현재 묶음: 학생 {len(students)}명 | 예상 입력 토큰 약 {tokens:,} 토큰"
        )
        self.txt_final_prompt.setPlainText(batch.get("final_prompt", ""))
        self._refresh_verification_preview()

    def _on_batch_selection_changed(self, _index: int) -> None:
        self._display_current_batch()

    def _on_copy_current_prompt(self) -> None:
        prompt_text = self.txt_final_prompt.toPlainText().strip()
        if not prompt_text:
            return
        QApplication.clipboard().setText(prompt_text)
        self.btn_copy_prompt.setText("✓ 클립보드에 복사 완료 (웹 AI에 Ctrl+V 하세요)")

    def _on_paste_and_verify(self) -> None:
        clip_text = QApplication.clipboard().text()
        if not clip_text or not clip_text.strip():
            self.lbl_verify_status.setText("[!] 클립보드에 붙여넣을 텍스트가 비어 있습니다.")
            return
        self.txt_llm_response.setPlainText(clip_text)

    def _on_response_text_changed(self) -> None:
        self._refresh_verification_preview()

    def _refresh_verification_preview(self) -> bool:
        batch = self._get_current_batch()
        batch_students = batch.get("students", []) if batch else []

        raw_resp = self.txt_llm_response.toPlainText().strip()
        if not raw_resp:
            self._parsed_items = []
            self._populate_preview_table(batch_students, {})
            self.lbl_verify_status.setText(
                f"▪ 대기 중: 현재 묶음 {len(batch_students)}명의 웹 AI JSON 응답을 붙여넣어 주세요."
            )
            return False

        try:
            items = extract_batch_items_from_response(raw_resp)
            self._parsed_items = items
        except Exception as exc:
            self._parsed_items = []
            self._populate_preview_table(batch_students, {})
            self.lbl_verify_status.setText(f"✕ JSON 추출 실패: {exc}")
            return False

        parsed_by_sid: Dict[str, Dict[str, str]] = {}
        for it in items:
            sid = normalize_student_id_str(it.get("학번", ""))
            if sid:
                parsed_by_sid[sid] = it

        self._populate_preview_table(batch_students, parsed_by_sid)

        batch_sids = {
            normalize_student_id_str(s.get("학번", ""))
            for s in batch_students
            if s.get("학번")
        }
        matched_cnt = sum(1 for sid in batch_sids if sid in parsed_by_sid)
        missing_cnt = max(0, len(batch_sids) - matched_cnt)
        extra_sids = [sid for sid in parsed_by_sid.keys() if sid not in batch_sids]

        if missing_cnt == 0 and not extra_sids:
            self.lbl_verify_status.setText(
                f"✓ 검증 완료: 현재 묶음 {len(batch_students)}명 전원 매칭됨 ({len(items)}건 추출)"
            )
        else:
            extra_note = f" | 배치 외 학번 {len(extra_sids)}건" if extra_sids else ""
            self.lbl_verify_status.setText(
                f"[!] 검증 요약: 총 {len(items)}건 추출 · 현재 묶음 매칭 {matched_cnt}/{len(batch_students)}명 · 누락 {missing_cnt}명{extra_note}"
            )
        return True

    def _populate_preview_table(
        self,
        batch_students: List[Dict[str, str]],
        parsed_by_sid: Dict[str, Dict[str, str]],
    ) -> None:
        rows_data: List[Dict[str, str]] = []
        seen_sids = set()

        for stu in batch_students:
            sid = normalize_student_id_str(stu.get("학번", ""))
            sname = stu.get("이름", "")
            seen_sids.add(sid)
            matched = parsed_by_sid.get(sid)
            if matched:
                res_text = matched.get("결과", "")
                if not res_text:
                    status = "[!] 결과 비어 있음"
                elif self.is_grading:
                    status = "✓ 매칭 완료"
                else:
                    b_cnt = calculate_neis_bytes(res_text)
                    status = f"✓ 매칭 완료 ({b_cnt}B)"
                rows_data.append({
                    "학번": sid,
                    "이름": sname or matched.get("이름", ""),
                    "결과": res_text,
                    "상태": status,
                })
            else:
                rows_data.append({
                    "학번": sid,
                    "이름": sname,
                    "결과": "",
                    "상태": "[!] 응답 대기" if not parsed_by_sid else "[!] 응답 누락",
                })

        for sid, extra in parsed_by_sid.items():
            if sid not in seen_sids:
                rows_data.append({
                    "학번": sid,
                    "이름": extra.get("이름", ""),
                    "결과": extra.get("결과", ""),
                    "상태": "▪ 추가 학번(엑셀 매칭 시 반영)",
                })

        self.table_preview.setRowCount(0)
        for row_idx, r in enumerate(rows_data):
            self.table_preview.insertRow(row_idx)
            sid_item = QTableWidgetItem(r["학번"])
            sid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            name_item = QTableWidgetItem(r["이름"])
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            res_raw = r["결과"].replace("\n", " ")
            preview_text = (res_raw[:90] + "...") if len(res_raw) > 90 else res_raw
            res_item = QTableWidgetItem(preview_text)
            res_item.setToolTip(r["결과"])

            status_text = r["상태"]
            status_item = QTableWidgetItem(status_text)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if status_text.startswith("✓"):
                status_item.setForeground(QBrush(QColor("#0F172A")))
            elif status_text.startswith("[!]"):
                status_item.setForeground(QBrush(QColor("#475569")))
            else:
                status_item.setForeground(QBrush(QColor("#64748B")))

            for col_idx, cell in enumerate((sid_item, name_item, res_item, status_item)):
                self.table_preview.setItem(row_idx, col_idx, cell)

    def _on_save_and_next_batch(self) -> None:
        if not self._refresh_verification_preview() or not self._parsed_items:
            QMessageBox.warning(
                self,
                "JSON 응답 확인",
                "웹 AI 응답에서 유효한 JSON 배열을 추출하지 못했습니다.\n"
                "응답 텍스트를 확인한 뒤 다시 시도해 주세요.",
            )
            return

        try:
            res = apply_batch_results_to_excel(
                excel_path=self.excel_path,
                output_path=self.output_path,
                parsed_items=self._parsed_items,
                is_grading=self.is_grading,
            )
        except Exception as exc:
            QMessageBox.critical(self, "엑셀 저장 오류", f"결과를 엑셀에 저장하는 중 오류가 발생했습니다:\n{exc}")
            return

        applied = res.get("applied_count", 0)
        unmatched = res.get("unmatched_ids", [])
        saved_path = res.get("output_path", str(self.output_path))
        self.output_path = Path(saved_path)
        self.last_saved_excel_path = saved_path
        self.total_saved_count += applied

        if applied == 0:
            QMessageBox.warning(
                self,
                "매칭된 학번 없음",
                "추출된 JSON의 학번 중 현재 엑셀과 일치하는 학생이 없습니다.\n"
                f"미매칭 학번: {', '.join(unmatched[:10])}",
            )
            return

        self.txt_llm_response.blockSignals(True)
        self.txt_llm_response.clear()
        self.txt_llm_response.blockSignals(False)

        next_prompt = self._rebuild_batches(copy_first=True)
        unmatched_note = f"\n- 미매칭 학번({len(unmatched)}건): {', '.join(unmatched[:8])}" if unmatched else ""

        if next_prompt is None:
            QMessageBox.information(
                self,
                "모든 배치 완료",
                f"이번 묶음 {applied}명 저장 완료! 모든 미완료 학생 처리가 완료되었습니다.{unmatched_note}\n\n"
                f"저장된 파일:\n{saved_path}",
            )
        else:
            remaining_batches = len(self._batches)
            self.lbl_verify_status.setText(
                f"✓ {applied}명 저장 완료 -> 다음 묶음 프롬프트를 클립보드에 자동 복사했습니다. (남은 묶음: {remaining_batches}개)"
            )

    # =========================================================================
    # Playwright 멀티 웹 AI 자동 실행 제어 및 실시간 갱신 핸들러
    # =========================================================================
    def _on_open_selected_web_ai(self) -> None:
        provider_key = str(self.combo_web_provider.currentData() or "gemini")
        url = PROVIDER_CONFIGS.get(provider_key, PROVIDER_CONFIGS["gemini"])["url"]
        webbrowser.open(url)

    def _set_auto_running_state(self, running: bool) -> None:
        has_batches = len(self._batches) > 0
        self.btn_auto_gemini.setEnabled((not running) and has_batches)
        self.btn_stop_auto_gemini.setEnabled(running)
        self.combo_web_provider.setEnabled(not running)
        self.chk_require_login.setEnabled(not running)
        self.btn_resplit.setEnabled(not running)
        self.spin_max_students.setEnabled(not running)
        self.spin_max_tokens.setEnabled(not running)
        self.combo_batch.setEnabled(not running)
        self.btn_save_and_next.setEnabled((not running) and has_batches)
        self.btn_paste_verify.setEnabled(not running)

    def _on_start_auto_gemini(self) -> None:
        if self._active_worker is not None:
            return
        if not self._batches:
            QMessageBox.information(self, "완료 안내", "처리할 미완료 학생 배치가 없습니다.")
            return

        selected_provider = str(self.combo_web_provider.currentData() or "gemini")
        require_login = bool(self.chk_require_login.isChecked())
        provider_name = PROVIDER_CONFIGS.get(selected_provider, PROVIDER_CONFIGS["gemini"])["name"]

        worker = WebGeminiAutoWorker(
            excel_path=self.excel_path,
            output_path=self.output_path,
            prompt_file=self.prompt_file,
            prompt_template=self._prompt_template,
            reference_dir=self.reference_dir,
            reference_context=self._reference_context,
            max_students=self.spin_max_students.value(),
            max_input_tokens=self.spin_max_tokens.value(),
            is_grading=self.is_grading,
            max_bytes=self.max_bytes,
            headless=False,
            provider=selected_provider,
            require_login=require_login,
        )
        worker.signals.progress.connect(self._on_auto_progress)
        worker.signals.log.connect(self._on_auto_log)
        worker.batch_completed.connect(self._on_auto_batch_completed)

        self._active_worker = worker
        self._set_auto_running_state(True)
        self.progress_auto.setValue(0)
        mode_str = "로그인 후 작업" if require_login else "비로그인 즉시 작업"
        self.lbl_auto_status.setText(
            f"▸ [{provider_name} · {mode_str}] Playwright 자동화 워커를 시작합니다..."
        )

        self._task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_AUTO_GEMINI,
            on_result=self._on_auto_result,
            on_error=self._on_auto_error,
            on_finished=self._on_auto_finished,
        )

    def _on_stop_auto_gemini(self) -> None:
        if self._active_worker is not None:
            self._active_worker.request_stop()
        self._task_runner.stop_task(self.TASK_AUTO_GEMINI)
        self.btn_stop_auto_gemini.setEnabled(False)
        self.lbl_auto_status.setText("[!] 자동 실행 중지 요청됨 (브라우저를 안전하게 닫는 중입니다...)")

    def _on_auto_progress(self, percent: int, message: str) -> None:
        self.progress_auto.setValue(percent)
        if message:
            self.lbl_auto_status.setText(f"▸ [{percent}%] {message}")

    def _on_auto_log(self, level: str, message: str) -> None:
        clean_msg = message
        if clean_msg.startswith("[Playwright 웹 ") and "] " in clean_msg:
            clean_msg = clean_msg.split("] ", 1)[1]
        if level == "ERROR":
            self.lbl_auto_status.setText(f"✕ {clean_msg}")
        elif level == "WARN":
            self.lbl_auto_status.setText(f"[!] {clean_msg}")
        elif level == "SUCCESS":
            self.lbl_auto_status.setText(f"✓ {clean_msg}")
        else:
            self.lbl_auto_status.setText(f"▪ {clean_msg}")

    def _on_auto_batch_completed(self, payload: Dict[str, Any]) -> None:
        applied = int(payload.get("applied_count", 0))
        out_path = str(payload.get("output_path", "") or "")
        if out_path:
            self.output_path = Path(out_path)
            self.last_saved_excel_path = out_path
        self.total_saved_count += applied

        # 방금 완료된 배치를 제외하고 상단 미완료 배치 목록 & 미리보기 테이블을 실시간 갱신
        self.txt_llm_response.blockSignals(True)
        self.txt_llm_response.clear()
        self.txt_llm_response.blockSignals(False)
        self._rebuild_batches(copy_first=False)

        b_idx = payload.get("batch_index", 0)
        tot = payload.get("total_batches", 0)
        self.lbl_verify_status.setText(
            f"✓ 자동 실행 Batch {b_idx}/{tot} 반영 완료 ({applied}명 저장 · 남은 미완료 배치: {len(self._batches)}개)"
        )

    def _on_auto_result(self, result: Dict[str, Any]) -> None:
        if result.get("missing_playwright"):
            QMessageBox.warning(
                self,
                "Playwright 설치 필요",
                "Playwright 패키지가 설치되어 있지 않습니다.\n\n"
                "터미널에서 아래 명령어를 실행한 뒤 다시 시도해 주세요:\n"
                "pip install playwright && playwright install chromium",
            )
            return

        comp = int(result.get("completed_batches", 0))
        tot = int(result.get("total_batches", 0))
        applied = int(result.get("applied_students", 0))
        out_path = str(result.get("output_path", self.output_path))
        if applied > 0:
            self.last_saved_excel_path = out_path

        self._rebuild_batches(copy_first=False)
        if comp > 0 and len(self._batches) == 0:
            QMessageBox.information(
                self,
                "Playwright 자동 실행 완료",
                f"모든 미완료 배치({comp}/{tot}개, 총 {applied}명) 처리가 완료되었습니다.\n\n"
                f"저장된 파일:\n{out_path}",
            )

    def _on_auto_error(self, err_msg: str) -> None:
        first_line = (err_msg or "").strip().split("\n")[0]
        self.lbl_auto_status.setText(f"✕ 자동 실행 오류: {first_line}")
        QMessageBox.critical(self, "Playwright 자동 실행 오류", first_line)

    def _on_auto_finished(self) -> None:
        self._active_worker = None
        self._set_auto_running_state(False)
        self._rebuild_batches(copy_first=False)

    def done(self, result: int) -> None:
        if self._active_worker is not None:
            self._active_worker.request_stop()
            self._task_runner.stop_task(self.TASK_AUTO_GEMINI)
        super().done(result)

    def closeEvent(self, event: Any) -> None:
        if self._active_worker is not None:
            self._active_worker.request_stop()
            self._task_runner.stop_task(self.TASK_AUTO_GEMINI)
        super().closeEvent(event)
