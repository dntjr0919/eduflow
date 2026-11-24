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

import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple
import webbrowser

import pandas as pd
from PyQt6.QtCore import (
    QByteArray,
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QCursor,
    QDesktopServices,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QPainter,
    QPixmap,
    QTextLayout,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
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
from core.database import DatabaseManager
from services.ai_agent_router import (
    build_agent_system_prompt,
    execute_action_tool,
    execute_read_only_tool,
    parse_agent_response,
    prepare_attachments_for_mode,
    summarize_tool_args,
)
from services.batch_prompt_service import (
    apply_batch_results_to_excel,
    build_student_batches,
    extract_batch_items_from_response,
    normalize_student_id_str,
    resolve_batch_output_path,
)
from services.hwpx_agent_service import (
    HwpxDocument,
    apply_hwpx_operations,
    build_hwpx_agent_prompt,
    build_hwpx_create_prompt,
    create_minimal_hwpx_document,
    execute_hwpx_hybrid_edit,
    extract_hwpx_dom_map,
    extract_hwpx_reference_chunks,
)
from services.hwpx_ref_session_service import (
    MAX_EXTRA_CHUNK_ROUNDS,
    SCANNED_OCR_BATCH_PAGES,
    build_hwpx_scanned_ocr_prompt,
    build_hwpx_stage1_selection_prompt,
    build_reference_index_summary,
    fallback_select_chunks_by_keywords,
    get_hwpx_ref_session_cache,
    parse_stage1_selection_response,
    parse_stage2_response_status,
    resolve_selected_chunks,
)
from services.hwpx_sandbox_runner import (
    extract_python_code_from_llm_response,
    validate_generated_code,
)
from services.llm_client import (
    MultiLlmClient,
    build_structured_prompt,
    extract_json_from_llm_response,
    format_parsed_json_to_readable_text,
)
from services.notifier import NotificationService
from services.record_generator_service import (
    get_active_ai_info,
    is_blank_or_failed,
    load_all_references,
    load_prompt_text,
)
from services.schedule_parser_service import (
    build_calendar_ai_prompt,
    is_calendar_query_intent,
    parse_calendar_ai_response,
    try_parse_direct_calendar_json,
)
from services.class_notice_digest_service import parse_notice_digest_ai_response
from services.web_gemini_auto_worker import (
    PROVIDER_CONFIGS,
    WebGeminiAutoWorker,
    normalize_web_url,
)
from ui.components.file_drop_helper import enable_file_drop
from ui.utils import install_no_scroll_wheel_filter
from utils.path_helper import get_data_path


_SVG_SESSION_MENU = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="3"/>'
    '<line x1="7" y1="8" x2="17" y2="8"/>'
    '<line x1="7" y1="12" x2="17" y2="12"/>'
    '<line x1="7" y1="16" x2="17" y2="16"/>'
    "</svg>"
)

_SVG_COPY_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>'
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    "</svg>"
)

_SVG_EDIT_PENCIL = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 20h9"/>'
    '<path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>'
    "</svg>"
)

_SVG_PAPERCLIP = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>'
    "</svg>"
)

_SVG_FOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>'
    "</svg>"
)

_SVG_SETTINGS_GEAR = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="3"/>'
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>'
    "</svg>"
)


def _render_inline_svg_icon(svg_markup: str, color_hex: str = "#334155", size: int = 16) -> QIcon:
    """인라인 SVG 문자열을 지정 색상/크기의 HiDPI QIcon으로 렌더링합니다."""
    colored = svg_markup.replace("currentColor", color_hex)
    renderer = QSvgRenderer(QByteArray(colored.encode("utf-8")))
    icon = QIcon()
    for dpr in (1, 2):
        px = size * dpr
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        painter.end()
        pm.setDevicePixelRatio(float(dpr))
        icon.addPixmap(pm)
    return icon


def build_attachment_entry(path_str: str, max_folder_files: int = 200) -> Optional[Dict[str, Any]]:
    """파일 또는 폴더 경로를 챗 첨부 메타데이터 딕셔너리로 변환합니다."""
    raw = str(path_str or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_dir():
        resolved = p.resolve()
        rel_files: List[str] = []
        total_count = 0
        skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
        for root, dirs, files in os.walk(resolved):
            dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
            for fname in sorted(files):
                total_count += 1
                if len(rel_files) < max_folder_files:
                    try:
                        rel = str((Path(root) / fname).relative_to(resolved)).replace("\\", "/")
                    except ValueError:
                        rel = fname
                    rel_files.append(rel)
        return {
            "type": "dir",
            "path": str(resolved),
            "name": resolved.name or str(resolved),
            "file_count": total_count,
            "files": rel_files,
        }
    if p.is_file():
        resolved = p.resolve()
        try:
            size = int(resolved.stat().st_size)
        except OSError:
            size = 0
        return {
            "type": "file",
            "path": str(resolved),
            "name": resolved.name,
            "size": size,
        }
    # 가상 테스트 경로 지원
    is_dir_hint = raw.endswith("/") or raw.endswith("\\")
    clean = raw.rstrip("/\\")
    return {
        "type": "dir" if is_dir_hint else "file",
        "path": clean,
        "name": Path(clean).name or clean,
        "file_count": 0,
        "files": [],
    }


def format_attachments_for_prompt(attachments: List[Dict[str, Any]]) -> str:
    """첨부 파일/폴더 메타데이터를 프롬프트 컨텍스트 문자열로 포맷팅합니다."""
    if not attachments:
        return ""
    blocks: List[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        atype = att.get("type", "file")
        name = str(att.get("name", "") or "")
        path = str(att.get("path", "") or "")
        if atype == "dir":
            files = att.get("files") or []
            total = int(att.get("file_count", len(files)) or len(files))
            lines = [f"[첨부 폴더] {name}", f"- 경로: {path}", f"- 하위 파일 목록 ({total}개):"]
            if files:
                for rf in files:
                    lines.append(f"  - {rf}")
                if total > len(files):
                    lines.append(f"  - ... 외 {total - len(files)}개")
            else:
                lines.append("  - (하위 파일 없음)")
            blocks.append("\n".join(lines))
        else:
            blocks.append(f"[첨부 파일] {name} ({path})")
    return "\n\n".join(blocks)


def elide_text_to_lines(
    text: str, font_metrics: QFontMetrics, width: int, max_lines: int = 2, font: Any = None
) -> Tuple[str, bool]:
    """주어진 폭에서 최대 max_lines 줄로 텍스트를 자르고 말줄임(...) 처리합니다."""
    raw = str(text or "")
    if not raw.strip():
        return "", False
    safe_w = max(80, int(width))
    # QTextLayout은 줄바꿈(\n)을 직접 처리하지 않으므로 단락별로 줄 수를 누적
    paragraphs = raw.splitlines() or [raw]
    collected_lines: List[str] = []
    truncated = False
    layout_font = font if font is not None else QApplication.font()

    for p_idx, para in enumerate(paragraphs):
        if len(collected_lines) >= max_lines:
            truncated = True
            break
        if para == "":
            collected_lines.append("")
            continue
        layout = QTextLayout(para, layout_font)
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(safe_w)
            seg = para[line.textStart() : line.textStart() + line.textLength()]
            collected_lines.append(seg)
            if len(collected_lines) >= max_lines:
                has_more_in_para = (line.textStart() + line.textLength()) < len(para)
                has_more_paras = p_idx < len(paragraphs) - 1
                if has_more_in_para or has_more_paras:
                    truncated = True
                break
        layout.endLayout()
        if truncated:
            break

    if truncated and collected_lines:
        last = collected_lines[-1].rstrip()
        elided_last = font_metrics.elidedText(
            f"{last}..." if not last.endswith("...") else last,
            Qt.TextElideMode.ElideRight,
            safe_w,
        )
        if not elided_last.endswith("...") and not elided_last.endswith("…"):
            elided_last = (elided_last[:-1] + "…") if len(elided_last) > 1 else "…"
        collected_lines[-1] = elided_last

    return "\n".join(collected_lines), truncated


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
        last_parsed_json: Any = None
        last_raw_response: str = ""

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
                last_raw_response = str(raw_reply or "").strip()
                try:
                    parsed = extract_json_from_llm_response(raw_reply)
                    last_parsed_json = parsed
                    formatted = format_parsed_json_to_readable_text(parsed)
                except Exception:
                    formatted = last_raw_response
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
            "parsed_json": last_parsed_json,
            "raw_response": last_raw_response,
        }


def _build_running_hwpx_seed_from_dom(dom_map: Optional[Dict[str, Any]]) -> HwpxDocument:
    """실행 워커 전용 경량 HwpxDocument 스냅샷을 DOM 맵으로부터 생성합니다."""
    seed_doc = create_minimal_hwpx_document(title="새 양식", empty=True)
    if not isinstance(dom_map, dict):
        return seed_doc
    blocks = dom_map.get("blocks") or []
    seed_ops: List[Dict[str, Any]] = []
    for blk in blocks:
        if blk.get("type") == "table":
            raw_rows = blk.get("rows") or []
            str_rows = [[str(c.get("text", "")) for c in row] for row in raw_rows]
            seed_ops.append({"op": "append_table", "headers": [], "rows": str_rows})
        else:
            if blk.get("page_break"):
                seed_ops.append({"op": "page_break"})
            else:
                seed_ops.append({"op": "append_paragraph", "text": str(blk.get("text", ""))})
    if seed_ops:
        apply_hwpx_operations(seed_doc, seed_ops)
    return seed_doc


class _DockApiHwpxAgentWorker(BaseWorker):
    """HWPX AI 문서 에이전트용 API 단일/분할 JSON Patch 생성 워커."""

    part_completed = pyqtSignal(dict)

    def __init__(
        self,
        dom_map: Optional[Dict[str, Any]],
        instruction: str,
        *,
        is_create_mode: bool = False,
        ref_chunks: Optional[List[Dict[str, Any]]] = None,
        reference_files: Optional[List[str]] = None,
        preselected_chunk_ids: Optional[List[str]] = None,
        hwpx_doc_snapshot: Optional[HwpxDocument] = None,
    ):
        super().__init__(task_name="HWPX AI 문서 에이전트 (API)")
        self.dom_map = dom_map
        self.instruction = instruction
        self.is_create_mode = bool(is_create_mode)
        self.ref_chunks: List[Dict[str, Any]] = list(ref_chunks or [])
        self.reference_files: List[str] = [str(p).strip() for p in (reference_files or []) if str(p).strip()]
        self.preselected_chunk_ids: List[str] = list(preselected_chunk_ids or [])
        self.hwpx_doc_snapshot = hwpx_doc_snapshot

    def _ocr_scanned_pages_in_batches(
        self,
        client: MultiLlmClient,
        file_path: str,
        page_numbers: List[int],
        *,
        is_toc: bool = False,
    ) -> None:
        cache = get_hwpx_ref_session_cache()
        p_obj = Path(file_path)
        for i in range(0, len(page_numbers), SCANNED_OCR_BATCH_PAGES):
            if self.is_stop_requested():
                break
            batch_pages = page_numbers[i : i + SCANNED_OCR_BATCH_PAGES]
            label = "목차" if is_toc else "본문"
            self.emit_progress(15, f"스캔 PDF {label} AI OCR 중 ({p_obj.name}: {batch_pages[0]}~{batch_pages[-1]}쪽)...")
            rendered = cache.render_pdf_pages_for_ocr(p_obj, batch_pages)
            sys_p, usr_p = build_hwpx_scanned_ocr_prompt(p_obj.name, batch_pages, is_toc=is_toc)
            img_bytes: Optional[bytes] = None
            mime_type = "image/png"
            valid_imgs = [(pn, ip) for pn, ip in rendered if ip is not None and ip.exists()]
            if len(valid_imgs) == 1:
                img_bytes = valid_imgs[0][1].read_bytes()
            elif not valid_imgs and p_obj.exists():
                img_bytes = p_obj.read_bytes()
                mime_type = "application/pdf"
            elif len(valid_imgs) > 1:
                # 여러 페이지는 페이지별로 순차 OCR하여 누락 방지
                for pn, ip in valid_imgs:
                    if self.is_stop_requested():
                        break
                    s1, u1 = build_hwpx_scanned_ocr_prompt(p_obj.name, [pn], is_toc=is_toc)
                    raw = client.generate(prompt=u1, system_prompt=s1, image_bytes=ip.read_bytes(), mime_type="image/png")
                    try:
                        parsed = extract_json_from_llm_response(raw)
                        pages_arr = parsed.get("pages") if isinstance(parsed, dict) else None
                        if isinstance(pages_arr, list) and pages_arr:
                            txt = str(pages_arr[0].get("text") or "").strip()
                        else:
                            txt = raw.strip()
                    except Exception:
                        txt = raw.strip()
                    cache.store_ocr_page_result(p_obj, pn, txt)
                continue

            raw = client.generate(prompt=usr_p, system_prompt=sys_p, image_bytes=img_bytes, mime_type=mime_type)
            try:
                parsed = extract_json_from_llm_response(raw)
                pages_arr = parsed.get("pages") if isinstance(parsed, dict) else None
                if isinstance(pages_arr, list) and pages_arr:
                    for item in pages_arr:
                        if isinstance(item, dict):
                            pn = int(item.get("page") or batch_pages[0])
                            cache.store_ocr_page_result(p_obj, pn, str(item.get("text") or ""))
                else:
                    for pn in batch_pages:
                        cache.store_ocr_page_result(p_obj, pn, raw.strip())
            except Exception:
                for pn in batch_pages:
                    cache.store_ocr_page_result(p_obj, pn, raw.strip())

    def _ensure_chunks_ocrd(self, client: MultiLlmClient, chunks: List[Dict[str, Any]]) -> None:
        pending_by_file: Dict[str, List[int]] = {}
        for ch in chunks:
            if ch.get("status") == "needs_ocr" and ch.get("file_path") and ch.get("page_num"):
                pending_by_file.setdefault(str(ch["file_path"]), []).append(int(ch["page_num"]))
        for fp, pages in pending_by_file.items():
            uniq_pages = sorted(set(pages))
            self._ocr_scanned_pages_in_batches(client, fp, uniq_pages, is_toc=False)

    def execute(self) -> Dict[str, Any]:
        client = MultiLlmClient()

        active_chunks = list(self.ref_chunks)
        if self.reference_files:
            summary = build_reference_index_summary(self.reference_files)
            for toc_item in summary.get("scanned_toc_targets") or []:
                self._ocr_scanned_pages_in_batches(
                    client,
                    str(toc_item["file_path"]),
                    list(toc_item["pages"]),
                    is_toc=True,
                )
            if summary.get("scanned_toc_targets"):
                summary = build_reference_index_summary(self.reference_files)

            if self.preselected_chunk_ids:
                active_chunks = resolve_selected_chunks(
                    self.reference_files, chunk_ids=self.preselected_chunk_ids
                )
            else:
                self.emit_progress(20, "참고 문서 인덱스 분석 및 범위 선별 중...")
                s1_sys, s1_usr = build_hwpx_stage1_selection_prompt(
                    self.instruction,
                    summary.get("index_text", ""),
                    self.dom_map,
                )
                s1_raw = client.generate(prompt=s1_usr, system_prompt=s1_sys)
                try:
                    s1 = parse_stage1_selection_response(s1_raw)
                except Exception:
                    s1 = {"status": "fallback"}

                if s1["status"] == "needs_clarification":
                    return {
                        "target_id": "hwpx_agent",
                        "mode": "api",
                        "clarification": s1,
                        "already_applied_parts": True,
                        "completed_parts": 0,
                        "total_parts": 1,
                    }
                if s1["status"] == "direct_operations":
                    return {
                        "target_id": "hwpx_agent",
                        "mode": "api",
                        "parsed_json": s1["parsed_json"],
                        "raw_response": s1_raw,
                        "immediate": True,
                        "already_applied_parts": False,
                        "completed_parts": 1,
                        "total_parts": 1,
                    }
                if s1["status"] == "fallback":
                    active_chunks = fallback_select_chunks_by_keywords(
                        self.reference_files, self.instruction
                    )
                else:
                    active_chunks = resolve_selected_chunks(
                        self.reference_files,
                        chunk_ids=s1.get("chunk_ids"),
                        page_ranges=s1.get("page_ranges"),
                    )

            self._ensure_chunks_ocrd(client, active_chunks)
            active_chunks = resolve_selected_chunks(
                self.reference_files,
                chunk_ids=[str(c.get("chunk_id")) for c in active_chunks if c.get("chunk_id")],
            )

        total_parts = len(active_chunks) if active_chunks else 1

        # 단일 파트 (기존 수정 모드 또는 지시사항/단일참고 생성 모드)
        if total_parts <= 1 and not self.reference_files:
            ref_content = str(active_chunks[0].get("content", "")) if active_chunks else ""
            if self.is_create_mode or ref_content:
                self.emit_progress(25, "새 HWPX 양식 생성 프롬프트 전송 중...")
                sys_p, usr_p = build_hwpx_create_prompt(
                    user_instruction=self.instruction,
                    reference_chunk=ref_content,
                    part_index=1,
                    total_parts=1,
                    current_dom_map=self.dom_map,
                )
            else:
                if self.hwpx_doc_snapshot is not None:
                    self.emit_progress(25, "HWPX 하이브리드 편집 실행 중...")
                    hybrid_res = execute_hwpx_hybrid_edit(
                        self.hwpx_doc_snapshot,
                        self.instruction,
                        lambda u, s: client.generate(prompt=u, system_prompt=s),
                        dom_map=self.dom_map,
                        max_retries=2,
                    )
                    if hybrid_res.get("mode") == "code_mode":
                        if not hybrid_res.get("success"):
                            raise RuntimeError(
                                f"하이브리드 코드 편집 실패 ({hybrid_res.get('attempts', 3)}회 시도): "
                                f"{hybrid_res.get('error', '')}"
                            )
                        self.emit_progress(100, "하이브리드 코드 편집 완료")
                        return {
                            "target_id": "hwpx_agent",
                            "mode": "api",
                            "exec_kind": "code_mode",
                            "code": hybrid_res["code"],
                            "raw_response": hybrid_res.get("raw_response", ""),
                            "attempts": hybrid_res.get("attempts", 1),
                            "immediate": True,
                            "already_applied_parts": False,
                            "completed_parts": 1,
                            "total_parts": 1,
                        }
                    parsed = hybrid_res.get("parsed_json") or {}
                    raw_reply = str(hybrid_res.get("raw_response", ""))
                else:
                    self.emit_progress(25, "HWPX DOM 맵 분석 및 AI 프롬프트 전송 중...")
                    sys_p, usr_p = build_hwpx_agent_prompt(self.dom_map or {}, self.instruction)
                    raw_reply = client.generate(prompt=usr_p, system_prompt=sys_p)
                    parsed = extract_json_from_llm_response(raw_reply)
            if self.is_create_mode or ref_content:
                raw_reply = client.generate(prompt=usr_p, system_prompt=sys_p)
                parsed = extract_json_from_llm_response(raw_reply)
            self.emit_progress(100, "JSON Patch 생성 완료")
            return {
                "target_id": "hwpx_agent",
                "mode": "api",
                "parsed_json": parsed,
                "raw_response": raw_reply,
                "immediate": bool(self.is_create_mode or ref_content),
                "already_applied_parts": False,
                "completed_parts": 1,
                "total_parts": 1,
            }

        # 다중/참고 파트 순차 생성 + need_more_chunks 루프(최대 3회)
        running_doc = _build_running_hwpx_seed_from_dom(self.dom_map)
        last_parsed: Dict[str, Any] = {}
        last_raw: str = ""
        completed_parts = 0
        seen_chunk_ids = {str(c.get("chunk_id", "")) for c in active_chunks if c.get("chunk_id")}
        extra_rounds = 0
        idx = 0

        while idx < len(active_chunks):
            if self.is_stop_requested():
                break
            chunk = active_chunks[idx]
            cur_total = len(active_chunks)
            pct_start = int((idx / max(1, cur_total)) * 100)
            self.emit_progress(
                max(5, pct_start),
                f"HWPX 양식 분할 생성 중... (Part {idx + 1}/{cur_total})",
            )
            cur_dom = extract_hwpx_dom_map(running_doc)
            sys_p, usr_p = build_hwpx_create_prompt(
                user_instruction=self.instruction,
                reference_chunk=str(chunk.get("content", "")),
                part_index=idx + 1,
                total_parts=cur_total,
                current_dom_map=cur_dom,
            )
            raw_reply = client.generate(prompt=usr_p, system_prompt=sys_p)
            parsed = extract_json_from_llm_response(raw_reply)
            s2 = parse_stage2_response_status(parsed)

            if s2["status"] == "needs_clarification":
                return {
                    "target_id": "hwpx_agent",
                    "mode": "api",
                    "clarification": s2,
                    "already_applied_parts": True,
                    "completed_parts": completed_parts,
                    "total_parts": cur_total,
                }

            if s2["status"] == "need_more_chunks" and extra_rounds < MAX_EXTRA_CHUNK_ROUNDS and self.reference_files:
                extra_rounds += 1
                more = resolve_selected_chunks(
                    self.reference_files,
                    chunk_ids=s2.get("chunk_ids"),
                    page_ranges=s2.get("pages"),
                )
                self._ensure_chunks_ocrd(client, more)
                more = resolve_selected_chunks(
                    self.reference_files,
                    chunk_ids=[str(c.get("chunk_id")) for c in more if c.get("chunk_id")],
                )
                added = False
                for mc in more:
                    mcid = str(mc.get("chunk_id", ""))
                    if mcid and mcid not in seen_chunk_ids:
                        seen_chunk_ids.add(mcid)
                        active_chunks.append(mc)
                        added = True
                if added:
                    idx += 1
                    continue

            ops = parsed.get("operations") if isinstance(parsed, dict) else None
            if isinstance(ops, list) and ops:
                apply_hwpx_operations(running_doc, [o for o in ops if isinstance(o, dict)])
            last_parsed = parsed if isinstance(parsed, dict) else {}
            last_raw = raw_reply
            completed_parts = idx + 1
            self.part_completed.emit(
                {
                    "target_id": "hwpx_agent",
                    "mode": "api",
                    "part_index": completed_parts,
                    "total_parts": len(active_chunks),
                    "parsed_json": last_parsed,
                    "raw_response": raw_reply,
                    "immediate": True,
                }
            )
            self.emit_progress(
                int((completed_parts / max(1, len(active_chunks))) * 100),
                f"Part {completed_parts}/{len(active_chunks)} 생성·반영 완료",
            )
            idx += 1

        return {
            "target_id": "hwpx_agent",
            "mode": "api",
            "parsed_json": last_parsed,
            "raw_response": last_raw,
            "already_applied_parts": True,
            "completed_parts": completed_parts,
            "total_parts": total_parts,
        }


class _DockApiCalendarTodoWorker(BaseWorker):
    """캘린더 & 할일 자연어/공문 텍스트 추출 또는 내 일정 질의응답을 처리하는 API 워커."""

    def __init__(self, instruction: str, schedule_context: str = ""):
        super().__init__(task_name="캘린더 & 할일 AI 추출 (API)")
        self.instruction = instruction
        self.schedule_context = schedule_context

    def execute(self) -> Dict[str, Any]:
        is_query = is_calendar_query_intent(self.instruction)
        self.emit_progress(
            25,
            "내 일정 조회 응답 생성 중..." if is_query else "일정·할일 추출 프롬프트 전송 중...",
        )
        sys_p, usr_p = build_calendar_ai_prompt(
            self.instruction,
            schedule_context=self.schedule_context,
        )
        client = MultiLlmClient()
        raw_reply = client.generate(prompt=usr_p, system_prompt=sys_p)
        if is_query:
            parsed_try = try_parse_direct_calendar_json(raw_reply)
            if parsed_try is None:
                self.emit_progress(100, "일정 조회 답변 완료")
                return {
                    "target_id": "calendar_todo",
                    "mode": "api",
                    "is_query_answer": True,
                    "raw_response": raw_reply,
                    "parsed_calendar": {"summary": "", "schedules": [], "todos": []},
                }
        self.emit_progress(80, "추출된 JSON 정규화 중...")
        parsed_cal = parse_calendar_ai_response(raw_reply)
        self.emit_progress(100, "일정·할일 JSON 추출 완료")
        return {
            "target_id": "calendar_todo",
            "mode": "api",
            "is_query_answer": False,
            "raw_response": raw_reply,
            "parsed_calendar": parsed_cal,
        }


class _DockApiClassNoticeDigestWorker(BaseWorker):
    """카톡 대화/학교종이 공지에서 학급 공지 요약 JSON 배열을 추출하는 API 워커."""

    def __init__(
        self,
        system_prompt: str,
        user_prompt: str,
        source_label: str = "카카오톡 대화",
        source_type: str = "kakao",
        digest_action: str = "kakao_digest",
        review_card_id: str = "",
    ):
        super().__init__(task_name="학급 공지 AI 추출·요약 (API)")
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.source_label = source_label
        self.source_type = source_type
        self.digest_action = digest_action
        self.review_card_id = review_card_id

    def execute(self) -> Dict[str, Any]:
        self.emit_progress(25, "공지 요약 프롬프트 전송 중...")
        client = MultiLlmClient()
        raw_reply = client.generate(prompt=self.user_prompt, system_prompt=self.system_prompt)
        self.emit_progress(80, "추출된 공지 JSON 정규화 중...")
        items = parse_notice_digest_ai_response(raw_reply, default_source_label=self.source_label)
        self.emit_progress(100, f"공지 {len(items)}건 추출 완료")
        return {
            "target_id": "class_notice_digest",
            "mode": "api",
            "raw_response": raw_reply,
            "items": items,
            "source_label": self.source_label,
            "source_type": self.source_type,
            "digest_action": self.digest_action,
            "review_card_id": self.review_card_id,
        }


class _DockApiGeneralChatWorker(BaseWorker):
    """범용 챗 (에이전트) API 모드 워커."""

    def __init__(
        self,
        prompt: str,
        history: Optional[List[Dict[str, Any]]] = None,
        model_label: str = "",
        system_prompt: Optional[str] = None,
    ):
        super().__init__(task_name="범용 AI 챗 응답 생성")
        self.prompt = prompt
        self.history = history or []
        self.model_label = model_label
        self.system_prompt = system_prompt

    def execute(self) -> Dict[str, Any]:
        self.emit_progress(20, "AI 응답 생성 중...")
        sys_prompt = self.system_prompt or build_agent_system_prompt()
        history_lines: List[str] = []
        # 최근 대화 맥락(최대 8개) 포함
        for msg in self.history[-8:]:
            role_tag = "사용자" if msg.get("role") == "user" else "AI"
            txt = str(msg.get("content", "") or "").strip()
            if txt:
                history_lines.append(f"[{role_tag}]\n{txt}")
        if history_lines:
            full_prompt = (
                "[이전 대화 맥락]\n"
                + "\n\n".join(history_lines)
                + "\n\n[현재 질문]\n"
                + self.prompt
            )
        else:
            full_prompt = self.prompt

        client = MultiLlmClient()
        reply = client.generate(prompt=full_prompt, system_prompt=sys_prompt)
        self.emit_progress(100, "AI 응답 완료")
        return {
            "target_id": "general_chat",
            "mode": "api",
            "raw_response": reply,
            "model_name": self.model_label,
        }


class _ChatInputTextEdit(QTextEdit):
    """Enter로 전송, Shift+Enter로 줄바꿈하는 챗 입력창."""

    send_requested = pyqtSignal()

    def keyPressEvent(self, event: Optional[QKeyEvent]) -> None:
        if event is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self.send_requested.emit()
                return
        super().keyPressEvent(event)


class _ChatMessageCard(QFrame):
    """아코디언 접힘/펼침(기본 2줄), 호버 체류 자동 펼침 및 길이 비례 애니메이션을 지원하는 챗 메시지 카드."""

    HOVER_EXPAND_DELAY_MS = 1000
    HOVER_COLLAPSE_DELAY_MS = 150
    HOVER_WATCH_INTERVAL_MS = 120
    ANIM_MIN_DURATION_MS = 220
    ANIM_MAX_DURATION_MS = 450
    QWIDGETSIZE_MAX = 16777215

    edit_requested = pyqtSignal(int)
    copy_requested = pyqtSignal(str)
    toggled = pyqtSignal(bool)

    def __init__(
        self,
        message_id: int,
        role: str,
        content: str,
        exec_mode: str = "api",
        model_name: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.message_id = int(message_id)
        self.role = str(role or "user")
        self.content = str(content or "")
        self.exec_mode = str(exec_mode or "api")
        self.model_name = str(model_name or "")
        self.attachments = attachments or []
        self._expanded = False
        self._pinned_expanded = False

        is_user = self.role == "user"
        _apply_role(
            self,
            role="chatBubbleUser" if is_user else "chatBubbleAi",
            obj_name="ChatBubbleUser" if is_user else "ChatBubbleAi",
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        bg = "#EFF6FF" if is_user else "#F8FAFC"
        border = "#BFDBFE" if is_user else "#E2E8F0"
        self.setStyleSheet(
            f"QFrame#{self.objectName()} {{ "
            f"background-color: {bg}; border: 1px solid {border}; border-radius: 10px; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 6)
        lay.setSpacing(4)

        # 상단 메타 행 (발화자 + 첨부 요약)
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        lbl_who = QLabel("나" if is_user else "AI")
        lbl_who.setStyleSheet(
            "font-size: 10px; font-weight: 700; color: "
            + ("#1D4ED8;" if is_user else "#0F172A;")
        )
        hdr.addWidget(lbl_who)
        if self.attachments:
            lbl_att = QLabel(f"첨부 {len(self.attachments)}건")
            lbl_att.setStyleSheet(
                "font-size: 10px; font-weight: 600; color: #475569; "
                "background-color: #E2E8F0; border-radius: 4px; padding: 1px 5px;"
            )
            hdr.addWidget(lbl_att)
        hdr.addStretch()
        self.lbl_expand_hint = QLabel("펼치기")
        self.lbl_expand_hint.setStyleSheet("font-size: 10px; color: #94A3B8;")
        hdr.addWidget(self.lbl_expand_hint)
        lay.addLayout(hdr)

        # 본문 (기본 2줄 + 말줄임)
        self.lbl_content = QLabel()
        self.lbl_content.setWordWrap(True)
        self.lbl_content.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_content.setStyleSheet("font-size: 12px; color: #0F172A; line-height: 1.4;")
        self.lbl_content.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.lbl_content)

        # 하단 호버 액션 바 (기본 숨김)
        self.action_bar = QWidget(self)
        ab_lay = QHBoxLayout(self.action_bar)
        ab_lay.setContentsMargins(0, 2, 0, 0)
        ab_lay.setSpacing(4)

        if is_user:
            ab_lay.addStretch()
            self.btn_edit = QPushButton()
            self.btn_edit.setFixedSize(22, 22)
            self.btn_edit.setIcon(_render_inline_svg_icon(_SVG_EDIT_PENCIL, "#475569", 13))
            self.btn_edit.setIconSize(QSize(13, 13))
            self.btn_edit.setToolTip("질문 수정 후 재전송")
            self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_edit.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid transparent; border-radius: 4px; }"
                "QPushButton:hover { background: #DBEAFE; border-color: #93C5FD; }"
            )
            self.btn_edit.clicked.connect(lambda: self.edit_requested.emit(self.message_id))
            ab_lay.addWidget(self.btn_edit)

            self.btn_copy = QPushButton()
            self.btn_copy.setFixedSize(22, 22)
            self.btn_copy.setIcon(_render_inline_svg_icon(_SVG_COPY_ICON, "#475569", 13))
            self.btn_copy.setIconSize(QSize(13, 13))
            self.btn_copy.setToolTip("질문 복사")
            self.btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_copy.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid transparent; border-radius: 4px; }"
                "QPushButton:hover { background: #DBEAFE; border-color: #93C5FD; }"
            )
            self.btn_copy.clicked.connect(self._on_copy_clicked)
            ab_lay.addWidget(self.btn_copy)
        else:
            self.btn_copy = QPushButton()
            self.btn_copy.setFixedSize(22, 22)
            self.btn_copy.setIcon(_render_inline_svg_icon(_SVG_COPY_ICON, "#475569", 13))
            self.btn_copy.setIconSize(QSize(13, 13))
            self.btn_copy.setToolTip("응답 복사")
            self.btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_copy.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid transparent; border-radius: 4px; }"
                "QPushButton:hover { background: #E2E8F0; border-color: #CBD5E1; }"
            )
            self.btn_copy.clicked.connect(self._on_copy_clicked)
            ab_lay.addWidget(self.btn_copy)

            badge_text = self.model_name.strip() or (
                "수동 입력" if self.exec_mode == "manual" else "AI"
            )
            self.lbl_model_badge = QLabel(badge_text)
            self.lbl_model_badge.setStyleSheet(
                "font-size: 10px; font-weight: 600; color: #0369A1; "
                "background-color: #E0F2FE; border: 1px solid #BAE6FD; "
                "border-radius: 8px; padding: 1px 7px;"
            )
            ab_lay.addWidget(self.lbl_model_badge)
            ab_lay.addStretch()

        self.action_bar.setMaximumHeight(0)
        self.action_bar.setVisible(False)
        lay.addWidget(self.action_bar)

        self._hover_expand_timer = QTimer(self)
        self._hover_expand_timer.setSingleShot(True)
        self._hover_expand_timer.setInterval(self.HOVER_EXPAND_DELAY_MS)
        self._hover_expand_timer.timeout.connect(self._on_hover_expand_timeout)

        self._hover_collapse_timer = QTimer(self)
        self._hover_collapse_timer.setSingleShot(True)
        self._hover_collapse_timer.setInterval(self.HOVER_COLLAPSE_DELAY_MS)
        self._hover_collapse_timer.timeout.connect(self._on_hover_collapse_timeout)

        self._hover_watch_timer = QTimer(self)
        self._hover_watch_timer.setInterval(self.HOVER_WATCH_INTERVAL_MS)
        self._hover_watch_timer.timeout.connect(self._on_hover_watch_tick)

        self._content_anim = QPropertyAnimation(self.lbl_content, b"maximumHeight", self)
        self._content_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._action_bar_anim = QPropertyAnimation(self.action_bar, b"maximumHeight", self)
        self._action_bar_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim_group = QParallelAnimationGroup(self)
        self._anim_group.addAnimation(self._content_anim)
        self._anim_group.addAnimation(self._action_bar_anim)
        self._anim_group.finished.connect(self._on_content_anim_finished)

        self._refresh_display()

    def _on_copy_clicked(self) -> None:
        QApplication.clipboard().setText(self.content)
        self.copy_requested.emit(self.content)

    def is_expanded(self) -> bool:
        return self._expanded

    def is_pinned_expanded(self) -> bool:
        return self._pinned_expanded

    def _measure_content_heights(self) -> Tuple[int, int]:
        fm = QFontMetrics(self.lbl_content.font())
        two_line_h = max(24, fm.lineSpacing() * 2 + 4)
        avail_w = self.lbl_content.width() if self.lbl_content.width() > 40 else max(180, (self.width() or 270) - 24)
        if self.lbl_content.text() == self.content and self.lbl_content.hasHeightForWidth():
            rendered_h = self.lbl_content.heightForWidth(avail_w)
            if rendered_h > 0:
                return two_line_h, rendered_h
        full_rect = fm.boundingRect(0, 0, avail_w, 100000, Qt.TextFlag.TextWordWrap, self.content)
        full_h = max(two_line_h, full_rect.height() + 4)
        return two_line_h, full_h

    def _measure_action_bar_height(self) -> int:
        hint_h = self.action_bar.sizeHint().height()
        return max(24, hint_h if hint_h > 0 else 24)

    def _compute_anim_duration_ms(self, delta_px: int) -> int:
        dur = int(self.ANIM_MIN_DURATION_MS + abs(delta_px) * 0.45)
        return max(self.ANIM_MIN_DURATION_MS, min(self.ANIM_MAX_DURATION_MS, dur))

    def _apply_collapsed_elided_text(self) -> None:
        fm = QFontMetrics(self.lbl_content.font())
        avail_w = self.lbl_content.width() if self.lbl_content.width() > 40 else max(180, (self.width() or 270) - 24)
        elided, was_truncated = elide_text_to_lines(
            self.content, fm, avail_w, max_lines=2, font=self.lbl_content.font()
        )
        self.lbl_content.setText(elided)
        self.lbl_expand_hint.setText("펼치기" if was_truncated else "")

    def set_expanded(self, expanded: bool, *, animate: bool = True, show_action_bar: Optional[bool] = None) -> None:
        self._expanded = bool(expanded)
        want_ab = self._expanded if show_action_bar is None else bool(show_action_bar)
        if self._anim_group.state() == QParallelAnimationGroup.State.Running:
            self._anim_group.stop()

        two_line_h, _ = self._measure_content_heights()
        start_content_h = self.lbl_content.height() if self.lbl_content.height() > 0 else two_line_h
        start_ab_h = self.action_bar.height() if self.action_bar.isVisible() else 0

        if self._expanded:
            self.lbl_content.setMaximumHeight(start_content_h)
            self.lbl_content.setText(self.content)
            self.lbl_expand_hint.setText("접기")
            _, full_h = self._measure_content_heights()
            end_content_h = max(start_content_h, full_h)
        else:
            end_content_h = min(start_content_h, two_line_h)

        end_ab_h = self._measure_action_bar_height() if want_ab else 0
        delta_px = max(abs(end_content_h - start_content_h), abs(end_ab_h - start_ab_h))

        if animate and delta_px > 1:
            if want_ab:
                self.action_bar.setMaximumHeight(start_ab_h)
                self.action_bar.setVisible(True)
            dur = self._compute_anim_duration_ms(delta_px)
            self._content_anim.setDuration(dur)
            self._content_anim.setStartValue(start_content_h)
            self._content_anim.setEndValue(end_content_h)
            self._action_bar_anim.setDuration(dur)
            self._action_bar_anim.setStartValue(start_ab_h)
            self._action_bar_anim.setEndValue(end_ab_h)
            self._anim_group.start()
        else:
            if want_ab:
                self.action_bar.setVisible(True)
                self.action_bar.setMaximumHeight(self.QWIDGETSIZE_MAX)
            else:
                self.action_bar.setVisible(False)
                self.action_bar.setMaximumHeight(0)
            if self._expanded:
                self.lbl_content.setMaximumHeight(self.QWIDGETSIZE_MAX)
            else:
                self._apply_collapsed_elided_text()
                self.lbl_content.setMaximumHeight(two_line_h)

        self.toggled.emit(self._expanded)

    def _on_content_anim_finished(self) -> None:
        if self._expanded:
            self.lbl_content.setMaximumHeight(self.QWIDGETSIZE_MAX)
            self.action_bar.setMaximumHeight(self.QWIDGETSIZE_MAX)
        else:
            self.action_bar.setVisible(False)
            self.action_bar.setMaximumHeight(0)
            self._apply_collapsed_elided_text()
            two_line_h, _ = self._measure_content_heights()
            self.lbl_content.setMaximumHeight(two_line_h)

    def _refresh_display(self) -> None:
        if self._expanded:
            self.lbl_content.setText(self.content)
            self.lbl_expand_hint.setText("접기")
            self.lbl_content.setMaximumHeight(self.QWIDGETSIZE_MAX)
        else:
            two_line_h, _ = self._measure_content_heights()
            self._apply_collapsed_elided_text()
            self.lbl_content.setMaximumHeight(two_line_h)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._expanded and self._anim_group.state() != QParallelAnimationGroup.State.Running:
            self._refresh_display()

    def mouseReleaseEvent(self, event) -> None:
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self._hover_expand_timer.stop()
            self._hover_collapse_timer.stop()
            self._hover_watch_timer.stop()
            new_state = not self._expanded
            self._pinned_expanded = new_state
            self.set_expanded(new_state, animate=True, show_action_bar=new_state)
        super().mouseReleaseEvent(event)

    def _cursor_inside_card(self) -> bool:
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size()).contains(QCursor.pos())

    def _collapse_from_hover(self) -> None:
        self._hover_watch_timer.stop()
        if not self._pinned_expanded:
            if self._expanded or not self.action_bar.isHidden():
                self.set_expanded(False, animate=True, show_action_bar=False)

    def _on_hover_expand_timeout(self) -> None:
        if self._pinned_expanded:
            return
        if not self._expanded or self.action_bar.isHidden():
            self.set_expanded(True, animate=True, show_action_bar=True)
        self._hover_watch_timer.start(self.HOVER_WATCH_INTERVAL_MS)

    def _on_hover_collapse_timeout(self) -> None:
        if self._cursor_inside_card() or self._pinned_expanded:
            return
        self._collapse_from_hover()

    def _on_hover_watch_tick(self) -> None:
        if self._pinned_expanded or (not self._expanded and self.action_bar.isHidden()):
            self._hover_watch_timer.stop()
            return
        if not self._cursor_inside_card():
            self._hover_expand_timer.stop()
            self._hover_collapse_timer.stop()
            self._collapse_from_hover()

    def enterEvent(self, event) -> None:
        self._hover_collapse_timer.stop()
        if not self._pinned_expanded and (not self._expanded or self.action_bar.isHidden()):
            self._hover_expand_timer.start(self.HOVER_EXPAND_DELAY_MS)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_expand_timer.stop()
        if not self._cursor_inside_card() and not self._pinned_expanded:
            if self._expanded or not self.action_bar.isHidden():
                self._hover_collapse_timer.start(self.HOVER_COLLAPSE_DELAY_MS)
        super().leaveEvent(event)


class _SessionPopover(QFrame):
    """저장된 AI 챗 세션 목록 및 '+ 새 대화' 팝오버."""

    new_session_requested = pyqtSignal()
    session_selected = pyqtSignal(int)
    session_deleted = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("AiChatSessionPopover")
        self.setFixedWidth(290)
        self.setMaximumHeight(380)
        self.setStyleSheet(
            "QFrame#AiChatSessionPopover { "
            "background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; }"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        lbl_title = QLabel("대화 세션")
        lbl_title.setStyleSheet("font-size: 12px; font-weight: 700; color: #0F172A;")
        self.btn_new_session = QPushButton("+ 새 대화")
        _apply_role(self.btn_new_session, role="primary", obj_name="BtnNewChatSession")
        self.btn_new_session.setFixedHeight(26)
        self.btn_new_session.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_session.clicked.connect(self._on_new_clicked)
        hdr.addWidget(lbl_title)
        hdr.addStretch()
        hdr.addWidget(self.btn_new_session)
        lay.addLayout(hdr)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_container = QWidget()
        self.list_lay = QVBoxLayout(self.list_container)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(4)
        self.list_lay.addStretch()
        self.scroll.setWidget(self.list_container)
        lay.addWidget(self.scroll, stretch=1)

    def _on_new_clicked(self) -> None:
        self.hide()
        self.new_session_requested.emit()

    def populate(self, sessions: List[Dict[str, Any]], active_session_id: Optional[int]) -> None:
        while self.list_lay.count() > 1:
            item = self.list_lay.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()

        if not sessions:
            empty = QLabel("저장된 대화가 없습니다.")
            empty.setStyleSheet("font-size: 11px; color: #94A3B8; padding: 12px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.list_lay.insertWidget(0, empty)
            self.scroll.setMinimumHeight(68)
            self.resize(290, 128)
            return

        for sess in sessions:
            sid = int(sess["id"])
            is_active = active_session_id == sid
            row_frame = QFrame()
            row_frame.setMinimumHeight(44)
            row_frame.setStyleSheet(
                "QFrame { "
                + (
                    "background-color: #EFF6FF; border: 1px solid #93C5FD; border-radius: 6px;"
                    if is_active
                    else "background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 6px;"
                )
                + " }"
            )
            r_lay = QHBoxLayout(row_frame)
            r_lay.setContentsMargins(8, 6, 6, 6)
            r_lay.setSpacing(4)

            title = str(sess.get("title", "새 대화") or "새 대화")
            cnt = int(sess.get("message_count", 0) or 0)
            updated = str(sess.get("updated_at", "") or "")[:16]
            btn_sel = QPushButton(f"{title}\n{cnt}개 메시지 · {updated}", row_frame)
            btn_sel.setMinimumWidth(0)
            btn_sel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            btn_sel.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_sel.setStyleSheet(
                "QPushButton { text-align: left; background: transparent; border: none; padding: 0px; "
                f"font-size: 11px; font-weight: {'700' if is_active else '500'}; color: #0F172A; }}"
            )
            btn_sel.clicked.connect(lambda _c=False, s_id=sid: (self.hide(), self.session_selected.emit(s_id)))

            btn_del = QPushButton("×", row_frame)
            btn_del.setFixedSize(24, 24)
            btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_del.setToolTip("세션 삭제")
            btn_del.setStyleSheet(
                "QPushButton { padding: 0px; margin: 0px; min-width: 24px; max-width: 24px; "
                "min-height: 24px; max-height: 24px; color: #64748B; background: transparent; "
                "border: none; font-size: 14px; font-weight: bold; }"
                "QPushButton:hover { color: #EF4444; background-color: #FEE2E2; border-radius: 4px; }"
            )
            btn_del.clicked.connect(lambda _c=False, s_id=sid: self.session_deleted.emit(s_id))

            r_lay.addWidget(btn_sel, stretch=1)
            r_lay.addWidget(btn_del, stretch=0)
            self.list_lay.insertWidget(self.list_lay.count() - 1, row_frame)

        scroll_h = min(310, max(68, len(sessions) * 48 + 8))
        self.scroll.setMinimumHeight(scroll_h)
        self.resize(290, min(380, scroll_h + 62))


class _ModeOptionsPopover(QFrame):
    """API 엔진 설정/상태 및 Playwright 세션 옵션을 담은 컴팩트 팝오버."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("AiDockModeOptionsPopover")
        self.setFixedWidth(270)
        self.setStyleSheet(
            "QFrame#AiDockModeOptionsPopover { "
            "background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; }"
        )
        self.body_lay = QVBoxLayout(self)
        self.body_lay.setContentsMargins(10, 10, 10, 10)
        self.body_lay.setSpacing(8)


class _ManualExtraPasteDialog(QDialog):
    """수동 복붙 모드에서 추가 AI 응답(JSON Patch 등)을 붙여넣어 누적 적용하는 모달."""

    def __init__(self, parent: Optional[QWidget] = None, target_kind: str = "hwpx_agent"):
        super().__init__(parent)
        self._target_kind = str(target_kind or "hwpx_agent")
        self.setWindowTitle("추가 AI 응답 붙여넣기")
        self.resize(540, 400)
        self.setMinimumSize(420, 300)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        guide = (
            "외부 AI 채팅창에서 이어서 받은 추가 JSON Patch 응답을 아래에 붙여넣어 주세요.\n"
            "현재 문서 상태에 이어서 패치가 누적 적용됩니다."
            if self._target_kind == "hwpx_agent"
            else "외부 AI 채팅창에서 이어서 받은 추가 응답을 아래에 붙여넣어 주세요."
        )
        lbl_guide = QLabel(guide)
        lbl_guide.setWordWrap(True)
        lbl_guide.setStyleSheet("font-size: 12px; color: #334155;")
        lay.addWidget(lbl_guide)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        self.btn_paste_clip = QPushButton("클립보드 붙여넣기")
        self.btn_paste_clip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_paste_clip.clicked.connect(self._on_paste_clipboard)
        top_bar.addWidget(self.btn_paste_clip)

        self.lbl_verify = QLabel(
            "대기 중: JSON 응답을 붙여넣으면 자동 검증됩니다."
            if self._target_kind == "hwpx_agent"
            else ""
        )
        self.lbl_verify.setWordWrap(True)
        self.lbl_verify.setStyleSheet("font-size: 11px; color: #64748B;")
        top_bar.addWidget(self.lbl_verify, stretch=1)
        lay.addLayout(top_bar)

        self.txt_response = QTextEdit()
        self.txt_response.setPlaceholderText("여기에 AI 응답(JSON)을 붙여넣으세요...")
        self.txt_response.textChanged.connect(self._on_text_changed)
        lay.addWidget(self.txt_response, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        btn_apply = QPushButton("적용")
        _apply_role(btn_apply, role="primary")
        btn_apply.setDefault(True)
        btn_apply.clicked.connect(self._on_accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_apply)
        lay.addLayout(btn_row)

    def _on_paste_clipboard(self) -> None:
        clip = QApplication.clipboard().text()
        if clip:
            self.txt_response.setPlainText(clip)

    def _on_text_changed(self) -> None:
        if self._target_kind not in ("hwpx_agent", "calendar_todo"):
            return
        raw = self.txt_response.toPlainText().strip()
        if not raw:
            self.lbl_verify.setStyleSheet("font-size: 11px; color: #64748B;")
            self.lbl_verify.setText("대기 중: JSON 응답을 붙여넣으면 자동 검증됩니다.")
            return
        if self._target_kind == "calendar_todo":
            try:
                parsed_cal = parse_calendar_ai_response(raw)
                s_cnt = len(parsed_cal.get("schedules") or [])
                t_cnt = len(parsed_cal.get("todos") or [])
                self.lbl_verify.setStyleSheet("font-size: 11px; color: #15803D; font-weight: 600;")
                self.lbl_verify.setText(
                    f"유효한 일정·할일 JSON 확인됨 (일정 {s_cnt}건, 할일 {t_cnt}건)"
                )
            except Exception as exc:
                self.lbl_verify.setStyleSheet("font-size: 11px; color: #B91C1C;")
                self.lbl_verify.setText(f"JSON 추출 오류: {exc}")
            return
        try:
            parsed = extract_json_from_llm_response(raw)
            ops = parsed.get("operations", []) if isinstance(parsed, dict) else []
            ops_cnt = len(ops) if isinstance(ops, list) else 0
            self.lbl_verify.setStyleSheet("font-size: 11px; color: #15803D; font-weight: 600;")
            self.lbl_verify.setText(f"유효한 HWPX JSON Patch 확인됨 (연산 {ops_cnt}건)")
        except Exception as exc:
            self.lbl_verify.setStyleSheet("font-size: 11px; color: #B91C1C;")
            self.lbl_verify.setText(f"JSON 추출 오류: {exc}")

    def _on_accept(self) -> None:
        raw = self.txt_response.toPlainText().strip()
        if not raw:
            QMessageBox.warning(self, "응답 확인", "붙여넣을 AI 응답 텍스트를 입력해 주세요.")
            return
        if self._target_kind == "calendar_todo":
            try:
                parse_calendar_ai_response(raw)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "JSON 검증 오류",
                    f"유효한 일정·할일 JSON 응답을 추출하지 못했습니다:\n{exc}",
                )
                return
        elif self._target_kind == "hwpx_agent":
            try:
                extract_json_from_llm_response(raw)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "JSON 검증 오류",
                    f"유효한 JSON Patch 응답을 추출하지 못했습니다:\n{exc}",
                )
                return
        self.accept()

    def response_text(self) -> str:
        return self.txt_response.toPlainText().strip()


# =============================================================================
# 3모드 공통 타깃 실행 어댑터 규격 및 레지스트리
# -----------------------------------------------------------------------------
# 새 AI 기능을 추가할 때는 TargetExecutionAdapter를 상속한 어댑터를 정의하고
# _DEFAULT_TARGET_ADAPTERS에 target_id로 등록하면 API / 수동 복붙 / Playwright
# 3모드 공통 파이프라인(_execute_target_pipeline)이 자동으로 동작합니다.
# (배치 분할의 agg_step2, 다중 턴/코드모드의 hwpx_agent, 도구 루프의 general_chat은
#  전용 전략으로 분리 유지됩니다.)
# =============================================================================
class TargetExecutionAdapter:
    """단일·첨부 기반 AI 타깃을 API/수동 복붙/Playwright 3모드 공통 파이프라인에 연결하는 어댑터."""

    target_id: str = ""
    empty_prompt_warning: str = "실행할 입력이나 대상 내용을 먼저 확인해 주세요."

    def build_prompt(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> str:
        raise NotImplementedError

    def get_attachments(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> List[str]:
        raw = ctx.get("attachment_paths") or []
        return [str(p).strip() for p in raw if str(p).strip()]

    def pre_execute_hook(
        self, dock: "AiExecutionDock", ctx: Dict[str, Any], mode_idx: int
    ) -> bool:
        """실행 전 가로채기 훅. True를 반환하면 AI 호출을 생략하고 종료합니다."""
        return False

    def create_api_worker(
        self, dock: "AiExecutionDock", ctx: Dict[str, Any], info: Dict[str, Any]
    ) -> BaseWorker:
        raise NotImplementedError

    def parse_manual_or_pw_result(
        self,
        dock: "AiExecutionDock",
        raw_text: str,
        ctx: Dict[str, Any],
        *,
        mode: str,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def format_manual_verify_text(self, parsed_payload: Dict[str, Any]) -> str:
        return "유효한 AI 응답 확인됨"

    def post_api_result_hook(
        self,
        dock: "AiExecutionDock",
        res: Dict[str, Any],
        ctx: Dict[str, Any],
        info: Dict[str, Any],
    ) -> str:
        return "API 실행 완료"

    def on_pw_started_hook(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> None:
        pass


class _ClassNoticeDigestAdapter(TargetExecutionAdapter):
    target_id = "class_notice_digest"
    empty_prompt_warning = (
        "중앙 '4. 학급 공지·긴급 알림' 탭에서 카톡 대화 또는 요약할 공지를 먼저 입력·선택해 주세요."
    )

    def build_prompt(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> str:
        sys_p = str(ctx.get("system_prompt") or "").strip()
        user_p = str(ctx.get("user_prompt") or "").strip()
        if not user_p:
            return ""
        user_p = dock._append_quote_to_prompt(user_p)
        return f"[System 지시문]\n{sys_p}\n\n{user_p}" if sys_p else user_p

    def get_attachments(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> List[str]:
        return []

    def create_api_worker(
        self, dock: "AiExecutionDock", ctx: Dict[str, Any], info: Dict[str, Any]
    ) -> BaseWorker:
        usr_p = str(ctx.get("user_prompt") or "").strip()
        if not usr_p:
            raise ValueError(self.empty_prompt_warning)
        return _DockApiClassNoticeDigestWorker(
            system_prompt=str(ctx.get("system_prompt") or "").strip(),
            user_prompt=dock._append_quote_to_prompt(usr_p),
            source_label=str(ctx.get("source_label") or "카카오톡 대화"),
            source_type=str(ctx.get("source_type") or "kakao"),
            digest_action=str(ctx.get("digest_action") or "kakao_digest"),
            review_card_id=str(ctx.get("review_card_id") or ""),
        )

    def parse_manual_or_pw_result(
        self,
        dock: "AiExecutionDock",
        raw_text: str,
        ctx: Dict[str, Any],
        *,
        mode: str,
    ) -> Dict[str, Any]:
        src_label = str(ctx.get("source_label") or "카카오톡 대화")
        items = parse_notice_digest_ai_response(raw_text, default_source_label=src_label)
        if not items:
            raise ValueError("추출된 공지사항이 없습니다.")
        return {
            "items": items,
            "source_label": src_label,
            "source_type": str(ctx.get("source_type") or "kakao"),
            "digest_action": str(ctx.get("digest_action") or "kakao_digest"),
            "review_card_id": str(ctx.get("review_card_id") or ""),
            "raw_response": raw_text,
        }

    def format_manual_verify_text(self, parsed_payload: Dict[str, Any]) -> str:
        cnt = len(parsed_payload.get("items") or [])
        return f"유효한 공지 JSON 확인됨 ({cnt}건)"

    def post_api_result_hook(
        self,
        dock: "AiExecutionDock",
        res: Dict[str, Any],
        ctx: Dict[str, Any],
        info: Dict[str, Any],
    ) -> str:
        n_cnt = len(res.get("items") or [])
        return f"API 공지 요약 완료 · {n_cnt}건"


class _CalendarTodoAdapter(TargetExecutionAdapter):
    target_id = "calendar_todo"
    empty_prompt_warning = "상단 입력창에 일정 요청이나 공문·안내문 텍스트를 입력해 주세요."

    def pre_execute_hook(
        self, dock: "AiExecutionDock", ctx: Dict[str, Any], mode_idx: int
    ) -> bool:
        inst = dock.txt_calendar_instruction.toPlainText().strip()
        if not inst:
            QMessageBox.information(dock, "입력 확인", self.empty_prompt_warning)
            dock.txt_calendar_instruction.setFocus()
            return True
        is_system_doc_prompt = (
            inst.startswith("[원본 파일:") or "--- [추출된 학사일정 원문] ---" in inst
        )
        if not is_system_doc_prompt and dock._try_handle_direct_calendar_json(inst):
            return True
        return False

    def build_prompt(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> str:
        inst = dock.txt_calendar_instruction.toPlainText().strip()
        if not inst:
            return ""
        sys_p, user_p = build_calendar_ai_prompt(
            dock._append_quote_to_prompt(inst),
            schedule_context=str(ctx.get("schedule_context") or ""),
        )
        return f"[System 지시문]\n{sys_p}\n\n{user_p}"

    def create_api_worker(
        self, dock: "AiExecutionDock", ctx: Dict[str, Any], info: Dict[str, Any]
    ) -> BaseWorker:
        inst = dock.txt_calendar_instruction.toPlainText().strip()
        return _DockApiCalendarTodoWorker(
            instruction=dock._append_quote_to_prompt(inst),
            schedule_context=str(ctx.get("schedule_context") or ""),
        )

    def parse_manual_or_pw_result(
        self,
        dock: "AiExecutionDock",
        raw_text: str,
        ctx: Dict[str, Any],
        *,
        mode: str,
    ) -> Dict[str, Any]:
        inst = dock.txt_calendar_instruction.toPlainText().strip()
        try:
            parsed_cal = parse_calendar_ai_response(raw_text)
        except Exception:
            if is_calendar_query_intent(inst) and raw_text.strip():
                return {"is_query_answer": True, "raw_response": raw_text}
            raise
        s_cnt = len(parsed_cal.get("schedules") or [])
        t_cnt = len(parsed_cal.get("todos") or [])
        if s_cnt == 0 and t_cnt == 0:
            if is_calendar_query_intent(inst) and raw_text.strip():
                return {"is_query_answer": True, "raw_response": raw_text}
            raise ValueError("추출된 일정이나 할일이 없습니다.")
        return {"parsed_calendar": parsed_cal, "raw_response": raw_text}

    def format_manual_verify_text(self, parsed_payload: Dict[str, Any]) -> str:
        if parsed_payload.get("is_query_answer"):
            return "일정 조회/요약 일반 텍스트 응답 확인됨 (적용 시 채팅창에 표시)"
        p_cal = parsed_payload.get("parsed_calendar") or {}
        s_cnt = len(p_cal.get("schedules") or [])
        t_cnt = len(p_cal.get("todos") or [])
        return f"유효한 일정·할일 JSON 확인됨 (일정 {s_cnt}건, 할일 {t_cnt}건)"

    def post_api_result_hook(
        self,
        dock: "AiExecutionDock",
        res: Dict[str, Any],
        ctx: Dict[str, Any],
        info: Dict[str, Any],
    ) -> str:
        model_label = str(info.get("model") or info.get("provider") or "API")
        if res.get("is_query_answer"):
            reply_txt = str(res.get("raw_response") or "").strip()
            if reply_txt:
                dock._append_chat_message(
                    role="assistant",
                    content=reply_txt,
                    exec_mode="api",
                    model_name=model_label,
                    tool_name="calendar_todo",
                )
            return "API 일정 조회 답변 완료"
        p_cal = res.get("parsed_calendar") or {}
        s_cnt = len(p_cal.get("schedules") or [])
        t_cnt = len(p_cal.get("todos") or [])
        dock._append_chat_message(
            role="assistant",
            content=f"일정 {s_cnt}건, 할일 {t_cnt}건을 추출했습니다.",
            exec_mode="api",
            model_name=model_label,
            tool_name="calendar_todo",
        )
        return f"API 추출 완료 · 일정 {s_cnt}건, 할일 {t_cnt}건"

    def on_pw_started_hook(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> None:
        dock._append_chat_message(
            role="assistant",
            content="🌐 Playwright 브라우저 자동화를 시작했습니다. 웹 AI에 프롬프트를 전송하고 응답을 회수하는 중입니다...",
            exec_mode="playwright",
            tool_name="calendar_todo",
        )


class _NoticeOcrAdapter(TargetExecutionAdapter):
    target_id = "notice_ocr"
    empty_prompt_warning = "가정통신문 파일 또는 본문을 먼저 입력해 주세요."

    def build_prompt(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> str:
        notice_prompt = str(ctx.get("ocr_prompt", "") or "").strip()
        return dock._append_quote_to_prompt(notice_prompt) if notice_prompt else ""

    def get_attachments(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> List[str]:
        raw_atts = ctx.get("attachment_paths")
        if raw_atts is None:
            raw_atts = ctx.get("ocr_files") or []
        return [str(p).strip() for p in (raw_atts or []) if str(p).strip()]

    def create_api_worker(
        self, dock: "AiExecutionDock", ctx: Dict[str, Any], info: Dict[str, Any]
    ) -> BaseWorker:
        files = ctx.get("ocr_files") or []
        if not files:
            raise ValueError("API 보정을 실행할 가정통신문 파일을 먼저 선택해 주세요.")
        return _DockApiOcrWorker(
            file_paths=files,
            prompt_guide=dock._append_quote_to_prompt(str(ctx.get("ocr_prompt", ""))),
        )

    def parse_manual_or_pw_result(
        self,
        dock: "AiExecutionDock",
        raw_text: str,
        ctx: Dict[str, Any],
        *,
        mode: str,
    ) -> Dict[str, Any]:
        parsed = extract_json_from_llm_response(raw_text)
        return {
            "parsed_json": parsed,
            "raw_response": raw_text,
            "formatted_text": raw_text,
        }

    def format_manual_verify_text(self, parsed_payload: Dict[str, Any]) -> str:
        parsed = parsed_payload.get("parsed_json")
        keys = ", ".join(list(parsed.keys())[:4]) if isinstance(parsed, dict) else ""
        return f"JSON 검증 성공 (추출 키: {keys})"

    def post_api_result_hook(
        self,
        dock: "AiExecutionDock",
        res: Dict[str, Any],
        ctx: Dict[str, Any],
        info: Dict[str, Any],
    ) -> str:
        return f"API OCR 완료 ({res.get('count', 0)}개 파일)"


class _OcrAdapter(TargetExecutionAdapter):
    target_id = "ocr"
    empty_prompt_warning = "중앙 탭에서 문서 파일 또는 텍스트를 먼저 확인해 주세요."

    def build_prompt(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> str:
        from ui.tabs.tab_doc_ocr import extract_local_text_from_files

        files = ctx.get("ocr_files") or []
        raw_text = (
            extract_local_text_from_files(files)
            if files
            else str(ctx.get("ocr_current_text", ""))
        )
        sys_p, user_p = build_structured_prompt(
            task_key="ocr_correction",
            raw_content=raw_text,
            custom_instruction=dock._append_quote_to_prompt(str(ctx.get("ocr_prompt", ""))),
        )
        return f"[System 지시문]\n{sys_p}\n\n{user_p}"

    def get_attachments(self, dock: "AiExecutionDock", ctx: Dict[str, Any]) -> List[str]:
        raw_atts = ctx.get("attachment_paths")
        if raw_atts is None:
            raw_atts = ctx.get("ocr_files") or []
        return [str(p).strip() for p in (raw_atts or []) if str(p).strip()]

    def create_api_worker(
        self, dock: "AiExecutionDock", ctx: Dict[str, Any], info: Dict[str, Any]
    ) -> BaseWorker:
        files = ctx.get("ocr_files") or []
        if not files:
            raise ValueError("API 보정을 실행할 문서/이미지 파일을 중앙 탭에서 먼저 추가해 주세요.")
        return _DockApiOcrWorker(
            file_paths=files,
            prompt_guide=dock._append_quote_to_prompt(str(ctx.get("ocr_prompt", ""))),
        )

    def parse_manual_or_pw_result(
        self,
        dock: "AiExecutionDock",
        raw_text: str,
        ctx: Dict[str, Any],
        *,
        mode: str,
    ) -> Dict[str, Any]:
        parsed = extract_json_from_llm_response(raw_text)
        return {
            "parsed_json": parsed,
            "raw_response": raw_text,
            "formatted_text": str(parsed.get("corrected_text") or raw_text)
            if isinstance(parsed, dict)
            else raw_text,
        }

    def format_manual_verify_text(self, parsed_payload: Dict[str, Any]) -> str:
        parsed = parsed_payload.get("parsed_json")
        keys = ", ".join(list(parsed.keys())[:4]) if isinstance(parsed, dict) else ""
        return f"JSON 검증 성공 (추출 키: {keys})"

    def post_api_result_hook(
        self,
        dock: "AiExecutionDock",
        res: Dict[str, Any],
        ctx: Dict[str, Any],
        info: Dict[str, Any],
    ) -> str:
        return f"API OCR 완료 ({res.get('count', 0)}개 파일)"


_DEFAULT_TARGET_ADAPTERS: Dict[str, TargetExecutionAdapter] = {
    "class_notice_digest": _ClassNoticeDigestAdapter(),
    "calendar_todo": _CalendarTodoAdapter(),
    "notice_ocr": _NoticeOcrAdapter(),
    "ocr": _OcrAdapter(),
}


def detect_hwpx_clipboard_payload(raw_text: str) -> Optional[Dict[str, Any]]:
    """
    클립보드 텍스트가 유효한 HWPX JSON("operations" 포함) 또는 코드 모드 Python 함수(def edit_hwpx)인지 판별합니다.
    일반 문장 등 무관한 텍스트는 None을 반환합니다.
    """
    txt = str(raw_text or "").strip()
    if not txt:
        return None

    try:
        parsed = extract_json_from_llm_response(txt)
        if isinstance(parsed, dict) and isinstance(parsed.get("operations"), list):
            return {
                "kind": "json",
                "parsed": parsed,
                "ops_count": len(parsed["operations"]),
            }
    except Exception:
        pass

    if "def edit_hwpx" in txt:
        try:
            code = extract_python_code_from_llm_response(txt)
            if "def edit_hwpx" in code:
                validate_generated_code(code)
                return {
                    "kind": "code_mode",
                    "code": code,
                }
        except Exception:
            pass

    return None


class AiExecutionDock(QFrame):
    """EduFlow 우측 접이식 AI 실행 사이드바 위젯 (폭 340px)."""

    EXPANDED_WIDTH = 340
    TASK_DOCK_API = "ai_dock_api_exec"
    TASK_DOCK_PLAYWRIGHT = "ai_dock_playwright_exec"

    target_changed = pyqtSignal(str)
    go_to_settings_requested = pyqtSignal()
    execution_completed = pyqtSignal(str, dict)
    ai_mode_changed = pyqtSignal()
    sidepanel_mode_requested = pyqtSignal(bool)
    always_on_top_requested = pyqtSignal(bool)

    def __init__(
        self,
        task_runner: TaskRunner,
        parent: Optional[QWidget] = None,
        db: Optional[DatabaseManager] = None,
    ):
        super().__init__(parent)
        _apply_role(self, obj_name="AiExecutionDock")
        self.task_runner = task_runner
        self.db = db if db is not None else DatabaseManager()

        self._targets: Dict[str, Dict[str, Any]] = {}
        self._active_target_id: str = ""
        self._target_locked: bool = False
        self._last_context: Dict[str, Any] = {}
        self._last_result_excel_path: str = ""

        # 챗봇 세션 & 메시지 상태
        self._active_session_id: Optional[int] = None
        self._chat_attachments: List[Dict[str, Any]] = []
        self._editing_message_id: Optional[int] = None
        self._message_cards: List[_ChatMessageCard] = []
        self._manual_waiting_collect: bool = False
        self._pw_waiting_collect: bool = False
        self._pw_inline_card: Optional[QFrame] = None
        self._last_manual_prompt: str = ""
        self._chat_extra_instruction: str = ""
        self._chat_extra_attachments: List[Dict[str, Any]] = []

        # 수동 복붙 상태
        self._manual_batches: List[Dict[str, Any]] = []
        self._manual_parsed_items: List[Dict[str, str]] = []
        self._manual_single_parsed: Dict[str, Any] = {}
        self._manual_provider: str = "gemini"
        self._manual_is_grading: bool = False

        # Playwright 상태
        self._pw_provider: str = "gemini"
        self._active_pw_worker: Optional[WebGeminiAutoWorker] = None
        self._external_login_worker: Any = None
        self._external_login_provider: str = ""
        self._is_api_running: bool = False
        self._pw_single_batches_applied: bool = False

        # HWPX 참고 문서 청크 상태
        self._hwpx_ref_chunks: List[Dict[str, Any]] = []
        self._hwpx_ref_signature: Tuple[str, ...] = ()
        self._hwpx_ref_error: str = ""
        self._hwpx_ref_index_summary: Dict[str, Any] = {}
        self._hwpx_selected_chunk_ids: List[str] = []
        self._hwpx_pending_clarification: Optional[Dict[str, Any]] = None
        self._hwpx_extra_chunk_round: int = 0
        self._pw_hwpx_stage1_pending: bool = False
        self._pw_next_stage_pending: bool = False

        # 미리보기 선택 인용 상태
        self._quoted_text: str = ""
        self._quoted_target_id: str = ""

        # 사이드패널 모드 & 수동 복붙 클립보드 자동 감지 상태
        self._sidepanel_mode: bool = False
        self._always_on_top: bool = False
        self._panel_root: Optional[QFrame] = None
        self._last_clipboard_payload_hash: str = ""
        self._detected_clipboard_payload: Optional[Dict[str, Any]] = None
        self._detected_clipboard_text: str = ""

        self.setMinimumWidth(0)
        self.setMaximumWidth(0)

        self._init_ui()
        self._register_default_general_chat_target()
        self._init_or_restore_session()
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
        self._panel_root = panel

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

        # 단축키 호환용 숨김 토글 버튼 유지 (MainWindow 단축키 연동)
        self.btn_lock_target = QPushButton("타겟 고정 OFF", target_header)
        self.btn_lock_target.setCheckable(True)
        self.btn_lock_target.setVisible(False)
        self.btn_lock_target.toggled.connect(self._on_lock_toggled)

        # 정사각형 테두리 + 평행선 3개 가운데 정렬 SVG 세션 아이콘 버튼
        self.btn_session_menu = QPushButton()
        _apply_role(self.btn_session_menu, role="sessionMenuBtn", obj_name="AiDockSessionBtn")
        self.btn_session_menu.setFixedSize(28, 28)
        self.btn_session_menu.setIcon(_render_inline_svg_icon(_SVG_SESSION_MENU, "#334155", 16))
        self.btn_session_menu.setIconSize(QSize(16, 16))
        self.btn_session_menu.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_session_menu.setToolTip("저장된 대화 세션 목록 / + 새 대화")
        self.btn_session_menu.setStyleSheet(
            "QPushButton#AiDockSessionBtn { background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 6px; }"
            "QPushButton#AiDockSessionBtn:hover { border-color: #0B4DCB; background-color: #EFF6FF; }"
        )
        self.btn_session_menu.clicked.connect(self._on_session_menu_clicked)

        self.btn_sidepanel_mode = QPushButton("사이드패널 모드")
        _apply_role(self.btn_sidepanel_mode, role="outline", obj_name="AiDockSidepanelBtn")
        self.btn_sidepanel_mode.setCheckable(True)
        self.btn_sidepanel_mode.setFixedHeight(26)
        self.btn_sidepanel_mode.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sidepanel_mode.setToolTip("화면 오른쪽 끝 420px 미니 사이드패널 모드로 전환")
        self.btn_sidepanel_mode.toggled.connect(self.sidepanel_mode_requested.emit)

        self.btn_always_on_top = QPushButton("항상 위에")
        _apply_role(self.btn_always_on_top, role="outline", obj_name="AiDockPinBtn")
        self.btn_always_on_top.setCheckable(True)
        self.btn_always_on_top.setFixedHeight(26)
        self.btn_always_on_top.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_always_on_top.setToolTip("창을 다른 프로그램(한/글 등) 위에 항상 고정")
        self.btn_always_on_top.toggled.connect(self.always_on_top_requested.emit)

        self._session_popover = _SessionPopover(self)
        self._session_popover.new_session_requested.connect(self.create_new_session)
        self._session_popover.session_selected.connect(self.load_session)
        self._session_popover.session_deleted.connect(self._on_delete_session_requested)

        th_top_row.addWidget(lbl_target_cap)
        th_top_row.addStretch()
        th_top_row.addWidget(self.btn_sidepanel_mode)
        th_top_row.addWidget(self.btn_always_on_top)
        th_top_row.addWidget(self.btn_session_menu)
        th_lay.addLayout(th_top_row)

        self.combo_target_selector = QComboBox()
        _apply_role(self.combo_target_selector, obj_name="ComboAiDockTarget")
        self.combo_target_selector.setFixedHeight(30)
        self.combo_target_selector.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.combo_target_selector.currentIndexChanged.connect(self._on_target_combo_changed)
        th_lay.addWidget(self.combo_target_selector)

        # 사이드패널 모드 전용 1줄 작업 파일명 배지
        self.lbl_sidepanel_file_badge = QLabel("")
        self.lbl_sidepanel_file_badge.setObjectName("SidepanelFileBadge")
        self.lbl_sidepanel_file_badge.setStyleSheet(
            "QLabel#SidepanelFileBadge { "
            "background-color: #EFF6FF; color: #1E40AF; border: 1px solid #BFDBFE; "
            "border-radius: 5px; padding: 4px 8px; font-size: 11.5px; font-weight: 700; }"
        )
        self.lbl_sidepanel_file_badge.setVisible(False)
        th_lay.addWidget(self.lbl_sidepanel_file_badge)

        root_lay.addWidget(target_header)

        # ---------------------------------------------------------------------
        # [2단] 3-모드 세그먼트 컨트롤 + ⚙ 옵션 팝오버 버튼
        # ---------------------------------------------------------------------
        seg_row = QHBoxLayout()
        seg_row.setContentsMargins(0, 0, 0, 0)
        seg_row.setSpacing(6)

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

        self.btn_mode_options = QPushButton()
        _apply_role(self.btn_mode_options, role="outline", obj_name="AiDockModeOptionsBtn")
        self.btn_mode_options.setFixedSize(32, 32)
        self.btn_mode_options.setIcon(_render_inline_svg_icon(_SVG_SETTINGS_GEAR, "#475569", 15))
        self.btn_mode_options.setIconSize(QSize(15, 15))
        self.btn_mode_options.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mode_options.setToolTip("API 엔진 상태 · Playwright 세션 옵션")
        self.btn_mode_options.clicked.connect(self._on_mode_options_clicked)

        self._build_mode_options_popover()

        seg_row.addWidget(seg_frame, stretch=1)
        seg_row.addWidget(self.btn_mode_options)
        root_lay.addLayout(seg_row)

        # [2.2단] 수동 복붙 모드 클립보드 AI 응답 감지 원클릭 배너 (기본 숨김)
        self.clipboard_detect_banner = QFrame(panel)
        self.clipboard_detect_banner.setObjectName("ClipboardDetectBanner")
        self.clipboard_detect_banner.setStyleSheet(
            "QFrame#ClipboardDetectBanner { "
            "background-color: #ECFDF5; border: 1px solid #10B981; border-radius: 6px; }"
        )
        cdb_lay = QHBoxLayout(self.clipboard_detect_banner)
        cdb_lay.setContentsMargins(8, 6, 6, 6)
        cdb_lay.setSpacing(6)
        self.lbl_clipboard_detect = QLabel("클립보드에서 AI 응답 감지됨")
        self.lbl_clipboard_detect.setWordWrap(True)
        self.lbl_clipboard_detect.setStyleSheet("color: #065F46; font-size: 11px; font-weight: 700;")
        self.btn_apply_detected_clipboard = QPushButton("바로 적용")
        _apply_role(self.btn_apply_detected_clipboard, role="primary", obj_name="BtnApplyDetectedClipboard")
        self.btn_apply_detected_clipboard.setFixedHeight(24)
        self.btn_apply_detected_clipboard.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply_detected_clipboard.clicked.connect(self._on_apply_detected_clipboard_clicked)
        self.btn_dismiss_clipboard_banner = QPushButton("✕")
        self.btn_dismiss_clipboard_banner.setFixedSize(22, 22)
        self.btn_dismiss_clipboard_banner.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dismiss_clipboard_banner.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #047857; font-weight: 700; }"
            "QPushButton:hover { color: #064E3B; }"
        )
        self.btn_dismiss_clipboard_banner.clicked.connect(self._dismiss_clipboard_detect_banner)
        cdb_lay.addWidget(self.lbl_clipboard_detect, stretch=1)
        cdb_lay.addWidget(self.btn_apply_detected_clipboard)
        cdb_lay.addWidget(self.btn_dismiss_clipboard_banner)
        self.clipboard_detect_banner.setVisible(False)
        root_lay.addWidget(self.clipboard_detect_banner)

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

        # [2.5단-B] 캘린더 & 할일 전용 지시사항/공문 입력창 (calendar_todo 타겟일 때만 표시)
        self.calendar_instruction_card = QFrame(panel)
        _apply_role(self.calendar_instruction_card, role="subCard")
        cal_lay = QVBoxLayout(self.calendar_instruction_card)
        cal_lay.setContentsMargins(10, 8, 10, 8)
        cal_lay.setSpacing(6)
        lbl_cal_inst = QLabel("일정·할일 추출 지시사항 / 공문·안내문")
        _apply_role(lbl_cal_inst, role="fieldLabel")
        self.txt_calendar_instruction = QTextEdit()
        _apply_role(self.txt_calendar_instruction, obj_name="CalendarAiInputBox")
        self.txt_calendar_instruction.setMinimumHeight(80)
        self.txt_calendar_instruction.setMaximumHeight(130)
        self.txt_calendar_instruction.setPlaceholderText(
            "예: 내일 오전 10시 교무회의, 11월 25일까지 2학년 수행평가 채점 완료\n"
            "또는 공문·안내문 본문을 그대로 붙여넣으세요."
        )
        cal_lay.addWidget(lbl_cal_inst)
        cal_lay.addWidget(self.txt_calendar_instruction)
        self.calendar_instruction_card.setVisible(False)
        root_lay.addWidget(self.calendar_instruction_card)

        # ---------------------------------------------------------------------
        # [3단] 모드별 실행 컨트롤 영역 (QStackedWidget in QScrollArea)
        # ---------------------------------------------------------------------
        self.body_scroll = QScrollArea(panel)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setMaximumHeight(220)

        self.mode_stack = QStackedWidget(self.body_scroll)
        _apply_role(self.mode_stack, obj_name="AiDockModeStack")

        self.mode_stack.addWidget(self._build_api_mode_page())
        self.mode_stack.addWidget(self._build_manual_mode_page())
        self.mode_stack.addWidget(self._build_playwright_mode_page())
        self.mode_stack.setCurrentIndex(1)

        self.body_scroll.setWidget(self.mode_stack)
        root_lay.addWidget(self.body_scroll)

        # ---------------------------------------------------------------------
        # [3.5단] 챗봇 대화 타임라인 (API / 수동 복붙 / Playwright 공용 누적)
        # ---------------------------------------------------------------------
        self.chat_scroll = QScrollArea(panel)
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_scroll.setMinimumHeight(140)
        _apply_role(self.chat_scroll, obj_name="AiChatTimelineScroll")

        self.chat_timeline_widget = QWidget()
        _apply_role(self.chat_timeline_widget, obj_name="AiChatTimelineBody")
        self.chat_timeline_lay = QVBoxLayout(self.chat_timeline_widget)
        self.chat_timeline_lay.setContentsMargins(2, 4, 2, 4)
        self.chat_timeline_lay.setSpacing(8)

        self.lbl_chat_empty = QLabel(
            "대화를 시작해 보세요.\n질문을 입력하거나 파일·폴더를 첨부할 수 있습니다."
        )
        self.lbl_chat_empty.setWordWrap(True)
        self.lbl_chat_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_chat_empty.setStyleSheet("color: #94A3B8; font-size: 11px; padding: 18px 8px;")
        self.chat_timeline_lay.addWidget(self.lbl_chat_empty)
        self.chat_timeline_lay.addStretch()

        self.manual_extra_paste_bar = QWidget(self.chat_timeline_widget)
        mep_lay = QHBoxLayout(self.manual_extra_paste_bar)
        mep_lay.setContentsMargins(0, 0, 0, 2)
        mep_lay.setSpacing(6)
        self.btn_manual_extra_paste = QPushButton("+ 추가 답변 붙여넣기")
        self.btn_manual_extra_paste.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_extra_paste.setMinimumHeight(26)
        self.btn_manual_extra_paste.setStyleSheet(
            "QPushButton { background-color: #FFFFFF; border: 1px solid #CBD5E1; "
            "border-radius: 6px; color: #334155; font-size: 11px; font-weight: 600; padding: 3px 10px; }"
            "QPushButton:hover { background-color: #F1F5F9; border-color: #94A3B8; color: #0F172A; }"
        )
        self.btn_manual_extra_paste.clicked.connect(self._on_manual_extra_paste_clicked)
        mep_lay.addWidget(self.btn_manual_extra_paste)

        self.btn_manual_open_web = QPushButton("🌐 웹 AI 열기")
        self.btn_manual_open_web.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_open_web.setMinimumHeight(26)
        self.btn_manual_open_web.setStyleSheet(
            "QPushButton { background-color: #EFF6FF; border: 1px solid #93C5FD; "
            "border-radius: 6px; color: #1D4ED8; font-size: 11px; font-weight: 600; padding: 3px 10px; }"
            "QPushButton:hover { background-color: #DBEAFE; border-color: #60A5FA; color: #1E3A8A; }"
        )
        self.btn_manual_open_web.clicked.connect(self._on_manual_open_web_clicked)
        mep_lay.addWidget(self.btn_manual_open_web)
        mep_lay.addStretch()
        self.manual_extra_paste_bar.setVisible(False)

        self.chat_scroll.setWidget(self.chat_timeline_widget)
        root_lay.addWidget(self.chat_scroll, stretch=1)

        # ---------------------------------------------------------------------
        # [3.8단] 하단 챗 입력칸 + 파일/폴더 첨부 칩 + 드래그앤드롭
        # ---------------------------------------------------------------------
        self.chat_input_card = QFrame(panel)
        _apply_role(self.chat_input_card, role="subCard", obj_name="AiChatInputCard")
        ci_lay = QVBoxLayout(self.chat_input_card)
        ci_lay.setContentsMargins(8, 8, 8, 8)
        ci_lay.setSpacing(6)

        # 수정-재전송 분기 배너 (기본 숨김)
        self.edit_branch_banner = QFrame(self.chat_input_card)
        self.edit_branch_banner.setStyleSheet(
            "QFrame { background-color: #FEF3C7; border: 1px solid #F59E0B; border-radius: 6px; }"
        )
        eb_lay = QHBoxLayout(self.edit_branch_banner)
        eb_lay.setContentsMargins(8, 4, 6, 4)
        eb_lay.setSpacing(6)
        lbl_eb = QLabel("질문 수정 중 (재전송 시 이후 대화 덮어씀)")
        lbl_eb.setStyleSheet("color: #92400E; font-size: 10.5px; font-weight: 600;")
        self.btn_cancel_edit = QPushButton("취소")
        self.btn_cancel_edit.setFixedHeight(20)
        self.btn_cancel_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel_edit.setStyleSheet(
            "QPushButton { background: #FFFFFF; border: 1px solid #D97706; border-radius: 4px; "
            "color: #92400E; font-size: 10px; padding: 0 6px; }"
        )
        self.btn_cancel_edit.clicked.connect(self._cancel_edit_message)
        eb_lay.addWidget(lbl_eb, stretch=1)
        eb_lay.addWidget(self.btn_cancel_edit)
        self.edit_branch_banner.setVisible(False)
        ci_lay.addWidget(self.edit_branch_banner)

        # 첨부 칩 목록 컨테이너
        self.attachment_chips_widget = QWidget(self.chat_input_card)
        self.attachment_chips_lay = QVBoxLayout(self.attachment_chips_widget)
        self.attachment_chips_lay.setContentsMargins(0, 0, 0, 0)
        self.attachment_chips_lay.setSpacing(4)
        self.attachment_chips_widget.setVisible(False)
        ci_lay.addWidget(self.attachment_chips_widget)

        # 직접 URL 입력란 (수동 복붙 / Playwright 자동에서 '직접 URL' 선택 시에만 표시)
        self.edit_chat_custom_url = QLineEdit(self.chat_input_card)
        _apply_role(self.edit_chat_custom_url, obj_name="EditChatCustomUrl")
        self.edit_chat_custom_url.setFixedHeight(28)
        self.edit_chat_custom_url.setPlaceholderText("https://... 접속할 웹 AI URL 입력")
        self.edit_chat_custom_url.textChanged.connect(self._on_chat_custom_url_changed)
        self.edit_chat_custom_url.setVisible(False)
        self.edit_manual_custom_url = self.edit_chat_custom_url
        self.edit_pw_custom_url = self.edit_chat_custom_url
        ci_lay.addWidget(self.edit_chat_custom_url)

        self.txt_chat_input = _ChatInputTextEdit(self.chat_input_card)
        _apply_role(self.txt_chat_input, obj_name="AiChatInputBox")
        self.txt_chat_input.setMinimumHeight(52)
        self.txt_chat_input.setMaximumHeight(92)
        self.txt_chat_input.setPlaceholderText(
            "메시지 입력 (Enter 전송 · Shift+Enter 줄바꿈)\n파일/폴더를 여기로 드래그앤드롭할 수 있습니다."
        )
        self.txt_chat_input.send_requested.connect(self._on_chat_send_clicked)
        enable_file_drop(
            self.txt_chat_input,
            allowed_exts=None,
            allow_dirs=True,
            allow_files=True,
            on_files_dropped=self._on_chat_paths_dropped,
            context_label="챗 첨부 파일/폴더",
            highlight_target=self.chat_input_card,
        )
        enable_file_drop(
            self.chat_input_card,
            allowed_exts=None,
            allow_dirs=True,
            allow_files=True,
            on_files_dropped=self._on_chat_paths_dropped,
            context_label="챗 첨부 파일/폴더",
            highlight_target=self.chat_input_card,
        )
        ci_lay.addWidget(self.txt_chat_input)

        ci_btn_row = QHBoxLayout()
        ci_btn_row.setSpacing(3)

        self.btn_attach_file = QPushButton()
        _apply_role(self.btn_attach_file, role="outline", obj_name="BtnChatAttachFile")
        self.btn_attach_file.setFixedSize(28, 28)
        self.btn_attach_file.setIcon(_render_inline_svg_icon(_SVG_PAPERCLIP, "#475569", 14))
        self.btn_attach_file.setIconSize(QSize(14, 14))
        self.btn_attach_file.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_attach_file.setToolTip("파일 첨부")
        self.btn_attach_file.clicked.connect(self._on_attach_file_clicked)

        self.btn_attach_folder = QPushButton()
        _apply_role(self.btn_attach_folder, role="outline", obj_name="BtnChatAttachFolder")
        self.btn_attach_folder.setFixedSize(28, 28)
        self.btn_attach_folder.setIcon(_render_inline_svg_icon(_SVG_FOLDER, "#475569", 14))
        self.btn_attach_folder.setIconSize(QSize(14, 14))
        self.btn_attach_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_attach_folder.setToolTip("폴더 첨부 (경로 및 하위 파일 목록 컨텍스트 포함)")
        self.btn_attach_folder.clicked.connect(self._on_attach_folder_clicked)

        self.combo_web_provider = QComboBox(self.chat_input_card)
        _apply_role(self.combo_web_provider, obj_name="ComboChatWebProvider")
        self.combo_web_provider.setFixedHeight(28)
        self.combo_web_provider.setMaximumWidth(104)
        self.combo_web_provider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for p_key, p_lbl in (
            ("gemini", "Gemini"),
            ("chatgpt", "ChatGPT"),
            ("claude", "Claude"),
            ("custom", "직접 URL"),
        ):
            self.combo_web_provider.addItem(p_lbl, p_key)
        self.combo_web_provider.currentIndexChanged.connect(self._on_chat_web_provider_changed)

        self.lbl_chat_api_badge = QLabel("Gemini")
        self.lbl_chat_api_badge.setStyleSheet(
            "font-size: 10.5px; font-weight: 600; color: #0369A1; "
            "background-color: #E0F2FE; border: 1px solid #BAE6FD; "
            "border-radius: 6px; padding: 2px 7px;"
        )
        self.lbl_chat_api_badge.setVisible(False)

        self.btn_chat_send = QPushButton("전송")
        _apply_role(self.btn_chat_send, role="primary", obj_name="BtnChatSend")
        self.btn_chat_send.setFixedHeight(28)
        self.btn_chat_send.setMinimumWidth(56)
        self.btn_chat_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_chat_send.clicked.connect(self._on_chat_send_clicked)

        self.btn_chat_cancel_collect = QPushButton("×")
        _apply_role(self.btn_chat_cancel_collect, role="outline", obj_name="BtnChatCancelCollect")
        self.btn_chat_cancel_collect.setFixedSize(24, 28)
        self.btn_chat_cancel_collect.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_chat_cancel_collect.setToolTip("응답 회수 대기 취소")
        self.btn_chat_cancel_collect.setVisible(False)
        self.btn_chat_cancel_collect.clicked.connect(self._cancel_manual_collect_waiting)

        ci_btn_row.addWidget(self.btn_attach_file)
        ci_btn_row.addWidget(self.btn_attach_folder)
        ci_btn_row.addStretch()
        ci_btn_row.addWidget(self.lbl_chat_api_badge)
        ci_btn_row.addWidget(self.combo_web_provider)
        ci_btn_row.addWidget(self.btn_chat_send)
        ci_btn_row.addWidget(self.btn_chat_cancel_collect)
        ci_lay.addLayout(ci_btn_row)

        # 수동 복붙 모드 분할 배치 바 (하단 입력창 바로 위, 다중 배치 시에만 표시)
        self.batch_bar_widget = QFrame(panel)
        _apply_role(self.batch_bar_widget, role="subCard", obj_name="AiBatchBar")
        bb_lay = QHBoxLayout(self.batch_bar_widget)
        bb_lay.setContentsMargins(8, 5, 8, 5)
        bb_lay.setSpacing(6)

        self.combo_manual_batch = QComboBox(self.batch_bar_widget)
        _apply_role(self.combo_manual_batch, obj_name="ComboDockManualBatch")
        self.combo_manual_batch.setFixedHeight(26)
        self.combo_manual_batch.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.combo_manual_batch.currentIndexChanged.connect(self._on_manual_batch_changed)

        self.btn_batch_copy = QPushButton("현재 배치 프롬프트 복사", self.batch_bar_widget)
        _apply_role(self.btn_batch_copy, role="outline", obj_name="BtnBatchCopyPrompt")
        self.btn_batch_copy.setFixedHeight(26)
        self.btn_batch_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_batch_copy.clicked.connect(self._on_manual_copy_only)

        self.lbl_batch_progress = QLabel("0/0", self.batch_bar_widget)
        _apply_role(self.lbl_batch_progress, role="mutedText")
        self.lbl_batch_progress.setStyleSheet("font-size: 11px; font-weight: 600;")

        bb_lay.addWidget(self.combo_manual_batch, stretch=1)
        bb_lay.addWidget(self.btn_batch_copy)
        bb_lay.addWidget(self.lbl_batch_progress)
        self.batch_bar_widget.setVisible(False)
        root_lay.addWidget(self.batch_bar_widget)

        root_lay.addWidget(self.chat_input_card)

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

        ft_lay.addWidget(self.progress_dock)
        ft_lay.addWidget(self.lbl_dock_progress_meta)
        root_lay.addWidget(footer)

        clip_scroll.setWidget(panel)
        outer_lay.addWidget(clip_scroll)

    # =========================================================================
    # 모드 옵션 팝오버 (API 엔진 / Playwright 세션 체크박스)
    # =========================================================================
    def _build_mode_options_popover(self) -> None:
        self._mode_options_popover = _ModeOptionsPopover(self)
        lay = self._mode_options_popover.body_lay

        lbl_api_cap = QLabel("연결 API 엔진")
        _apply_role(lbl_api_cap, role="fieldLabel")
        self.lbl_api_model_badge = QLabel("확인 중...")
        self.lbl_api_model_badge.setWordWrap(True)
        _apply_role(self.lbl_api_model_badge, role="statusBanner")

        link_row = QHBoxLayout()
        link_row.setSpacing(6)
        self.btn_api_go_settings = QPushButton("환경설정 이동")
        _apply_role(self.btn_api_go_settings, role="secondary")
        self.btn_api_go_settings.setFixedHeight(28)
        self.btn_api_go_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_api_go_settings.clicked.connect(
            lambda: (self._mode_options_popover.hide(), self.go_to_settings_requested.emit())
        )

        self.btn_api_refresh = QPushButton("상태 갱신")
        _apply_role(self.btn_api_refresh, role="outline")
        self.btn_api_refresh.setFixedHeight(28)
        self.btn_api_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_api_refresh.clicked.connect(self.refresh_ai_info)

        link_row.addWidget(self.btn_api_go_settings, stretch=1)
        link_row.addWidget(self.btn_api_refresh)

        lay.addWidget(lbl_api_cap)
        lay.addWidget(self.lbl_api_model_badge)
        lay.addLayout(link_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #E2E8F0;")
        lay.addWidget(sep)

        lbl_pw_cap = QLabel("Playwright 브라우저 옵션")
        _apply_role(lbl_pw_cap, role="fieldLabel")
        self.lbl_pw_exec_mode = QLabel("실행 방식")
        _apply_role(self.lbl_pw_exec_mode, role="mutedText")
        self.combo_pw_exec_mode = QComboBox()
        _apply_role(self.combo_pw_exec_mode, obj_name="ComboPlaywrightExecMode")
        self.combo_pw_exec_mode.setFixedHeight(28)
        self.combo_pw_exec_mode.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.combo_pw_exec_mode.addItem("완전 자동: 배치 무인 반복", "auto")
        self.combo_pw_exec_mode.addItem("반자동: 프롬프트 자동 입력 · 수동 전송", "semi_auto")

        self.chk_pw_keep_login = QCheckBox("로그인 세션 유지")
        self.chk_pw_keep_login.setChecked(True)
        self.chk_pw_temp_chat = QCheckBox("임시 채팅으로 실행")
        self.chk_pw_temp_chat.setChecked(True)

        lay.addWidget(lbl_pw_cap)
        lay.addWidget(self.lbl_pw_exec_mode)
        lay.addWidget(self.combo_pw_exec_mode)
        lay.addWidget(self.chk_pw_keep_login)
        lay.addWidget(self.chk_pw_temp_chat)

    def _on_mode_options_clicked(self) -> None:
        btn_pos = self.btn_mode_options.mapToGlobal(self.btn_mode_options.rect().bottomRight())
        pop_x = max(0, btn_pos.x() - self._mode_options_popover.width())
        pop_y = btn_pos.y() + 4
        self._mode_options_popover.move(pop_x, pop_y)
        self._mode_options_popover.show()

    # =========================================================================
    # Page 0: API 모드
    # =========================================================================
    def _build_api_mode_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(8)

        self.api_summary_card = QFrame()
        _apply_role(self.api_summary_card, role="subCard")
        sc_lay = QVBoxLayout(self.api_summary_card)
        sc_lay.setContentsMargins(10, 8, 10, 8)
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
        lay.addWidget(self.api_summary_card)

        self.btn_api_start = QPushButton("API 일괄 생성 시작")
        _apply_role(self.btn_api_start, role="primary", obj_name="BtnDockApiStart")
        self.btn_api_start.setFixedHeight(34)
        self.btn_api_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_api_start.clicked.connect(self._on_api_start_clicked)

        self.btn_api_stop = QPushButton("작업 중지")
        _apply_role(self.btn_api_stop, role="danger", obj_name="BtnDockApiStop")
        self.btn_api_stop.setFixedHeight(32)
        self.btn_api_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_api_stop.setEnabled(False)
        self.btn_api_stop.setVisible(False)
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

        self.lbl_manual_batch_meta = QLabel("대상 엑셀 파일을 선택하면 배치가 생성됩니다.")
        self.lbl_manual_batch_meta.setWordWrap(True)
        _apply_role(self.lbl_manual_batch_meta, role="mutedText")

        mb_lay.addLayout(split_row)
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

        # 특수 타겟용 프롬프트 복사 버튼 행 (범용 챗에서는 숨김)
        self.manual_copy_card = QFrame()
        _apply_role(self.manual_copy_card, role="subCard")
        mc_outer_lay = QVBoxLayout(self.manual_copy_card)
        mc_outer_lay.setContentsMargins(8, 6, 8, 6)
        mc_outer_lay.setSpacing(6)
        mc_copy_lay = QHBoxLayout()
        mc_copy_lay.setContentsMargins(0, 0, 0, 0)
        mc_copy_lay.setSpacing(6)

        self.btn_manual_copy_open = QPushButton("프롬프트 복사 & 웹 열기")
        _apply_role(self.btn_manual_copy_open, role="primary", obj_name="BtnDockManualCopyOpen")
        self.btn_manual_copy_open.setFixedHeight(32)
        self.btn_manual_copy_open.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_copy_open.clicked.connect(self._on_manual_copy_and_open)

        self.btn_manual_copy_only = QPushButton("복사만")
        _apply_role(self.btn_manual_copy_only, role="outline")
        self.btn_manual_copy_only.setFixedHeight(32)
        self.btn_manual_copy_only.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_copy_only.clicked.connect(self._on_manual_copy_only)

        mc_copy_lay.addWidget(self.btn_manual_copy_open, stretch=1)
        mc_copy_lay.addWidget(self.btn_manual_copy_only)

        self.btn_manual_clipboard_apply = QPushButton("클립보드 바로 적용")
        _apply_role(self.btn_manual_clipboard_apply, role="secondary", obj_name="BtnDockClipboardApply")
        self.btn_manual_clipboard_apply.setFixedHeight(30)
        self.btn_manual_clipboard_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_clipboard_apply.setToolTip(
            "클립보드의 HWPX JSON(\"operations\") 또는 Python 코드(def edit_hwpx)를 확인해 즉시 문서에 반영합니다."
        )
        self.btn_manual_clipboard_apply.clicked.connect(self._on_manual_clipboard_apply_clicked)

        mc_outer_lay.addLayout(mc_copy_lay)
        mc_outer_lay.addWidget(self.btn_manual_clipboard_apply)
        lay.addWidget(self.manual_copy_card)

        # 내부/테스트 호환용 숨김 위젯 (중간 붙여넣기 UI 제거, 하단 전송↔응답 회수로 통합)
        self.txt_manual_response = QTextEdit(page)
        self.txt_manual_response.setVisible(False)
        self.txt_manual_response.textChanged.connect(self._on_manual_response_changed)
        self.lbl_manual_verify = QLabel(page)
        self.lbl_manual_verify.setVisible(False)
        self.btn_manual_apply_next = QPushButton(page)
        self.btn_manual_apply_next.setVisible(False)
        self.btn_manual_apply_next.clicked.connect(self._on_manual_apply_and_next)

        lay.addStretch()
        return page

    # =========================================================================
    # Page 2: Playwright 자동 / 반자동 모드 (실행·대기 중 미니 바만 표시)
    # =========================================================================
    def _build_playwright_mode_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(6)

        self.lbl_pw_summary = QLabel(page)
        self.lbl_pw_summary.setVisible(False)

        self.btn_pw_collect_next = QPushButton("응답 회수 · 다음 배치")
        _apply_role(self.btn_pw_collect_next, role="secondary", obj_name="BtnDockPwCollectNext")
        self.btn_pw_collect_next.setFixedHeight(32)
        self.btn_pw_collect_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pw_collect_next.setEnabled(False)
        self.btn_pw_collect_next.setVisible(False)
        self.btn_pw_collect_next.clicked.connect(self._on_pw_collect_next_clicked)

        self.btn_pw_confirm_login = QPushButton("인증/로그인 완료 · 작업 재개")
        _apply_role(self.btn_pw_confirm_login, role="teal", obj_name="BtnDockPwConfirmLogin")
        self.btn_pw_confirm_login.setFixedHeight(32)
        self.btn_pw_confirm_login.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pw_confirm_login.setEnabled(False)
        self.btn_pw_confirm_login.setVisible(False)
        self.btn_pw_confirm_login.clicked.connect(self._on_pw_confirm_login_clicked)

        self.btn_pw_stop = QPushButton("작업 중지")
        _apply_role(self.btn_pw_stop, role="danger", obj_name="BtnDockPwStop")
        self.btn_pw_stop.setFixedHeight(32)
        self.btn_pw_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pw_stop.setEnabled(False)
        self.btn_pw_stop.setVisible(False)
        self.btn_pw_stop.clicked.connect(self._on_pw_stop_clicked)

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
    def _register_default_general_chat_target(self) -> None:
        self.register_target(
            target_id="general_chat",
            label="범용 챗 (에이전트)",
            context_getter=lambda: {"target_id": "general_chat", "kind": "general_chat"},
            on_result=None,
        )

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
        elif target_id == "general_chat":
            self.combo_target_selector.insertItem(0, label, target_id)
        else:
            self.combo_target_selector.addItem(label, target_id)
        self.combo_target_selector.blockSignals(False)

        if not self._active_target_id:
            self.set_target(target_id, force=True)

    def is_target_locked(self) -> bool:
        return self._target_locked

    def active_target_id(self) -> str:
        return self._active_target_id

    def current_mode_index(self) -> int:
        return int(self.mode_stack.currentIndex())

    @staticmethod
    def get_web_provider_label(provider_key: str, custom_url: str = "") -> str:
        key = str(provider_key or "gemini").strip().lower()
        mapping = {
            "gemini": "Gemini",
            "chatgpt": "ChatGPT",
            "claude": "Claude",
        }
        if key in mapping:
            return mapping[key]
        if key == "custom":
            url_str = str(custom_url or "").strip()
            if url_str:
                from urllib.parse import urlparse

                parsed = urlparse(url_str if "://" in url_str else f"https://{url_str}")
                host = (parsed.netloc or "").strip()
                if host:
                    return host[:24]
            return "직접 URL"
        return key.upper() if key else "Gemini"

    def manual_provider_display(self) -> str:
        custom_url = (
            self.edit_manual_custom_url.text().strip()
            if hasattr(self, "edit_manual_custom_url")
            else ""
        )
        return self.get_web_provider_label(self._manual_provider, custom_url)

    def playwright_provider_display(self) -> str:
        custom_url = (
            self.edit_pw_custom_url.text().strip()
            if hasattr(self, "edit_pw_custom_url")
            else ""
        )
        return self.get_web_provider_label(self._pw_provider, custom_url)

    def playwright_exec_mode_display(self) -> str:
        if hasattr(self, "combo_pw_exec_mode") and self.combo_pw_exec_mode.currentData() == "auto":
            return "완전 자동"
        return "반자동"

    def set_mode_index(self, index: int) -> None:
        if 0 <= index < self.mode_stack.count():
            self._cancel_manual_collect_waiting()
            self.mode_stack.setCurrentIndex(index)
            btn = self.mode_btn_group.button(index)
            if btn is not None:
                btn.setChecked(True)
            self._sync_chat_input_provider_ui()
            self._sync_body_scroll_visibility()
            self.ai_mode_changed.emit()

    def reset_target_state(self) -> None:
        """타겟 전환 또는 새 문서 열기 시 이전 타겟의 응답창·첨부파일·배치·결과 캐시를 즉시 초기화합니다."""
        self._last_context = {}
        self._last_result_excel_path = ""
        self._manual_batches = []
        self._manual_parsed_items = []
        self._manual_single_parsed = {}
        self._hwpx_ref_chunks = []
        self._hwpx_ref_signature = ()
        self._hwpx_ref_error = ""
        self._hwpx_ref_index_summary = {}
        self._hwpx_selected_chunk_ids = []
        self._hwpx_pending_clarification = None
        self._hwpx_extra_chunk_round = 0
        self._pw_hwpx_stage1_pending = False
        self._pw_next_stage_pending = False
        if hasattr(self, "txt_manual_response"):
            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.clear()
            self.txt_manual_response.blockSignals(False)
        if hasattr(self, "combo_manual_batch"):
            self.combo_manual_batch.blockSignals(True)
            self.combo_manual_batch.clear()
            self.combo_manual_batch.blockSignals(False)
        self._sync_batch_bar_visibility()
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
        """선택된 인용 텍스트 및 하단 추가 지시·첨부 컨텍스트를 프롬프트 끝에 자동 첨부합니다."""
        out = prompt_text
        if self._quoted_text:
            loc_str = self._quoted_target_id or "미지정"
            quote_block = (
                "\n\n[선택된 인용 영역]\n"
                f"- 위치 ID: {loc_str}\n"
                "- 선택 원문:\n"
                '"""\n'
                f"{self._quoted_text}\n"
                '"""'
            )
            out = f"{out}{quote_block}"
        if (
            self._chat_extra_instruction
            and self._active_target_id not in ("hwpx_agent", "calendar_todo")
        ):
            out = f"{out}\n\n[추가 지시사항]\n{self._chat_extra_instruction}"
        if self._chat_extra_attachments:
            att_block = format_attachments_for_prompt(self._chat_extra_attachments)
            if att_block:
                out = f"{out}\n\n{att_block}"
        return out

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
            self._cancel_manual_collect_waiting()

        self._active_target_id = target_id

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
        self._cancel_manual_collect_waiting()
        self.mode_stack.setCurrentIndex(mode_idx)
        self._sync_chat_input_provider_ui()
        self.sync_from_context(self._fetch_fresh_context())
        self.ai_mode_changed.emit()

    def _sync_body_scroll_visibility(self, _kind: str = "") -> None:
        """모든 타겟을 챗봇 타임라인 중심으로 통합하여 중간 레거시 컨트롤 영역을 항상 숨깁니다."""
        self.body_scroll.setVisible(False)
        self._sync_batch_bar_visibility()
        self._sync_chat_send_button_state()
        self._sync_manual_extra_paste_button()

    def _sync_batch_bar_visibility(self) -> None:
        """수동 복붙 모드이면서 분할 배치가 2개 이상 존재할 때만 하단 1줄 배치 바를 표시합니다."""
        if not hasattr(self, "batch_bar_widget"):
            return
        mode_idx = self.current_mode_index()
        total = self.combo_manual_batch.count()
        show_bar = mode_idx == 1 and total > 1
        self.batch_bar_widget.setVisible(show_bar)
        if show_bar:
            cur_idx = max(0, self.combo_manual_batch.currentIndex())
            self.lbl_batch_progress.setText(f"{cur_idx + 1}/{total}")
        else:
            self.lbl_batch_progress.setText(f"0/{total}" if total == 0 else f"1/{total}")

    def _sync_chat_send_button_state(self) -> None:
        """활성 타겟과 회수 대기 상태에 맞춰 하단 입력창 placeholder 및 전송/작업 시작/응답 회수 버튼을 갱신합니다."""
        if not hasattr(self, "btn_chat_send"):
            return
        self.txt_chat_input.setEnabled(True)
        tid = self._active_target_id or "general_chat"
        if tid in ("agg_step2", "ocr"):
            self.txt_chat_input.setPlaceholderText(
                "추가 지시 입력 (비워두고 바로 [작업 시작] 가능)"
            )
            default_label = "작업 시작"
        elif tid == "hwpx_agent":
            self.txt_chat_input.setPlaceholderText(
                "예: 2026년 일정으로 바꾸고 중화적정 시험문항 1개 추가해줘\n"
                "(Enter 전송 · Shift+Enter 줄바꿈)"
            )
            default_label = "전송"
        elif tid == "calendar_todo":
            self.txt_chat_input.setPlaceholderText(
                "예: 내일 오전 10시 교무회의, 11월 25일까지 2학년 수행평가 채점 완료\n"
                "또는 공문·안내문 본문을 그대로 붙여넣으세요."
            )
            default_label = "전송"
        else:
            self.txt_chat_input.setPlaceholderText(
                "메시지 입력 (Enter 전송 · Shift+Enter 줄바꿈)\n파일/폴더를 여기로 드래그앤드롭할 수 있습니다."
            )
            default_label = "전송"

        if self._manual_waiting_collect:
            self.btn_chat_send.setText("응답 회수")
            self.btn_chat_send.setToolTip("입력창 또는 클립보드의 웹 AI 응답을 회수하여 등록합니다.")
            self.btn_chat_cancel_collect.setVisible(True)
        elif self._pw_waiting_collect:
            self.btn_chat_send.setText("응답 회수")
            self.btn_chat_send.setToolTip("Playwright 브라우저에서 웹 AI 응답을 회수합니다.")
            self.btn_chat_cancel_collect.setVisible(False)
        else:
            self.btn_chat_send.setText(default_label)
            self.btn_chat_send.setToolTip("")
            self.btn_chat_cancel_collect.setVisible(False)

    def _sync_chat_input_provider_ui(self) -> None:
        """현재 실행 모드(API / 수동 / Playwright)에 맞춰 하단 모델 콤보/배지 및 직접 URL 입력란을 동기화합니다."""
        mode_idx = self.current_mode_index()
        if mode_idx == 0:
            self.combo_web_provider.setVisible(False)
            self.edit_chat_custom_url.setVisible(False)
            self.lbl_chat_api_badge.setVisible(True)
            return

        self.lbl_chat_api_badge.setVisible(False)
        self.combo_web_provider.setVisible(True)
        active_key = self._manual_provider if mode_idx == 1 else self._pw_provider
        self.combo_web_provider.blockSignals(True)
        idx = self.combo_web_provider.findData(active_key)
        if idx >= 0:
            self.combo_web_provider.setCurrentIndex(idx)
        self.combo_web_provider.blockSignals(False)
        self.edit_chat_custom_url.setVisible(active_key == "custom")

    def _on_chat_web_provider_changed(self, _index: int) -> None:
        key = str(self.combo_web_provider.currentData() or "gemini")
        mode_idx = self.current_mode_index()
        if mode_idx == 1:
            self._manual_provider = key
        elif mode_idx == 2:
            self._pw_provider = key
        self.edit_chat_custom_url.setVisible(key == "custom")
        if key == "custom":
            self.edit_chat_custom_url.setFocus()
        self.ai_mode_changed.emit()

    def _on_chat_custom_url_changed(self, _text: str) -> None:
        self.ai_mode_changed.emit()

    def _fetch_fresh_context(self, target_id: Optional[str] = None) -> Dict[str, Any]:
        tid = target_id or self._active_target_id
        meta = self._targets.get(tid)
        if meta is None:
            return {}
        try:
            ctx = meta["context_getter"]()
            if isinstance(ctx, dict):
                return ctx
        except Exception as exc:
            try:
                self.db.add_log("WARN", "AiDock", f"컨텍스트 조회 실패 ({tid}): {exc}")
            except Exception:
                pass
        return {}

    def _apply_target_visibility(
        self,
        kind: str,
        *,
        hwpx_multi_parts: bool = False,
        has_atts: bool = False,
    ) -> None:
        """모든 타겟 전용 위젯을 먼저 일괄 숨김 처리한 뒤, 현재 타겟 전용 위젯만 표시합니다."""
        # 1) 모든 타겟 전용 위젯 일괄 숨김 (이전 타겟 잔존 방지)
        self.hwpx_instruction_card.setVisible(False)
        self.calendar_instruction_card.setVisible(False)
        self.api_summary_card.setVisible(False)
        self.chk_api_run_integrated.setVisible(False)
        self.manual_batch_card.setVisible(False)
        if hasattr(self, "spin_manual_students"):
            self.spin_manual_students.setVisible(False)
        if hasattr(self, "spin_manual_tokens"):
            self.spin_manual_tokens.setVisible(False)
        if hasattr(self, "btn_manual_resplit"):
            self.btn_manual_resplit.setVisible(False)
        self.manual_ocr_card.setVisible(False)
        self.manual_attachment_card.setVisible(False)
        self.manual_copy_card.setVisible(True)
        self.combo_pw_exec_mode.setVisible(True)
        self.btn_manual_apply_next.setVisible(True)
        self.btn_api_start.setVisible(True)

        # 2) 현재 타겟에 해당하는 전용 위젯만 활성화
        if kind == "general_chat":
            self.manual_copy_card.setVisible(False)
            self.btn_manual_apply_next.setVisible(False)
            self.btn_api_start.setVisible(False)
            self.combo_pw_exec_mode.setVisible(True)
            self.lbl_pw_exec_mode.setText("Playwright 실행 방식")
            self.btn_pw_collect_next.setText("응답 회수")
            self.lbl_pw_summary.setText("하단 입력창에서 메시지를 전송하면 브라우저로 자동 입력됩니다.")
        elif kind == "calendar_todo":
            self.btn_api_start.setText("일정·할일 AI 추출 실행")
            self.btn_manual_apply_next.setText("JSON 검증 후 일정·할일 확인")
            self.lbl_pw_exec_mode.setText("Playwright 실행 방식 (단일)")
            self.btn_pw_collect_next.setText("응답 회수")
        elif kind == "class_notice_digest":
            self.api_summary_card.setVisible(True)
            self.combo_pw_exec_mode.setVisible(True)
            self.btn_api_start.setText("AI 공지 추출·요약 실행")
            self.btn_manual_apply_next.setText("JSON 검증 후 검수 목록에 추가")
            self.lbl_pw_exec_mode.setText("Playwright 실행 방식 (단일)")
            self.btn_pw_collect_next.setText("응답 회수")
        elif kind == "hwpx_agent":
            self.api_summary_card.setVisible(True)
            self.combo_pw_exec_mode.setVisible(True)
            self.manual_batch_card.setVisible(hwpx_multi_parts)
            self.btn_api_start.setText("API 일괄 생성 시작")
            self.lbl_pw_exec_mode.setText("Playwright 실행 방식")
            self.btn_pw_collect_next.setText("응답 회수 · 다음 배치")
            self.btn_manual_apply_next.setText(
                "JSON Patch 검증 후 적용 · 다음 파트 복사"
                if hwpx_multi_parts
                else "JSON Patch 검증 후 문서에 적용"
            )
        elif kind == "ocr":
            self.api_summary_card.setVisible(True)
            self.manual_ocr_card.setVisible(True)
            self.manual_attachment_card.setVisible(has_atts)
            self.combo_pw_exec_mode.setVisible(True)
            self.btn_api_start.setText("API 일괄 생성 시작")
            self.lbl_pw_exec_mode.setText("Playwright 실행 방식")
            self.btn_pw_collect_next.setText("응답 회수 · 다음 배치")
            self.btn_manual_apply_next.setText("검증 후 OCR 결과 반영")
        else:  # excel_batch (과제 파이프라인 > 2단계 세특 생성)
            self.api_summary_card.setVisible(True)
            self.chk_api_run_integrated.setVisible(True)
            self.manual_batch_card.setVisible(True)
            if hasattr(self, "spin_manual_students"):
                self.spin_manual_students.setVisible(True)
            if hasattr(self, "spin_manual_tokens"):
                self.spin_manual_tokens.setVisible(True)
            if hasattr(self, "btn_manual_resplit"):
                self.btn_manual_resplit.setVisible(True)
            self.manual_attachment_card.setVisible(has_atts)
            self.combo_pw_exec_mode.setVisible(True)
            self.btn_api_start.setText("API 일괄 생성 시작")
            self.lbl_pw_exec_mode.setText("Playwright 실행 방식")
            self.btn_pw_collect_next.setText("응답 회수 · 다음 배치")
            self.btn_manual_apply_next.setText("검증 후 엑셀 반영 · 다음 묶음 복사")

    def sync_from_context(self, ctx: Optional[Dict[str, Any]] = None) -> None:
        """중앙 입력창 변경(300ms 디바운스) 또는 타겟 전환 시 우측 사이드바 배치 요약을 재계산합니다."""
        if not ctx:
            ctx = self._fetch_fresh_context()
        if not ctx and self._active_target_id:
            fallback_kind = (
                self._active_target_id
                if self._active_target_id in ("general_chat", "ocr", "hwpx_agent", "calendar_todo", "class_notice_digest")
                else "excel_batch"
            )
            ctx = {"target_id": self._active_target_id, "kind": fallback_kind}
        if not ctx:
            return
        tid = str(ctx.get("target_id", "") or "")
        if self._active_target_id and tid and tid != self._active_target_id:
            return

        prev_ctx = self._last_context or {}
        prev_tid = str(prev_ctx.get("target_id", "") or "")
        kind = str(ctx.get("kind") or "")
        if not kind:
            kind = (
                tid
                if tid in ("general_chat", "ocr", "hwpx_agent", "calendar_todo", "class_notice_digest")
                else "excel_batch"
            )
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
                # 문서 전환/새 양식 클릭 시에도 중앙 탭의 첨부 참고목록 캐시는 sync 아래에서 재평가되도록 둠
                self.reset_target_state()

        self._last_context = dict(ctx)
        self._update_sidepanel_file_badge(self._last_context)
        is_general = kind == "general_chat"
        is_excel = kind == "excel_batch"
        is_hwpx = kind == "hwpx_agent"
        is_cal = kind == "calendar_todo"

        hwpx_chunks = self._ensure_hwpx_ref_chunks(ctx) if is_hwpx else []
        hwpx_total_parts = len(hwpx_chunks)
        hwpx_multi_parts = is_hwpx and hwpx_total_parts >= 2

        # 첨부파일 목록 추출 및 수동 복붙 안내 배너 토글 (general_chat / hwpx_agent / calendar_todo는 제외)
        if is_general or is_hwpx or is_cal:
            attachment_paths: List[str] = []
        else:
            raw_atts = ctx.get("attachment_paths")
            if raw_atts is None and kind == "ocr":
                raw_atts = ctx.get("ocr_files") or []
            attachment_paths = [str(p).strip() for p in (raw_atts or []) if str(p).strip()]
        has_atts = len(attachment_paths) > 0

        self._apply_target_visibility(
            kind,
            hwpx_multi_parts=hwpx_multi_parts,
            has_atts=has_atts,
        )

        if has_atts:
            self.lbl_manual_attachment_info.setText(
                f"첨부파일 {len(attachment_paths)}개 포함: 보안상 웹 브라우저에 파일이 자동 첨부되지 않습니다. "
                "웹 창이 열리면 아래 폴더에서 파일을 직접 드래그해 첨부해 주세요."
            )
            names_preview = ", ".join(Path(p).name for p in attachment_paths[:3])
            if len(attachment_paths) > 3:
                names_preview += f" 외 {len(attachment_paths) - 3}건"
            self.lbl_manual_attachment_names.setText(f"대상 파일: {names_preview}")

        if is_general:
            self._sync_body_scroll_visibility("general_chat")
            self.lbl_dock_progress_meta.setText("범용 챗 · 준비 완료")
            return
        self._sync_body_scroll_visibility(kind)
        if is_hwpx:
            dom_map = ctx.get("dom_map")
            hwpx_path = str(ctx.get("hwpx_path", "") or "").strip()
            ref_files = ctx.get("reference_files") or []
            is_create = bool(ctx.get("is_create_mode"))
            self._sync_hwpx_manual_part_combo(hwpx_chunks)
            if self._hwpx_ref_error:
                msg = f"참고 문서 파싱 경고: {self._hwpx_ref_error}"
            elif is_create or ref_files or not isinstance(dom_map, dict):
                blk_cnt = dom_map.get("block_count", len(dom_map.get("blocks", []))) if isinstance(dom_map, dict) else 0
                if hwpx_multi_parts:
                    msg = f"새 HWPX 양식 생성 · 참고 {len(ref_files)}건 (Part 1~{hwpx_total_parts} 분할, 현재 {blk_cnt}블록)"
                elif ref_files:
                    msg = f"새 HWPX 양식 생성 · 참고 {len(ref_files)}건 (단일 파트, 현재 {blk_cnt}블록)"
                elif isinstance(dom_map, dict):
                    msg = f"새 HWPX 양식 생성 모드 (지시사항 단독, 현재 {blk_cnt}블록)"
                else:
                    msg = "지시사항 입력 후 실행 시 새 .hwpx 양식이 자동 생성됩니다."
            else:
                blk_cnt = dom_map.get("block_count", len(dom_map.get("blocks", [])))
                fname = Path(hwpx_path).name if hwpx_path else dom_map.get("file_name", "HWPX")
                msg = f"HWPX 로드됨: {fname} (블록 {blk_cnt}개)"
            self.lbl_api_summary.setText(msg)
            self.lbl_pw_summary.setText(msg)
            self.lbl_dock_progress_meta.setText(msg)
        elif is_excel:
            self._rebuild_manual_batches(copy_first=False, ctx=ctx, quiet=True)
            if has_atts:
                cur_pw = self.lbl_pw_summary.text().strip()
                att_note = f"첨부 예정 파일 {len(attachment_paths)}개 — 자동 업로드됨"
                self.lbl_pw_summary.setText(f"{cur_pw}\n{att_note}" if cur_pw else att_note)
        elif is_cal:
            msg = "단일 프롬프트 모드로 실행됩니다. 상단 입력창의 지시사항/공문을 웹 AI에 전달합니다."
            self.lbl_pw_summary.setText(msg)
            self.lbl_dock_progress_meta.setText("일정·할일 추출 준비 완료")
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
        if hasattr(self, "lbl_chat_api_badge"):
            self.lbl_chat_api_badge.setText(model_disp or p_name)
        self.ai_mode_changed.emit()

    # =========================================================================
    # API 모드 실행
    # =========================================================================
    def _set_api_busy(self, busy: bool) -> None:
        self._is_api_running = busy
        self.btn_api_start.setEnabled(not busy)
        self.btn_api_stop.setEnabled(busy)
        self.btn_api_stop.setVisible(busy)
        self._sync_body_scroll_visibility()

    def _get_target_adapter(
        self, target_id: Optional[str] = None
    ) -> Optional[TargetExecutionAdapter]:
        tid = str(target_id or self._active_target_id or "").strip()
        return _DEFAULT_TARGET_ADAPTERS.get(tid)

    def _execute_target_pipeline(self, mode_idx: int) -> bool:
        """
        TargetExecutionAdapter가 등록된 타깃에 대해 API(0) / 수동 복붙(1) / Playwright(2)
        3모드 공통 파이프라인을 실행합니다. 어댑터가 없는 특수 타깃은 False를 반환합니다.
        """
        ctx = self._fetch_fresh_context()
        if not ctx and self._active_target_id:
            ctx = {"target_id": self._active_target_id, "kind": self._active_target_id}
        if not ctx:
            self.lbl_dock_progress_meta.setText("실행 불가: 활성 타깃 컨텍스트가 없습니다.")
            return False
        target_id = str(ctx.get("target_id") or self._active_target_id or "").strip()
        adapter = self._get_target_adapter(target_id)
        if adapter is None:
            return False

        if adapter.pre_execute_hook(self, ctx, mode_idx):
            return True

        if mode_idx == 0:
            if self._is_api_running:
                return True
            info = get_active_ai_info()
            if not info.get("has_key", False):
                err_msg = (
                    f"현재 선택된 AI 공급자({info.get('provider_display')})의 API Key가 등록되어 있지 않습니다."
                )
                self.lbl_dock_progress_meta.setText(f"API 실행 불가: {err_msg}")
                self._append_chat_message(
                    role="assistant",
                    content=f"⚠️ {err_msg}\n환경설정 탭에서 API Key를 등록해 주세요.",
                    exec_mode="api",
                    tool_name=target_id,
                )
                reply = QMessageBox.warning(
                    self,
                    "API Key 필요",
                    f"{err_msg}\n환경설정 탭으로 이동하여 API Key를 등록하시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self.go_to_settings_requested.emit()
                return True

            try:
                worker = adapter.create_api_worker(self, ctx, info)
            except Exception as exc:
                QMessageBox.information(self, "입력 확인", str(exc))
                return True

            worker.signals.progress.connect(self._on_dock_worker_progress)
            self._set_api_busy(True)
            self.progress_dock.setValue(0)
            self.lbl_dock_progress_meta.setText("API 작업 시작 중...")

            def _on_res(res: Dict[str, Any]) -> None:
                self._dispatch_result(target_id, res)
                self.sync_from_context(self._fetch_fresh_context(target_id))
                meta_msg = adapter.post_api_result_hook(self, res, ctx, info)
                if meta_msg:
                    self.lbl_dock_progress_meta.setText(meta_msg)

            def _on_err(err: str) -> None:
                first = (err or "").strip().split("\n")[0]
                self.lbl_dock_progress_meta.setText(f"오류: {first}")
                self._append_chat_message(
                    role="assistant",
                    content=f"⚠️ API 실행 오류: {first}",
                    exec_mode="api",
                    tool_name=target_id,
                )
                QMessageBox.critical(self, "API 실행 오류", first)

            self.task_runner.start_worker(
                worker=worker,
                task_key=self.TASK_DOCK_API,
                on_result=_on_res,
                on_error=_on_err,
                on_finished=lambda: self._set_api_busy(False),
            )
            return True

        if mode_idx == 1:
            prompt_text = adapter.build_prompt(self, ctx)
            if not prompt_text:
                self.lbl_dock_progress_meta.setText(f"수동 복붙 중단: {adapter.empty_prompt_warning}")
                self._append_chat_message(
                    role="assistant",
                    content=f"⚠️ 수동 복붙 중단: {adapter.empty_prompt_warning}",
                    exec_mode="manual",
                    tool_name=target_id,
                )
                QMessageBox.information(self, "프롬프트 확인", adapter.empty_prompt_warning)
                return True
            att_paths = [Path(p) for p in adapter.get_attachments(self, ctx) if Path(p).is_file()]
            self._notify_manual_attachments(target_id, att_paths)
            self._copy_and_open_manual_web(prompt_text)
            return True

        # mode_idx == 2 (Playwright 자동)
        if self._active_pw_worker is not None:
            return True
        custom_url = ""
        if self._pw_provider == "custom":
            custom_url = normalize_web_url(self.edit_pw_custom_url.text())
            if not custom_url:
                QMessageBox.warning(self, "URL 확인", "직접 URL 모드에서는 접속할 웹 AI 주소를 입력해 주세요.")
                self.edit_pw_custom_url.setFocus()
                return True

        prompt_text = adapter.build_prompt(self, ctx)
        if not prompt_text:
            QMessageBox.warning(self, "입력 확인", adapter.empty_prompt_warning)
            return True

        exec_mode = str(self.combo_pw_exec_mode.currentData() or "auto")
        keep_login = bool(self.chk_pw_keep_login.isChecked())
        use_temp_chat = bool(self.chk_pw_temp_chat.isChecked())
        attachment_paths = adapter.get_attachments(self, ctx)
        task_label = str(ctx.get("task_label") or target_id).strip()

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
        worker.signals.progress.connect(self._on_dock_worker_progress)
        worker.waiting_for_login.connect(self._on_pw_waiting_for_login)
        worker.waiting_for_semi_auto_collect.connect(self._on_pw_waiting_for_collect)
        worker.waiting_for_element_click.connect(self._on_pw_waiting_for_click)
        worker.batch_completed.connect(
            lambda payload, tid=target_id: self._on_pw_batch_completed(tid, payload)
        )

        self._active_pw_worker = worker
        self._pw_single_batches_applied = False
        self._set_pw_running_state(True)
        self.progress_dock.setValue(0)
        self.lbl_dock_progress_meta.setText(f"[{task_label}] Playwright 브라우저 실행 중...")
        adapter.on_pw_started_hook(self, ctx)

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_DOCK_PLAYWRIGHT,
            on_result=lambda res, tid=target_id: self._on_pw_finished_result(tid, res),
            on_error=self._on_pw_error,
            on_finished=self._on_pw_finished,
        )
        return True

    def _on_api_start_clicked(self) -> None:
        if self._execute_target_pipeline(0):
            return
        if self._is_api_running:
            return
        ctx = self._fetch_fresh_context()
        if not ctx and self._active_target_id:
            ctx = {"target_id": self._active_target_id, "kind": self._active_target_id}
        if not ctx:
            return

        info = get_active_ai_info()
        if not info.get("has_key", False):
            err_msg = (
                f"현재 선택된 AI 공급자({info.get('provider_display')})의 API Key가 등록되어 있지 않습니다."
            )
            self.lbl_dock_progress_meta.setText(f"API 실행 불가: {err_msg}")
            self._append_chat_message(
                role="assistant",
                content=f"⚠️ {err_msg}\n환경설정 탭에서 API Key를 등록해 주세요.",
                exec_mode="api",
                tool_name=str(ctx.get("target_id") or self._active_target_id or "general_chat"),
            )
            reply = QMessageBox.warning(
                self,
                "API Key 필요",
                f"{err_msg}\n환경설정 탭으로 이동하여 API Key를 등록하시겠습니까?",
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
            chunks = self._ensure_hwpx_ref_chunks(ctx)
            if self._hwpx_ref_error:
                QMessageBox.warning(self, "참고 문서 오류", self._hwpx_ref_error)
                return
            create_or_ref = self._is_hwpx_create_or_ref_mode(ctx)
            if not inst and not chunks:
                msg = (
                    "상단 입력창에 생성할 양식 지시사항을 입력하거나 참고 파일을 첨부해 주세요."
                    if create_or_ref
                    else "상단 입력창에 문서 수정 또는 새 양식 생성 지시사항을 입력해 주세요."
                )
                QMessageBox.information(self, "지시사항 확인", msg)
                self.txt_hwpx_instruction.setFocus()
                return
            effective_inst = inst or "참고 문서의 구조와 내용을 충실히 반영하여 새 HWPX 양식을 생성하세요."
            ref_files_list = [str(p).strip() for p in (ctx.get("reference_files") or []) if str(p).strip()]
            raw_entries = ctx.get("hwpx_entries")
            doc_snap = None
            if isinstance(raw_entries, dict) and raw_entries:
                hwpx_path_str = str(ctx.get("hwpx_path") or "").strip()
                doc_snap = HwpxDocument(
                    source_path=Path(hwpx_path_str) if hwpx_path_str else None,
                    entries=dict(raw_entries),
                )
            worker = _DockApiHwpxAgentWorker(
                dom_map=dom_map if isinstance(dom_map, dict) else None,
                instruction=self._append_quote_to_prompt(effective_inst),
                is_create_mode=create_or_ref,
                ref_chunks=chunks,
                reference_files=ref_files_list,
                preselected_chunk_ids=list(self._hwpx_selected_chunk_ids),
                hwpx_doc_snapshot=doc_snap,
            )
            worker.part_completed.connect(
                lambda p_payload, tid=str(ctx.get("target_id", self._active_target_id)): self._on_api_hwpx_part_completed(
                    tid, p_payload
                )
            )
        elif kind == "calendar_todo":
            inst = self.txt_calendar_instruction.toPlainText().strip()
            if not inst:
                QMessageBox.information(
                    self,
                    "입력 확인",
                    "상단 입력창에 일정 요청이나 공문·안내문 텍스트를 입력해 주세요.",
                )
                self.txt_calendar_instruction.setFocus()
                return
            if self._try_handle_direct_calendar_json(inst):
                return
            worker = _DockApiCalendarTodoWorker(
                instruction=self._append_quote_to_prompt(inst),
                schedule_context=str(ctx.get("schedule_context") or ""),
            )
        elif kind == "class_notice_digest":
            sys_p = str(ctx.get("system_prompt") or "").strip()
            usr_p = str(ctx.get("user_prompt") or "").strip()
            if not usr_p:
                QMessageBox.information(
                    self,
                    "입력 확인",
                    "중앙 '4. 학급 공지·긴급 알림' 탭에서 카톡 대화 또는 요약할 공지를 먼저 입력·선택해 주세요.",
                )
                return
            worker = _DockApiClassNoticeDigestWorker(
                system_prompt=sys_p,
                user_prompt=self._append_quote_to_prompt(usr_p),
                source_label=str(ctx.get("source_label") or "카카오톡 대화"),
                source_type=str(ctx.get("source_type") or "kakao"),
                digest_action=str(ctx.get("digest_action") or "kakao_digest"),
                review_card_id=str(ctx.get("review_card_id") or ""),
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
            if kind == "hwpx_agent" and isinstance(res.get("clarification"), dict):
                self._handle_hwpx_clarification_or_more_chunks(target_id, res["clarification"], mode="api")
                return
            if not res.get("already_applied_parts"):
                self._dispatch_result(target_id, res)
            self.sync_from_context(self._fetch_fresh_context(target_id))
            if kind == "ocr":
                self.lbl_dock_progress_meta.setText(f"API OCR 완료 ({res.get('count', 0)}개 파일)")
            elif kind == "calendar_todo":
                if res.get("is_query_answer"):
                    reply_txt = str(res.get("raw_response") or "").strip()
                    if reply_txt:
                        self._append_chat_message(
                            role="assistant",
                            content=reply_txt,
                            exec_mode="api",
                            model_name=str(info.get("model") or info.get("provider") or "API"),
                            tool_name=target_id,
                        )
                    self.lbl_dock_progress_meta.setText("API 일정 조회 답변 완료")
                else:
                    p_cal = res.get("parsed_calendar") or {}
                    s_cnt = len(p_cal.get("schedules") or [])
                    t_cnt = len(p_cal.get("todos") or [])
                    summary_msg = f"일정 {s_cnt}건, 할일 {t_cnt}건을 추출했습니다."
                    self._append_chat_message(
                        role="assistant",
                        content=summary_msg,
                        exec_mode="api",
                        model_name=str(info.get("model") or info.get("provider") or "API"),
                        tool_name=target_id,
                    )
                    self.lbl_dock_progress_meta.setText(
                        f"API 추출 완료 · 일정 {s_cnt}건, 할일 {t_cnt}건"
                    )
            elif kind == "class_notice_digest":
                n_cnt = len(res.get("items") or [])
                self.lbl_dock_progress_meta.setText(f"API 공지 요약 완료 · {n_cnt}건")
            elif kind == "hwpx_agent":
                c_p = res.get("completed_parts", 1)
                t_p = res.get("total_parts", 1)
                self.lbl_dock_progress_meta.setText(
                    f"API HWPX 양식 완료 ({c_p}/{t_p} 파트)"
                    if t_p > 1
                    else "API HWPX Patch 생성 완료"
                )
            else:
                self.lbl_dock_progress_meta.setText(
                    f"API 완료 · {res.get('completed', 0)}/{res.get('total', 0)}명"
                )

        def _on_err(err: str) -> None:
            first = (err or "").strip().split("\n")[0]
            self.lbl_dock_progress_meta.setText(f"오류: {first}")
            self._append_chat_message(
                role="assistant",
                content=f"⚠️ API 실행 오류: {first}",
                exec_mode="api",
                tool_name=target_id,
            )
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
        self.ai_mode_changed.emit()

    def _on_manual_custom_url_changed(self, text: str) -> None:
        if text.strip() and not self.manual_provider_chips["custom"].isChecked():
            self.manual_provider_chips["custom"].setChecked(True)
            self._manual_provider = "custom"
            self.edit_manual_custom_url.setEnabled(True)
        self.ai_mode_changed.emit()

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
            self._sync_batch_bar_visibility()
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
        self._sync_batch_bar_visibility()

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

    def _ensure_hwpx_ref_chunks(self, ctx: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """참고 파일 목록이 변경된 경우에만 세션 캐시 인덱싱을 수행하고 선택된 청크(또는 전체 청크)를 반환합니다."""
        target_ctx = ctx if ctx is not None else self._last_context
        ref_files = tuple(
            str(p).strip() for p in (target_ctx.get("reference_files") or []) if str(p).strip()
        )
        if ref_files != self._hwpx_ref_signature:
            self._hwpx_ref_signature = ref_files
            self._hwpx_ref_error = ""
            self._hwpx_selected_chunk_ids = []
            self._hwpx_pending_clarification = None
            self._hwpx_extra_chunk_round = 0
            if not ref_files:
                self._hwpx_ref_chunks = []
                self._hwpx_ref_index_summary = {}
            else:
                try:
                    self._hwpx_ref_index_summary = build_reference_index_summary(
                        list(ref_files), max_chars_per_chunk=8000
                    )
                    self._hwpx_ref_chunks = extract_hwpx_reference_chunks(
                        list(ref_files), max_chars_per_chunk=8000
                    )
                except Exception as exc:
                    self._hwpx_ref_chunks = []
                    self._hwpx_ref_index_summary = {}
                    self._hwpx_ref_error = str(exc)

        if self._hwpx_selected_chunk_ids and ref_files:
            selected = resolve_selected_chunks(
                list(ref_files), chunk_ids=self._hwpx_selected_chunk_ids
            )
            total = len(selected)
            out: List[Dict[str, Any]] = []
            for idx, ch in enumerate(selected, start=1):
                item = dict(ch)
                item["part_index"] = idx
                item["total_parts"] = total
                out.append(item)
            return out
        return self._hwpx_ref_chunks

    def _should_run_hwpx_stage1(self, ctx: Dict[str, Any], chunks: List[Dict[str, Any]]) -> bool:
        if not ctx.get("reference_files") or self._hwpx_selected_chunk_ids:
            return False
        summary = self._hwpx_ref_index_summary or {}
        if not summary.get("index_text"):
            return False
        if summary.get("has_scanned_pdf"):
            return True
        if any(c.get("status") == "needs_ocr" for c in chunks):
            return True
        sum_chunks = summary.get("chunks") or []
        total_chars = sum(int(c.get("char_count") or 0) for c in sum_chunks)
        return len(sum_chunks) >= 3 or total_chars > 12000

    def _handle_hwpx_clarification_or_more_chunks(
        self,
        target_id: str,
        parsed_or_status: Dict[str, Any],
        *,
        mode: str = "api",
    ) -> bool:
        """
        needs_clarification / need_more_chunks 응답을 처리합니다.
        처리된 경우 True를 반환해 HWPX 문서 적용을 보류합니다.
        """
        st = parse_stage2_response_status(parsed_or_status)
        status = st.get("status")
        if status == "needs_clarification":
            question = str(st.get("question") or "작업 범위를 구체적으로 알려주세요.").strip()
            options = st.get("options") or []
            opt_lines = "\n".join(f"- {opt}" for opt in options) if options else ""
            msg_text = f"[확인 필요] {question}" + (f"\n\n선택 예시:\n{opt_lines}" if opt_lines else "")
            orig_inst = (
                self.txt_hwpx_instruction.toPlainText().strip()
                if hasattr(self, "txt_hwpx_instruction")
                else ""
            )
            self._hwpx_pending_clarification = {
                "original_instruction": orig_inst,
                "question": question,
                "options": options,
            }
            self._append_chat_message(
                role="assistant",
                content=msg_text,
                exec_mode=mode,
                model_name="EduFlow Agent",
                tool_name=target_id or "hwpx_agent",
            )
            self.lbl_dock_progress_meta.setText("AI가 추가 조건을 질문했습니다. 답변을 입력해 주세요.")
            return True

        if status == "need_more_chunks":
            if self._hwpx_extra_chunk_round >= MAX_EXTRA_CHUNK_ROUNDS:
                self.lbl_dock_progress_meta.setText(
                    f"추가 청크 요청 최대 횟수({MAX_EXTRA_CHUNK_ROUNDS}회)에 도달했습니다."
                )
                return True
            self._hwpx_extra_chunk_round += 1
            ref_files = list(self._hwpx_ref_signature)
            more = resolve_selected_chunks(
                ref_files,
                chunk_ids=st.get("chunk_ids"),
                page_ranges=st.get("pages"),
            )
            existing = set(self._hwpx_selected_chunk_ids)
            for ch in more:
                cid = str(ch.get("chunk_id", ""))
                if cid and cid not in existing:
                    existing.add(cid)
                    self._hwpx_selected_chunk_ids.append(cid)
            self.lbl_dock_progress_meta.setText(
                f"추가 참고 구간({len(more)}개)을 불러왔습니다. (라운드 {self._hwpx_extra_chunk_round}/{MAX_EXTRA_CHUNK_ROUNDS})"
            )
            return True

        return False

    def _is_hwpx_create_or_ref_mode(self, ctx: Optional[Dict[str, Any]] = None) -> bool:
        """새 양식 생성 모드이거나 참고 파일이 첨부되었거나 빈 문서 상태인지 판별합니다."""
        target_ctx = ctx if ctx is not None else self._last_context
        if bool(target_ctx.get("is_create_mode")):
            return True
        if target_ctx.get("reference_files"):
            return True
        dom_map = target_ctx.get("dom_map")
        if not isinstance(dom_map, dict):
            return True
        return int(dom_map.get("block_count", len(dom_map.get("blocks", []))) or 0) == 0

    def _sync_hwpx_manual_part_combo(self, chunks: List[Dict[str, Any]]) -> None:
        """참고 문서가 2개 이상 파트로 분할된 경우 combo_manual_batch를 Part 1/N..N/N 목록으로 동기화합니다."""
        if len(chunks) < 2:
            if self._manual_batches and self._manual_batches[0].get("is_hwpx_part"):
                self._manual_batches = []
                self.combo_manual_batch.blockSignals(True)
                self.combo_manual_batch.clear()
                self.combo_manual_batch.blockSignals(False)
                self._sync_batch_bar_visibility()
            return

        already_matches = (
            len(self._manual_batches) == len(chunks)
            and all(b.get("is_hwpx_part") for b in self._manual_batches)
            and all(
                self._manual_batches[i].get("content") == chunks[i].get("content")
                for i in range(len(chunks))
            )
        )
        if already_matches and self.combo_manual_batch.count() == len(chunks):
            self._update_manual_batch_meta_label()
            return

        prev_idx = self.combo_manual_batch.currentIndex()
        self._manual_batches = [
            {
                "batch_index": int(c.get("part_index", idx + 1)),
                "total_parts": int(c.get("total_parts", len(chunks))),
                "content": str(c.get("content", "")),
                "students": [],
                "estimated_tokens": max(1, len(str(c.get("content", ""))) // 2),
                "is_hwpx_part": True,
            }
            for idx, c in enumerate(chunks)
        ]
        self.combo_manual_batch.blockSignals(True)
        self.combo_manual_batch.clear()
        total_p = len(self._manual_batches)
        for idx, part in enumerate(self._manual_batches, start=1):
            char_cnt = len(part["content"])
            self.combo_manual_batch.addItem(
                f"Part {idx}/{total_p} ({char_cnt:,}자 · 예상 {part['estimated_tokens']:,} 토큰)"
            )
        if 0 <= prev_idx < total_p:
            self.combo_manual_batch.setCurrentIndex(prev_idx)
        elif total_p > 0:
            self.combo_manual_batch.setCurrentIndex(0)
        self.combo_manual_batch.blockSignals(False)
        self._sync_batch_bar_visibility()
        self._update_manual_batch_meta_label()

    def _on_api_hwpx_part_completed(self, target_id: str, payload: Dict[str, Any]) -> None:
        self._dispatch_result(target_id, payload)
        self.sync_from_context(self._fetch_fresh_context(target_id))

    def _update_manual_batch_meta_label(self) -> None:
        batch = self._get_current_manual_batch()
        if batch is None:
            self.lbl_manual_batch_meta.setText("선택된 배치가 없습니다.")
            return
        if batch.get("is_hwpx_part"):
            p_idx = batch.get("batch_index", 1)
            t_parts = batch.get("total_parts", 1)
            c_len = len(str(batch.get("content", "")))
            self.lbl_manual_batch_meta.setText(
                f"선택 파트: Part {p_idx}/{t_parts} · 참고 텍스트 {c_len:,}자"
            )
            self._on_manual_response_changed()
            return
        stus = batch.get("students", [])
        toks = batch.get("estimated_tokens", 0)
        self.lbl_manual_batch_meta.setText(
            f"선택 묶음: 학생 {len(stus)}명 · 예상 입력 약 {toks:,} 토큰"
        )
        self._on_manual_response_changed()

    def _on_manual_batch_changed(self, _idx: int) -> None:
        self._update_manual_batch_meta_label()
        self._sync_batch_bar_visibility()

    def _build_current_manual_prompt(self) -> str:
        """실행 시점의 최신 컨텍스트를 읽어 현재 대상의 프롬프트를 생성합니다."""
        ctx = self._fetch_fresh_context()
        if not ctx:
            return ""
        kind = str(ctx.get("kind", "") or "")
        if kind == "hwpx_agent" or self._active_target_id == "hwpx_agent":
            dom_map = ctx.get("dom_map")
            inst = self.txt_hwpx_instruction.toPlainText().strip()
            chunks = self._ensure_hwpx_ref_chunks(ctx)
            if self._hwpx_ref_error:
                return ""
            create_or_ref = self._is_hwpx_create_or_ref_mode(ctx)
            if not create_or_ref:
                if not isinstance(dom_map, dict) or not inst:
                    return ""
                sys_p, usr_p = build_hwpx_agent_prompt(dom_map, self._append_quote_to_prompt(inst))
                return f"{sys_p}\n\n{usr_p}"

            if not inst and not chunks:
                return ""
            effective_inst = inst or "참고 문서의 구조와 내용을 충실히 반영하여 새 HWPX 양식을 생성하세요."
            if self._should_run_hwpx_stage1(ctx, chunks):
                idx_text = str((self._hwpx_ref_index_summary or {}).get("index_text") or "").strip()
                if idx_text:
                    sys_p, usr_p = build_hwpx_stage1_selection_prompt(
                        self._append_quote_to_prompt(effective_inst),
                        idx_text,
                        dom_map if isinstance(dom_map, dict) else None,
                    )
                    return f"{sys_p}\n\n{usr_p}"
            if chunks:
                self._sync_hwpx_manual_part_combo(chunks)
                total_parts = len(chunks)
                cur_idx = max(0, min(self.combo_manual_batch.currentIndex(), total_parts - 1))
                part_chunk = chunks[cur_idx]
                ref_content = str(part_chunk.get("content", ""))
                part_index = int(part_chunk.get("part_index", cur_idx + 1))
            else:
                total_parts = 1
                part_index = 1
                ref_content = ""

            sys_p, usr_p = build_hwpx_create_prompt(
                user_instruction=self._append_quote_to_prompt(effective_inst),
                reference_chunk=ref_content,
                part_index=part_index,
                total_parts=total_parts,
                current_dom_map=dom_map if isinstance(dom_map, dict) else None,
            )
            return f"{sys_p}\n\n{usr_p}"
        adapter = self._get_target_adapter(str(ctx.get("target_id") or self._active_target_id))
        if adapter is not None:
            return adapter.build_prompt(self, ctx)

        # 실행 시점 최신 컨텍스트로 배치 재계산 (현재 선택된 batch_index 최대한 유지)
        prev_idx = max(0, self.combo_manual_batch.currentIndex())
        self._rebuild_manual_batches(copy_first=False, ctx=ctx, quiet=True)
        if 0 <= prev_idx < len(self._manual_batches):
            self.combo_manual_batch.setCurrentIndex(prev_idx)
        batch = self._get_current_manual_batch()
        base_prompt = str(batch.get("final_prompt", "")) if batch else ""
        return self._append_quote_to_prompt(base_prompt) if base_prompt else ""

    @staticmethod
    def _hash_clipboard_text(text: str) -> str:
        return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()

    def set_panel_width(self, width: int) -> None:
        if self._panel_root is not None:
            self._panel_root.setFixedWidth(max(self.EXPANDED_WIDTH, int(width)))

    def set_sidepanel_mode_ui(self, enabled: bool) -> None:
        self._sidepanel_mode = bool(enabled)
        if hasattr(self, "btn_sidepanel_mode"):
            self.btn_sidepanel_mode.blockSignals(True)
            self.btn_sidepanel_mode.setChecked(self._sidepanel_mode)
            self.btn_sidepanel_mode.blockSignals(False)
        self._update_sidepanel_file_badge(self._last_context)

    def set_always_on_top_ui(self, enabled: bool) -> None:
        self._always_on_top = bool(enabled)
        if hasattr(self, "btn_always_on_top"):
            self.btn_always_on_top.blockSignals(True)
            self.btn_always_on_top.setChecked(self._always_on_top)
            self.btn_always_on_top.blockSignals(False)

    def _update_sidepanel_file_badge(self, ctx: Optional[Dict[str, Any]] = None) -> None:
        if not hasattr(self, "lbl_sidepanel_file_badge"):
            return
        if not self._sidepanel_mode:
            self.lbl_sidepanel_file_badge.setVisible(False)
            return
        active_ctx = ctx or self._last_context or {}
        filename = ""
        for key in ("original_hwp_path", "hwpx_path", "excel_path", "file_path"):
            val = str(active_ctx.get(key, "") or "").strip()
            if val:
                filename = Path(val).name
                break
        if not filename:
            ocr_files = active_ctx.get("ocr_files") or active_ctx.get("attachment_paths") or []
            if isinstance(ocr_files, list) and ocr_files:
                filename = Path(str(ocr_files[0])).name
        if filename:
            self.lbl_sidepanel_file_badge.setText(f"편집 중: {filename}")
        else:
            label = str(active_ctx.get("label") or self._active_target_id or "일반 대화").strip()
            self.lbl_sidepanel_file_badge.setText(f"대상: {label}")
        self.lbl_sidepanel_file_badge.setVisible(True)

    def on_host_window_activated(self) -> None:
        if self.current_mode_index() != 1:
            return
        self.check_clipboard_for_hwpx_response(auto_apply=False)

    def check_clipboard_for_hwpx_response(self, *, auto_apply: bool = False) -> bool:
        if self.current_mode_index() != 1:
            return False
        cb = QApplication.clipboard()
        if cb is None:
            return False
        clip_text = str(cb.text() or "")
        detected = detect_hwpx_clipboard_payload(clip_text)
        if detected is None:
            return False

        clip_hash = self._hash_clipboard_text(clip_text)
        if not auto_apply and clip_hash == self._last_clipboard_payload_hash:
            return False

        self._last_clipboard_payload_hash = clip_hash
        self._detected_clipboard_payload = detected
        self._detected_clipboard_text = clip_text

        self.txt_manual_response.blockSignals(True)
        self.txt_manual_response.setPlainText(clip_text)
        self.txt_manual_response.blockSignals(False)
        self._on_manual_response_changed()

        if auto_apply:
            self._on_apply_detected_clipboard_clicked()
            return True

        if detected.get("kind") == "code_mode":
            detail = "Python 코드 편집(def edit_hwpx)"
        else:
            ops_cnt = int(detected.get("ops_count", 0))
            detail = f"HWPX JSON {ops_cnt}건"
        self.lbl_clipboard_detect.setText(f"클립보드에서 AI 응답 감지됨 ({detail})")
        self.btn_apply_detected_clipboard.setText("바로 적용")
        self.clipboard_detect_banner.setVisible(True)
        return True

    def _dismiss_clipboard_detect_banner(self) -> None:
        self.clipboard_detect_banner.setVisible(False)

    def _on_manual_clipboard_apply_clicked(self) -> None:
        if self.check_clipboard_for_hwpx_response(auto_apply=True):
            return
        raw_resp = self.txt_manual_response.toPlainText().strip()
        if raw_resp and detect_hwpx_clipboard_payload(raw_resp) is not None:
            self._on_manual_apply_and_next()
            return
        self.lbl_dock_progress_meta.setText(
            "클립보드에 유효한 HWPX JSON 또는 def edit_hwpx 코드가 없습니다."
        )

    def _on_apply_detected_clipboard_clicked(self) -> None:
        self.clipboard_detect_banner.setVisible(False)
        if not self.txt_manual_response.toPlainText().strip() and self._detected_clipboard_text:
            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.setPlainText(self._detected_clipboard_text)
            self.txt_manual_response.blockSignals(False)
        self._on_manual_apply_and_next()

    def _on_manual_copy_only(self) -> None:
        prompt_text = self._build_current_manual_prompt()
        if not prompt_text:
            QMessageBox.information(self, "프롬프트 확인", "복사할 프롬프트가 없습니다. 대상 파일과 배치를 확인해 주세요.")
            return
        self._last_clipboard_payload_hash = self._hash_clipboard_text(prompt_text)
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
        self._last_clipboard_payload_hash = self._hash_clipboard_text(prompt_text)
        QApplication.clipboard().setText(prompt_text)
        opened = self._open_external_web_url(url)
        self.lbl_manual_verify.setText(
            f"프롬프트 복사 완료 · 웹 브라우저 열기: {url}"
            if opened
            else f"프롬프트 복사 완료 · 웹 브라우저 열기 실패 ({url})"
        )

    def _extract_context_attachment_paths(self, ctx: Optional[Dict[str, Any]] = None) -> List[Path]:
        """컨텍스트에서 실제 존재하는 첨부파일 경로 목록을 중복 없이 추출합니다."""
        active_ctx = ctx or self._fetch_fresh_context() or self._last_context or {}
        kind = str(active_ctx.get("kind", "") or "")
        if kind == "hwpx_agent":
            return []
        raw_atts = active_ctx.get("attachment_paths")
        if raw_atts is None:
            raw_atts = active_ctx.get("attached_files")
        if raw_atts is None and kind == "ocr":
            raw_atts = active_ctx.get("ocr_files") or []

        candidates: List[str] = [str(p).strip() for p in (raw_atts or []) if str(p).strip()]
        for att in getattr(self, "_chat_extra_attachments", []) or []:
            if isinstance(att, dict) and att.get("path"):
                candidates.append(str(att["path"]).strip())
            elif isinstance(att, str) and att.strip():
                candidates.append(att.strip())

        seen: set[str] = set()
        paths: List[Path] = []
        for raw in candidates:
            p = Path(raw)
            key = str(p).lower()
            if key in seen or not p.exists():
                continue
            seen.add(key)
            paths.append(p)
        return paths

    def _open_attachment_in_explorer(self, paths: List[Path]) -> Optional[Path]:
        """첨부파일 위치를 OS 탐색기(Windows는 explorer /select)로 엽니다. 오프스크린/테스트 환경에서는 건너뜁니다."""
        target = next((p for p in paths if p.exists()), None)
        if target is None:
            return None
        if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
            return target
        if sys.platform == "win32" and target.is_file():
            import subprocess

            subprocess.Popen(["explorer", "/select,", str(target.resolve())])
        else:
            parent_dir = target.parent if target.is_file() else target
            os.startfile(str(parent_dir.resolve()))
        return target

    def _on_open_attachment_dir(self) -> None:
        """수동 복붙 모드에서 첨부파일이 위치한 폴더를 엽니다."""
        ctx = self._fetch_fresh_context() or self._last_context
        kind = str(ctx.get("kind", "") or "")
        if kind == "hwpx_agent":
            QMessageBox.information(self, "파일 없음", "HWPX 에이전트 타겟에는 별도 첨부파일이 없습니다.")
            return
        paths = self._extract_context_attachment_paths(ctx)
        if not paths:
            QMessageBox.information(self, "파일 없음", "열 수 있는 첨부파일 경로를 찾지 못했습니다.")
            return
        try:
            self._open_attachment_in_explorer(paths)
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

        adapter = self._get_target_adapter(str(ctx.get("target_id") or self._active_target_id))
        if adapter is not None:
            try:
                parsed_payload = adapter.parse_manual_or_pw_result(self, raw_resp, ctx, mode="manual")
                self._manual_single_parsed = parsed_payload
                if isinstance(parsed_payload.get("items"), list):
                    self._manual_parsed_items = parsed_payload["items"]
                self.lbl_manual_verify.setText(adapter.format_manual_verify_text(parsed_payload))
            except Exception as exc:
                self._manual_single_parsed = {}
                self._manual_parsed_items = []
                self.lbl_manual_verify.setText(f"JSON 추출 오류: {exc}")
            return
            if kind == "hwpx_agent":
                detected_code = detect_hwpx_clipboard_payload(raw_resp)
                if isinstance(detected_code, dict) and detected_code.get("kind") == "code_mode":
                    self._manual_single_parsed = {"exec_kind": "code_mode", "code": detected_code["code"]}
                    self.lbl_manual_verify.setText("유효한 HWPX Python 코드 확인됨 (def edit_hwpx)")
                    return
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
            detected_code = detect_hwpx_clipboard_payload(raw_resp)
            if isinstance(detected_code, dict) and detected_code.get("kind") == "code_mode":
                self.txt_manual_response.blockSignals(True)
                self.txt_manual_response.clear()
                self.txt_manual_response.blockSignals(False)
                self._dispatch_result(
                    target_id,
                    {
                        "target_id": target_id,
                        "mode": "manual",
                        "exec_kind": "code_mode",
                        "code": detected_code["code"],
                        "immediate": True,
                    },
                )
                return
            chunks = self._ensure_hwpx_ref_chunks(ctx)
            is_stage1 = self._should_run_hwpx_stage1(ctx, chunks)
            try:
                parsed = extract_json_from_llm_response(raw_resp)
            except Exception as exc:
                if is_stage1 and not isinstance(parsed.get("operations"), list):
                    inst_txt = self.txt_hwpx_instruction.toPlainText().strip()
                    selected = fallback_select_chunks_by_keywords(
                        list(self._hwpx_ref_signature), inst_txt
                    )
                    self._hwpx_selected_chunk_ids = [
                        str(c.get("chunk_id")) for c in selected if c.get("chunk_id")
                    ]
                    self.txt_manual_response.blockSignals(True)
                    self.txt_manual_response.clear()
                    self.txt_manual_response.blockSignals(False)
                    new_chunks = self._ensure_hwpx_ref_chunks(ctx)
                    self._sync_hwpx_manual_part_combo(new_chunks)
                    next_prompt = self._build_current_manual_prompt()
                    if next_prompt:
                        QApplication.clipboard().setText(next_prompt)
                    self.lbl_manual_verify.setText(
                        f"로컬 키워드 선별 완료 ({len(new_chunks)}개 구간) — 2단계 프롬프트가 클립보드에 복사되었습니다!"
                    )
                    return
                QMessageBox.warning(self, "JSON 검증 오류", f"유효한 JSON Patch 응답을 추출하지 못했습니다:\n{exc}")
                return
            if is_stage1 and not isinstance(parsed.get("operations"), list):
                try:
                    s1 = parse_stage1_selection_response(parsed)
                except Exception:
                    s1 = {"status": "fallback"}
                if s1["status"] == "needs_clarification":
                    self.txt_manual_response.blockSignals(True)
                    self.txt_manual_response.clear()
                    self.txt_manual_response.blockSignals(False)
                    self._handle_hwpx_clarification_or_more_chunks(target_id, s1, mode="manual")
                    return
                if s1["status"] in ("ready", "fallback"):
                    if s1["status"] == "fallback":
                        selected = fallback_select_chunks_by_keywords(
                            list(self._hwpx_ref_signature),
                            self.txt_hwpx_instruction.toPlainText().strip(),
                        )
                    else:
                        selected = resolve_selected_chunks(
                            list(self._hwpx_ref_signature),
                            chunk_ids=s1.get("chunk_ids"),
                            page_ranges=s1.get("page_ranges"),
                        )
                    self._hwpx_selected_chunk_ids = [
                        str(c.get("chunk_id")) for c in selected if c.get("chunk_id")
                    ]
                    self.txt_manual_response.blockSignals(True)
                    self.txt_manual_response.clear()
                    self.txt_manual_response.blockSignals(False)
                    new_chunks = self._ensure_hwpx_ref_chunks(ctx)
                    self._sync_hwpx_manual_part_combo(new_chunks)
                    next_prompt = self._build_current_manual_prompt()
                    if next_prompt:
                        QApplication.clipboard().setText(next_prompt)
                    self.lbl_manual_verify.setText(
                        f"1단계 선별 완료 ({len(new_chunks)}개 구간) — 2단계 프롬프트가 클립보드에 복사되었습니다!"
                    )
                    self.lbl_dock_progress_meta.setText(
                        f"참고 구간 {len(new_chunks)}개 선별 완료 · 2단계 프롬프트 복사됨"
                    )
                    return

            if self._handle_hwpx_clarification_or_more_chunks(target_id, parsed, mode="manual"):
                self.txt_manual_response.blockSignals(True)
                self.txt_manual_response.clear()
                self.txt_manual_response.blockSignals(False)
                new_chunks = self._ensure_hwpx_ref_chunks(ctx)
                self._sync_hwpx_manual_part_combo(new_chunks)
                next_prompt = self._build_current_manual_prompt()
                if next_prompt:
                    QApplication.clipboard().setText(next_prompt)
                return

            total_parts = len(chunks)
            is_multi_parts = total_parts >= 2
            cur_idx = max(0, self.combo_manual_batch.currentIndex()) if is_multi_parts else 0
            create_or_ref = self._is_hwpx_create_or_ref_mode(ctx)

            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.clear()
            self.txt_manual_response.blockSignals(False)
            self._dispatch_result(
                target_id,
                {
                    "target_id": target_id,
                    "mode": "manual",
                    "parsed_json": parsed,
                    "immediate": bool(is_multi_parts or create_or_ref),
                },
            )

            if is_multi_parts and cur_idx + 1 < total_parts:
                next_idx = cur_idx + 1
                self.combo_manual_batch.setCurrentIndex(next_idx)
                self.sync_from_context(self._fetch_fresh_context(target_id))
                self.combo_manual_batch.setCurrentIndex(next_idx)
                next_prompt = self._build_current_manual_prompt()
                if next_prompt:
                    QApplication.clipboard().setText(next_prompt)
                pct = int(((cur_idx + 1) / total_parts) * 100)
                self.progress_dock.setValue(pct)
                self.lbl_manual_verify.setText(
                    f"Part {cur_idx + 1}/{total_parts} 적용 완료 · Part {next_idx + 1}/{total_parts} 프롬프트 클립보드 복사됨!"
                )
                self.lbl_dock_progress_meta.setText(
                    f"수동 파트 {cur_idx + 1}/{total_parts} 반영 완료"
                )
            elif is_multi_parts:
                self.sync_from_context(self._fetch_fresh_context(target_id))
                self.lbl_manual_verify.setText(
                    f"전체 {total_parts}/{total_parts} 파트 HWPX 문서 반영 완료!"
                )
                self.lbl_dock_progress_meta.setText(f"수동 생성 {total_parts}/{total_parts} 완료")
                self.progress_dock.setValue(100)
            else:
                self.lbl_manual_verify.setText("HWPX JSON Patch가 문서에 적용되었습니다.")
                self.lbl_dock_progress_meta.setText("수동 Patch 반영 완료")
                self.progress_dock.setValue(100)
            return
        adapter = self._get_target_adapter(target_id)
        if adapter is not None:
            try:
                parsed_payload = adapter.parse_manual_or_pw_result(
                    self, raw_resp, ctx, mode="manual"
                )
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "JSON 검증 오류",
                    f"유효한 응답을 추출하지 못했습니다:\n{exc}",
                )
                return
            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.clear()
            self.txt_manual_response.blockSignals(False)
            if parsed_payload.get("is_query_answer"):
                self._append_chat_message(
                    role="assistant",
                    content=str(parsed_payload.get("raw_response") or raw_resp),
                    exec_mode="manual",
                    model_name="Manual Web",
                    tool_name=target_id,
                )
            self._dispatch_result(
                target_id,
                {"target_id": target_id, "mode": "manual", **parsed_payload},
            )
            verify_msg = adapter.format_manual_verify_text(parsed_payload)
            self.lbl_manual_verify.setText(verify_msg)
            self.lbl_dock_progress_meta.setText(verify_msg)
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
        self.ai_mode_changed.emit()

    def _ensure_pw_inline_card(self, message: str = "Playwright 브라우저 작업 진행 중...", *, needs_login: bool = False) -> QFrame:
        if self._pw_inline_card is None:
            card = QFrame(self.chat_timeline_widget)
            _apply_role(card, obj_name="PwInlineStatusCard")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(10, 8, 10, 8)
            lay.setSpacing(6)

            lbl = QLabel(message, card)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-size: 11.5px; color: #1E293B; border: none; background: transparent;")
            lay.addWidget(lbl)

            btn_row = QHBoxLayout()
            btn_row.setSpacing(6)

            btn_login = QPushButton("인증/로그인 완료 · 작업 재개", card)
            _apply_role(btn_login, role="primary", obj_name="BtnInlinePwConfirmLogin")
            btn_login.setFixedHeight(26)
            btn_login.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_login.setVisible(False)
            btn_login.clicked.connect(self._on_pw_confirm_login_clicked)

            btn_stop = QPushButton("작업 중지", card)
            _apply_role(btn_stop, role="danger", obj_name="BtnInlinePwStop")
            btn_stop.setFixedHeight(26)
            btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_stop.clicked.connect(self._on_pw_stop_clicked)

            btn_row.addWidget(btn_login)
            btn_row.addWidget(btn_stop)
            btn_row.addStretch()
            lay.addLayout(btn_row)

            card.lbl_status = lbl  # type: ignore[attr-defined]
            card.btn_confirm_login = btn_login  # type: ignore[attr-defined]
            card.btn_stop = btn_stop  # type: ignore[attr-defined]
            self._pw_inline_card = card
            self.lbl_chat_empty.setVisible(False)
            insert_idx = max(0, self.chat_timeline_lay.count() - 1)
            self.chat_timeline_lay.insertWidget(insert_idx, card)

        card = self._pw_inline_card
        card.lbl_status.setText(message)  # type: ignore[attr-defined]
        card.btn_confirm_login.setVisible(needs_login)  # type: ignore[attr-defined]
        card.btn_confirm_login.setEnabled(needs_login)  # type: ignore[attr-defined]
        if needs_login:
            card.setStyleSheet(
                "QFrame#PwInlineStatusCard { background-color: #FFFBEB; border: 1px solid #F59E0B; border-radius: 10px; }"
            )
        else:
            card.setStyleSheet(
                "QFrame#PwInlineStatusCard { background-color: #F8FAFC; border: 1px solid #CBD5E1; border-radius: 10px; }"
            )
        self._scroll_timeline_to_bottom()
        return card

    def _remove_pw_inline_card(self) -> None:
        if self._pw_inline_card is not None:
            self._pw_inline_card.setVisible(False)
            self._pw_inline_card.deleteLater()
            self._pw_inline_card = None

    def _set_pw_running_state(self, running: bool) -> None:
        self.combo_web_provider.setEnabled(not running)
        self.edit_chat_custom_url.setEnabled(not running)
        self.combo_pw_exec_mode.setEnabled(not running)
        self.chk_pw_keep_login.setEnabled(not running)
        self.chk_pw_temp_chat.setEnabled(not running)
        self.btn_pw_stop.setEnabled(running)
        self.btn_pw_stop.setVisible(running)
        if running:
            self._ensure_pw_inline_card("Playwright 브라우저 작업 진행 중...", needs_login=False)
        else:
            self._pw_waiting_collect = False
            self.btn_pw_collect_next.setEnabled(False)
            self.btn_pw_collect_next.setVisible(False)
            self.btn_pw_confirm_login.setEnabled(False)
            self.btn_pw_confirm_login.setVisible(False)
            self._remove_pw_inline_card()
        self._sync_body_scroll_visibility()

    def _on_pw_start_clicked(self) -> None:
        if self._execute_target_pipeline(2):
            return
        if self._active_pw_worker is not None:
            return
        ctx = self._fetch_fresh_context()
        if not ctx and self._active_target_id:
            ctx = {"target_id": self._active_target_id, "kind": self._active_target_id}
        if not ctx:
            return

        custom_url = ""
        if self._pw_provider == "custom":
            custom_url = normalize_web_url(self.edit_pw_custom_url.text())
            if not custom_url:
                QMessageBox.warning(self, "URL 확인", "직접 URL 모드에서는 접속할 웹 AI 주소를 입력해 주세요.")
                self.edit_pw_custom_url.setFocus()
                return

        exec_mode = str(self.combo_pw_exec_mode.currentData() or "auto")
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

        if kind == "hwpx_agent":
            chunks = self._ensure_hwpx_ref_chunks(ctx)
            if self._hwpx_ref_error:
                QMessageBox.warning(self, "참고 문서 오류", self._hwpx_ref_error)
                return
            inst = self.txt_hwpx_instruction.toPlainText().strip()
            create_or_ref = self._is_hwpx_create_or_ref_mode(ctx)
            if not inst and not chunks:
                msg = (
                    "상단 입력창에 생성할 양식 지시사항을 입력하거나 참고 파일을 첨부해 주세요."
                    if create_or_ref
                    else "중앙 탭에서 HWPX 문서를 열고 수정 지시사항을 입력해 주세요."
                )
                QMessageBox.warning(self, "입력 확인", msg)
                return

            if self._should_run_hwpx_stage1(ctx, chunks):
                idx_text = str((self._hwpx_ref_index_summary or {}).get("index_text") or "").strip()
                if idx_text:
                    self._pw_hwpx_stage1_pending = True
                    s1_sys, s1_usr = build_hwpx_stage1_selection_prompt(
                        self._append_quote_to_prompt(
                            inst or "참고 문서의 구조와 내용을 충실히 반영하여 새 HWPX 양식을 생성하세요."
                        ),
                        idx_text,
                        ctx.get("dom_map") if isinstance(ctx.get("dom_map"), dict) else None,
                    )
                    worker = WebGeminiAutoWorker(
                        excel_path=None,
                        single_prompt=f"{s1_sys}\n\n{s1_usr}",
                        attachment_paths=[],
                        provider=self._pw_provider,
                        custom_url=custom_url,
                        execution_mode=exec_mode,
                        require_login=False,
                        keep_login_session=keep_login,
                        use_temp_chat=use_temp_chat,
                        headless=False,
                        task_label=f"{task_label} (1단계 범위 선별)",
                    )
                else:
                    self._pw_hwpx_stage1_pending = False
            else:
                self._pw_hwpx_stage1_pending = False

            if self._pw_hwpx_stage1_pending:
                pass
            elif len(chunks) >= 2:
                effective_inst = self._append_quote_to_prompt(
                    inst or "참고 문서의 구조와 내용을 충실히 반영하여 새 HWPX 양식을 생성하세요."
                )
                running_doc = _build_running_hwpx_seed_from_dom(ctx.get("dom_map"))
                placeholders = [
                    f"[Part {c['part_index']}/{len(chunks)}] {str(c.get('content', ''))[:80]}"
                    for c in chunks
                ]

                def _build_pw_part_prompt(
                    b_idx: int, total_b: int, prev_res: Optional[Dict[str, Any]]
                ) -> str:
                    if b_idx > 1 and isinstance(prev_res, dict):
                        p_json = prev_res.get("parsed_json")
                        ops = p_json.get("operations") if isinstance(p_json, dict) else None
                        if isinstance(ops, list) and ops:
                            apply_hwpx_operations(
                                running_doc, [o for o in ops if isinstance(o, dict)]
                            )
                    cur_dom = extract_hwpx_dom_map(running_doc)
                    ref_txt = str(chunks[b_idx - 1].get("content", ""))
                    sys_p, usr_p = build_hwpx_create_prompt(
                        user_instruction=effective_inst,
                        reference_chunk=ref_txt,
                        part_index=b_idx,
                        total_parts=total_b,
                        current_dom_map=cur_dom,
                    )
                    return f"{sys_p}\n\n{usr_p}"

                worker = WebGeminiAutoWorker(
                    excel_path=None,
                    single_prompts=placeholders,
                    prompt_builder=_build_pw_part_prompt,
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
                prompt_text = self._build_current_manual_prompt()
                if not prompt_text:
                    QMessageBox.warning(
                        self,
                        "입력 확인",
                        "중앙 탭에서 HWPX 문서를 열고 수정/생성 지시사항을 입력해 주세요.",
                    )
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
        elif kind == "calendar_todo":
            inst = self.txt_calendar_instruction.toPlainText().strip()
            if not inst:
                QMessageBox.warning(
                    self,
                    "입력 확인",
                    "상단 입력창에 일정 요청이나 공문·안내문 텍스트를 입력해 주세요.",
                )
                self.txt_calendar_instruction.setFocus()
                return
            if self._try_handle_direct_calendar_json(inst):
                return
            prompt_text = self._build_current_manual_prompt()
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
        elif kind == "ocr":
            prompt_text = self._build_current_manual_prompt()
            if not prompt_text:
                QMessageBox.warning(
                    self, "입력 확인", "중앙 탭에서 파일이나 보정 지침을 확인해 주세요."
                )
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
        elif kind == "class_notice_digest":
            prompt_text = self._build_current_manual_prompt()
            if not prompt_text:
                QMessageBox.warning(
                    self, "입력 확인", "중앙 '4. 학급 공지·긴급 알림' 탭에서 카톡 대화 또는 요약할 공지를 먼저 입력해 주세요."
                )
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
        self._pw_single_batches_applied = False
        self._set_pw_running_state(True)
        self.progress_dock.setValue(0)
        self.lbl_dock_progress_meta.setText(
            f"[{task_label}] Playwright 브라우저 실행 중..."
            if kind in ("ocr", "hwpx_agent", "calendar_todo", "class_notice_digest")
            else "Playwright 브라우저 실행 중..."
        )
        if kind == "calendar_todo":
            self._append_chat_message(
                role="assistant",
                content="🌐 Playwright 브라우저 자동화를 시작했습니다. 웹 AI에 프롬프트를 전송하고 응답을 회수하는 중입니다...",
                exec_mode="playwright",
                tool_name=target_id,
            )

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_DOCK_PLAYWRIGHT,
            on_result=lambda res, tid=target_id: self._on_pw_finished_result(tid, res),
            on_error=self._on_pw_error,
            on_finished=self._on_pw_finished,
        )

    def _notify_attention(self, title: str, message: str) -> None:
        """로그인/인증/입력창 클릭 대기 시 데스크톱 토스트 알림과 작업 표시줄 깜빡임을 실행합니다."""
        try:
            NotificationService().notify(title, message, force_external=False)
        except Exception:
            pass
        try:
            win = self.window()
            if win is not None:
                QApplication.alert(win, 0)
        except Exception:
            pass

    def run_playwright_single_prompt(
        self,
        *,
        prompt_text: str,
        attachment_paths: List[str],
        task_label: str,
        on_result: Any,
        on_error: Optional[Any] = None,
    ) -> bool:
        """외부 다이얼로그/모듈에서 단일 프롬프트와 첨부 이미지를 Playwright 자동으로 실행하고 콜백으로 결과를 전달합니다."""
        if self._active_pw_worker is not None:
            QMessageBox.information(
                self,
                "Playwright 실행 중",
                "이미 다른 Playwright 자동 작업이 실행 중입니다. 완료 후 다시 시도해 주세요.",
            )
            return False

        custom_url = ""
        if self._pw_provider == "custom":
            custom_url = normalize_web_url(self.edit_pw_custom_url.text())
            if not custom_url:
                QMessageBox.warning(
                    self, "URL 확인", "직접 URL 모드에서는 접속할 웹 AI 주소를 입력해 주세요."
                )
                return False

        exec_mode = str(self.combo_pw_exec_mode.currentData() or "auto")
        keep_login = bool(self.chk_pw_keep_login.isChecked())
        use_temp_chat = bool(self.chk_pw_temp_chat.isChecked())

        worker = WebGeminiAutoWorker(
            excel_path=None,
            single_prompt=str(prompt_text or ""),
            attachment_paths=[str(p).strip() for p in (attachment_paths or []) if str(p).strip()],
            provider=self._pw_provider,
            custom_url=custom_url,
            execution_mode=exec_mode,
            require_login=False,
            keep_login_session=keep_login,
            use_temp_chat=use_temp_chat,
            headless=False,
            task_label=task_label,
        )

        self._active_pw_worker = worker
        self._pw_single_batches_applied = False
        self.btn_pw_start.setEnabled(False)
        self.btn_pw_stop.setEnabled(True)
        self.progress_dock.setValue(0)
        self.lbl_dock_progress_meta.setText(f"[{task_label}] Playwright 브라우저 실행 중...")
        self._append_pw_inline_card(f"[{task_label}] Playwright 브라우저 실행 중...")

        delivered_payload: Dict[str, Any] = {}
        delivered_flag = {"sent": False}

        def _on_batch(payload: Dict[str, Any]) -> None:
            if isinstance(payload, dict):
                delivered_payload.update(payload)
                if payload.get("raw_response") or payload.get("formatted_text") or payload.get("parsed_json"):
                    delivered_flag["sent"] = True
                    if callable(on_result):
                        on_result(payload)

        worker.signals.progress.connect(self._on_dock_worker_progress)
        worker.waiting_for_login.connect(self._on_pw_waiting_for_login)
        worker.waiting_for_semi_auto_collect.connect(self._on_pw_waiting_for_collect)
        worker.waiting_for_element_click.connect(self._on_pw_waiting_for_click)
        worker.batch_completed.connect(_on_batch)

        def _handle_result(res: Dict[str, Any]) -> None:
            if isinstance(res, dict) and res.get("missing_playwright"):
                QMessageBox.warning(
                    self,
                    "Playwright 패키지 필요",
                    "Playwright가 설치되어 있지 않습니다.\n"
                    "터미널에서 'pip install playwright && playwright install chromium'을 실행해 주세요.",
                )
                if callable(on_error):
                    on_error("Playwright 패키지가 설치되어 있지 않습니다.")
                return
            if delivered_flag["sent"]:
                return
            merged = dict(delivered_payload)
            if isinstance(res, dict):
                merged.update({k: v for k, v in res.items() if v not in (None, "")})
            if callable(on_result):
                on_result(merged)

        def _handle_error(err: str) -> None:
            self._on_pw_worker_error(err)
            if callable(on_error):
                on_error(err)

        self.task_runner.start_worker(
            worker,
            task_key=self.TASK_DOCK_PLAYWRIGHT,
            on_result=_handle_result,
            on_error=_handle_error,
            on_finished=self._on_pw_worker_finished,
        )
        return True

    def _set_progress_meta_alert(self, text: str, alert: bool = True) -> None:
        self.lbl_dock_progress_meta.setText(text)
        if alert:
            self.lbl_dock_progress_meta.setStyleSheet(
                "background:#FEF3C7; color:#92400E; padding:4px 8px; border-radius:4px; font-weight:600;"
            )
        else:
            self.lbl_dock_progress_meta.setStyleSheet("")

    def _on_pw_waiting_for_login(self, provider_name: str = "") -> None:
        self.btn_pw_confirm_login.setText("인증/로그인 완료 · 작업 재개")
        self.btn_pw_confirm_login.setVisible(True)
        self.btn_pw_confirm_login.setEnabled(True)
        self._sync_body_scroll_visibility()
        prefix = f"[{provider_name}] " if provider_name else ""
        msg = (
            f"{prefix}브라우저에서 로그인·약관 동의·봇 인증 완료 후 [인증/로그인 완료 · 작업 재개] 버튼을 눌러주세요."
        )
        self._ensure_pw_inline_card(msg, needs_login=True)
        self._set_progress_meta_alert(msg, alert=True)
        self._notify_attention("EduFlow 웹 AI 로그인/인증 대기", msg)

    def _on_pw_waiting_for_click(self, domain: str = "") -> None:
        msg = f"[{domain}] 입력창을 자동 탐지하지 못했습니다. 브라우저에서 입력창을 1회 클릭하세요."
        self._ensure_pw_inline_card(msg, needs_login=False)
        self._set_progress_meta_alert(msg, alert=True)
        self._notify_attention("EduFlow 웹 AI 입력창 클릭 대기", msg)

    def _on_pw_waiting_for_collect(self, batch_idx: int, total_batches: int) -> None:
        self.btn_pw_collect_next.setVisible(True)
        self.btn_pw_collect_next.setEnabled(True)
        self._pw_waiting_collect = True
        self._sync_chat_send_button_state()
        self._sync_body_scroll_visibility()
        msg = f"Batch {batch_idx}/{total_batches} 입력됨 · 웹에서 전송·완료 후 하단 [응답 회수] 버튼을 눌러 주세요."
        self._ensure_pw_inline_card(msg, needs_login=False)
        self.lbl_dock_progress_meta.setText(msg)

    def show_external_login_waiting(
        self,
        worker: Any,
        message: str = "",
        provider_name: str = "학교종이",
    ) -> None:
        """외부 Playwright 워커(예: 학교종이 수집기)의 로그인 대기를 채팅 인라인 카드 + 확인 버튼으로 연동합니다."""
        self._external_login_worker = worker
        self._external_login_provider = str(provider_name or "외부 서비스").strip()
        self.btn_pw_confirm_login.setText("인증/로그인 완료 · 작업 재개")
        self.btn_pw_confirm_login.setVisible(True)
        self.btn_pw_confirm_login.setEnabled(True)
        self._sync_body_scroll_visibility()
        display_msg = str(message or "").strip() or (
            f"[{self._external_login_provider}] 브라우저에서 학교종이 로그인 후, "
            "수집할 우리 학교 또는 우리 반의 학교안내문/알림장 목록 화면까지 이동한 뒤 "
            "[인증/로그인 완료 · 작업 재개]를 눌러주세요."
        )
        self._ensure_pw_inline_card(display_msg, needs_login=True)
        self._set_progress_meta_alert(display_msg, alert=True)
        self._notify_attention(f"EduFlow {self._external_login_provider} 로그인 대기", display_msg)

    def clear_external_login_waiting(self, status_message: str = "") -> None:
        """외부 Playwright 워커의 로그인 대기 UI를 정리합니다."""
        self._external_login_worker = None
        self._external_login_provider = ""
        self.btn_pw_confirm_login.setEnabled(False)
        self.btn_pw_confirm_login.setVisible(False)
        self._remove_pw_inline_card()
        self._set_progress_meta_alert(status_message or "대기 중", alert=False)

    def _on_pw_confirm_login_clicked(self) -> None:
        if self._external_login_worker is not None:
            if hasattr(self._external_login_worker, "confirm_login"):
                self._external_login_worker.confirm_login()
            self.btn_pw_confirm_login.setEnabled(False)
            self.btn_pw_confirm_login.setVisible(False)
            prov = self._external_login_provider or "외부 서비스"
            msg = f"[{prov}] 로그인 확인 완료 — 공지 목록 수집 진행 중..."
            self._ensure_pw_inline_card(msg, needs_login=False)
            self._set_progress_meta_alert(msg, alert=False)
            return
        if self._active_pw_worker is not None:
            self._active_pw_worker.confirm_login()
        self.btn_pw_confirm_login.setEnabled(False)
        self.btn_pw_confirm_login.setVisible(False)
        self._ensure_pw_inline_card("로그인 확인 완료 — 프롬프트 입력 진행 중...", needs_login=False)
        self._set_progress_meta_alert("로그인 확인 완료 — 프롬프트 입력 진행 중...", alert=False)

    def _on_pw_collect_next_clicked(self) -> None:
        if self._active_pw_worker is not None:
            manual_txt = self.txt_manual_response.toPlainText().strip()
            self._active_pw_worker.trigger_collect_and_next(manual_txt)
        self._pw_waiting_collect = False
        self._sync_chat_send_button_state()
        self.btn_pw_collect_next.setEnabled(False)
        self.btn_pw_collect_next.setVisible(False)
        self._ensure_pw_inline_card("응답 회수 및 반영 중...", needs_login=False)
        self.lbl_dock_progress_meta.setText("응답 회수 및 반영 중...")

    def _on_pw_stop_clicked(self) -> None:
        self._pw_next_stage_pending = False
        if self._external_login_worker is not None:
            if hasattr(self._external_login_worker, "request_stop"):
                self._external_login_worker.request_stop()
            self.clear_external_login_waiting("학교종이 수집 중지 요청됨...")
            return
        if self._active_pw_worker is not None:
            self._active_pw_worker.request_stop()
        self.task_runner.stop_task(self.TASK_DOCK_PLAYWRIGHT)
        self._pw_waiting_collect = False
        self._sync_chat_send_button_state()
        self.btn_pw_stop.setEnabled(False)
        self.btn_pw_collect_next.setEnabled(False)
        self.btn_pw_confirm_login.setEnabled(False)
        if self._pw_inline_card is not None:
            self._pw_inline_card.btn_stop.setEnabled(False)  # type: ignore[attr-defined]
            self._pw_inline_card.lbl_status.setText("Playwright 작업 중지 요청됨...")  # type: ignore[attr-defined]
        self.lbl_dock_progress_meta.setText("Playwright 작업 중지 요청됨...")

    def _enrich_pw_payload_with_adapter(
        self, target_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        enriched = {"target_id": target_id, "mode": "playwright", **payload}
        adapter = self._get_target_adapter(target_id)
        if adapter is None:
            return enriched
        raw_txt = str(enriched.get("raw_response") or enriched.get("formatted_text") or "").strip()
        if not raw_txt:
            return enriched
        ctx = self._fetch_fresh_context(target_id)
        try:
            parsed_extra = adapter.parse_manual_or_pw_result(
                self, raw_txt, ctx, mode="playwright"
            )
            enriched.update(parsed_extra)
        except Exception:
            pass
        return enriched

    def _on_pw_batch_completed(self, target_id: str, payload: Dict[str, Any]) -> None:
        if payload.get("is_single_prompt"):
            self._pw_single_batches_applied = True
        enriched = self._enrich_pw_payload_with_adapter(target_id, payload)
        self._dispatch_result(target_id, enriched)
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
        if target_id == "hwpx_agent" and self._pw_hwpx_stage1_pending:
            self._pw_hwpx_stage1_pending = False
            raw_s1 = res.get("parsed_json") or res.get("raw_response") or ""
            try:
                s1 = parse_stage1_selection_response(raw_s1)
            except Exception:
                s1 = {"status": "fallback"}

            if s1["status"] == "needs_clarification":
                self._handle_hwpx_clarification_or_more_chunks(target_id, s1, mode="playwright")
                self.sync_from_context(self._fetch_fresh_context(target_id))
                return

            if s1["status"] != "direct_operations":
                if s1["status"] == "fallback":
                    inst_txt = self.txt_hwpx_instruction.toPlainText().strip()
                    selected = fallback_select_chunks_by_keywords(
                        list(self._hwpx_ref_signature), inst_txt
                    )
                else:
                    selected = resolve_selected_chunks(
                        list(self._hwpx_ref_signature),
                        chunk_ids=s1.get("chunk_ids"),
                        page_ranges=s1.get("page_ranges"),
                    )
                self._hwpx_selected_chunk_ids = [
                    str(c.get("chunk_id")) for c in selected if c.get("chunk_id")
                ]
                self.sync_from_context(self._fetch_fresh_context(target_id))
                self.lbl_dock_progress_meta.setText(
                    f"1단계 범위 선별 완료({len(self._hwpx_selected_chunk_ids)}개 구간) — 2단계 자동 실행 대기 중..."
                )
                self._pw_next_stage_pending = True
                return

        if target_id == "hwpx_agent" and isinstance(res.get("parsed_json"), dict):
            if self._handle_hwpx_clarification_or_more_chunks(target_id, res["parsed_json"], mode="playwright"):
                self.sync_from_context(self._fetch_fresh_context(target_id))
                if self._hwpx_pending_clarification is None and self._hwpx_extra_chunk_round <= MAX_EXTRA_CHUNK_ROUNDS:
                    self._pw_next_stage_pending = True
                return

        if not self._pw_single_batches_applied:
            pw_payload = self._enrich_pw_payload_with_adapter(target_id, res)
            self._dispatch_result(target_id, pw_payload)
        self._pw_single_batches_applied = False
        reply_txt = str(res.get("raw_response", "") or res.get("summary", "") or "").strip()
        if reply_txt:
            provider_label = self.playwright_provider_display()
            self._append_chat_message(
                role="assistant",
                content=reply_txt,
                exec_mode="playwright",
                model_name=f"{provider_label} Web" if provider_label else "Playwright",
                tool_name=target_id or self._active_target_id or "general_chat",
            )
        self.sync_from_context(self._fetch_fresh_context(target_id))

    def _on_pw_error(self, err: str) -> None:
        first = (err or "").strip().split("\n")[0]
        self._set_progress_meta_alert(f"오류: {first}", alert=False)
        self._append_chat_message(
            role="assistant",
            content=f"⚠️ Playwright 실행 오류: {first}",
            exec_mode="playwright",
            tool_name=self._active_target_id or "general_chat",
        )
        QMessageBox.critical(self, "Playwright 실행 오류", first)

    def _on_pw_finished(self) -> None:
        self._active_pw_worker = None
        self._set_pw_running_state(False)
        self.lbl_dock_progress_meta.setStyleSheet("")
        if self._pw_next_stage_pending:
            self._pw_next_stage_pending = False
            QTimer.singleShot(0, self._launch_pending_pw_stage)

    def _launch_pending_pw_stage(self) -> None:
        if self.task_runner.is_running(self.TASK_DOCK_PLAYWRIGHT):
            QTimer.singleShot(25, self._launch_pending_pw_stage)
            return
        self._on_pw_start_clicked()

    def shutdown_workers(self) -> None:
        """앱 종료 시 실행 중인 도크 워커를 안전하게 중단합니다."""
        self._pw_next_stage_pending = False
        if self._active_pw_worker is not None:
            try:
                self._active_pw_worker.request_stop()
            except Exception:
                pass
        self.task_runner.stop_task(self.TASK_DOCK_PLAYWRIGHT, wait_ms=3000, wait=True)
        self.task_runner.stop_task(self.TASK_DOCK_API, wait_ms=3000, wait=True)

    # =========================================================================
    # 공통 상태 / 결과 핸들러
    # =========================================================================
    def _on_dock_worker_progress(self, percent: int, message: str) -> None:
        self.progress_dock.setValue(max(0, min(100, int(percent))))
        if message:
            self.lbl_dock_progress_meta.setText(message)
            if (
                self._pw_inline_card is not None
                and not self._pw_inline_card.btn_confirm_login.isVisible()  # type: ignore[attr-defined]
            ):
                self._pw_inline_card.lbl_status.setText(message)  # type: ignore[attr-defined]

    def _dispatch_result(self, target_id: str, payload: Dict[str, Any]) -> None:
        meta = self._targets.get(target_id)
        if meta and callable(meta.get("on_result")):
            try:
                meta["on_result"](payload)
            except Exception as exc:
                self.lbl_dock_progress_meta.setText(f"결과 반영 경고: {exc}")
        self.execution_completed.emit(target_id, payload)

    # =========================================================================
    # 로컬 DB 기반 챗봇 세션 / 타임라인 / 첨부 / 수정 분기
    # =========================================================================
    def _init_or_restore_session(self) -> None:
        sessions = self.db.get_ai_chat_sessions(limit=1)
        if sessions:
            self.load_session(int(sessions[0]["id"]))
        else:
            self.create_new_session()

    def create_new_session(self, title: str = "새 대화") -> int:
        target_mode = self._active_target_id or "general_chat"
        sid = self.db.create_ai_chat_session(title=title, target_mode=target_mode)
        self._active_session_id = sid
        self._cancel_edit_message()
        self._cancel_manual_collect_waiting()
        self._clear_timeline()
        self.lbl_dock_progress_meta.setText("새 대화 세션이 시작되었습니다.")
        return sid

    def load_session(self, session_id: int) -> None:
        sess = self.db.get_ai_chat_session(int(session_id))
        if sess is None:
            return
        self._active_session_id = int(sess["id"])
        self._cancel_edit_message()
        self._cancel_manual_collect_waiting()
        self._clear_timeline()
        messages = self.db.get_ai_chat_messages(self._active_session_id)
        for msg in messages:
            self._render_message_card(msg)
        self._scroll_timeline_to_bottom()

    def _on_session_menu_clicked(self) -> None:
        sessions = self.db.get_ai_chat_sessions(limit=100)
        self._session_popover.populate(sessions, self._active_session_id)
        btn_pos = self.btn_session_menu.mapToGlobal(self.btn_session_menu.rect().bottomRight())
        pop_x = max(0, btn_pos.x() - self._session_popover.width())
        pop_y = btn_pos.y() + 4
        self._session_popover.move(pop_x, pop_y)
        self._session_popover.show()

    def _on_delete_session_requested(self, session_id: int) -> None:
        self.db.delete_ai_chat_session(int(session_id))
        if self._active_session_id == int(session_id):
            remaining = self.db.get_ai_chat_sessions(limit=1)
            if remaining:
                self.load_session(int(remaining[0]["id"]))
            else:
                self.create_new_session()
        sessions = self.db.get_ai_chat_sessions(limit=100)
        self._session_popover.populate(sessions, self._active_session_id)

    def _clear_timeline(self) -> None:
        self._remove_pw_inline_card()
        if hasattr(self, "manual_extra_paste_bar"):
            self.manual_extra_paste_bar.setVisible(False)
            self.chat_timeline_lay.removeWidget(self.manual_extra_paste_bar)
        for card in self._message_cards:
            card.deleteLater()
        self._message_cards.clear()
        self.lbl_chat_empty.setVisible(True)

    def _render_message_card(self, msg: Dict[str, Any]) -> _ChatMessageCard:
        self.lbl_chat_empty.setVisible(False)
        card = _ChatMessageCard(
            message_id=int(msg.get("id", 0)),
            role=str(msg.get("role", "user")),
            content=str(msg.get("content", "")),
            exec_mode=str(msg.get("exec_mode", "api")),
            model_name=str(msg.get("model_name", "")),
            attachments=msg.get("attachments") if isinstance(msg.get("attachments"), list) else [],
            parent=self.chat_timeline_widget,
        )
        card.edit_requested.connect(self._start_edit_message)
        card.copy_requested.connect(
            lambda _txt: self.lbl_dock_progress_meta.setText("메시지가 클립보드에 복사되었습니다.")
        )
        self._message_cards.append(card)
        # 마지막 stretch 직전에 삽입
        insert_idx = max(0, self.chat_timeline_lay.count() - 1)
        self.chat_timeline_lay.insertWidget(insert_idx, card)
        self._sync_manual_extra_paste_button()
        return card

    def _sync_manual_extra_paste_button(self) -> None:
        """수동 복붙 모드일 때만 가장 마지막 AI 응답 카드 바로 아래에 '+ 추가 답변 붙여넣기' 버튼을 표시합니다."""
        if not hasattr(self, "manual_extra_paste_bar"):
            return
        is_manual = self.current_mode_index() == 1
        last_ai_card = None
        for card in reversed(self._message_cards):
            if getattr(card, "role", "") == "assistant":
                last_ai_card = card
                break
        if not is_manual or (last_ai_card is None and not self._manual_waiting_collect):
            self.manual_extra_paste_bar.setVisible(False)
            self.chat_timeline_lay.removeWidget(self.manual_extra_paste_bar)
            return
        self.chat_timeline_lay.removeWidget(self.manual_extra_paste_bar)
        ai_idx = self.chat_timeline_lay.indexOf(last_ai_card) if last_ai_card is not None else -1
        insert_idx = ai_idx + 1 if ai_idx >= 0 else max(0, self.chat_timeline_lay.count() - 1)
        self.chat_timeline_lay.insertWidget(insert_idx, self.manual_extra_paste_bar)
        self.manual_extra_paste_bar.setVisible(True)
        if self._manual_waiting_collect:
            QTimer.singleShot(0, self._scroll_timeline_to_bottom)

    def _on_manual_extra_paste_clicked(self) -> None:
        if self.current_mode_index() != 1:
            return
        ctx = self._fetch_fresh_context()
        kind = str(ctx.get("kind", "general_chat") or "general_chat")
        dlg = _ManualExtraPasteDialog(self, target_kind=kind)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        raw_resp = dlg.response_text()
        if not raw_resp:
            return

        provider_label = self.manual_provider_display()
        model_label = f"{provider_label} Web" if provider_label else "수동 입력"
        tid = self._active_target_id or "general_chat"
        self._handle_agent_response(
            raw_resp,
            exec_mode="manual",
            model_name=model_label,
            tool_name_default=tid,
            allow_followup=False,
        )
        if tid != "general_chat":
            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.setPlainText(raw_resp)
            self.txt_manual_response.blockSignals(False)
            self._on_manual_apply_and_next()
        self._scroll_timeline_to_bottom()
        self.lbl_dock_progress_meta.setText("추가 수동 응답 적용 완료")

    def _scroll_timeline_to_bottom(self) -> None:
        sb = self.chat_scroll.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def _append_chat_message(
        self,
        role: str,
        content: str,
        exec_mode: str = "api",
        model_name: str = "",
        tool_name: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        if self._active_session_id is None:
            self.create_new_session()
        sid = int(self._active_session_id or 0)
        atts = attachments or []
        msg_id = self.db.add_ai_chat_message(
            session_id=sid,
            role=role,
            content=content,
            exec_mode=exec_mode,
            model_name=model_name,
            tool_name=tool_name,
            attachments=atts,
        )
        # 첫 사용자 질문 시 세션 제목 자동 요약
        if role == "user":
            sess = self.db.get_ai_chat_session(sid)
            if sess and str(sess.get("title", "")) == "새 대화":
                first_line = re.sub(r"\s+", " ", (content or "").strip())
                new_title = (first_line[:24] + "...") if len(first_line) > 24 else (first_line or "새 대화")
                self.db.update_ai_chat_session(sid, title=new_title)
        self._render_message_card(
            {
                "id": msg_id,
                "role": role,
                "content": content,
                "exec_mode": exec_mode,
                "model_name": model_name,
                "tool_name": tool_name,
                "attachments": atts,
            }
        )
        self._scroll_timeline_to_bottom()
        return msg_id

    def _start_edit_message(self, message_id: int) -> None:
        if self._active_session_id is None:
            return
        messages = self.db.get_ai_chat_messages(self._active_session_id)
        target_msg = next((m for m in messages if int(m["id"]) == int(message_id)), None)
        if target_msg is None:
            return
        self._editing_message_id = int(message_id)
        self.txt_chat_input.setPlainText(str(target_msg.get("content", "")))
        self.txt_chat_input.setFocus()
        self._chat_attachments = list(target_msg.get("attachments") or [])
        self._refresh_attachment_chips()
        self.edit_branch_banner.setVisible(True)

    def _cancel_edit_message(self) -> None:
        self._editing_message_id = None
        if hasattr(self, "edit_branch_banner"):
            self.edit_branch_banner.setVisible(False)

    # -------------------------------------------------------------------------
    # 첨부 파일 / 폴더 칩 관리
    # -------------------------------------------------------------------------
    def _on_attach_file_clicked(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "첨부할 파일 선택")
        for p in paths:
            self._add_attachment_path(p)

    def _on_attach_folder_clicked(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "첨부할 폴더 선택")
        if folder:
            self._add_attachment_path(folder)

    def _on_chat_paths_dropped(self, paths: List[str]) -> None:
        for p in paths:
            self._add_attachment_path(p)

    def _add_attachment_path(self, path_str: str) -> None:
        entry = build_attachment_entry(path_str)
        if entry is None:
            return
        existing_paths = {str(a.get("path", "")) for a in self._chat_attachments}
        if entry["path"] in existing_paths:
            return
        self._chat_attachments.append(entry)
        self._refresh_attachment_chips()

    def _remove_attachment(self, idx: int) -> None:
        if 0 <= idx < len(self._chat_attachments):
            self._chat_attachments.pop(idx)
            self._refresh_attachment_chips()

    def _refresh_attachment_chips(self) -> None:
        while self.attachment_chips_lay.count():
            item = self.attachment_chips_lay.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()

        if not self._chat_attachments:
            self.attachment_chips_widget.setVisible(False)
            return

        self.attachment_chips_widget.setVisible(True)
        for idx, att in enumerate(self._chat_attachments):
            chip = QFrame(self.attachment_chips_widget)
            chip.setStyleSheet(
                "QFrame { background-color: #F1F5F9; border: 1px solid #CBD5E1; border-radius: 6px; }"
            )
            c_lay = QHBoxLayout(chip)
            c_lay.setContentsMargins(6, 2, 4, 2)
            c_lay.setSpacing(4)
            is_dir = att.get("type") == "dir"
            prefix = "[폴더]" if is_dir else "[파일]"
            cnt_suffix = f" ({att.get('file_count', 0)}개)" if is_dir else ""
            lbl = QLabel(f"{prefix} {att.get('name', '')}{cnt_suffix}")
            lbl.setToolTip(str(att.get("path", "")))
            lbl.setStyleSheet("font-size: 10.5px; color: #334155;")
            btn_rm = QPushButton("×")
            btn_rm.setFixedSize(16, 16)
            btn_rm.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_rm.setStyleSheet(
                "QPushButton { background: transparent; border: none; color: #64748B; font-weight: 700; }"
                "QPushButton:hover { color: #DC2626; }"
            )
            btn_rm.clicked.connect(lambda _c=False, i=idx: self._remove_attachment(i))
            c_lay.addWidget(lbl, stretch=1)
            c_lay.addWidget(btn_rm)
            self.attachment_chips_lay.addWidget(chip)

    # -------------------------------------------------------------------------
    # 챗 입력 전송 및 3-모드 실행 (범용 챗 & 타겟 연동)
    # -------------------------------------------------------------------------
    def _current_api_model_label(self) -> str:
        info = get_active_ai_info()
        model = str(info.get("responded_model") or info.get("model") or "").strip()
        p_disp = str(info.get("provider_display", "Gemini") or "Gemini").strip()
        if model:
            return model if p_disp.lower() in model.lower() else f"{p_disp} {model}"
        return p_disp

    def _build_chat_prompt_with_attachments(
        self,
        text: str,
        attachments: List[Dict[str, Any]],
        mode_idx: Optional[int] = None,
    ) -> str:
        eff_mode = self.current_mode_index() if mode_idx is None else int(mode_idx)
        prep = prepare_attachments_for_mode(attachments, mode_idx=eff_mode)
        att_ctx = str(prep.get("prompt_context", "") or "").strip()
        base = self._append_quote_to_prompt(text)
        if att_ctx:
            return f"{base}\n\n[첨부 컨텍스트]\n{att_ctx}"
        return base

    def _append_action_approval_card(
        self,
        tool_name: str,
        tool_label: str,
        args: Dict[str, Any],
    ) -> QFrame:
        card = QFrame(self.chat_timeline_widget)
        _apply_role(card, obj_name="AgentActionApprovalCard")
        card.setStyleSheet(
            "QFrame#AgentActionApprovalCard { "
            "background-color: #EFF6FF; border: 1px solid #93C5FD; border-radius: 10px; }"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        arg_summary = summarize_tool_args(tool_name, args)
        lbl = QLabel(
            f"[실행 승인 필요] {tool_label} ({tool_name})\n인자: {arg_summary}",
            card,
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size: 11.5px; color: #1E3A8A; border: none; background: transparent;")
        lay.addWidget(lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_approve = QPushButton("실행 승인", card)
        _apply_role(btn_approve, role="primary", obj_name="BtnAgentApproveTool")
        btn_approve.setFixedHeight(26)
        btn_approve.setCursor(Qt.CursorShape.PointingHandCursor)

        btn_cancel = QPushButton("취소", card)
        _apply_role(btn_cancel, role="outline", obj_name="BtnAgentCancelTool")
        btn_cancel.setFixedHeight(26)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)

        btn_row.addWidget(btn_approve)
        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        def _on_approve() -> None:
            btn_approve.setEnabled(False)
            btn_cancel.setEnabled(False)
            res = execute_action_tool(tool_name, args, db=self.db)
            if tool_name == "process_hwpx_or_ocr" and res.get("ok"):
                tgt = str((res.get("data") or {}).get("target_id", "") or "").strip()
                if tgt:
                    self.set_target(tgt, force=True)
            elif tool_name == "parse_notice_pdf" and res.get("ok"):
                data = res.get("data") or {}
                self._dispatch_result("notice_ocr", data)
                self.set_target("notice_ocr", force=True)
            card.setVisible(False)
            card.deleteLater()
            self._append_chat_message(
                role="assistant",
                content=f"[{tool_label} 실행 결과]\n{res.get('summary', '')}",
                exec_mode="agent_tool",
                model_name="EduFlow Tool",
                tool_name=tool_name,
            )
            self.lbl_dock_progress_meta.setText(str(res.get("summary", "도구 실행 완료")))

        def _on_cancel() -> None:
            card.setVisible(False)
            card.deleteLater()
            self.lbl_dock_progress_meta.setText(f"[{tool_label}] 실행이 취소되었습니다.")

        btn_approve.clicked.connect(_on_approve)
        btn_cancel.clicked.connect(_on_cancel)

        card.lbl_summary = lbl  # type: ignore[attr-defined]
        card.btn_approve = btn_approve  # type: ignore[attr-defined]
        card.btn_cancel = btn_cancel  # type: ignore[attr-defined]

        self.lbl_chat_empty.setVisible(False)
        insert_idx = max(0, self.chat_timeline_lay.count() - 1)
        self.chat_timeline_lay.insertWidget(insert_idx, card)
        self._scroll_timeline_to_bottom()
        return card

    def _handle_agent_response(
        self,
        raw_reply: str,
        *,
        exec_mode: str,
        model_name: str,
        tool_name_default: str = "general_chat",
        allow_followup: bool = True,
    ) -> None:
        txt = str(raw_reply or "").strip()
        if not txt:
            return
        if (self._active_target_id or "general_chat") != "general_chat":
            self._append_chat_message(
                role="assistant",
                content=txt,
                exec_mode=exec_mode,
                model_name=model_name,
                tool_name=tool_name_default,
            )
            return

        parsed = parse_agent_response(txt)
        if not parsed.get("is_tool_call"):
            self._append_chat_message(
                role="assistant",
                content=txt,
                exec_mode=exec_mode,
                model_name=model_name,
                tool_name="general_chat",
            )
            return

        t_name = str(parsed.get("tool_name", ""))
        t_kind = str(parsed.get("tool_kind", "read"))
        t_label = str(parsed.get("tool_label", t_name))
        args = parsed.get("args") or {}
        reply_intro = str(parsed.get("reply", "") or "").strip()

        if t_kind == "read":
            tool_res = execute_read_only_tool(
                t_name,
                args,
                db=self.db,
                session_id=self._active_session_id,
            )
            data_str = json.dumps(tool_res.get("data", {}), ensure_ascii=False, indent=2)[:2500]
            if exec_mode in ("api", "playwright") and allow_followup:
                followup_prompt = (
                    f"[조회형 도구 실행 완료: {t_label} ({t_name})]\n"
                    f"요약: {tool_res.get('summary', '')}\n"
                    f"조회 데이터:\n{data_str}\n\n"
                    "위 실제 조회 결과를 바탕으로 사용자 질문에 최종 답변을 한국어로 작성하세요. (더 이상 도구 JSON을 출력하지 마세요.)"
                )
                if exec_mode == "api":
                    self._execute_general_chat_api(
                        followup_prompt,
                        allow_followup=False,
                        tool_name_override=t_name,
                    )
                else:
                    self._active_pw_worker = None
                    self._execute_general_chat_playwright(
                        followup_prompt,
                        allow_followup=False,
                        tool_name_override=t_name,
                    )
                return

            combined = (
                f"{reply_intro}\n\n" if reply_intro else ""
            ) + f"[도구 자동 조회: {t_label}]\n{tool_res.get('summary', '')}\n{data_str}"
            self._append_chat_message(
                role="assistant",
                content=combined.strip(),
                exec_mode=exec_mode,
                model_name=model_name,
                tool_name=t_name,
            )
            return

        # 실행/변경형 도구: 안내 문구 말풍선(선택) + 인라인 승인 카드
        if reply_intro:
            self._append_chat_message(
                role="assistant",
                content=reply_intro,
                exec_mode=exec_mode,
                model_name=model_name,
                tool_name=t_name,
            )
        self._append_action_approval_card(t_name, t_label, args)

    @staticmethod
    def _open_external_web_url(url: str) -> bool:
        """
        Windows 등에서 기본 웹 브라우저(크롬 등) 새 탭이 확실히 열리도록
        QDesktopServices.openUrl -> os.startfile -> webbrowser.open_new_tab 순으로 시도합니다.
        """
        target_url = str(url or "").strip()
        if not target_url:
            return False
        try:
            if QDesktopServices.openUrl(QUrl(target_url)):
                return True
        except Exception:
            pass
        if sys.platform == "win32" and hasattr(os, "startfile"):
            try:
                os.startfile(target_url)  # type: ignore[attr-defined]
                return True
            except Exception:
                pass
        try:
            return bool(webbrowser.open_new_tab(target_url))
        except Exception:
            return False

    def _copy_and_open_manual_web(self, prompt_text: str) -> None:
        QApplication.clipboard().setText(prompt_text)
        self._enter_manual_collect_waiting(prompt_text)
        provider_key = str(self.combo_web_provider.currentData() or self._manual_provider or "gemini")
        url = self._resolve_target_web_url(provider_key, self.edit_chat_custom_url)
        if not url:
            QMessageBox.information(
                self,
                "URL 확인",
                "직접 URL 모드에서는 접속할 웹 AI 주소를 입력해 주세요.\n프롬프트는 클립보드에 복사되었습니다.",
            )
            self.edit_chat_custom_url.setFocus()
            self.lbl_dock_progress_meta.setText(
                "프롬프트가 복사되었습니다. 웹 AI 주소를 입력하거나 붙여넣기 후 [응답 회수]를 눌러주세요."
            )
            return
        opened = self._open_external_web_url(url)
        if opened:
            self.lbl_dock_progress_meta.setText(
                f"프롬프트 복사 완료 · 웹 브라우저 열기: {url} (Ctrl+V 후 응답을 복사해 [응답 회수] 또는 [+ 추가 답변 붙여넣기])"
            )
        else:
            self.lbl_dock_progress_meta.setText(
                f"프롬프트 복사 완료 · 웹 브라우저 열기 실패 ({url}) — 브라우저에서 직접 열어 붙여넣기 해주세요."
            )

    def _notify_manual_attachments(self, target_id: str, att_paths: List[Path]) -> None:
        """수동 복붙 모드에서 첨부파일 안내를 타임라인에 표시하고 탐색기를 엽니다."""
        if not att_paths:
            return
        file_names = ", ".join(p.name for p in att_paths)
        self._append_chat_message(
            role="assistant",
            content=(
                f"📎 첨부파일 안내: {file_names}\n"
                "열린 웹 AI 창으로 파일을 드래그하여 함께 첨부한 뒤, 답변을 복사해 [응답 회수]를 눌러주세요."
            ),
            exec_mode="manual",
            tool_name=target_id,
        )
        try:
            self._open_attachment_in_explorer(att_paths)
        except Exception:
            pass

    def _on_manual_open_web_clicked(self) -> None:
        provider_key = str(self.combo_web_provider.currentData() or self._manual_provider or "gemini")
        url = self._resolve_target_web_url(provider_key, self.edit_chat_custom_url)
        if not url:
            QMessageBox.information(
                self,
                "URL 확인",
                "직접 URL 모드에서는 접속할 웹 AI 주소를 입력해 주세요.",
            )
            self.edit_chat_custom_url.setFocus()
            return
        opened = self._open_external_web_url(url)
        self.lbl_dock_progress_meta.setText(
            f"웹 브라우저 열기: {url}"
            if opened
            else f"웹 브라우저 열기 실패 ({url}) — 브라우저에서 직접 열어 주세요."
        )

    def prepare_manual_prompt_for_target(self, target_id: str) -> str:
        """지정 타겟의 수동 복붙용 프롬프트를 패널 타임라인·클립보드에 띄우고 웹 AI 창을 열어 응답 회수 대기로 전환합니다."""
        self.set_target(target_id, force=True)
        ctx = self._fetch_fresh_context()
        prompt_text = self._build_current_manual_prompt()
        if not prompt_text:
            msg = "수동 복붙 중단: 생성된 프롬프트가 비어 있습니다. 지시사항이나 문서를 확인해 주세요."
            self.lbl_dock_progress_meta.setText(msg)
            self._append_chat_message(
                role="assistant",
                content=f"⚠️ {msg}",
                exec_mode="manual",
                tool_name=target_id,
            )
            return ""
        att_paths = self._extract_context_attachment_paths(ctx)
        self._append_chat_message(
            role="user",
            content=prompt_text,
            exec_mode="manual",
            tool_name=target_id,
            attachments=[{"path": str(p), "name": p.name} for p in att_paths],
        )
        self._notify_manual_attachments(target_id, att_paths)
        self._copy_and_open_manual_web(prompt_text)
        return prompt_text

    def _enter_manual_collect_waiting(self, prompt_text: str) -> None:
        self._last_manual_prompt = str(prompt_text or "")
        self._manual_waiting_collect = True
        self.btn_chat_send.setText("응답 회수")
        self.btn_chat_send.setToolTip("입력창 또는 클립보드의 웹 AI 응답을 회수하여 등록합니다.")
        self.txt_chat_input.setPlaceholderText(
            "웹 AI 응답(JSON 또는 답변)을 여기에 붙여넣고 [응답 회수]를 누르세요.\n"
            "(응답을 복사한 상태라면 입력창이 비어 있어도 [응답 회수]로 자동 회수됩니다.)"
        )
        self.btn_chat_cancel_collect.setVisible(True)
        self._sync_manual_extra_paste_button()

    def _cancel_manual_collect_waiting(self) -> None:
        self._manual_waiting_collect = False
        self.btn_chat_send.setText("전송")
        self.btn_chat_send.setToolTip("")
        self.btn_chat_cancel_collect.setVisible(False)
        self._sync_chat_send_button_state()
        self._sync_manual_extra_paste_button()

    def _on_manual_collect_from_chat(self) -> None:
        input_txt = self.txt_chat_input.toPlainText().strip()
        from_input = bool(input_txt)
        candidate = input_txt if from_input else (QApplication.clipboard().text() or "").strip()
        if not candidate:
            self.lbl_dock_progress_meta.setText(
                "회수할 응답이 없습니다. 웹 AI 응답을 복사하거나 입력창에 붙여넣어 주세요."
            )
            return
        if not from_input and self._last_manual_prompt and candidate == self._last_manual_prompt.strip():
            self.lbl_dock_progress_meta.setText(
                "클립보드에 직전 전송 프롬프트가 그대로 있습니다. 웹 AI 응답을 복사한 뒤 다시 눌러 주세요."
            )
            return

        if from_input:
            self.txt_chat_input.clear()

        provider_label = self.manual_provider_display()
        model_label = f"{provider_label} Web" if provider_label else "수동 입력"
        tid = self._active_target_id or "general_chat"
        self._handle_agent_response(
            candidate,
            exec_mode="manual",
            model_name=model_label,
            tool_name_default=tid,
            allow_followup=False,
        )
        self._cancel_manual_collect_waiting()
        if tid != "general_chat":
            self.txt_manual_response.blockSignals(True)
            self.txt_manual_response.setPlainText(candidate)
            self.txt_manual_response.blockSignals(False)
            self._on_manual_apply_and_next()
        self.lbl_dock_progress_meta.setText("수동 응답 회수 완료")

    def _try_handle_direct_calendar_json(self, raw_text: str, *, from_chat: bool = False) -> bool:
        """
        사용자가 입력창에 유효한 일정·할일 JSON(배열/객체 또는 ```json 코드블록)을 직접 붙여넣은 경우
        AI API/Playwright를 호출하지 않고 즉시 파싱해 CalendarTodoTab으로 전달합니다.
        """
        parsed_cal = try_parse_direct_calendar_json(raw_text)
        if parsed_cal is None:
            return False
        s_cnt = len(parsed_cal.get("schedules") or [])
        t_cnt = len(parsed_cal.get("todos") or [])
        if from_chat:
            self._append_chat_message(
                role="assistant",
                content=f"입력하신 JSON을 직접 파싱했습니다 (일정 {s_cnt}건, 할일 {t_cnt}건). 등록 확인 창으로 전달합니다.",
                exec_mode="direct_json",
                model_name="JSON 직접 입력",
                tool_name="calendar_todo",
            )
        self._dispatch_result(
            "calendar_todo",
            {
                "target_id": "calendar_todo",
                "mode": "direct_json",
                "raw_response": raw_text,
                "parsed_calendar": parsed_cal,
            },
        )
        self.lbl_dock_progress_meta.setText(
            f"JSON 직접 입력 파싱 완료 · 일정 {s_cnt}건, 할일 {t_cnt}건"
        )
        self.progress_dock.setValue(100)
        return True

    def _on_chat_send_clicked(self) -> None:
        if self._pw_waiting_collect:
            self._on_pw_collect_next_clicked()
            return
        if self._manual_waiting_collect:
            self._on_manual_collect_from_chat()
            return

        tid = self._active_target_id or "general_chat"
        raw_text = self.txt_chat_input.toPlainText().strip()
        attachments = list(self._chat_attachments)
        if not raw_text and not attachments and tid == "general_chat":
            return

        # 수정-재전송 분기 처리: 해당 메시지 포함 이후 대화 삭제 후 재구성
        if self._editing_message_id is not None and self._active_session_id is not None:
            self.db.delete_ai_chat_messages_from(
                self._active_session_id, self._editing_message_id, inclusive=True
            )
            self._cancel_edit_message()
            self.load_session(self._active_session_id)

        # 특수 타겟의 지시사항 입력창과 연동
        if tid == "hwpx_agent":
            if self._hwpx_pending_clarification:
                orig = str(self._hwpx_pending_clarification.get("original_instruction") or "").strip()
                combined = f"{orig}\n[사용자 추가 조건]: {raw_text}".strip() if orig else raw_text
                self.txt_hwpx_instruction.setPlainText(combined)
                self._hwpx_pending_clarification = None
            else:
                self.txt_hwpx_instruction.setPlainText(raw_text)
        elif tid == "calendar_todo":
            if raw_text:
                self.txt_calendar_instruction.setPlainText(raw_text)

        self._chat_extra_instruction = raw_text
        self._chat_extra_attachments = list(attachments)

        self.txt_chat_input.clear()
        self._chat_attachments.clear()
        self._refresh_attachment_chips()

        mode_idx = self.current_mode_index()
        exec_mode = {0: "api", 1: "manual", 2: "playwright"}.get(mode_idx, "api")

        # 사용자 말풍선 DB 저장 & 타임라인 추가
        self._append_chat_message(
            role="user",
            content=raw_text or ("(첨부 컨텍스트 전송)" if attachments else f"[{tid}] 실행 요청"),
            exec_mode=exec_mode,
            tool_name=tid,
            attachments=attachments,
        )

        if tid != "general_chat":
            if tid == "calendar_todo" and raw_text:
                if self._try_handle_direct_calendar_json(raw_text, from_chat=True):
                    return
            if self._execute_target_pipeline(mode_idx):
                return
            if mode_idx == 0:
                self._on_api_start_clicked()
            elif mode_idx == 1:
                prompt_text = self._build_current_manual_prompt()
                if not prompt_text:
                    QMessageBox.information(
                        self,
                        "프롬프트 확인",
                        "복사할 프롬프트가 없습니다. 대상 파일과 배치를 확인해 주세요.",
                    )
                    return
                att_paths = self._extract_context_attachment_paths()
                self._notify_manual_attachments(tid, att_paths)
                self._copy_and_open_manual_web(prompt_text)
            else:
                self._on_pw_start_clicked()
            return

        full_prompt = self._build_chat_prompt_with_attachments(raw_text, attachments, mode_idx=mode_idx)
        if mode_idx == 0:
            self._execute_general_chat_api(full_prompt)
        elif mode_idx == 1:
            self._execute_general_chat_manual(full_prompt)
        else:
            prep = prepare_attachments_for_mode(attachments, mode_idx=2)
            self._execute_general_chat_playwright(full_prompt, prep.get("pw_attach_paths") or [])

    def _execute_general_chat_api(
        self,
        full_prompt: str,
        *,
        allow_followup: bool = True,
        tool_name_override: str = "general_chat",
    ) -> None:
        if self._is_api_running and allow_followup:
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

        history = (
            self.db.get_ai_chat_messages(self._active_session_id)[:-1]
            if self._active_session_id is not None
            else []
        )
        model_label = self._current_api_model_label()
        sys_p = (
            None
            if allow_followup
            else "당신은 EduFlow AI 어시스턴트입니다. 제공된 도구 조회 결과를 바탕으로 사용자에게 최종 한국어 답변만 작성하세요."
        )
        worker = _DockApiGeneralChatWorker(
            prompt=full_prompt,
            history=history,
            model_label=model_label,
            system_prompt=sys_p,
        )
        worker.signals.progress.connect(self._on_dock_worker_progress)
        self._set_api_busy(True)
        self.progress_dock.setValue(0)
        self.lbl_dock_progress_meta.setText("범용 챗 API 응답 생성 중...")

        def _on_res(res: Dict[str, Any]) -> None:
            reply_txt = str(res.get("raw_response", "") or "").strip()
            self._set_api_busy(False)
            if reply_txt:
                self._handle_agent_response(
                    reply_txt,
                    exec_mode="api",
                    model_name=str(res.get("model_name", "") or model_label),
                    tool_name_default=tool_name_override,
                    allow_followup=allow_followup,
                )
            self.lbl_dock_progress_meta.setText("범용 챗 API 응답 완료")

        def _on_err(err: str) -> None:
            first = (err or "").strip().split("\n")[0]
            self.lbl_dock_progress_meta.setText(f"오류: {first}")
            self._append_chat_message(
                role="assistant",
                content=f"⚠️ API 실행 오류: {first}",
                exec_mode="api",
                tool_name=target_id,
            )
            QMessageBox.critical(self, "API 실행 오류", first)

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_DOCK_API,
            on_result=_on_res,
            on_error=_on_err,
            on_finished=lambda: self._set_api_busy(False),
        )

    def _execute_general_chat_manual(self, full_prompt: str) -> None:
        agent_prompt = f"{build_agent_system_prompt()}\n\n[사용자 요청]\n{full_prompt}"
        self._copy_and_open_manual_web(agent_prompt)

    def _on_manual_register_chat_response(self) -> None:
        raw_resp = self.txt_manual_response.toPlainText().strip()
        if not raw_resp:
            QMessageBox.warning(self, "응답 확인", "등록할 AI 응답 텍스트를 먼저 붙여넣어 주세요.")
            return
        provider_label = self.manual_provider_display()
        model_label = f"{provider_label} Web" if provider_label else "수동 입력"
        self.txt_manual_response.blockSignals(True)
        self.txt_manual_response.clear()
        self.txt_manual_response.blockSignals(False)
        self._append_chat_message(
            role="assistant",
            content=raw_resp,
            exec_mode="manual",
            model_name=model_label,
            tool_name=self._active_target_id or "general_chat",
        )
        self.lbl_manual_verify.setText("AI 응답이 대화 타임라인에 등록되었습니다.")
        self.lbl_dock_progress_meta.setText("수동 응답 등록 완료")

    def _execute_general_chat_playwright(
        self,
        full_prompt: str,
        attachment_paths: Optional[List[str]] = None,
        *,
        allow_followup: bool = True,
        tool_name_override: str = "general_chat",
    ) -> None:
        if self._active_pw_worker is not None and allow_followup:
            return
        custom_url = ""
        if self._pw_provider == "custom":
            custom_url = normalize_web_url(self.edit_pw_custom_url.text())
            if not custom_url:
                QMessageBox.warning(self, "URL 확인", "직접 URL 모드에서는 접속할 웹 AI 주소를 입력해 주세요.")
                self.edit_pw_custom_url.setFocus()
                return

        exec_mode = str(self.combo_pw_exec_mode.currentData() or "auto")
        keep_login = bool(self.chk_pw_keep_login.isChecked())
        use_temp_chat = bool(self.chk_pw_temp_chat.isChecked())
        provider_label = self.playwright_provider_display()

        single_p = (
            f"{build_agent_system_prompt()}\n\n[사용자 요청]\n{full_prompt}"
            if allow_followup
            else full_prompt
        )
        worker = WebGeminiAutoWorker(
            excel_path=None,
            single_prompt=single_p,
            attachment_paths=attachment_paths or [],
            provider=self._pw_provider,
            custom_url=custom_url,
            execution_mode=exec_mode,
            require_login=False,
            keep_login_session=keep_login,
            use_temp_chat=use_temp_chat,
            headless=False,
            task_label="범용 AI 챗",
        )
        worker.signals.progress.connect(self._on_dock_worker_progress)
        worker.waiting_for_login.connect(self._on_pw_waiting_for_login)
        worker.waiting_for_semi_auto_collect.connect(self._on_pw_waiting_for_collect)
        worker.waiting_for_element_click.connect(self._on_pw_waiting_for_click)

        self._active_pw_worker = worker
        self._pw_single_batches_applied = False
        self._set_pw_running_state(True)
        self.progress_dock.setValue(0)
        self.lbl_dock_progress_meta.setText("[범용 AI 챗] Playwright 브라우저 실행 중...")

        def _on_pw_chat_res(res: Dict[str, Any]) -> None:
            if res.get("missing_playwright"):
                QMessageBox.warning(
                    self,
                    "Playwright 패키지 필요",
                    "Playwright가 설치되어 있지 않습니다.\n"
                    "터미널에서 'pip install playwright && playwright install chromium'을 실행해 주세요.",
                )
                return
            reply_txt = str(res.get("raw_response", "") or res.get("response_text", "") or "").strip()
            if reply_txt:
                self._handle_agent_response(
                    reply_txt,
                    exec_mode="playwright",
                    model_name=f"{provider_label} Web",
                    tool_name_default=tool_name_override,
                    allow_followup=allow_followup,
                )
            self.lbl_dock_progress_meta.setText("Playwright 범용 챗 완료")

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_DOCK_PLAYWRIGHT,
            on_result=_on_pw_chat_res,
            on_error=self._on_pw_error,
            on_finished=self._on_pw_finished,
        )
