"""
우측 접이식 AI 실행 사이드바 컴포넌트 (ui/components/ai_execution_dock.py).
- Google Stitch 3단 워크스테이션 규격의 우측 실행 도크 (폭 340px, Slate Workstation 토큰 적용)
- 4단 수직 구조:
  1) 상단 Target Context 헤더 (ACTIVE AI TARGET, 대상 표시 박스, 수동 전환 드롭다운, 타겟 고정 토글)
  2) 3-모드 세그먼트 컨트롤 (API 모드 | 수동 복붙 | Playwright 자동, 고정 높이 32px)
  3) 모드별 실행 컨트롤 영역 (QStackedWidget + 스크롤)
  4) 하단 고정 상태 푸터 (ModernProgressBar, 배치 진행 텍스트, 결과 엑셀 열기)
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional
import webbrowser

import pandas as pd
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import BaseWorker, TaskRunner
from services.batch_prompt_service import (
    apply_batch_results_to_excel,
    build_student_batches,
    extract_batch_items_from_response,
    normalize_student_id_str,
    resolve_batch_output_path,
)
from services.llm_client import (
    build_structured_prompt,
    extract_json_from_llm_response,
    format_parsed_json_to_readable_text,
)
from services.record_generator_service import (
    get_active_ai_info,
    is_blank_or_failed,
    load_all_references,
    load_prompt_text,
)
from services.web_gemini_auto_worker import (
    PROVIDER_CONFIGS,
    WebGeminiAutoWorker,
    normalize_web_url,
)
from ui.utils import install_no_scroll_wheel_filter


def _apply_role(widget: QWidget, role: str = "", obj_name: str = "") -> None:
    """위젯에 objectName 및 role 속성을 부여하고 QSS를 재적용합니다."""
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


class _DockApiRecordWorker(BaseWorker):
    """우측 도크에서 2단계 세특/채점 또는 1~2단계 통합 API 파이프라인을 실행하는 백그라운드 워커."""

    def __init__(
        self,
        run_integrated: bool,
        excel_path: str,
        docx_dir: str,
        schema_path: Optional[Path],
        prompt_file: Optional[Path],
        reference_dir: Optional[Path],
        max_bytes: int,
        provider: str,
        model_name: str,
    ):
        title = "원클릭 AI 과제·세특 파이프라인" if run_integrated else "AI 과세특·채점 일괄 생성"
        super().__init__(task_name=title)
        self.run_integrated = run_integrated
        self.excel_path = Path(excel_path) if excel_path else None
        self.docx_dir = Path(docx_dir) if docx_dir else None
        self.schema_path = schema_path
        self.prompt_file = prompt_file
        self.reference_dir = reference_dir
        self.max_bytes = max_bytes
        self.provider = provider
        self.model_name = model_name

    def execute(self) -> Dict[str, Any]:
        from datetime import datetime
        from services.multi_doc_extractor_service import extract_documents_to_excel
        from services.record_generator_service import generate_records_from_excel

        if self.run_integrated:
            if self.docx_dir is None or not self.docx_dir.is_dir():
                raise ValueError("1단계 학생 과제 폴더를 먼저 선택해 주세요.")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            intermediate_excel = self.docx_dir / f"과제취합_{ts}.xlsx"
            self.emit_progress(5, "1단계: 학생 과제 문서(.docx/.hwpx/.hwp/.pdf) 통합 추출 중...")
            step1_res = extract_documents_to_excel(
                doc_dir=self.docx_dir,
                schema_path=self.schema_path,
                output_path=intermediate_excel,
                progress_callback=lambda pct, msg: self.emit_progress(int(pct * 0.4), f"1단계 추출: {msg}"),
                log_callback=lambda lvl, msg: self.emit_log(lvl, msg),
                stop_checker=self.is_stop_requested,
            )
            if self.is_stop_requested():
                return {"target_id": "agg_step2", "stopped": True, "output_path": str(intermediate_excel)}
            if step1_res.get("success", 0) == 0:
                raise ValueError("1단계 DOCX 추출 결과 유효한 학생 데이터가 없습니다.")

            final_output = self.docx_dir / f"최종과세특_{ts}.xlsx"
            step2_res = generate_records_from_excel(
                excel_path=Path(step1_res["output_file"]),
                output_path=final_output,
                prompt_file=self.prompt_file,
                reference_dir=self.reference_dir,
                max_bytes=self.max_bytes,
                provider=self.provider,
                model_name=self.model_name,
                progress_callback=lambda pct, msg: self.emit_progress(40 + int(pct * 0.6), f"2단계 AI 생성: {msg}"),
                log_callback=lambda lvl, msg: self.emit_log(lvl, msg),
                stop_checker=self.is_stop_requested,
            )
            self.emit_progress(100, "통합 파이프라인 완료")
            return {
                "target_id": "agg_step2",
                "mode": "api",
                "integrated": True,
                "intermediate_excel": step1_res.get("output_file", ""),
                "output_path": step2_res.get("output_path", ""),
                "completed": step2_res.get("completed", 0),
                "total": step2_res.get("total", 0),
                "step1": step1_res,
            }

        if self.excel_path is None or not self.excel_path.is_file():
            raise ValueError("대상 엑셀 파일(.xlsx)을 먼저 선택해 주세요.")

        res = generate_records_from_excel(
            excel_path=self.excel_path,
            prompt_file=self.prompt_file,
            reference_dir=self.reference_dir,
            max_bytes=self.max_bytes,
            provider=self.provider,
            model_name=self.model_name,
            progress_callback=lambda pct, msg: self.emit_progress(pct, msg),
            log_callback=lambda lvl, msg: self.emit_log(lvl, msg),
            stop_checker=self.is_stop_requested,
        )
        self.emit_progress(100, f"완료 ({res.get('completed', 0)}/{res.get('total', 0)}명)")
        return {
            "target_id": "agg_step2",
            "mode": "api",
            "integrated": False,
            "output_path": res.get("output_path", ""),
            "completed": res.get("completed", 0),
            "total": res.get("total", 0),
        }


class _DockApiOcrWorker(BaseWorker):
    """우측 도크에서 OCR 및 텍스트 보정 API 작업을 실행하는 백그라운드 워커."""

    def __init__(self, file_paths: List[str], prompt_guide: str):
        super().__init__(task_name="AI OCR 및 텍스트 보정")
        self.file_paths = list(file_paths)
        self.prompt_guide = prompt_guide.strip()

    def execute(self) -> Dict[str, Any]:
        import mimetypes
        from services.llm_client import MultiLlmClient
        from ui.tabs.tab_doc_ocr import extract_local_text_from_files

        llm_client = MultiLlmClient()
        total = len(self.file_paths)
        results: List[str] = []

        for idx, fp in enumerate(self.file_paths, start=1):
            if self.is_stop_requested():
                break
            path_obj = Path(fp)
            if not path_obj.exists():
                continue
            fname = path_obj.name
            self.emit_progress(int((idx / max(1, total)) * 100), f"AI 보정 중 ({idx}/{total}): {fname}")

            ext = path_obj.suffix.lower()
            local_text = extract_local_text_from_files([str(path_obj)])
            sys_prompt, user_prompt = build_structured_prompt(
                task_key="ocr_correction",
                raw_content=local_text or f"첨부 파일명: {fname}",
                custom_instruction=self.prompt_guide,
            )

            image_bytes: Optional[bytes] = None
            mime_type = "image/png"
            if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                image_bytes = path_obj.read_bytes()
                guessed, _ = mimetypes.guess_type(str(path_obj))
                mime_type = guessed or "image/png"
            elif ext == ".pdf" and llm_client.provider == "gemini":
                image_bytes = path_obj.read_bytes()
                mime_type = "application/pdf"

            try:
                raw_reply = llm_client.generate(
                    prompt=user_prompt,
                    system_prompt=sys_prompt,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                )
                try:
                    parsed = extract_json_from_llm_response(raw_reply)
                    formatted = format_parsed_json_to_readable_text(parsed)
                except Exception:
                    formatted = raw_reply.strip()
                results.append(
                    f"=== [{fname}] ({llm_client.provider.upper()} / {llm_client.model_name}) ===\n{formatted}\n"
                )
            except Exception as exc:
                results.append(f"=== [{fname}] API 처리 오류 ===\n{exc}\n")
                self.emit_log("ERROR", f"API 호출 실패 ({fname}): {exc}")

        self.emit_progress(100, "AI OCR 및 텍스트 보정 완료")
        return {
            "target_id": "ocr",
            "mode": "api",
            "count": len(results),
            "formatted_text": "\n".join(results),
        }


class _DockApiHwpxAgentWorker(BaseWorker):
    """HWPX AI 문서 에이전트용 API 단일 JSON Patch 생성 워커."""

    def __init__(self, dom_map: Dict[str, Any], instruction: str):
        super().__init__(task_name="HWPX AI 문서 에이전트 (API)")
        self.dom_map = dom_map
        self.instruction = instruction

    def execute(self) -> Dict[str, Any]:
        from services.hwpx_agent_service import build_hwpx_agent_prompt

        self.emit_progress(25, "HWPX DOM 맵 분석 및 AI 프롬프트 전송 중...")
        sys_p, usr_p = build_hwpx_agent_prompt(self.dom_map, self.instruction)
        client = MultiLlmClient()
        raw_reply = client.generate(prompt=usr_p, system_prompt=sys_p)
        parsed = extract_json_from_llm_response(raw_reply)
        self.emit_progress(100, "JSON Patch 생성 완료")
        return {
            "target_id": "hwpx_agent",
            "mode": "api",
            "parsed_json": parsed,
            "raw_response": raw_reply,
        }


class AiExecutionDock(QFrame):
    """EduFlow 우측 접이식 AI 실행 사이드바 위젯 (폭 340px)."""

    EXPANDED_WIDTH = 340
    TASK_DOCK_API = "ai_dock_api_exec"
    TASK_DOCK_PLAYWRIGHT = "ai_dock_playwright_exec"

    target_changed = pyqtSignal(str)
    go_to_settings_requested = pyqtSignal()
    execution_completed = pyqtSignal(str, dict)

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        _apply_role(self, obj_name="AiExecutionDock")
        self.task_runner = task_runner

        self._targets: Dict[str, Dict[str, Any]] = {}
        self._active_target_id: str = ""
        self._target_locked: bool = False
        self._last_context: Dict[str, Any] = {}
        self._last_result_excel_path: str = ""

        # 수동 복붙 상태
        self._manual_batches: List[Dict[str, Any]] = []
        self._manual_parsed_items: List[Dict[str, str]] = []
        self._manual_single_parsed: Dict[str, Any] = {}
        self._manual_provider: str = "gemini"
        self._manual_is_grading: bool = False

        # Playwright 상태
        self._pw_provider: str = "gemini"
        self._active_pw_worker: Optional[WebGeminiAutoWorker] = None
        self._is_api_running: bool = False

        # 미리보기 선택 인용 상태
        self._quoted_text: str = ""
        self._quoted_target_id: str = ""

        self.setMinimumWidth(0)
        self.setMaximumWidth(0)

        self._init_ui()
        self.refresh_ai_info()

    # =========================================================================
    # UI 구성 (4단 구조)
    # =========================================================================
    def _init_ui(self) -> None:
        outer_lay = QVBoxLayout(self)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        # 애니메이션 폭 축소(0 <-> 340px) 중 내부 버튼/텍스트가 줄바꿈되거나 뭉개지지 않도록
        # 외부 클리핑 스크롤 영역 내부에 340px 고정 폭 패널을 배치합니다.
        clip_scroll = QScrollArea(self)
        clip_scroll.setWidgetResizable(True)
        clip_scroll.setFrameShape(QFrame.Shape.NoFrame)
        clip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        clip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        clip_scroll.setMinimumWidth(0)

        panel = QFrame(clip_scroll)
        _apply_role(panel, obj_name="AiDockPanelRoot")
        panel.setFixedWidth(self.EXPANDED_WIDTH)

        root_lay = QVBoxLayout(panel)
        root_lay.setContentsMargins(10, 12, 10, 10)
        root_lay.setSpacing(10)

        # ---------------------------------------------------------------------
        # [1단] 상단 Target Context 헤더
        # ---------------------------------------------------------------------
        target_header = QFrame(panel)
        _apply_role(target_header, obj_name="AiDockTargetHeader")
        th_lay = QVBoxLayout(target_header)
        th_lay.setContentsMargins(10, 10, 10, 10)
        th_lay.setSpacing(7)

        th_top_row = QHBoxLayout()
        th_top_row.setSpacing(6)
        lbl_target_cap = QLabel("ACTIVE AI TARGET")
        _apply_role(lbl_target_cap, role="dockSectionLabel")

        self.btn_lock_target = QPushButton("타겟 고정 OFF")
        _apply_role(self.btn_lock_target, role="lockToggleBtn", obj_name="AiDockLockBtn")
        self.btn_lock_target.setCheckable(True)
        self.btn_lock_target.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_lock_target.setToolTip("켜면 중앙 카드를 클릭해도 활성 AI 타겟이 전환되지 않습니다.")
        self.btn_lock_target.toggled.connect(self._on_lock_toggled)

        th_top_row.addWidget(lbl_target_cap)
        th_top_row.addStretch()
        th_top_row.addWidget(self.btn_lock_target)
        th_lay.addLayout(th_top_row)

        self.lbl_active_target_box = QLabel("대상 미선택")
        self.lbl_active_target_box.setWordWrap(True)
        _apply_role(self.lbl_active_target_box, obj_name="ActiveTargetDisplayBox")
        th_lay.addWidget(self.lbl_active_target_box)

        self.combo_target_selector = QComboBox()
        _apply_role(self.combo_target_selector, obj_name="ComboAiDockTarget")
        self.combo_target_selector.setFixedHeight(30)
        self.combo_target_selector.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.combo_target_selector.currentIndexChanged.connect(self._on_target_combo_changed)
        th_lay.addWidget(self.combo_target_selector)

        root_lay.addWidget(target_header)

        # ---------------------------------------------------------------------
        # [2단] 3-모드 세그먼트 컨트롤 (QButtonGroup + 고정 높이 32px)
        # ---------------------------------------------------------------------
        seg_frame = QFrame(panel)
        _apply_role(seg_frame, obj_name="AiDockSegmentFrame")
        seg_lay = QHBoxLayout(seg_frame)
        seg_lay.setContentsMargins(3, 3, 3, 3)
        seg_lay.setSpacing(3)

        self.mode_btn_group = QButtonGroup(self)
        self.mode_btn_group.setExclusive(True)

        self.btn_mode_api = QPushButton("API 모드")
        self.btn_mode_manual = QPushButton("수동 복붙")
        self.btn_mode_playwright = QPushButton("Playwright 자동")

        for idx, btn in enumerate((self.btn_mode_api, self.btn_mode_manual, self.btn_mode_playwright)):
            btn.setCheckable(True)
            btn.setFixedHeight(32)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            _apply_role(btn, role="segmentBtn")
            self.mode_btn_group.addButton(btn, idx)
            seg_lay.addWidget(btn, stretch=1)

        self.btn_mode_manual.setChecked(True)
        self.mode_btn_group.idClicked.connect(self._on_mode_segment_clicked)
        root_lay.addWidget(seg_frame)

        # [2.3단] 미리보기 선택 인용 배지 (이모지 미사용)
        self.quote_badge_frame = QFrame(panel)
        _apply_role(self.quote_badge_frame, role="subCard", obj_name="AiDockQuoteBadge")
        qb_lay = QHBoxLayout(self.quote_badge_frame)
        qb_lay.setContentsMargins(10, 6, 8, 6)
        qb_lay.setSpacing(6)
        self.lbl_quote_summary = QLabel("")
        self.lbl_quote_summary.setWordWrap(True)
        _apply_role(self.lbl_quote_summary, role="statusBanner")
        self.btn_clear_quote = QPushButton("해제")
        _apply_role(self.btn_clear_quote, role="outline")
        self.btn_clear_quote.setFixedHeight(24)
        self.btn_clear_quote.setFixedWidth(48)
        self.btn_clear_quote.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_quote.clicked.connect(self.clear_quoted_selection)
        qb_lay.addWidget(self.lbl_quote_summary, stretch=1)
        qb_lay.addWidget(self.btn_clear_quote, 0, Qt.AlignmentFlag.AlignTop)
        self.quote_badge_frame.setVisible(False)
        root_lay.addWidget(self.quote_badge_frame)

        # [2.5단] HWPX 에이전트 전용 문서 수정 지시사항 입력창 (hwpx_agent 타겟일 때만 표시)
        self.hwpx_instruction_card = QFrame(panel)
        _apply_role(self.hwpx_instruction_card, role="subCard")
        hw_lay = QVBoxLayout(self.hwpx_instruction_card)
        hw_lay.setContentsMargins(10, 8, 10, 8)
        hw_lay.setSpacing(6)
        lbl_hw_inst = QLabel("문서 수정 지시사항 (HWPX AI 에이전트)")
        _apply_role(lbl_hw_inst, role="fieldLabel")
        self.txt_hwpx_instruction = QTextEdit()
        _apply_role(self.txt_hwpx_instruction, obj_name="WebAiInputBox")
        self.txt_hwpx_instruction.setMinimumHeight(74)
        self.txt_hwpx_instruction.setMaximumHeight(110)
        self.txt_hwpx_instruction.setPlaceholderText(
            "예: 2026년 일정으로 바꾸고 중화적정 시험문항 1개 추가해줘"
        )
        hw_lay.addWidget(lbl_hw_inst)
        hw_lay.addWidget(self.txt_hwpx_instruction)
        self.hwpx_instruction_card.setVisible(False)
        root_lay.addWidget(self.hwpx_instruction_card)

        # ---------------------------------------------------------------------
        # [3단] 모드별 실행 컨트롤 영역 (QStackedWidget in QScrollArea)
        # ---------------------------------------------------------------------
        body_scroll = QScrollArea(panel)
        body_scroll.setWidgetResizable(True)
        body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.mode_stack = QStackedWidget(body_scroll)
        _apply_role(self.mode_stack, obj_name="AiDockModeStack")

        self.mode_stack.addWidget(self._build_api_mode_page())
        self.mode_stack.addWidget(self._build_manual_mode_page())
        self.mode_stack.addWidget(self._build_playwright_mode_page())
        self.mode_stack.setCurrentIndex(1)

        body_scroll.setWidget(self.mode_stack)
        root_lay.addWidget(body_scroll, stretch=1)

        # ---------------------------------------------------------------------
        # [4단] 하단 고정 상태 푸터
        # ---------------------------------------------------------------------
        footer = QFrame(panel)
        _apply_role(footer, obj_name="AiDockFooter")
        ft_lay = QVBoxLayout(footer)
        ft_lay.setContentsMargins(10, 10, 10, 10)
        ft_lay.setSpacing(6)

        self.progress_dock = QProgressBar(footer)
        _apply_role(self.progress_dock, obj_name="ModernProgressBar")
        self.progress_dock.setRange(0, 100)
        self.progress_dock.setValue(0)
        self.progress_dock.setTextVisible(False)

        self.lbl_dock_progress_meta = QLabel("대기 중 · 준비 완료")
        self.lbl_dock_progress_meta.setWordWrap(True)
        _apply_role(self.lbl_dock_progress_meta, role="mutedText")

        self.btn_dock_open_excel = QPushButton("생성된 결과 엑셀 열기 (.xlsx)")
        _apply_role(self.btn_dock_open_excel, role="success", obj_name="BtnDockOpenResultExcel")
        self.btn_dock_open_excel.setFixedHeight(32)
        self.btn_dock_open_excel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dock_open_excel.setEnabled(False)
        self.btn_dock_open_excel.clicked.connect(self._on_open_result_excel)

        ft_lay.addWidget(self.progress_dock)
        ft_lay.addWidget(self.lbl_dock_progress_meta)
        ft_lay.addWidget(self.btn_dock_open_excel)
        root_lay.addWidget(footer)

        clip_scroll.setWidget(panel)
        outer_lay.addWidget(clip_scroll)

    # =========================================================================
    # Page 0: API 모드
    # =========================================================================
    def _build_api_mode_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(10)

        model_card = QFrame()
        _apply_role(model_card, role="subCard")
        mc_lay = QVBoxLayout(model_card)
        mc_lay.setContentsMargins(10, 10, 10, 10)
        mc_lay.setSpacing(6)

        lbl_cap = QLabel("연결 AI 엔진")
        _apply_role(lbl_cap, role="fieldLabel")
        self.lbl_api_model_badge = QLabel("확인 중...")
        self.lbl_api_model_badge.setWordWrap(True)
        _apply_role(self.lbl_api_model_badge, role="statusBanner")

        link_row = QHBoxLayout()
        link_row.setSpacing(6)
        self.btn_api_go_settings = QPushButton("환경설정 이동")
        _apply_role(self.btn_api_go_settings, role="secondary")
        self.btn_api_go_settings.setFixedHeight(28)
        self.btn_api_go_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_api_go_settings.clicked.connect(self.go_to_settings_requested.emit)

        self.btn_api_refresh = QPushButton("상태 갱신")
        _apply_role(self.btn_api_refresh, role="outline")
        self.btn_api_refresh.setFixedHeight(28)
        self.btn_api_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_api_refresh.clicked.connect(self.refresh_ai_info)

        link_row.addWidget(self.btn_api_go_settings, stretch=1)
        link_row.addWidget(self.btn_api_refresh)
        mc_lay.addWidget(lbl_cap)
        mc_lay.addWidget(self.lbl_api_model_badge)
        mc_lay.addLayout(link_row)
        lay.addWidget(model_card)

        summary_card = QFrame()
        _apply_role(summary_card, role="subCard")
        sc_lay = QVBoxLayout(summary_card)
        sc_lay.setContentsMargins(10, 10, 10, 10)
        sc_lay.setSpacing(6)

        lbl_sum_cap = QLabel("미완료 현황 요약")
        _apply_role(lbl_sum_cap, role="fieldLabel")
        self.lbl_api_summary = QLabel("대상 파일이나 문서를 선택하면 요약이 표시됩니다.")
        self.lbl_api_summary.setWordWrap(True)
        _apply_role(self.lbl_api_summary, role="mutedText")

        self.chk_api_run_integrated = QCheckBox("1단계 DOCX 추출부터 원클릭 통합 실행")
        self.chk_api_run_integrated.setChecked(False)

        sc_lay.addWidget(lbl_sum_cap)
        sc_lay.addWidget(self.lbl_api_summary)
        sc_lay.addWidget(self.chk_api_run_integrated)
        lay.addWidget(summary_card)

        self.btn_api_start = QPushButton("API 일괄 생성 시작")
        _apply_role(self.btn_api_start, role="primary", obj_name="BtnDockApiStart")
        self.btn_api_start.setFixedHeight(36)
        self.btn_api_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_api_start.clicked.connect(self._on_api_start_clicked)

        self.btn_api_stop = QPushButton("작업 중지")
        _apply_role(self.btn_api_stop, role="danger", obj_name="BtnDockApiStop")
        self.btn_api_stop.setFixedHeight(34)
        self.btn_api_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_api_stop.setEnabled(False)
        self.btn_api_stop.clicked.connect(self._on_api_stop_clicked)

        lay.addWidget(self.btn_api_start)
        lay.addWidget(self.btn_api_stop)
        lay.addStretch()
        return page

    # =========================================================================
    # Page 1: 수동 복붙 모드
    # =========================================================================
    def _build_manual_mode_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(8)

        # 배치 분할 설정 (엑셀 배치 타겟 전용)
        self.manual_batch_card = QFrame()
        _apply_role(self.manual_batch_card, role="subCard")
        mb_lay = QVBoxLayout(self.manual_batch_card)
        mb_lay.setContentsMargins(10, 8, 10, 8)
        mb_lay.setSpacing(6)

        split_row = QHBoxLayout()
        split_row.setSpacing(4)
        self.spin_manual_students = QSpinBox()
        self.spin_manual_students.setRange(1, 50)
        self.spin_manual_students.setValue(15)
        self.spin_manual_students.setSuffix("명")
        self.spin_manual_students.setFixedHeight(28)
        self.spin_manual_students.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        install_no_scroll_wheel_filter(self.spin_manual_students)

        self.spin_manual_tokens = QSpinBox()
        self.spin_manual_tokens.setRange(3000, 100000)
        self.spin_manual_tokens.setSingleStep(1000)
        self.spin_manual_tokens.setValue(15000)
        self.spin_manual_tokens.setSuffix("T")
        self.spin_manual_tokens.setFixedHeight(28)
        self.spin_manual_tokens.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        install_no_scroll_wheel_filter(self.spin_manual_tokens)

        self.btn_manual_resplit = QPushButton("분할")
        _apply_role(self.btn_manual_resplit, role="secondary")
        self.btn_manual_resplit.setFixedHeight(28)
        self.btn_manual_resplit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_resplit.clicked.connect(lambda: self._rebuild_manual_batches(copy_first=False))

        split_row.addWidget(self.spin_manual_students, stretch=1)
        split_row.addWidget(self.spin_manual_tokens, stretch=1)
        split_row.addWidget(self.btn_manual_resplit)

        lbl_batch_sel = QLabel("배치 선택")
        _apply_role(lbl_batch_sel, role="fieldLabel")
        self.combo_manual_batch = QComboBox()
        _apply_role(self.combo_manual_batch, obj_name="ComboDockManualBatch")
        self.combo_manual_batch.setFixedHeight(30)
        self.combo_manual_batch.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.combo_manual_batch.currentIndexChanged.connect(self._on_manual_batch_changed)

        self.lbl_manual_batch_meta = QLabel("대상 엑셀 파일을 선택하면 배치가 생성됩니다.")
        self.lbl_manual_batch_meta.setWordWrap(True)
        _apply_role(self.lbl_manual_batch_meta, role="mutedText")

        mb_lay.addLayout(split_row)
        mb_lay.addWidget(lbl_batch_sel)
        mb_lay.addWidget(self.combo_manual_batch)
        mb_lay.addWidget(self.lbl_manual_batch_meta)
        lay.addWidget(self.manual_batch_card)

        # OCR 타겟 요약 박스
        self.manual_ocr_card = QFrame()
        _apply_role(self.manual_ocr_card, role="subCard")
        moc_lay = QVBoxLayout(self.manual_ocr_card)
        moc_lay.setContentsMargins(10, 8, 10, 8)
        moc_lay.setSpacing(4)
        lbl_ocr_cap = QLabel("OCR 보정 대상")
        _apply_role(lbl_ocr_cap, role="fieldLabel")
        self.lbl_manual_ocr_meta = QLabel("파일 목록 또는 텍스트를 준비한 뒤 프롬프트를 복사하세요.")
        self.lbl_manual_ocr_meta.setWordWrap(True)
        _apply_role(self.lbl_manual_ocr_meta, role="mutedText")
        moc_lay.addWidget(lbl_ocr_cap)
        moc_lay.addWidget(self.lbl_manual_ocr_meta)
        self.manual_ocr_card.setVisible(False)
        lay.addWidget(self.manual_ocr_card)

        # 첨부파일 수동 업로드 안내 배너 (attachment_paths >= 1 일 때만 표시)
        self.manual_attachment_card = QFrame()
        self.manual_attachment_card.setObjectName("ManualAttachmentBanner")
        self.manual_attachment_card.setStyleSheet(
            "QFrame#ManualAttachmentBanner { "
            "background-color: #FEF3C7; border: 1px solid #F59E0B; border-radius: 6px; }"
        )
        att_lay = QVBoxLayout(self.manual_attachment_card)
        att_lay.setContentsMargins(10, 8, 10, 8)
        att_lay.setSpacing(6)

        self.lbl_manual_attachment_info = QLabel("")
        self.lbl_manual_attachment_info.setWordWrap(True)
        self.lbl_manual_attachment_info.setStyleSheet("color: #92400E; font-weight: 700; font-size: 12px;")

        self.lbl_manual_attachment_names = QLabel("")
        self.lbl_manual_attachment_names.setWordWrap(True)
        self.lbl_manual_attachment_names.setStyleSheet("color: #78350F; font-size: 11px;")

        self.btn_open_attachment_dir = QPushButton("첨부파일 위치 열기")
        _apply_role(self.btn_open_attachment_dir, role="secondary", obj_name="BtnOpenAttachmentDir")
        self.btn_open_attachment_dir.setFixedHeight(28)
        self.btn_open_attachment_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_attachment_dir.clicked.connect(self._on_open_attachment_dir)

        att_lay.addWidget(self.lbl_manual_attachment_info)
        att_lay.addWidget(self.lbl_manual_attachment_names)
        att_lay.addWidget(self.btn_open_attachment_dir)
        self.manual_attachment_card.setVisible(False)
        lay.addWidget(self.manual_attachment_card)

        # 대상 웹 AI 선택 칩 4종 + 커스텀 URL
        web_card = QFrame()
        _apply_role(web_card, role="subCard")
        wc_lay = QVBoxLayout(web_card)
        wc_lay.setContentsMargins(10, 8, 10, 8)
        wc_lay.setSpacing(6)

        lbl_chips = QLabel("대상 웹 AI 선택")
        _apply_role(lbl_chips, role="fieldLabel")
        wc_lay.addWidget(lbl_chips)

        (
            chip_lay,
            self.manual_chip_group,
            self.manual_provider_chips,
        ) = self._create_provider_chips(self._on_manual_provider_chip_clicked)
        wc_lay.addLayout(chip_lay)

        self.edit_manual_custom_url = QLineEdit()
        _apply_role(self.edit_manual_custom_url, obj_name="EditManualCustomUrl")
        self.edit_manual_custom_url.setFixedHeight(30)
        self.edit_manual_custom_url.setPlaceholderText("https://... 직접 URL 선택 시 웹 주소 입력")
        self.edit_manual_custom_url.setEnabled(False)
        self.edit_manual_custom_url.textChanged.connect(self._on_manual_custom_url_changed)
        wc_lay.addWidget(self.edit_manual_custom_url)

        copy_row = QHBoxLayout()
        copy_row.setSpacing(6)
        self.btn_manual_copy_open = QPushButton("프롬프트 복사 & 웹 열기")
        _apply_role(self.btn_manual_copy_open, role="primary", obj_name="BtnDockManualCopyOpen")
        self.btn_manual_copy_open.setFixedHeight(34)
        self.btn_manual_copy_open.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_copy_open.clicked.connect(self._on_manual_copy_and_open)

        self.btn_manual_copy_only = QPushButton("복사만")
        _apply_role(self.btn_manual_copy_only, role="outline")
        self.btn_manual_copy_only.setFixedHeight(34)
        self.btn_manual_copy_only.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_copy_only.clicked.connect(self._on_manual_copy_only)

        copy_row.addWidget(self.btn_manual_copy_open, stretch=1)
        copy_row.addWidget(self.btn_manual_copy_only)
        wc_lay.addLayout(copy_row)
        lay.addWidget(web_card)

        # 웹 AI 응답 JSON 붙여넣기
        resp_card = QFrame()
        _apply_role(resp_card, role="subCard")
        rc_lay = QVBoxLayout(resp_card)
        rc_lay.setContentsMargins(10, 8, 10, 8)
        rc_lay.setSpacing(6)

        resp_hdr = QHBoxLayout()
        lbl_resp_cap = QLabel("웹 AI 응답 JSON 붙여넣기")
        _apply_role(lbl_resp_cap, role="fieldLabel")
        self.btn_manual_paste_clip = QPushButton("붙여넣기")
        _apply_role(self.btn_manual_paste_clip, role="outline")
        self.btn_manual_paste_clip.setFixedHeight(24)
        self.btn_manual_paste_clip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_paste_clip.clicked.connect(self._on_manual_paste_from_clipboard)
        resp_hdr.addWidget(lbl_resp_cap)
        resp_hdr.addStretch()
        resp_hdr.addWidget(self.btn_manual_paste_clip)
        rc_lay.addLayout(resp_hdr)

        self.txt_manual_response = QTextEdit()
        _apply_role(self.txt_manual_response, obj_name="WebAiInputBox")
        self.txt_manual_response.setMinimumHeight(100)
        self.txt_manual_response.setMaximumHeight(130)
        self.txt_manual_response.setPlaceholderText(
            "웹 AI에서 복사한 ```json ... ``` 응답을 붙여넣으세요 (Ctrl+V)."
        )
        self.txt_manual_response.textChanged.connect(self._on_manual_response_changed)
        rc_lay.addWidget(self.txt_manual_response)

        self.lbl_manual_verify = QLabel("대기 중: JSON 응답을 붙여넣으면 자동 검증됩니다.")
        self.lbl_manual_verify.setWordWrap(True)
        _apply_role(self.lbl_manual_verify, role="mutedText")
        rc_lay.addWidget(self.lbl_manual_verify)

        self.btn_manual_apply_next = QPushButton("검증 후 엑셀 반영 · 다음 묶음 복사")
        _apply_role(self.btn_manual_apply_next, role="primary", obj_name="BtnDockManualApplyNext")
        self.btn_manual_apply_next.setFixedHeight(36)
        self.btn_manual_apply_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_apply_next.clicked.connect(self._on_manual_apply_and_next)
        rc_lay.addWidget(self.btn_manual_apply_next)

        lay.addWidget(resp_card)
        lay.addStretch()
        return page

    # =========================================================================
    # Page 2: Playwright 자동 / 반자동 모드
    # =========================================================================
    def _build_playwright_mode_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(8)

        web_card = QFrame()
        _apply_role(web_card, role="subCard")
        wc_lay = QVBoxLayout(web_card)
        wc_lay.setContentsMargins(10, 8, 10, 8)
        wc_lay.setSpacing(6)

        lbl_chips = QLabel("대상 웹 AI 선택")
        _apply_role(lbl_chips, role="fieldLabel")
        wc_lay.addWidget(lbl_chips)

        (
            chip_lay,
            self.pw_chip_group,
            self.pw_provider_chips,
        ) = self._create_provider_chips(self._on_pw_provider_chip_clicked)
        wc_lay.addLayout(chip_lay)

        self.edit_pw_custom_url = QLineEdit()
        _apply_role(self.edit_pw_custom_url, obj_name="EditPlaywrightCustomUrl")
        self.edit_pw_custom_url.setFixedHeight(30)
        self.edit_pw_custom_url.setPlaceholderText("https://... 직접 URL 선택 시 접속 주소 입력")
        self.edit_pw_custom_url.setEnabled(False)
        wc_lay.addWidget(self.edit_pw_custom_url)
        lay.addWidget(web_card)

        opt_card = QFrame()
        _apply_role(opt_card, role="subCard")
        oc_lay = QVBoxLayout(opt_card)
        oc_lay.setContentsMargins(10, 8, 10, 8)
        oc_lay.setSpacing(6)

        lbl_exec_mode = QLabel("실행 방식")
        _apply_role(lbl_exec_mode, role="fieldLabel")
        self.combo_pw_exec_mode = QComboBox()
        _apply_role(self.combo_pw_exec_mode, obj_name="ComboPlaywrightExecMode")
        self.combo_pw_exec_mode.setFixedHeight(30)
        self.combo_pw_exec_mode.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.combo_pw_exec_mode.addItem("반자동: 프롬프트 자동 입력 · 수동 전송", "semi_auto")
        self.combo_pw_exec_mode.addItem("완전 자동: 배치 무인 반복", "auto")

        self.chk_pw_keep_login = QCheckBox("로그인 세션 유지")
        self.chk_pw_keep_login.setChecked(True)
        self.chk_pw_temp_chat = QCheckBox("임시 채팅으로 실행")
        self.chk_pw_temp_chat.setChecked(True)

        self.lbl_pw_summary = QLabel("미완료 배치 준비 상태가 여기에 표시됩니다.")
        self.lbl_pw_summary.setWordWrap(True)
        _apply_role(self.lbl_pw_summary, role="mutedText")

        oc_lay.addWidget(lbl_exec_mode)
        oc_lay.addWidget(self.combo_pw_exec_mode)
        oc_lay.addWidget(self.chk_pw_keep_login)
        oc_lay.addWidget(self.chk_pw_temp_chat)
        oc_lay.addWidget(self.lbl_pw_summary)
        lay.addWidget(opt_card)

        self.btn_pw_start = QPushButton("브라우저 열기 & 프롬프트 입력")
        _apply_role(self.btn_pw_start, role="primary", obj_name="BtnDockPwStart")
        self.btn_pw_start.setFixedHeight(36)
        self.btn_pw_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pw_start.clicked.connect(self._on_pw_start_clicked)

        self.btn_pw_collect_next = QPushButton("응답 회수 · 다음 배치")
        _apply_role(self.btn_pw_collect_next, role="secondary", obj_name="BtnDockPwCollectNext")
        self.btn_pw_collect_next.setFixedHeight(34)
        self.btn_pw_collect_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pw_collect_next.setEnabled(False)
        self.btn_pw_collect_next.clicked.connect(self._on_pw_collect_next_clicked)

        self.btn_pw_confirm_login = QPushButton("인증/로그인 완료 · 작업 재개")
        _apply_role(self.btn_pw_confirm_login, role="teal", obj_name="BtnDockPwConfirmLogin")
        self.btn_pw_confirm_login.setFixedHeight(34)
        self.btn_pw_confirm_login.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pw_confirm_login.setEnabled(False)
        self.btn_pw_confirm_login.clicked.connect(self._on_pw_confirm_login_clicked)

        self.btn_pw_stop = QPushButton("작업 중지")
        _apply_role(self.btn_pw_stop, role="danger", obj_name="BtnDockPwStop")
        self.btn_pw_stop.setFixedHeight(34)
        self.btn_pw_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pw_stop.setEnabled(False)
        self.btn_pw_stop.clicked.connect(self._on_pw_stop_clicked)

        lay.addWidget(self.btn_pw_start)
        lay.addWidget(self.btn_pw_collect_next)
        lay.addWidget(self.btn_pw_confirm_login)
        lay.addWidget(self.btn_pw_stop)
        lay.addStretch()
        return page

    def _create_provider_chips(
        self, callback: Callable[[str], None]
    ) -> tuple[QHBoxLayout, QButtonGroup, Dict[str, QPushButton]]:
        row = QHBoxLayout()
        row.setSpacing(4)
        group = QButtonGroup(self)
        group.setExclusive(True)
        chips: Dict[str, QPushButton] = {}
        defs = [
            ("gemini", "Gemini"),
            ("chatgpt", "ChatGPT"),
            ("claude", "Claude"),
            ("custom", "직접 URL"),
        ]
        for key, label in defs:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            _apply_role(btn, role="webAiChip")
            btn.clicked.connect(lambda _chk=False, k=key: callback(k))
            group.addButton(btn)
            chips[key] = btn
            row.addWidget(btn, stretch=1)
        chips["gemini"].setChecked(True)
        return row, group, chips

    # =========================================================================
    # 타겟 등록 및 실시간 컨텍스트 동기화
    # =========================================================================
    def register_target(
        self,
        target_id: str,
        label: str,
        context_getter: Callable[[], Dict[str, Any]],
        on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._targets[target_id] = {
            "label": label,
            "context_getter": context_getter,
            "on_result": on_result,
        }
        self.combo_target_selector.blockSignals(True)
        existing_idx = self.combo_target_selector.findData(target_id)
        if existing_idx >= 0:
            self.combo_target_selector.setItemText(existing_idx, label)
        else:
            self.combo_target_selector.addItem(label, target_id)
        self.combo_target_selector.blockSignals(False)

        if not self._active_target_id:
            self.set_target(target_id, force=True)

    def is_target_locked(self) -> bool:
        return self._target_locked

    def active_target_id(self) -> str:
        return self._active_target_id

    def set_mode_index(self, index: int) -> None:
        if 0 <= index < self.mode_stack.count():
            self.mode_stack.setCurrentIndex(index)
            btn = self.mode_btn_group.button(index)
            if btn is not None:
                btn.setChecked(True)

    def reset_target_state(self) -> None:
        """타겟 전환 또는 새 문서 열기 시 이전 타겟의 응답창·첨부파일·배치·결과 캐시를 즉시 초기화합니다."""
        self._last_context = {}
        self._last_result_excel_path = ""
        self._manual_batches = []
        self._manual_parsed_items = []
        self._manual_single_parsed = {}
        if hasattr(self, "btn_dock_open_excel"):
            self.btn_dock_open_excel.setEnabled(False)
        if hasattr(self, "txt_manual_response"):
            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.clear()
            self.txt_manual_response.blockSignals(False)
        if hasattr(self, "combo_manual_batch"):
            self.combo_manual_batch.blockSignals(True)
            self.combo_manual_batch.clear()
            self.combo_manual_batch.blockSignals(False)
        if hasattr(self, "manual_attachment_card"):
            self.manual_attachment_card.setVisible(False)
        if hasattr(self, "lbl_manual_attachment_names"):
            self.lbl_manual_attachment_names.setText("")
        if hasattr(self, "lbl_manual_verify"):
            self.lbl_manual_verify.setText("대기 중: JSON 응답을 붙여넣으면 자동 검증됩니다.")
        self.clear_quoted_selection()

    def set_quoted_selection(self, text: str, target_id: str = "") -> None:
        """미리보기에서 드래그로 선택된 인용 텍스트와 위치 ID를 사이드바 배지에 표시합니다."""
        cleaned = (text or "").strip()
        if len(cleaned) < 2:
            self.clear_quoted_selection()
            return
        self._quoted_text = cleaned
        self._quoted_target_id = (target_id or "").strip()
        single_line = re.sub(r"\s+", " ", cleaned).strip()
        preview = single_line[:20] + ("..." if len(single_line) > 20 else "")
        loc_tag = f"[{self._quoted_target_id}] " if self._quoted_target_id else ""
        if hasattr(self, "lbl_quote_summary"):
            self.lbl_quote_summary.setText(f'인용 {loc_tag}"{preview}" ({len(cleaned)}자)')
            self.lbl_quote_summary.setToolTip(cleaned)
        if hasattr(self, "quote_badge_frame"):
            self.quote_badge_frame.setVisible(True)

    def clear_quoted_selection(self) -> None:
        """현재 선택된 인용 상태를 해제합니다."""
        self._quoted_text = ""
        self._quoted_target_id = ""
        if hasattr(self, "lbl_quote_summary"):
            self.lbl_quote_summary.setText("")
            self.lbl_quote_summary.setToolTip("")
        if hasattr(self, "quote_badge_frame"):
            self.quote_badge_frame.setVisible(False)

    def _append_quote_to_prompt(self, prompt_text: str) -> str:
        """선택된 인용 텍스트가 있으면 프롬프트 끝에 표준 인용 블록을 자동 첨부합니다."""
        if not self._quoted_text:
            return prompt_text
        loc_str = self._quoted_target_id or "미지정"
        quote_block = (
            "\n\n[선택된 인용 영역]\n"
            f"- 위치 ID: {loc_str}\n"
            "- 선택 원문:\n"
            '"""\n'
            f"{self._quoted_text}\n"
            '"""'
        )
        return f"{prompt_text}{quote_block}"

    def set_target(self, target_id: str, force: bool = False) -> bool:
        """활성 AI 타겟을 전환합니다. 잠금 상태에서는 force=True일 때만 전환됩니다."""
        if target_id not in self._targets:
            return False
        if self._target_locked and not force:
            return False
        if self._active_target_id == target_id and not force:
            return False

        if self._active_target_id != target_id:
            self.reset_target_state()

        self._active_target_id = target_id
        meta = self._targets[target_id]
        self.lbl_active_target_box.setText(meta["label"])

        self.combo_target_selector.blockSignals(True)
        idx = self.combo_target_selector.findData(target_id)
        if idx >= 0:
            self.combo_target_selector.setCurrentIndex(idx)
        self.combo_target_selector.blockSignals(False)

        ctx = self._fetch_fresh_context(target_id)
        self.sync_from_context(ctx)
        self.target_changed.emit(target_id)
        return True

    def on_central_target_clicked(self, target_id: str) -> None:
        """중앙 작업 카드 클릭/포커스 시 타겟을 전환합니다(잠금 OFF일 때만)."""
        if self._target_locked:
            return
        self.set_target(target_id, force=False)

    def _on_lock_toggled(self, checked: bool) -> None:
        self._target_locked = bool(checked)
        self.btn_lock_target.setText("타겟 고정 ON" if checked else "타겟 고정 OFF")

    def _on_target_combo_changed(self, _index: int) -> None:
        tid = str(self.combo_target_selector.currentData() or "").strip()
        if tid:
            self.set_target(tid, force=True)

    def _on_mode_segment_clicked(self, mode_idx: int) -> None:
        self.mode_stack.setCurrentIndex(mode_idx)
        self.sync_from_context(self._fetch_fresh_context())

    def _fetch_fresh_context(self, target_id: Optional[str] = None) -> Dict[str, Any]:
        tid = target_id or self._active_target_id
        meta = self._targets.get(tid)
        if meta is None:
            return {}
        try:
            ctx = meta["context_getter"]()
            if isinstance(ctx, dict):
                return ctx
        except Exception:
            pass
        return {}

    def sync_from_context(self, ctx: Optional[Dict[str, Any]] = None) -> None:
        """중앙 입력창 변경(300ms 디바운스) 또는 타겟 전환 시 우측 사이드바 배치 요약을 재계산합니다."""
        if not ctx:
            ctx = self._fetch_fresh_context()
        if not ctx:
            return
        tid = str(ctx.get("target_id", "") or "")
        if self._active_target_id and tid and tid != self._active_target_id:
            return

        prev_ctx = self._last_context or {}
        prev_tid = str(prev_ctx.get("target_id", "") or "")
        kind = str(ctx.get("kind", "excel_batch"))
        if prev_tid and tid and prev_tid != tid:
            self.reset_target_state()
        elif kind == "hwpx_agent" and prev_ctx.get("kind") == "hwpx_agent":
            prev_doc_id = prev_ctx.get("doc_instance_id", 0)
            new_doc_id = ctx.get("doc_instance_id", 0)
            prev_hwpx_path = str(prev_ctx.get("hwpx_path", "") or "")
            new_hwpx_path = str(ctx.get("hwpx_path", "") or "")
            if (prev_doc_id and new_doc_id and prev_doc_id != new_doc_id) or (
                prev_hwpx_path and new_hwpx_path and prev_hwpx_path != new_hwpx_path
            ):
                self.reset_target_state()

        self._last_context = dict(ctx)
        is_excel = kind == "excel_batch"
        is_hwpx = kind == "hwpx_agent"

        self.hwpx_instruction_card.setVisible(is_hwpx)
        self.chk_api_run_integrated.setVisible(is_excel)
        self.manual_batch_card.setVisible(is_excel)
        self.manual_ocr_card.setVisible((not is_excel) and (not is_hwpx))
        if is_hwpx:
            self.btn_manual_apply_next.setText("JSON Patch 검증 후 문서에 적용")
        else:
            self.btn_manual_apply_next.setText(
                "검증 후 엑셀 반영 · 다음 묶음 복사" if is_excel else "검증 후 OCR 결과 반영"
            )

        # 첨부파일 목록 추출 및 수동 복붙 안내 배너 토글 (hwpx_agent는 ocr_files와 엄격 격리)
        if is_hwpx:
            attachment_paths: List[str] = []
        else:
            raw_atts = ctx.get("attachment_paths")
            if raw_atts is None and kind == "ocr":
                raw_atts = ctx.get("ocr_files") or []
            attachment_paths = [str(p).strip() for p in (raw_atts or []) if str(p).strip()]
        has_atts = len(attachment_paths) > 0
        self.manual_attachment_card.setVisible(has_atts)
        if has_atts:
            self.lbl_manual_attachment_info.setText(
                f"첨부파일 {len(attachment_paths)}개 포함: 보안상 웹 브라우저에 파일이 자동 첨부되지 않습니다. "
                "웹 창이 열리면 아래 폴더에서 파일을 직접 드래그해 첨부해 주세요."
            )
            names_preview = ", ".join(Path(p).name for p in attachment_paths[:3])
            if len(attachment_paths) > 3:
                names_preview += f" 외 {len(attachment_paths) - 3}건"
            self.lbl_manual_attachment_names.setText(f"대상 파일: {names_preview}")

        if is_hwpx:
            dom_map = ctx.get("dom_map")
            hwpx_path = str(ctx.get("hwpx_path", "") or "").strip()
            if isinstance(dom_map, dict):
                blk_cnt = dom_map.get("block_count", len(dom_map.get("blocks", [])))
                fname = Path(hwpx_path).name if hwpx_path else dom_map.get("file_name", "HWPX")
                msg = f"HWPX 로드됨: {fname} (블록 {blk_cnt}개)"
            else:
                msg = "중앙 [4. HWPX AI 문서 에이전트] 탭에서 .hwpx 파일을 먼저 열어주세요."
            self.lbl_api_summary.setText(msg)
            self.lbl_pw_summary.setText(msg)
            self.lbl_dock_progress_meta.setText(msg)
        elif is_excel:
            last_res = str(ctx.get("last_result_excel_path", "") or "").strip()
            if last_res and Path(last_res).exists():
                self._set_result_excel_path(last_res)
            self._rebuild_manual_batches(copy_first=False, ctx=ctx, quiet=True)
            if has_atts:
                cur_pw = self.lbl_pw_summary.text().strip()
                att_note = f"첨부 예정 파일 {len(attachment_paths)}개 — 자동 업로드됨"
                self.lbl_pw_summary.setText(f"{cur_pw}\n{att_note}" if cur_pw else att_note)
        else:
            files = ctx.get("ocr_files") or []
            cur_text = str(ctx.get("ocr_current_text", "") or "").strip()
            if files:
                msg = f"등록된 문서 {len(files)}개 ({Path(files[0]).name}" + (
                    f" 외 {len(files) - 1}건)" if len(files) > 1 else ")"
                )
            elif cur_text:
                msg = f"현재 텍스트 {len(cur_text):,}자 입력됨"
            else:
                msg = "중앙 탭에서 문서 파일을 추가하거나 텍스트를 입력하세요."
            self.lbl_api_summary.setText(msg)
            self.lbl_manual_ocr_meta.setText(msg)
            pw_msg = msg
            if has_atts:
                pw_msg += f"\n첨부 예정 파일 {len(attachment_paths)}개 — 자동 업로드됨"
            self.lbl_pw_summary.setText(pw_msg)
            self.lbl_dock_progress_meta.setText(f"OCR 대상 · {len(files)}개 파일")

    def refresh_ai_info(self) -> None:
        """현재 ConfigManager의 API AI 모델 정보를 우측 도크 배지에 반영합니다."""
        info = get_active_ai_info()
        p_name = info.get("provider_display", "Gemini")
        model = info.get("model", "")
        responded = info.get("responded_model", "")
        verified = info.get("connection_verified", False)
        has_key = info.get("has_key", False)
        model_disp = f"{model} -> {responded}" if (verified and responded and responded != model) else model
        if has_key:
            tag = "연결 확인됨" if verified else "키 등록됨"
            self.lbl_api_model_badge.setText(f"{p_name} · {model_disp} ({tag})")
        else:
            self.lbl_api_model_badge.setText(f"{p_name} · {model_disp} (API Key 미등록)")

    # =========================================================================
    # API 모드 실행
    # =========================================================================
    def _set_api_busy(self, busy: bool) -> None:
        self._is_api_running = busy
        self.btn_api_start.setEnabled(not busy)
        self.btn_api_stop.setEnabled(busy)

    def _on_api_start_clicked(self) -> None:
        if self._is_api_running:
            return
        ctx = self._fetch_fresh_context()
        if not ctx:
            return

        info = get_active_ai_info()
        if not info.get("has_key", False):
            reply = QMessageBox.warning(
                self,
                "API Key 필요",
                f"현재 선택된 AI 공급자({info.get('provider_display')})의 API Key가 등록되어 있지 않습니다.\n"
                "환경설정 탭으로 이동하여 API Key를 등록하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.go_to_settings_requested.emit()
            return

        kind = ctx.get("kind", "excel_batch")
        if kind == "hwpx_agent":
            dom_map = ctx.get("dom_map")
            inst = self.txt_hwpx_instruction.toPlainText().strip()
            if not isinstance(dom_map, dict):
                QMessageBox.information(
                    self, "문서 확인", "중앙 [4. HWPX AI 문서 에이전트] 탭에서 .hwpx 파일을 먼저 열어주세요."
                )
                return
            if not inst:
                QMessageBox.information(
                    self, "지시사항 확인", "상단 입력창에 문서 수정 지시사항을 입력해 주세요."
                )
                self.txt_hwpx_instruction.setFocus()
                return
            worker = _DockApiHwpxAgentWorker(
                dom_map=dom_map,
                instruction=self._append_quote_to_prompt(inst),
            )
        elif kind == "ocr":
            files = ctx.get("ocr_files") or []
            if not files:
                QMessageBox.information(
                    self,
                    "파일 확인",
                    "API 보정을 실행할 문서/이미지 파일을 중앙 탭에서 먼저 추가해 주세요.",
                )
                return
            worker: BaseWorker = _DockApiOcrWorker(
                file_paths=files,
                prompt_guide=self._append_quote_to_prompt(str(ctx.get("ocr_prompt", ""))),
            )
        else:
            run_integrated = self.chk_api_run_integrated.isChecked()
            excel_path = str(ctx.get("excel_path", "") or "").strip()
            docx_dir = str(ctx.get("docx_dir", "") or "").strip()
            if run_integrated:
                if not docx_dir or not Path(docx_dir).is_dir():
                    QMessageBox.warning(self, "폴더 확인", "1단계 학생 DOCX 폴더를 먼저 선택해 주세요.")
                    return
            elif not excel_path or not Path(excel_path).is_file():
                QMessageBox.warning(self, "파일 확인", "대상 엑셀(.xlsx) 파일을 먼저 선택해 주세요.")
                return

            schema_str = str(ctx.get("schema_path", "") or "").strip()
            prompt_str = str(ctx.get("prompt_file", "") or "").strip()
            ref_str = str(ctx.get("reference_dir", "") or "").strip()
            worker = _DockApiRecordWorker(
                run_integrated=run_integrated,
                excel_path=excel_path,
                docx_dir=docx_dir,
                schema_path=Path(schema_str) if schema_str else None,
                prompt_file=Path(prompt_str) if prompt_str else None,
                reference_dir=Path(ref_str) if ref_str and Path(ref_str).is_dir() else None,
                max_bytes=int(ctx.get("max_bytes", 1500)),
                provider=str(info.get("provider", "gemini")),
                model_name=str(info.get("model", "")),
            )

        worker.signals.progress.connect(self._on_dock_worker_progress)
        self._set_api_busy(True)
        self.progress_dock.setValue(0)
        self.lbl_dock_progress_meta.setText("API 작업 시작 중...")

        target_id = str(ctx.get("target_id", self._active_target_id))

        def _on_res(res: Dict[str, Any]) -> None:
            out_path = str(res.get("output_path", "") or "")
            if out_path:
                self._set_result_excel_path(out_path)
            self._dispatch_result(target_id, res)
            self.sync_from_context(self._fetch_fresh_context(target_id))
            if kind == "ocr":
                self.lbl_dock_progress_meta.setText(f"API OCR 완료 ({res.get('count', 0)}개 파일)")
            else:
                self.lbl_dock_progress_meta.setText(
                    f"API 완료 · {res.get('completed', 0)}/{res.get('total', 0)}명"
                )

        def _on_err(err: str) -> None:
            first = (err or "").strip().split("\n")[0]
            self.lbl_dock_progress_meta.setText(f"오류: {first}")
            QMessageBox.critical(self, "API 실행 오류", first)

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_DOCK_API,
            on_result=_on_res,
            on_error=_on_err,
            on_finished=lambda: self._set_api_busy(False),
        )

    def _on_api_stop_clicked(self) -> None:
        self.btn_api_stop.setEnabled(False)
        self.task_runner.stop_task(self.TASK_DOCK_API)
        self.lbl_dock_progress_meta.setText("작업 중단 요청됨 (안전 종료 대기 중)...")

    # =========================================================================
    # 수동 복붙 모드
    # =========================================================================
    def _on_manual_provider_chip_clicked(self, key: str) -> None:
        self._manual_provider = key
        is_custom = key == "custom"
        self.edit_manual_custom_url.setEnabled(is_custom)
        if is_custom:
            self.edit_manual_custom_url.setFocus()

    def _on_manual_custom_url_changed(self, text: str) -> None:
        if text.strip() and not self.manual_provider_chips["custom"].isChecked():
            self.manual_provider_chips["custom"].setChecked(True)
            self._manual_provider = "custom"
            self.edit_manual_custom_url.setEnabled(True)

    def _resolve_target_web_url(self, provider: str, custom_url_edit: QLineEdit) -> str:
        if provider == "custom":
            return normalize_web_url(custom_url_edit.text())
        return PROVIDER_CONFIGS.get(provider, PROVIDER_CONFIGS["gemini"])["url"]

    def _rebuild_manual_batches(
        self,
        copy_first: bool = False,
        ctx: Optional[Dict[str, Any]] = None,
        quiet: bool = False,
    ) -> Optional[str]:
        if ctx is None:
            ctx = self._fetch_fresh_context("agg_step2")
        if not ctx or ctx.get("kind") != "excel_batch":
            return None

        excel_str = str(ctx.get("excel_path", "") or "").strip()
        if not excel_str or not Path(excel_str).is_file():
            self._manual_batches = []
            self.combo_manual_batch.blockSignals(True)
            self.combo_manual_batch.clear()
            self.combo_manual_batch.blockSignals(False)
            self.lbl_manual_batch_meta.setText("대상 엑셀 파일(.xlsx)을 선택하면 배치가 계산됩니다.")
            self.lbl_api_summary.setText("대상 엑셀 파일(.xlsx)이 선택되지 않았습니다.")
            self.lbl_pw_summary.setText("대상 엑셀 파일(.xlsx)이 선택되지 않았습니다.")
            self.lbl_dock_progress_meta.setText("대기 중 · 대상 엑셀 없음")
            return None

        prompt_str = str(ctx.get("prompt_file", "") or "").strip()
        prompt_path = Path(prompt_str) if prompt_str else None
        ref_str = str(ctx.get("reference_dir", "") or "").strip()
        ref_dir = Path(ref_str) if ref_str and Path(ref_str).is_dir() else None
        max_bytes = int(ctx.get("max_bytes", 1500))

        self._manual_is_grading = bool(prompt_path is not None and "채점" in prompt_path.name)
        out_path = resolve_batch_output_path(
            excel_str, None, is_grading=self._manual_is_grading
        )
        target_excel = out_path if out_path.exists() else Path(excel_str)

        try:
            df = pd.read_excel(target_excel)
        except Exception as exc:
            if not quiet:
                QMessageBox.critical(self, "엑셀 읽기 오류", f"엑셀 파일을 읽는 중 오류가 발생했습니다:\n{exc}")
            self.lbl_manual_batch_meta.setText(f"엑셀 읽기 오류: {exc}")
            return None

        prompt_template = load_prompt_text(prompt_path)
        ref_context = load_all_references(ref_dir) if ref_dir else ""

        self._manual_batches = build_student_batches(
            df=df,
            prompt_template=prompt_template,
            reference_context=ref_context,
            max_students=self.spin_manual_students.value(),
            max_input_tokens=self.spin_manual_tokens.value(),
            is_grading=self._manual_is_grading,
            max_bytes=max_bytes,
        )

        out_col = "AI_채점결과" if self._manual_is_grading else "생성된_과세특"
        total_students = len(df)
        done_students = (
            sum(1 for v in df[out_col] if not is_blank_or_failed(v))
            if out_col in df.columns
            else 0
        )
        pending_students = max(0, total_students - done_students)

        summary_txt = (
            f"파일: {target_excel.name}\n"
            f"전체 {total_students}명 · 완료 {done_students}명 · 미완료 {pending_students}명 "
            f"({len(self._manual_batches)}개 배치)"
        )
        self.lbl_api_summary.setText(summary_txt)
        self.lbl_pw_summary.setText(summary_txt)

        pct = int(round((done_students / total_students) * 100)) if total_students > 0 else 0
        if not self._is_api_running and self._active_pw_worker is None:
            self.progress_dock.setValue(pct)
            self.lbl_dock_progress_meta.setText(
                f"완료 {done_students}/{total_students}명 · 남은 배치 {len(self._manual_batches)}개"
            )

        self.combo_manual_batch.blockSignals(True)
        self.combo_manual_batch.clear()
        total_b = len(self._manual_batches)
        for batch in self._manual_batches:
            stus = batch.get("students", [])
            b_idx = batch.get("batch_index", 1)
            toks = batch.get("estimated_tokens", 0)
            if stus:
                first_s = f"{stus[0].get('학번', '')} {stus[0].get('이름', '')}".strip()
                stu_desc = f"{first_s} 외 {len(stus)-1}명" if len(stus) > 1 else first_s
            else:
                stu_desc = "0명"
            self.combo_manual_batch.addItem(
                f"Batch {b_idx}/{total_b} ({stu_desc} · {toks:,}T)",
                batch,
            )
        self.combo_manual_batch.blockSignals(False)

        if total_b == 0:
            self.lbl_manual_batch_meta.setText("모든 학생의 결과가 이미 작성되어 있습니다.")
            self.btn_manual_copy_open.setEnabled(False)
            self.btn_manual_apply_next.setEnabled(False)
            return None

        self.btn_manual_copy_open.setEnabled(True)
        self.btn_manual_apply_next.setEnabled(True)
        self.combo_manual_batch.setCurrentIndex(0)
        self._update_manual_batch_meta_label()

        if copy_first:
            first_prompt = self._manual_batches[0]["final_prompt"]
            QApplication.clipboard().setText(first_prompt)
            return first_prompt
        return None

    def _get_current_manual_batch(self) -> Optional[Dict[str, Any]]:
        idx = self.combo_manual_batch.currentIndex()
        if 0 <= idx < len(self._manual_batches):
            return self._manual_batches[idx]
        return None

    def _update_manual_batch_meta_label(self) -> None:
        batch = self._get_current_manual_batch()
        if batch is None:
            self.lbl_manual_batch_meta.setText("선택된 배치가 없습니다.")
            return
        stus = batch.get("students", [])
        toks = batch.get("estimated_tokens", 0)
        self.lbl_manual_batch_meta.setText(
            f"선택 묶음: 학생 {len(stus)}명 · 예상 입력 약 {toks:,} 토큰"
        )
        self._on_manual_response_changed()

    def _on_manual_batch_changed(self, _idx: int) -> None:
        self._update_manual_batch_meta_label()

    def _build_current_manual_prompt(self) -> str:
        """실행 시점의 최신 컨텍스트를 읽어 현재 대상의 프롬프트를 생성합니다."""
        ctx = self._fetch_fresh_context()
        if not ctx:
            return ""
        kind = str(ctx.get("kind", "") or "")
        if kind == "hwpx_agent" or self._active_target_id == "hwpx_agent":
            dom_map = ctx.get("dom_map")
            inst = self.txt_hwpx_instruction.toPlainText().strip()
            if not isinstance(dom_map, dict) or not inst:
                return ""
            from services.hwpx_agent_service import build_hwpx_agent_prompt

            sys_p, usr_p = build_hwpx_agent_prompt(dom_map, self._append_quote_to_prompt(inst))
            return f"{sys_p}\n\n{usr_p}"
        if kind == "ocr":
            from ui.tabs.tab_doc_ocr import extract_local_text_from_files

            files = ctx.get("ocr_files") or []
            raw_text = extract_local_text_from_files(files) if files else str(ctx.get("ocr_current_text", ""))
            sys_p, user_p = build_structured_prompt(
                task_key="ocr_correction",
                raw_content=raw_text,
                custom_instruction=self._append_quote_to_prompt(str(ctx.get("ocr_prompt", ""))),
            )
            return f"[System 지시문]\n{sys_p}\n\n{user_p}"

        # 실행 시점 최신 컨텍스트로 배치 재계산 (현재 선택된 batch_index 최대한 유지)
        prev_idx = max(0, self.combo_manual_batch.currentIndex())
        self._rebuild_manual_batches(copy_first=False, ctx=ctx, quiet=True)
        if 0 <= prev_idx < len(self._manual_batches):
            self.combo_manual_batch.setCurrentIndex(prev_idx)
        batch = self._get_current_manual_batch()
        base_prompt = str(batch.get("final_prompt", "")) if batch else ""
        return self._append_quote_to_prompt(base_prompt) if base_prompt else ""

    def _on_manual_copy_only(self) -> None:
        prompt_text = self._build_current_manual_prompt()
        if not prompt_text:
            QMessageBox.information(self, "프롬프트 확인", "복사할 프롬프트가 없습니다. 대상 파일과 배치를 확인해 주세요.")
            return
        QApplication.clipboard().setText(prompt_text)
        self.lbl_manual_verify.setText("프롬프트가 클립보드에 복사되었습니다 (웹 AI에 Ctrl+V 하세요).")

    def _on_manual_copy_and_open(self) -> None:
        prompt_text = self._build_current_manual_prompt()
        if not prompt_text:
            QMessageBox.information(self, "프롬프트 확인", "복사할 프롬프트가 없습니다. 대상 파일과 배치를 확인해 주세요.")
            return
        url = self._resolve_target_web_url(self._manual_provider, self.edit_manual_custom_url)
        if not url:
            QMessageBox.warning(self, "URL 확인", "직접 URL 모드에서는 접속할 웹 AI 주소를 입력해 주세요.")
            self.edit_manual_custom_url.setFocus()
            return
        QApplication.clipboard().setText(prompt_text)
        webbrowser.open(url)
        self.lbl_manual_verify.setText("프롬프트를 클립보드에 복사하고 웹 AI를 열었습니다.")

    def _on_open_attachment_dir(self) -> None:
        """수동 복붙 모드에서 첨부파일이 위치한 폴더를 엽니다."""
        ctx = self._fetch_fresh_context() or self._last_context
        kind = str(ctx.get("kind", "") or "")
        if kind == "hwpx_agent":
            QMessageBox.information(self, "파일 없음", "HWPX 에이전트 타겟에는 별도 첨부파일이 없습니다.")
            return
        raw_atts = ctx.get("attachment_paths")
        if raw_atts is None and kind == "ocr":
            raw_atts = ctx.get("ocr_files") or []
        paths = [Path(str(p).strip()) for p in (raw_atts or []) if str(p).strip()]
        target = next((p for p in paths if p.exists()), None)
        if target is None:
            QMessageBox.information(self, "파일 없음", "열 수 있는 첨부파일 경로를 찾지 못했습니다.")
            return
        try:
            if sys.platform == "win32" and target.is_file():
                import subprocess

                subprocess.Popen(["explorer", "/select,", str(target.resolve())])
            else:
                parent_dir = target.parent if target.is_file() else target
                os.startfile(str(parent_dir.resolve()))
        except Exception as exc:
            QMessageBox.warning(self, "폴더 열기 실패", f"폴더를 여는 중 오류가 발생했습니다:\n{exc}")

    def _on_manual_paste_from_clipboard(self) -> None:
        clip_text = QApplication.clipboard().text()
        if not clip_text or not clip_text.strip():
            self.lbl_manual_verify.setText("클립보드가 비어 있습니다.")
            return
        self.txt_manual_response.setPlainText(clip_text)

    def _on_manual_response_changed(self) -> None:
        raw_resp = self.txt_manual_response.toPlainText().strip()
        ctx = self._last_context or self._fetch_fresh_context()
        kind = str(ctx.get("kind", "excel_batch"))

        if not raw_resp:
            self._manual_parsed_items = []
            self._manual_single_parsed = {}
            self.lbl_manual_verify.setText("대기 중: JSON 응답을 붙여넣으면 자동 검증됩니다.")
            return

        if kind in ("ocr", "hwpx_agent"):
            try:
                parsed = extract_json_from_llm_response(raw_resp)
                self._manual_single_parsed = parsed
                if kind == "hwpx_agent":
                    ops_cnt = len(parsed.get("operations", [])) if isinstance(parsed.get("operations"), list) else 0
                    self.lbl_manual_verify.setText(
                        f"유효한 HWPX JSON Patch 확인됨 (연산 {ops_cnt}건)"
                    )
                else:
                    self.lbl_manual_verify.setText(
                        f"JSON 검증 성공 (추출 키: {', '.join(list(parsed.keys())[:4])})"
                    )
            except Exception as exc:
                self._manual_single_parsed = {}
                self.lbl_manual_verify.setText(f"JSON 추출 오류: {exc}")
            return

        try:
            items = extract_batch_items_from_response(raw_resp)
            self._manual_parsed_items = items
        except Exception as exc:
            self._manual_parsed_items = []
            self.lbl_manual_verify.setText(f"JSON 추출 실패: {exc}")
            return

        batch = self._get_current_manual_batch()
        batch_students = batch.get("students", []) if batch else []
        batch_sids = {
            normalize_student_id_str(s.get("학번", ""))
            for s in batch_students
            if s.get("학번")
        }
        parsed_sids = {
            normalize_student_id_str(it.get("학번", ""))
            for it in items
            if it.get("학번")
        }
        matched = sum(1 for sid in batch_sids if sid in parsed_sids)
        missing = max(0, len(batch_sids) - matched)
        self.lbl_manual_verify.setText(
            f"검증 완료: {len(items)}건 추출 · 현재 묶음 {matched}/{len(batch_students)}명 매칭 (누락 {missing}명)"
        )

    def _on_manual_apply_and_next(self) -> None:
        ctx = self._fetch_fresh_context()
        if not ctx:
            return
        raw_resp = self.txt_manual_response.toPlainText().strip()
        if not raw_resp:
            QMessageBox.warning(self, "응답 확인", "웹 AI 응답 텍스트를 먼저 붙여넣어 주세요.")
            return

        target_id = str(ctx.get("target_id", self._active_target_id))
        if ctx.get("kind") == "hwpx_agent":
            try:
                parsed = extract_json_from_llm_response(raw_resp)
            except Exception as exc:
                QMessageBox.warning(self, "JSON 검증 오류", f"유효한 JSON Patch 응답을 추출하지 못했습니다:\n{exc}")
                return
            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.clear()
            self.txt_manual_response.blockSignals(False)
            self._dispatch_result(
                target_id,
                {"target_id": target_id, "mode": "manual", "parsed_json": parsed},
            )
            self.lbl_manual_verify.setText("HWPX JSON Patch가 문서에 적용되었습니다.")
            self.lbl_dock_progress_meta.setText("수동 Patch 반영 완료")
            self.progress_dock.setValue(100)
            return
        if ctx.get("kind") == "ocr":
            try:
                parsed = extract_json_from_llm_response(raw_resp)
                formatted = format_parsed_json_to_readable_text(parsed)
            except Exception as exc:
                QMessageBox.warning(self, "JSON 검증 오류", f"유효한 JSON 응답을 추출하지 못했습니다:\n{exc}")
                return
            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.clear()
            self.txt_manual_response.blockSignals(False)
            self._dispatch_result(
                target_id,
                {"target_id": target_id, "mode": "manual", "formatted_text": formatted, "parsed_json": parsed},
            )
            self.lbl_manual_verify.setText("OCR 결과가 중앙 편집창에 반영되었습니다.")
            self.lbl_dock_progress_meta.setText("수동 OCR 반영 완료")
            self.progress_dock.setValue(100)
            return

        excel_str = str(ctx.get("excel_path", "") or "").strip()
        if not excel_str or not Path(excel_str).is_file():
            QMessageBox.warning(self, "엑셀 확인", "대상 엑셀 파일(.xlsx)을 먼저 선택해 주세요.")
            return

        try:
            items = extract_batch_items_from_response(raw_resp)
        except Exception as exc:
            QMessageBox.warning(self, "JSON 확인", f"유효한 JSON 배열을 추출하지 못했습니다:\n{exc}")
            return

        prompt_str = str(ctx.get("prompt_file", "") or "").strip()
        is_grading = bool(prompt_str and "채점" in Path(prompt_str).name)
        out_path = resolve_batch_output_path(excel_str, None, is_grading=is_grading)

        try:
            res = apply_batch_results_to_excel(
                excel_path=excel_str,
                output_path=out_path,
                parsed_items=items,
                is_grading=is_grading,
            )
        except Exception as exc:
            QMessageBox.critical(self, "엑셀 저장 오류", f"결과 저장 중 오류가 발생했습니다:\n{exc}")
            return

        applied = int(res.get("applied_count", 0))
        saved_path = str(res.get("output_path", "") or "")
        if applied == 0:
            unmatched = res.get("unmatched_ids", [])
            QMessageBox.warning(
                self,
                "학번 매칭 실패",
                f"엑셀과 일치하는 학번이 없습니다.\n미매칭: {', '.join(unmatched[:8])}",
            )
            return

        if saved_path:
            self._set_result_excel_path(saved_path)

        self.txt_manual_response.blockSignals(True)
        self.txt_manual_response.clear()
        self.txt_manual_response.blockSignals(False)

        next_prompt = self._rebuild_manual_batches(copy_first=True, ctx=ctx, quiet=True)
        self._dispatch_result(
            target_id,
            {
                "target_id": target_id,
                "mode": "manual",
                "output_path": saved_path,
                "applied_count": applied,
                "remaining_batches": len(self._manual_batches),
            },
        )
        if next_prompt is None:
            self.lbl_manual_verify.setText(f"{applied}명 반영 완료. 모든 배치가 완료되었습니다.")
            QMessageBox.information(
                self,
                "모든 배치 완료",
                f"이번 묶음 {applied}명 저장 완료! 모든 미완료 학생 처리가 끝났습니다.\n\n저장 파일:\n{saved_path}",
            )
        else:
            self.lbl_manual_verify.setText(
                f"{applied}명 반영 완료 -> 다음 묶음 프롬프트가 클립보드에 자동 복사되었습니다. (남은 {len(self._manual_batches)}개)"
            )

    # =========================================================================
    # Playwright 자동 / 반자동 모드
    # =========================================================================
    def _on_pw_provider_chip_clicked(self, key: str) -> None:
        self._pw_provider = key
        is_custom = key == "custom"
        self.edit_pw_custom_url.setEnabled(is_custom)
        if is_custom:
            self.edit_pw_custom_url.setFocus()

    def _set_pw_running_state(self, running: bool) -> None:
        for chip in self.pw_provider_chips.values():
            chip.setEnabled(not running)
        self.edit_pw_custom_url.setEnabled((not running) and self._pw_provider == "custom")
        self.combo_pw_exec_mode.setEnabled(not running)
        self.chk_pw_keep_login.setEnabled(not running)
        self.chk_pw_temp_chat.setEnabled(not running)
        self.btn_pw_start.setEnabled(not running)
        self.btn_pw_stop.setEnabled(running)
        if not running:
            self.btn_pw_collect_next.setEnabled(False)
            self.btn_pw_confirm_login.setEnabled(False)

    def _on_pw_start_clicked(self) -> None:
        if self._active_pw_worker is not None:
            return
        ctx = self._fetch_fresh_context()
        if not ctx:
            return

        custom_url = ""
        if self._pw_provider == "custom":
            custom_url = normalize_web_url(self.edit_pw_custom_url.text())
            if not custom_url:
                QMessageBox.warning(self, "URL 확인", "직접 URL 모드에서는 접속할 웹 AI 주소를 입력해 주세요.")
                self.edit_pw_custom_url.setFocus()
                return

        exec_mode = str(self.combo_pw_exec_mode.currentData() or "semi_auto")
        keep_login = bool(self.chk_pw_keep_login.isChecked())
        use_temp_chat = bool(self.chk_pw_temp_chat.isChecked())
        kind = str(ctx.get("kind", "excel_batch"))
        if kind == "hwpx_agent":
            attachment_paths: List[str] = []
        else:
            raw_atts = ctx.get("attachment_paths")
            if raw_atts is None and kind == "ocr":
                raw_atts = ctx.get("ocr_files") or []
            attachment_paths = [str(p).strip() for p in (raw_atts or []) if str(p).strip()]

        task_label = str(
            ctx.get("task_label", "")
            or ("HWPX 문서 편집" if kind == "hwpx_agent" else "단일 OCR/보정")
        ).strip()

        if kind in ("ocr", "hwpx_agent"):
            prompt_text = self._build_current_manual_prompt()
            if not prompt_text:
                msg = (
                    "중앙 탭에서 HWPX 문서를 열고 수정 지시사항을 입력해 주세요."
                    if kind == "hwpx_agent"
                    else "중앙 탭에서 파일이나 보정 지침을 확인해 주세요."
                )
                QMessageBox.warning(self, "입력 확인", msg)
                return
            worker = WebGeminiAutoWorker(
                excel_path=None,
                single_prompt=prompt_text,
                attachment_paths=attachment_paths,
                provider=self._pw_provider,
                custom_url=custom_url,
                execution_mode=exec_mode,
                require_login=False,
                keep_login_session=keep_login,
                use_temp_chat=use_temp_chat,
                headless=False,
                task_label=task_label,
            )
        else:
            excel_str = str(ctx.get("excel_path", "") or "").strip()
            if not excel_str or not Path(excel_str).is_file():
                QMessageBox.warning(self, "대상 엑셀 확인", "대상 엑셀 파일(.xlsx)을 먼저 선택해 주세요.")
                return
            prompt_str = str(ctx.get("prompt_file", "") or "").strip()
            ref_str = str(ctx.get("reference_dir", "") or "").strip()
            worker = WebGeminiAutoWorker(
                excel_path=excel_str,
                prompt_file=Path(prompt_str) if prompt_str else None,
                reference_dir=Path(ref_str) if ref_str and Path(ref_str).is_dir() else None,
                attachment_paths=attachment_paths,
                max_students=self.spin_manual_students.value(),
                max_input_tokens=self.spin_manual_tokens.value(),
                max_bytes=int(ctx.get("max_bytes", 1500)),
                headless=False,
                provider=self._pw_provider,
                require_login=False,
                keep_login_session=keep_login,
                use_temp_chat=use_temp_chat,
                execution_mode=exec_mode,
                custom_url=custom_url,
            )

        target_id = str(ctx.get("target_id", self._active_target_id))
        worker.signals.progress.connect(self._on_dock_worker_progress)
        worker.waiting_for_login.connect(self._on_pw_waiting_for_login)
        worker.waiting_for_semi_auto_collect.connect(self._on_pw_waiting_for_collect)
        worker.waiting_for_element_click.connect(self._on_pw_waiting_for_click)
        worker.batch_completed.connect(lambda payload, tid=target_id: self._on_pw_batch_completed(tid, payload))

        self._active_pw_worker = worker
        self._set_pw_running_state(True)
        self.progress_dock.setValue(0)
        self.lbl_dock_progress_meta.setText(
            f"[{task_label}] Playwright 브라우저 실행 중..."
            if kind in ("ocr", "hwpx_agent")
            else "Playwright 브라우저 실행 중..."
        )

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_DOCK_PLAYWRIGHT,
            on_result=lambda res, tid=target_id: self._on_pw_finished_result(tid, res),
            on_error=self._on_pw_error,
            on_finished=self._on_pw_finished,
        )

    def _on_pw_waiting_for_login(self, provider_name: str = "") -> None:
        self.btn_pw_confirm_login.setText("인증/로그인 완료 · 작업 재개")
        self.btn_pw_confirm_login.setEnabled(True)
        prefix = f"[{provider_name}] " if provider_name else ""
        self.lbl_dock_progress_meta.setText(
            f"{prefix}브라우저에서 로그인·약관 동의·봇 인증 완료 후 [인증/로그인 완료 · 작업 재개] 버튼을 눌러주세요."
        )

    def _on_pw_waiting_for_click(self, domain: str = "") -> None:
        self.lbl_dock_progress_meta.setText(
            f"[{domain}] 입력창을 자동 탐지하지 못했습니다. 브라우저에서 입력창을 1회 클릭하세요."
        )

    def _on_pw_waiting_for_collect(self, batch_idx: int, total_batches: int) -> None:
        self.btn_pw_collect_next.setEnabled(True)
        self.lbl_dock_progress_meta.setText(
            f"Batch {batch_idx}/{total_batches} 입력됨 · 웹에서 전송·완료 후 [응답 회수 · 다음 배치] 클릭"
        )

    def _on_pw_confirm_login_clicked(self) -> None:
        if self._active_pw_worker is not None:
            self._active_pw_worker.confirm_login()
        self.btn_pw_confirm_login.setEnabled(False)
        self.lbl_dock_progress_meta.setText("로그인 확인 완료 — 프롬프트 입력 진행 중...")

    def _on_pw_collect_next_clicked(self) -> None:
        if self._active_pw_worker is not None:
            manual_txt = self.txt_manual_response.toPlainText().strip()
            self._active_pw_worker.trigger_collect_and_next(manual_txt)
        self.btn_pw_collect_next.setEnabled(False)
        self.lbl_dock_progress_meta.setText("응답 회수 및 엑셀 반영 중...")

    def _on_pw_stop_clicked(self) -> None:
        if self._active_pw_worker is not None:
            self._active_pw_worker.request_stop()
        self.task_runner.stop_task(self.TASK_DOCK_PLAYWRIGHT)
        self.btn_pw_stop.setEnabled(False)
        self.btn_pw_collect_next.setEnabled(False)
        self.btn_pw_confirm_login.setEnabled(False)
        self.lbl_dock_progress_meta.setText("Playwright 작업 중지 요청됨...")

    def _on_pw_batch_completed(self, target_id: str, payload: Dict[str, Any]) -> None:
        out_path = str(payload.get("output_path", "") or "")
        if out_path:
            self._set_result_excel_path(out_path)
        self._dispatch_result(target_id, {"target_id": target_id, "mode": "playwright", **payload})
        self.sync_from_context(self._fetch_fresh_context(target_id))

    def _on_pw_finished_result(self, target_id: str, res: Dict[str, Any]) -> None:
        if res.get("missing_playwright"):
            QMessageBox.warning(
                self,
                "Playwright 패키지 필요",
                "Playwright가 설치되어 있지 않습니다.\n"
                "터미널에서 'pip install playwright && playwright install chromium'을 실행해 주세요.",
            )
            return
        out_path = str(res.get("output_path", "") or "")
        if out_path:
            self._set_result_excel_path(out_path)
        self._dispatch_result(target_id, {"target_id": target_id, "mode": "playwright", **res})
        self.sync_from_context(self._fetch_fresh_context(target_id))

    def _on_pw_error(self, err: str) -> None:
        first = (err or "").strip().split("\n")[0]
        self.lbl_dock_progress_meta.setText(f"오류: {first}")
        QMessageBox.critical(self, "Playwright 실행 오류", first)

    def _on_pw_finished(self) -> None:
        self._active_pw_worker = None
        self._set_pw_running_state(False)

    # =========================================================================
    # 공통 상태 / 결과 핸들러
    # =========================================================================
    def _on_dock_worker_progress(self, percent: int, message: str) -> None:
        self.progress_dock.setValue(max(0, min(100, int(percent))))
        if message:
            self.lbl_dock_progress_meta.setText(message)

    def _dispatch_result(self, target_id: str, payload: Dict[str, Any]) -> None:
        meta = self._targets.get(target_id)
        if meta and callable(meta.get("on_result")):
            try:
                meta["on_result"](payload)
            except Exception as exc:
                self.lbl_dock_progress_meta.setText(f"결과 반영 경고: {exc}")
        self.execution_completed.emit(target_id, payload)

    def _set_result_excel_path(self, path_str: str) -> None:
        if path_str and Path(path_str).exists():
            self._last_result_excel_path = str(Path(path_str).resolve())
            self.btn_dock_open_excel.setEnabled(True)

    def _on_open_result_excel(self) -> None:
        if self._last_result_excel_path and Path(self._last_result_excel_path).exists():
            os.startfile(self._last_result_excel_path)
        else:
            QMessageBox.information(self, "안내", "아직 생성된 결과 엑셀 파일이 없습니다.")
