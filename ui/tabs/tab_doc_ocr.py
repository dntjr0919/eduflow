"""
[탭 2] 문서 처리 & AI OCR / 파일 병합 (ui/tabs/tab_doc_ocr.py)
- 기존 QGroupBox를 HoverZoomCard(1초 호버 시 Z-order 최상단 부상 + OutCubic 대각선 슬라이딩 확대/축소)로 교체
- 1순위 핵심 구현: [학번/이름 기준 제출 파일 분석 · 미제출자 대조 · 표준 파일명 일괄 변경 · 학번순 단일 PDF 병합]
- 2순위 구현: [2. AI OCR 및 문서 텍스트 보정] 서브탭 하단 실행 영역에 QHBoxLayout으로
  1) [AI 자동 실행 (API 사용)] 버튼 (기존 워커 연결)
  2) [수동 복붙 모드 열기] 버튼 (클릭 시 ManualLlmDialog 팝업 연결)
  을 나란히 배치
- '제출 파일 학번순 정렬 미리보기' 카드 및 '미제출자 명단' 카드에 [전체화면] / [원래 크기] 포커스 토글 버튼 제공
"""

import csv
import html
import mimetypes
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from PyQt6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import BaseWorker, TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.docx_extractor_service import (
    extract_docx_to_excel,
    load_all_schemas,
    load_single_schema,
)
from services.batch_prompt_service import resolve_batch_output_path
from services.excel_merger_service import merge_excel_assignments
from services.record_generator_service import (
    generate_records_from_excel,
    get_active_ai_info,
    get_gemini_api_key,
    is_blank_or_failed,
    list_prompt_files,
    calculate_neis_bytes,
)
from services.llm_client import (
    MultiLlmClient,
    build_structured_prompt,
    extract_json_from_llm_response,
    format_parsed_json_to_readable_text,
)
from services.notifier import NotificationService
from ui.components.hover_zoom_card import HoverZoomCard
from ui.dialogs.batch_manual_prompt_dialog import BatchManualPromptDialog
from ui.dialogs.manual_llm_dialog import ManualLlmDialog
from ui.dialogs.docx_schema_create_dialog import DocxSchemaCreateDialog
from ui.dialogs.prompt_edit_dialog import PromptEditDialog
from ui.utils import install_no_scroll_wheel_filter

try:
    from pypdf import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    try:
        from PyPDF2 import PdfReader, PdfWriter  # type: ignore
        HAS_PYPDF = True
    except ImportError:
        HAS_PYPDF = False
        PdfReader = None  # type: ignore
        PdfWriter = None  # type: ignore


SUPPORTED_SUBMISSION_EXTS = {
    ".pdf", ".hwp", ".hwpx", ".doc", ".docx",
    ".xls", ".xlsx", ".csv", ".ppt", ".pptx",
    ".txt", ".zip", ".jpg", ".jpeg", ".png",
}


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


def build_floating_tooltip(title: str, lines: List[str]) -> str:
    """테이블/목록 항목 위에 마우스를 올렸을 때 플로팅 팝업 카드 형태로 전체 내용을 보여주는 HTML 툴팁."""
    escaped_title = html.escape(title)
    body_html = "<br/>".join(lines)
    return (
        f"<div style='min-width: 260px; padding: 2px 4px;'>"
        f"<div style='color: #60a5fa; font-weight: 700; margin-bottom: 4px;'>{escaped_title}</div>"
        f"<div style='color: #f8fafc; line-height: 1.45;'>{body_html}</div>"
        f"</div>"
    )


def _student_sort_key(item: Dict[str, Any]) -> Tuple[int, int, str, str]:
    sid = str(item.get("student_id", "")).strip()
    if sid.isdigit():
        return (0, int(sid), str(item.get("name", "")), str(item.get("orig_name", "")))
    elif sid:
        return (1, 999999, sid, str(item.get("orig_name", "")))
    return (2, 999999, str(item.get("name", "")), str(item.get("orig_name", "")))


def build_standard_filename(
    student_id: str,
    student_name: str,
    assignment_title: str,
    ext: str,
) -> str:
    sid_clean = re.sub(r'[\\/:*?"<>|]', "", (student_id or "학번미상").strip())
    name_clean = re.sub(r'[\\/:*?"<>|]', "", (student_name or "이름미상").strip())
    title_clean = re.sub(r'[\\/:*?"<>|]', "", (assignment_title or "과제").strip())
    ext_clean = ext if ext.startswith(".") else f".{ext}"

    if title_clean:
        return f"{sid_clean}_{name_clean}_{title_clean}{ext_clean}"
    return f"{sid_clean}_{name_clean}{ext_clean}"


def resolve_non_conflicting_path(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    idx = 2
    while True:
        alt = dest_dir / f"{stem}_({idx}){suffix}"
        if not alt.exists():
            return alt
        idx += 1


def extract_local_text_from_files(file_paths: List[str]) -> str:
    chunks: List[str] = []
    for fp in file_paths:
        p = Path(fp)
        if not p.exists():
            continue
        ext = p.suffix.lower()
        if ext == ".pdf" and HAS_PYPDF and PdfReader is not None:
            try:
                reader = PdfReader(str(p))
                txt = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
                chunks.append(f"[{p.name}]\n{txt or '(스캔 이미지 기반 PDF 파일)'}")
            except Exception as e:
                chunks.append(f"[{p.name}] PDF 텍스트 읽기 실패: {e}")
        elif ext in (".txt", ".md", ".csv"):
            for enc in ("utf-8-sig", "cp949", "utf-8"):
                try:
                    chunks.append(f"[{p.name}]\n{p.read_text(encoding=enc)}")
                    break
                except UnicodeDecodeError:
                    continue
        else:
            chunks.append(f"[첨부 파일: {p.name}]")
    return "\n\n".join(chunks).strip()


# =============================================================================
# 1. 비동기 백그라운드 워커 클래스들 (BaseWorker 상속)
# =============================================================================
class StudentSubmissionAnalyzeWorker(BaseWorker):
    def __init__(
        self,
        folder_path: str,
        recursive: bool,
        ext_filter: str,
        id_regex: str,
        name_regex: str,
        assignment_title: str,
        roster: List[Dict[str, Any]],
    ):
        super().__init__(task_name="제출 파일 학번/이름 분석 및 명렬표 대조")
        self.folder_path = Path(folder_path)
        self.recursive = recursive
        self.ext_filter = ext_filter.lower().strip()
        self.id_regex = id_regex.strip() or r"(?<!\d)(\d{4,5})(?!\d)"
        self.name_regex = name_regex.strip() or r"([가-힣]{2,4})"
        self.assignment_title = assignment_title.strip() or "과제"
        self.roster = roster

    def execute(self) -> Dict[str, Any]:
        if not self.folder_path.exists() or not self.folder_path.is_dir():
            raise FileNotFoundError(f"선택한 폴더가 존재하지 않습니다: {self.folder_path}")

        id_pattern = re.compile(self.id_regex)
        name_pattern = re.compile(self.name_regex)

        roster_by_id: Dict[str, Dict[str, Any]] = {}
        roster_by_name: Dict[str, List[Dict[str, Any]]] = {}
        for stu in self.roster:
            sid = str(stu.get("student_id", "")).strip()
            sname = str(stu.get("name", "")).strip()
            if sid:
                roster_by_id[sid] = stu
            if sname:
                roster_by_name.setdefault(sname, []).append(stu)

        all_files: List[Path] = []
        iterator = self.folder_path.rglob("*") if self.recursive else self.folder_path.iterdir()
        for p in iterator:
            if not p.is_file():
                continue
            if p.name.startswith("~$") or p.name.startswith("."):
                continue
            ext = p.suffix.lower()
            if self.ext_filter == "all":
                if ext in SUPPORTED_SUBMISSION_EXTS:
                    all_files.append(p)
            elif self.ext_filter == "pdf":
                if ext == ".pdf":
                    all_files.append(p)
            elif self.ext_filter == "hwp":
                if ext in (".hwp", ".hwpx"):
                    all_files.append(p)
            elif self.ext_filter == "word_excel":
                if ext in (".doc", ".docx", ".xls", ".xlsx", ".csv"):
                    all_files.append(p)
            else:
                all_files.append(p)

        total = len(all_files)
        self.emit_log("INFO", f"폴더 스캔 완료: 대상 파일 {total}개 발견 (명렬표 등록 인원: {len(self.roster)}명)")

        common_noise_words = {
            "과제", "제출", "보고서", "수행평가", "독서", "감상문", "활동지",
            "계획서", "발표", "자료", "최종", "수정", "완료", "학번", "이름",
            self.assignment_title,
        }

        analyzed_files: List[Dict[str, Any]] = []
        submitted_student_ids = set()

        for idx, file_path in enumerate(all_files, start=1):
            if self.is_stop_requested():
                self.emit_log("WARN", "사용자 요청으로 파일 분석이 중단되었습니다.")
                break

            pct = int((idx / max(1, total)) * 100)
            self.emit_progress(pct, f"파일 분석 중 ({idx}/{total}): {file_path.name}")

            stem = file_path.stem
            ext = file_path.suffix.lower()

            extracted_id = ""
            id_match = id_pattern.search(stem)
            if id_match:
                extracted_id = id_match.group(1) if id_match.lastindex else id_match.group(0)

            extracted_name = ""
            for candidate_name in roster_by_name.keys():
                if candidate_name and candidate_name in stem:
                    extracted_name = candidate_name
                    break

            if not extracted_name:
                name_candidates = name_pattern.findall(stem)
                for cand in name_candidates:
                    cand_str = cand[0] if isinstance(cand, tuple) else str(cand)
                    if cand_str not in common_noise_words:
                        extracted_name = cand_str
                        break

            match_status = "미등록/확인필요"
            matched_student: Optional[Dict[str, Any]] = None

            if extracted_id and extracted_id in roster_by_id:
                matched_student = roster_by_id[extracted_id]
                official_name = matched_student["name"]
                if extracted_name == official_name:
                    match_status = "명렬표 일치"
                else:
                    extracted_name = official_name
                    match_status = "학번 매칭(이름보완)"
            elif extracted_name and extracted_name in roster_by_name:
                candidates = roster_by_name[extracted_name]
                if len(candidates) == 1:
                    matched_student = candidates[0]
                    extracted_id = str(matched_student["student_id"])
                    match_status = "이름 매칭(학번보완)"
                else:
                    match_status = "동명이인(학번확인)"
            elif extracted_id or extracted_name:
                match_status = "명렬표 외 추출"

            if matched_student is not None:
                submitted_student_ids.add(str(matched_student["student_id"]))

            new_filename = build_standard_filename(
                student_id=extracted_id,
                student_name=extracted_name,
                assignment_title=self.assignment_title,
                ext=ext,
            )

            analyzed_files.append(
                {
                    "checked": True,
                    "student_id": extracted_id,
                    "name": extracted_name,
                    "match_status": match_status,
                    "orig_name": file_path.name,
                    "ext": ext,
                    "new_name": new_filename,
                    "orig_path": str(file_path.resolve()),
                }
            )

        analyzed_files.sort(key=_student_sort_key)

        missing_students: List[Dict[str, Any]] = []
        for stu in self.roster:
            sid = str(stu.get("student_id", "")).strip()
            if sid not in submitted_student_ids:
                missing_students.append(stu)

        self.emit_progress(
            100,
            f"분석 완료: 파일 {len(analyzed_files)}개 / 제출 {len(submitted_student_ids)}명 / 미제출 {len(missing_students)}명",
        )
        self.emit_log(
            "SUCCESS",
            f"제출 분석 완료 — 제출 학생: {len(submitted_student_ids)}명, 미제출 학생: {len(missing_students)}명",
        )

        return {
            "files": analyzed_files,
            "missing_students": missing_students,
            "submitted_count": len(submitted_student_ids),
            "roster_count": len(self.roster),
        }


class StandardFileRenameWorker(BaseWorker):
    def __init__(
        self,
        items: List[Dict[str, Any]],
        output_dir: str,
        copy_mode: bool = True,
    ):
        super().__init__(task_name="학번순 표준 파일명 일괄 변경")
        self.items = items
        self.output_dir = Path(output_dir)
        self.copy_mode = copy_mode

    def execute(self) -> Dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        sorted_items = sorted(self.items, key=_student_sort_key)
        total = len(sorted_items)

        success_count = 0
        fail_count = 0
        processed_paths: List[str] = []

        self.emit_log(
            "INFO",
            f"총 {total}개 파일을 {'새 폴더에 표준 이름으로 복사' if self.copy_mode else '원본 위치에서 이름 변경'}합니다.",
        )

        for idx, item in enumerate(sorted_items, start=1):
            if self.is_stop_requested():
                self.emit_log("WARN", "사용자 요청으로 파일명 일괄 변경 작업이 중단되었습니다.")
                break

            orig_path = Path(item["orig_path"])
            new_name = item["new_name"]
            pct = int((idx / max(1, total)) * 100)
            self.emit_progress(pct, f"표준 파일명 적용 중 ({idx}/{total}): {new_name}")

            if not orig_path.exists():
                fail_count += 1
                self.emit_log("WARN", f"원본 파일 없음: {orig_path}")
                continue

            try:
                target_dir = self.output_dir if self.copy_mode else orig_path.parent
                target_dir.mkdir(parents=True, exist_ok=True)

                if (
                    not self.copy_mode
                    and orig_path.parent.resolve() == target_dir.resolve()
                    and orig_path.name == new_name
                ):
                    success_count += 1
                    processed_paths.append(str(orig_path))
                    continue

                dest_path = resolve_non_conflicting_path(target_dir, new_name)
                if self.copy_mode:
                    shutil.copy2(str(orig_path), str(dest_path))
                else:
                    shutil.move(str(orig_path), str(dest_path))

                success_count += 1
                processed_paths.append(str(dest_path))
                self.emit_log("SUCCESS", f"{orig_path.name} -> {dest_path.name}")
            except Exception as exc:
                fail_count += 1
                self.emit_log("ERROR", f"파일 처리 실패 ({orig_path.name}): {exc}")

        self.emit_progress(100, f"표준 파일명 변경 완료 (성공 {success_count}건 / 실패 {fail_count}건)")
        return {
            "success_count": success_count,
            "fail_count": fail_count,
            "output_dir": str(self.output_dir.resolve()),
            "processed_paths": processed_paths,
        }


class PdfMergeByStudentWorker(BaseWorker):
    def __init__(
        self,
        items: List[Dict[str, Any]],
        output_pdf_path: str,
        add_bookmarks: bool = True,
    ):
        super().__init__(task_name="학번순 단일 PDF 병합")
        self.items = items
        self.output_pdf_path = Path(output_pdf_path)
        self.add_bookmarks = add_bookmarks

    def execute(self) -> Dict[str, Any]:
        if not HAS_PYPDF or PdfWriter is None or PdfReader is None:
            raise RuntimeError(
                "pypdf 패키지가 설치되어 있지 않습니다.\n"
                "터미널에서 'pip install pypdf>=4.0.0'을 실행한 뒤 다시 시도해 주세요."
            )

        pdf_items = [
            it for it in self.items if str(it.get("ext", "")).lower() == ".pdf"
        ]
        pdf_items.sort(key=_student_sort_key)

        total = len(pdf_items)
        if total == 0:
            raise ValueError("선택된 항목 중 병합할 PDF(.pdf) 파일이 없습니다.")

        writer = PdfWriter()
        merged_files_count = 0
        total_pages_count = 0
        skipped_files: List[str] = []

        self.emit_log("INFO", f"총 {total}개의 PDF 파일을 학번 순서대로 단일 PDF로 병합합니다.")

        for idx, item in enumerate(pdf_items, start=1):
            if self.is_stop_requested():
                self.emit_log("WARN", "사용자 요청으로 PDF 병합이 중단되었습니다.")
                break

            pdf_path = Path(item["orig_path"])
            sid = item.get("student_id") or "학번미상"
            sname = item.get("name") or pdf_path.stem
            pct = int((idx / total) * 100)
            self.emit_progress(pct, f"PDF 병합 중 ({idx}/{total}): [{sid}] {sname}")

            if not pdf_path.exists():
                skipped_files.append(f"{pdf_path.name} (파일 없음)")
                self.emit_log("WARN", f"PDF 파일을 찾을 수 없어 건너뜁니다: {pdf_path.name}")
                continue

            try:
                reader = PdfReader(str(pdf_path))
                if getattr(reader, "is_encrypted", False):
                    try:
                        reader.decrypt("")
                    except Exception:
                        pass

                num_pages = len(reader.pages)
                if num_pages == 0:
                    skipped_files.append(f"{pdf_path.name} (0페이지)")
                    continue

                start_page_index = total_pages_count
                for page in reader.pages:
                    writer.add_page(page)

                if self.add_bookmarks:
                    bookmark_title = f"[{sid}] {sname} ({pdf_path.name})"
                    try:
                        writer.add_outline_item(bookmark_title, start_page_index)
                    except Exception:
                        pass

                merged_files_count += 1
                total_pages_count += num_pages
                self.emit_log(
                    "SUCCESS",
                    f"병합 완료: [{sid}] {sname} ({num_pages}페이지 추가, 누적 {total_pages_count}p)",
                )
            except Exception as exc:
                skipped_files.append(f"{pdf_path.name} ({exc})")
                self.emit_log("ERROR", f"PDF 읽기 오류 ({pdf_path.name}): {exc}")

        if merged_files_count == 0:
            raise RuntimeError("정상적으로 병합된 PDF 파일이 없습니다.")

        self.output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_pdf_path, "wb") as out_f:
            writer.write(out_f)

        self.emit_progress(
            100,
            f"PDF 병합 완료: {merged_files_count}개 파일 ({total_pages_count}페이지) -> {self.output_pdf_path.name}",
        )
        return {
            "output_pdf": str(self.output_pdf_path.resolve()),
            "merged_files": merged_files_count,
            "total_pages": total_pages_count,
            "skipped_files": skipped_files,
        }


class OcrAndLlmWorker(BaseWorker):
    def __init__(self, file_paths: List[str], prompt_guide: str):
        super().__init__(task_name="멀티 AI OCR 및 문서 보정")
        self.file_paths = file_paths
        self.prompt_guide = prompt_guide.strip()
        self.llm_client = MultiLlmClient()

    def execute(self) -> Dict[str, Any]:
        total = len(self.file_paths)
        results: List[str] = []
        self.emit_log(
            "INFO",
            f"[{self.llm_client.provider.upper()} / {self.llm_client.model_name}] 총 {total}개 파일 AI 처리를 시작합니다.",
        )

        for idx, fp in enumerate(self.file_paths, start=1):
            if self.is_stop_requested():
                self.emit_log("WARN", "사용자 요청으로 AI OCR 작업이 중단되었습니다.")
                break

            path_obj = Path(fp)
            fname = path_obj.name
            pct = int((idx / max(1, total)) * 100)
            self.emit_progress(pct, f"AI 보정 중 ({idx}/{total}): {fname}")

            if not path_obj.exists():
                continue

            ext = path_obj.suffix.lower()
            local_text = extract_local_text_from_files([str(path_obj)])
            sys_prompt, structured_user_prompt = build_structured_prompt(
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
            elif ext == ".pdf" and self.llm_client.provider == "gemini":
                image_bytes = path_obj.read_bytes()
                mime_type = "application/pdf"

            try:
                raw_reply = self.llm_client.generate(
                    prompt=structured_user_prompt,
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
                    f"=== [{fname}] ({self.llm_client.provider.upper()} / {self.llm_client.model_name}) ===\n{formatted}\n"
                )
                self.emit_log("SUCCESS", f"AI 보정 및 파싱 완료: {fname}")
            except Exception as exc:
                results.append(f"=== [{fname}] API 처리 오류 ===\n{exc}\n")
                self.emit_log("ERROR", f"API 호출 실패 ({fname}): {exc}")

        self.emit_progress(100, "멀티 AI OCR 및 텍스트 보정 완료")
        return {"count": len(results), "combined_text": "\n".join(results)}


class DocxToExcelWorker(BaseWorker):
    """학생별 DOCX 과제 문서에서 학번/이름/문항별 응답을 추출해 Excel로 변환하는 백그라운드 워커."""

    def __init__(
        self,
        docx_dir: Path | str,
        schema_path: Optional[Path | str] = None,
        output_xlsx: Optional[Path | str] = None,
    ):
        super().__init__(task_name="과제 DOCX -> 엑셀 추출")
        self.docx_dir = Path(docx_dir)
        self.schema_path = Path(schema_path) if schema_path else None
        self.output_xlsx = Path(output_xlsx) if output_xlsx else None

    def execute(self) -> Dict[str, Any]:
        self.emit_log("INFO", f"학생 DOCX 과제 추출 시작 (폴더: {self.docx_dir.name})")
        res = extract_docx_to_excel(
            docx_dir=self.docx_dir,
            schema_path=self.schema_path,
            output_path=self.output_xlsx,
            progress_callback=lambda pct, msg: self.emit_progress(pct, msg),
            log_callback=lambda lvl, msg: self.emit_log(lvl, msg),
            stop_checker=self.is_stop_requested,
        )
        if self.is_stop_requested():
            self.emit_log("WARN", "사용자 요청으로 과제 추출 작업이 중단되었습니다.")
            return res

        out_path = res.get("output_file", "")
        succ = res.get("success", 0)
        tot = res.get("total", 0)
        failed = res.get("failed", 0)
        errors = res.get("errors", [])

        if failed > 0:
            self.emit_log("ERROR", f"총 {len(errors)}개 파일 추출 실패 상세:")
            for e in errors:
                self.emit_log("ERROR", f"  • {e.get('파일명', '알수없음')}: {e.get('오류내용', '원인 미상')}")
            self.emit_log("INFO", "생성된 엑셀 파일 내 '오류 파일' 시트에도 상세 내용이 기록되었습니다.")

        self.emit_log("SUCCESS", f"엑셀 추출 완료: {out_path} (성공 {succ}/{tot}명)")
        self.emit_progress(100, f"추출 완료 ({succ}/{tot}명)")
        return res


class ExcelColumnMergeWorker(BaseWorker):
    """여러 개의 과제 엑셀 파일을 '학번' 기준으로 가로 결합(Column Merge)하는 백그라운드 워커."""

    def __init__(
        self,
        excel_paths: List[Path | str],
        output_path: Optional[Path | str] = None,
    ):
        super().__init__(task_name="다중 과제 엑셀 가로 병합")
        self.excel_paths = [Path(p) for p in excel_paths]
        self.output_path = Path(output_path) if output_path else None

    def execute(self) -> Dict[str, Any]:
        self.emit_log("INFO", f"다중 과제 엑셀 가로 병합 시작 (대상 파일: {len(self.excel_paths)}개)")
        res = merge_excel_assignments(
            excel_paths=self.excel_paths,
            output_path=self.output_path,
            log_callback=lambda lvl, msg: self.emit_log(lvl, msg),
            progress_callback=lambda pct, msg: self.emit_progress(pct, msg),
            stop_checker=self.is_stop_requested,
        )
        if self.is_stop_requested():
            self.emit_log("WARN", "사용자 요청으로 엑셀 가로 병합 작업이 중단되었습니다.")
            return res

        out_path = res.get("output_file", "")
        stu_cnt = res.get("total_students", 0)
        col_cnt = res.get("merged_columns", 0)
        rev_cnt = res.get("review_count", 0)
        err_cnt = res.get("error_count", 0)

        self.emit_log("SUCCESS", f"엑셀 가로 병합 완료: {out_path} (학생 {stu_cnt}명, 열 {col_cnt}개)")
        if rev_cnt > 0 or err_cnt > 0:
            self.emit_log("WARN", f"확인 필요 항목 발생: 이름 불일치 {rev_cnt}건, 학번 오류 {err_cnt}건 (엑셀 [검토_및_오류] 시트 참조)")
        self.emit_progress(100, f"병합 완료 ({stu_cnt}명 통합)")
        return res


class RecordGenerateWorker(BaseWorker):
    """엑셀 파일의 응답 데이터를 기반으로 MultiLlmClient를 호출하여 AI 과세특을 생성하는 백그라운드 워커."""

    def __init__(
        self,
        excel_path: Path | str,
        output_path: Optional[Path | str] = None,
        prompt_file: Optional[Path | str] = None,
        reference_dir: Optional[Path | str] = None,
        max_bytes: int = 1500,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        super().__init__(task_name="AI 과세특·채점 생성")
        self.excel_path = Path(excel_path)
        self.output_path = Path(output_path) if output_path else None
        self.prompt_file = Path(prompt_file) if prompt_file else None
        self.reference_dir = Path(reference_dir) if reference_dir else None
        self.max_bytes = max_bytes
        self.provider = provider
        self.model_name = model_name

    def execute(self) -> Dict[str, Any]:
        self.emit_log("INFO", f"AI 과세특 생성 시작 (파일: {self.excel_path.name})")
        res = generate_records_from_excel(
            excel_path=self.excel_path,
            output_path=self.output_path,
            prompt_file=self.prompt_file,
            reference_dir=self.reference_dir,
            max_bytes=self.max_bytes,
            provider=self.provider,
            model_name=self.model_name,
            progress_callback=lambda pct, msg: self.emit_progress(pct, msg),
            log_callback=lambda lvl, msg: self.emit_log(lvl, msg),
            stop_checker=self.is_stop_requested,
        )
        if self.is_stop_requested():
            self.emit_log("WARN", "사용자 요청으로 과세특 생성이 안전하게 중단되었습니다.")
            return res

        out_path = res.get("output_path", "")
        comp = res.get("completed", 0)
        tot = res.get("total", 0)
        self.emit_log("SUCCESS", f"과세특 생성 완료: {out_path} (완료 {comp}/{tot}명)")
        self.emit_progress(100, f"과세특 생성 완료 ({comp}/{tot}명)")
        return res


class IntegratedPipelineWorker(BaseWorker):
    """1단계 DOCX 파싱 -> 중간 엑셀 생성 -> 2단계 AI 과세특 생성을 원클릭으로 순차 실행하는 통합 워커."""

    def __init__(
        self,
        docx_dir: Path | str,
        schema_path: Optional[Path | str] = None,
        prompt_file: Optional[Path | str] = None,
        reference_dir: Optional[Path | str] = None,
        max_bytes: int = 1500,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        super().__init__(task_name="과제 취합 및 AI 과세특 일괄 생성")
        self.docx_dir = Path(docx_dir)
        self.schema_path = Path(schema_path) if schema_path else None
        self.prompt_file = Path(prompt_file) if prompt_file else None
        self.reference_dir = Path(reference_dir) if reference_dir else None
        self.max_bytes = max_bytes
        self.provider = provider
        self.model_name = model_name

    def execute(self) -> Dict[str, Any]:
        # [1단계] DOCX -> 중간 엑셀
        self.emit_log("INFO", "=== [1단계] 학생 DOCX 과제 일괄 추출 시작 ===")
        self.emit_progress(5, "1단계: 학생 DOCX 파일 파싱 중...")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        intermediate_excel = self.docx_dir / f"과제취합_{ts}.xlsx"

        step1_res = extract_docx_to_excel(
            docx_dir=self.docx_dir,
            schema_path=self.schema_path,
            output_path=intermediate_excel,
            progress_callback=lambda pct, msg: self.emit_progress(int(pct * 0.4), f"1단계 DOCX 추출: {msg}"),
            log_callback=lambda lvl, msg: self.emit_log(lvl, msg),
            stop_checker=self.is_stop_requested,
        )

        if self.is_stop_requested():
            self.emit_log("WARN", "1단계 진행 중 사용자에 의해 작업이 중단되었습니다.")
            return {"step1": step1_res, "step2": None, "output_path": str(intermediate_excel)}

        step1_errors = step1_res.get("errors", [])
        if step1_res.get("failed", 0) > 0:
            self.emit_log("ERROR", f"총 {len(step1_errors)}개 파일 추출 실패 상세:")
            for e in step1_errors:
                self.emit_log("ERROR", f"  • {e.get('파일명', '알수없음')}: {e.get('오류내용', '원인 미상')}")
            self.emit_log("INFO", "생성된 엑셀 파일 내 '오류 파일' 시트에도 상세 내용이 기록되었습니다.")

        if step1_res.get("success", 0) == 0:
            raise ValueError(f"1단계 DOCX 과제 추출 결과 유효한 데이터가 없습니다. (오류: {len(step1_errors)}건)")

        self.emit_log("SUCCESS", f"1단계 완료: 중간 엑셀 생성됨 -> {step1_res.get('output_file')}")

        # [2단계] 중간 엑셀 -> 최종 AI 과세특
        self.emit_log("INFO", "=== [2단계] AI 과세특 및 채점 생성 시작 ===")
        self.emit_progress(40, "2단계: AI 과세특 생성 준비 중...")

        final_output = self.docx_dir / f"최종과세특_{ts}.xlsx"
        step2_res = generate_records_from_excel(
            excel_path=Path(step1_res.get("output_file")),
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

        if self.is_stop_requested():
            self.emit_log("WARN", "2단계 진행 중 사용자에 의해 작업이 안전하게 중단되었습니다.")
        else:
            self.emit_progress(100, "전체 일괄 처리 완료!")
            self.emit_log("SUCCESS", f"원클릭 일괄 파이프라인 완료! 최종 파일: {step2_res.get('output_path')}")

        return {
            "step1": step1_res,
            "step2": step2_res,
            "output_path": step2_res.get("output_path"),
        }


# =============================================================================
# 2. [탭 2] 메인 UI 위젯 클래스 (DocOcrTab)
# =============================================================================
class DocOcrTab(QWidget):
    """[탭 2] 문서 처리 & AI OCR / 파일 병합 메인 탭 위젯."""

    ai_target_requested = pyqtSignal(str)
    ai_context_changed = pyqtSignal(dict)

    TASK_ANALYZE = "doc_tab_analyze_submissions"
    TASK_RENAME = "doc_tab_standard_rename"
    TASK_PDF_MERGE = "doc_tab_pdf_merge"
    TASK_OCR = "doc_tab_ai_ocr"
    TASK_EXCEL_AGG = "doc_tab_excel_agg"
    TASK_DOCX_EXTRACT = "doc_tab_docx_extract"
    TASK_RECORD_GEN = "doc_tab_record_gen"
    TASK_INTEGRATED = "doc_tab_integrated_pipeline"
    TASK_COLUMN_MERGE = "doc_tab_excel_column_merge"

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="DocOcrTabPage")
        self.task_runner = task_runner
        self.config = ConfigManager()
        self.db = DatabaseManager()
        self.notifier = NotificationService()

        self._analyzed_items: List[Dict[str, Any]] = []
        self._missing_students: List[Dict[str, Any]] = []
        self._focused_card: Optional[str] = None
        self._active_ai_target_id: str = "agg_step2"

        self._context_debounce_timer = QTimer(self)
        self._context_debounce_timer.setSingleShot(True)
        self._context_debounce_timer.setInterval(300)
        self._context_debounce_timer.timeout.connect(self._on_debounced_context_sync)

        self._init_ui()
        self._wire_ai_context_sync()
        self.set_active_ai_target("agg_step2")

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.sub_tabs = QTabWidget()
        apply_qss_role(self.sub_tabs, obj_name="DocOcrSubTabWidget")
        self.sub_tabs.addTab(
            self._build_excel_aggregator_subtab(),
            "1. 과제 파이프라인 (DOCX 추출 + AI 세특)",
        )
        self.sub_tabs.addTab(
            self._build_ocr_subtab(),
            "2. AI OCR 및 텍스트 보정",
        )
        self.sub_tabs.addTab(
            self._build_student_merge_subtab(),
            "3. 파일 취합 & 명렬표 대조 & PDF 병합",
        )
        layout.addWidget(self.sub_tabs)

    # =========================================================================
    # 공통 레이아웃 헬퍼 (Stitch 상단 라벨 + 하단 필드 블록)
    # =========================================================================
    def _field_block(self, label_text: str, field_layout: QHBoxLayout) -> QVBoxLayout:
        block = QVBoxLayout()
        block.setSpacing(4)
        lbl = QLabel(label_text)
        apply_qss_role(lbl, role="fieldLabel")
        block.addWidget(lbl)
        block.addLayout(field_layout)
        return block

    # =========================================================================
    # [서브탭 1] 학번/이름 기준 파일 취합 · 미제출자 확인 · 표준 이름 변경 · PDF 병합
    # =========================================================================
    def _build_student_merge_subtab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        page = QWidget()
        page.setMinimumWidth(0)
        page.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.MinimumExpanding
        )
        apply_qss_role(page, obj_name="SubTabStudentMerge")
        main_lay = QVBoxLayout(page)
        main_lay.setContentsMargins(8, 8, 22, 22)
        main_lay.setSpacing(12)

        self.cfg_group = HoverZoomCard(
            title="1단계: 학생 제출물 폴더 선택 및 정규표현식(Regex) 설정",
            parent=page,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.cfg_group, role="hoverZoomCard", obj_name="SubmissionRegexGroup")
        cfg_lay = self.cfg_group.body_layout()

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("<b>제출물 폴더:</b>"))
        self.edit_submission_dir = QLineEdit()
        self.edit_submission_dir.setMinimumHeight(32)
        self.edit_submission_dir.setPlaceholderText(
            "학생들이 제출한 파일(PDF, HWP, DOCX, XLSX 등)이 들어있는 폴더를 선택하세요..."
        )
        btn_browse_sub = QPushButton("폴더 찾아보기...")
        apply_qss_role(btn_browse_sub, role="secondary")
        btn_browse_sub.setMinimumHeight(32)
        btn_browse_sub.clicked.connect(self._on_browse_submission_dir)
        btn_open_sub = QPushButton("폴더 열기")
        apply_qss_role(btn_open_sub, role="outline")
        btn_open_sub.setMinimumHeight(32)
        btn_open_sub.clicked.connect(self._on_open_submission_dir)

        self.chk_recursive = QCheckBox("하위 폴더까지 포함 검색")
        self.combo_ext_filter = QComboBox()
        self.combo_ext_filter.setMinimumHeight(32)
        install_no_scroll_wheel_filter(self.combo_ext_filter)
        self.combo_ext_filter.addItem("모든 제출 문서 (PDF/HWP/Office/이미지)", "all")
        self.combo_ext_filter.addItem("PDF 파일만 (.pdf)", "pdf")
        self.combo_ext_filter.addItem("한글 파일만 (.hwp, .hwpx)", "hwp")
        self.combo_ext_filter.addItem("워드/엑셀 파일만 (.docx, .xlsx 등)", "word_excel")

        folder_row.addWidget(self.edit_submission_dir, stretch=1)
        folder_row.addWidget(btn_browse_sub)
        folder_row.addWidget(btn_open_sub)
        cfg_lay.addLayout(folder_row)

        # 검색 옵션은 별도 행으로 분리 (한 줄에 몰아넣어 창 최소 너비가 커지는 문제 방지)
        option_row = QHBoxLayout()
        option_row.addWidget(QLabel("검색 대상:"))
        option_row.addWidget(self.combo_ext_filter, stretch=1)
        option_row.addSpacing(8)
        option_row.addWidget(self.chk_recursive)
        option_row.addStretch()
        cfg_lay.addLayout(option_row)

        regex_row = QHBoxLayout()
        regex_row.addWidget(QLabel("학번 정규식:"))
        self.edit_id_regex = QLineEdit(r"(?<!\d)(\d{4,5})(?!\d)")
        self.edit_id_regex.setMinimumHeight(32)
        regex_row.addWidget(self.edit_id_regex, stretch=1)
        regex_row.addWidget(QLabel("이름 정규식:"))
        self.edit_name_regex = QLineEdit(r"([가-힣]{2,4})")
        self.edit_name_regex.setMinimumHeight(32)
        regex_row.addWidget(self.edit_name_regex, stretch=1)
        regex_row.addWidget(QLabel("표준 과제명:"))
        self.edit_assignment_title = QLineEdit("수행평가과제")
        self.edit_assignment_title.setMinimumHeight(32)
        self.edit_assignment_title.textChanged.connect(self._on_assignment_title_changed)
        regex_row.addWidget(self.edit_assignment_title, stretch=1)
        cfg_lay.addLayout(regex_row)

        self.btn_scan_analyze = QPushButton("폴더 스캔 및 명렬표 미제출자 대조 실행")
        apply_qss_role(self.btn_scan_analyze, role="primary", obj_name="BtnScanAnalyze")
        self.btn_scan_analyze.setMinimumHeight(36)
        self.btn_scan_analyze.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_scan_analyze.clicked.connect(self.run_submission_analysis)

        scan_row = QHBoxLayout()
        scan_row.addStretch()
        scan_row.addWidget(self.btn_scan_analyze)
        cfg_lay.addLayout(scan_row)

        main_lay.addWidget(self.cfg_group)

        summary_bar = QHBoxLayout()
        self.lbl_summary_badge = QLabel(
            "제출물 폴더를 선택하고 [폴더 스캔 및 명렬표 미제출자 대조 실행]을 클릭하세요."
        )
        self.lbl_summary_badge.setWordWrap(True)
        apply_qss_role(self.lbl_summary_badge, role="infoBanner", obj_name="SubmissionSummaryBanner")
        summary_bar.addWidget(self.lbl_summary_badge, stretch=1)
        main_lay.addLayout(summary_bar)

        # 좌우 가로 분할(QSplitter) 대신 상하 세로 스택: 미리보기 카드 → 미제출자 카드 (각각 너비 100%)

        # ---------------------------------------------------------------------
        # 상단 카드: 제출 파일 학번순 정렬 미리보기
        # ---------------------------------------------------------------------
        self.left_group = HoverZoomCard(
            title="2단계: 제출 파일 학번순 정렬 및 표준 파일명 미리보기",
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.left_group, role="hoverZoomCard", obj_name="SubmittedFilesGroup")
        self.left_group.setMinimumHeight(260)

        self.btn_focus_submissions = QPushButton("전체화면")
        apply_qss_role(self.btn_focus_submissions, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_submissions.setCheckable(True)
        self.btn_focus_submissions.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_submissions.setToolTip("제출 파일 미리보기 카드를 전체화면으로 크게 보거나 원래 크기로 복원합니다.")
        self.btn_focus_submissions.clicked.connect(lambda: self._toggle_card_focus("submissions"))
        self.left_group.add_header_widget(self.btn_focus_submissions)

        left_lay = self.left_group.body_layout()

        left_top_bar = QHBoxLayout()
        btn_check_all = QPushButton("전체 선택")
        apply_qss_role(btn_check_all, role="secondary")
        btn_uncheck_all = QPushButton("전체 해제")
        apply_qss_role(btn_uncheck_all, role="outline")
        btn_check_all.clicked.connect(lambda: self._set_all_files_checked(True))
        btn_uncheck_all.clicked.connect(lambda: self._set_all_files_checked(False))

        left_top_bar.addWidget(btn_check_all)
        left_top_bar.addWidget(btn_uncheck_all)
        left_top_bar.addStretch()
        left_lay.addLayout(left_top_bar)

        self.table_submissions = QTableWidget(0, 7)
        self.table_submissions.setAlternatingRowColors(True)
        self.table_submissions.setMouseTracking(True)
        self.table_submissions.verticalHeader().setDefaultSectionSize(32)
        self.table_submissions.setHorizontalHeaderLabels(
            [
                "선택",
                "학번",
                "이름",
                "명렬표 대조 상태",
                "원본 파일명",
                "변경될 표준 파일명 ([학번]_[이름]_과제명.ext)",
                "원본 전체 경로",
            ]
        )
        self.table_submissions.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_submissions.itemChanged.connect(self._on_submission_table_item_changed)

        sub_header = self.table_submissions.horizontalHeader()
        sub_header.setMinimumSectionSize(56)
        sub_header.setStretchLastSection(False)
        sub_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        sub_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        sub_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        sub_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        # 파일명 2개 열은 남은 폭을 균등 분할(창이 좁아지면 함께 줄어듦), 긴 경로 열은 고정 폭 + 가운데 생략
        sub_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        sub_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        sub_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        self.table_submissions.setColumnWidth(6, 160)
        self.table_submissions.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table_submissions.setWordWrap(True)
        self.table_submissions.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        left_lay.addWidget(self.table_submissions, stretch=1)
        main_lay.addWidget(self.left_group, stretch=3)

        # ---------------------------------------------------------------------
        # 하단 카드: 미제출자 명단
        # ---------------------------------------------------------------------
        self.right_group = HoverZoomCard(
            title="명렬표 미제출자 명단 (학생 명렬표 DB 자동 대조)",
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.right_group, role="hoverZoomCard", obj_name="MissingStudentsGroup")
        self.right_group.setMinimumHeight(260)

        self.btn_focus_missing = QPushButton("전체화면")
        apply_qss_role(self.btn_focus_missing, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_missing.setCheckable(True)
        self.btn_focus_missing.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_missing.setToolTip("미제출자 명단 카드를 전체화면으로 크게 보거나 원래 크기로 복원합니다.")
        self.btn_focus_missing.clicked.connect(lambda: self._toggle_card_focus("missing"))
        self.right_group.add_header_widget(self.btn_focus_missing)

        right_lay = self.right_group.body_layout()

        missing_btn_bar = QHBoxLayout()
        btn_copy_missing = QPushButton("명단 텍스트 복사")
        apply_qss_role(btn_copy_missing, role="secondary")
        btn_export_missing = QPushButton("제출/미제출 현황 엑셀(.xlsx) 저장")
        apply_qss_role(btn_export_missing, role="secondary")

        btn_copy_missing.clicked.connect(self._on_copy_missing_to_clipboard)
        btn_export_missing.clicked.connect(self._on_export_submission_report_xlsx)

        missing_btn_bar.addWidget(btn_copy_missing)
        missing_btn_bar.addWidget(btn_export_missing)
        missing_btn_bar.addStretch()
        right_lay.addLayout(missing_btn_bar)

        self.table_missing = QTableWidget(0, 5)
        self.table_missing.setAlternatingRowColors(True)
        self.table_missing.setMouseTracking(True)
        self.table_missing.verticalHeader().setDefaultSectionSize(32)
        self.table_missing.setHorizontalHeaderLabels(
            ["학번", "학년-반-번호", "이름", "학생 연락처", "학부모 연락처"]
        )
        self.table_missing.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_missing.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        miss_header = self.table_missing.horizontalHeader()
        miss_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        miss_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        miss_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        miss_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        miss_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

        self.table_missing.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table_missing.setWordWrap(True)
        self.table_missing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        right_lay.addWidget(self.table_missing, stretch=1)
        main_lay.addWidget(self.right_group, stretch=2)

        self.action_group = HoverZoomCard(
            title="3단계: 학번순 파일명 일괄 표준화 및 단일 PDF 병합 실행",
            parent=page,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.action_group, role="hoverZoomCard", obj_name="BatchMergeActionGroup")

        self.combo_rename_mode = QComboBox()
        self.combo_rename_mode.setMinimumHeight(34)
        install_no_scroll_wheel_filter(self.combo_rename_mode)
        self.combo_rename_mode.addItem("안전 모드: 하위 '_정리완료' 폴더에 복사 생성 (원본 보존)", "copy")
        self.combo_rename_mode.addItem("직접 변경: 원본 폴더 내 파일명 즉시 변경", "move")

        self.btn_execute_rename = QPushButton("학번순 표준 파일명 일괄 적용")
        apply_qss_role(self.btn_execute_rename, role="secondary", obj_name="BtnExecuteStandardRename")
        self.btn_execute_rename.setMinimumHeight(36)
        self.btn_execute_rename.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_execute_rename.clicked.connect(self.run_standard_rename)

        self.chk_pdf_bookmark = QCheckBox("PDF 병합 시 학생별 책갈피 자동 삽입")
        self.chk_pdf_bookmark.setChecked(True)

        self.btn_execute_pdf_merge = QPushButton("학번 순서대로 단일 PDF 병합 저장")
        apply_qss_role(self.btn_execute_pdf_merge, role="primary", obj_name="BtnExecutePdfMerge")
        self.btn_execute_pdf_merge.setMinimumHeight(36)
        self.btn_execute_pdf_merge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_execute_pdf_merge.clicked.connect(self.run_pdf_merge)

        # 1행: 파일명 표준화 모드 + 실행 / 2행: PDF 책갈피 옵션 + 병합 실행 (한 줄 과밀 배치로 인한 가로 넘침 방지)
        rename_row = QHBoxLayout()
        rename_row.addWidget(self.combo_rename_mode, stretch=1)
        rename_row.addWidget(self.btn_execute_rename)

        pdf_row = QHBoxLayout()
        pdf_row.addWidget(self.chk_pdf_bookmark)
        pdf_row.addStretch()
        pdf_row.addWidget(self.btn_execute_pdf_merge)

        self.action_group.body_layout().addLayout(rename_row)
        self.action_group.body_layout().addLayout(pdf_row)
        main_lay.addWidget(self.action_group)
        scroll.setWidget(page)
        return scroll

    # =========================================================================
    # 섹션 카드 전체화면 포커스 토글 ([전체화면] <-> [원래 크기])
    # =========================================================================
    def _toggle_card_focus(self, target: str) -> None:
        if self._focused_card == target:
            self._focused_card = None
            self.left_group.set_zoom_enabled(True)
            self.right_group.set_zoom_enabled(True)
            self.cfg_group.show()
            self.action_group.show()
            self.left_group.show()
            self.right_group.show()
            self.btn_focus_submissions.setChecked(False)
            self.btn_focus_submissions.setText("전체화면")
            self.btn_focus_missing.setChecked(False)
            self.btn_focus_missing.setText("전체화면")
            return

        self._focused_card = target
        self.cfg_group.hide()
        if target == "submissions":
            self.left_group.set_zoom_enabled(False)
            self.left_group.show()
            self.right_group.hide()
            self.action_group.show()
            self.btn_focus_submissions.setChecked(True)
            self.btn_focus_submissions.setText("원래 크기")
            self.btn_focus_missing.setChecked(False)
            self.btn_focus_missing.setText("전체화면")
        elif target == "missing":
            self.right_group.set_zoom_enabled(False)
            self.left_group.hide()
            self.right_group.show()
            self.action_group.hide()
            self.btn_focus_missing.setChecked(True)
            self.btn_focus_missing.setText("원래 크기")
            self.btn_focus_submissions.setChecked(False)
            self.btn_focus_submissions.setText("전체화면")

    # =========================================================================
    # [서브탭 2] 2. AI OCR 및 문서 텍스트 보정 (하단 QHBoxLayout 2버튼 나란히 배치)
    # =========================================================================
    def _build_ocr_subtab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        w = QWidget()
        w.setMinimumWidth(0)
        w.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.MinimumExpanding
        )
        apply_qss_role(w, obj_name="SubTabAiOcr")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 22, 22)
        lay.setSpacing(12)

        self.card_ocr_main = HoverZoomCard(
            title="AI OCR 및 문서 텍스트 보정",
            parent=w,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_ocr_main, role="hoverZoomCard", obj_name="AiOcrHybridGroup")
        self.card_ocr_main.setProperty("aiTargetSelected", "false")
        self.lbl_target_badge_ocr = QLabel("AI 대상 선택됨")
        apply_qss_role(self.lbl_target_badge_ocr, obj_name="AiTargetSelectedBadge")
        self.lbl_target_badge_ocr.setVisible(False)
        self.card_ocr_main.add_header_widget(self.lbl_target_badge_ocr)
        g_lay = self.card_ocr_main.body_layout()

        # 상단 파일 추가 및 결과 저장 툴바
        file_toolbar = QHBoxLayout()
        btn_add = QPushButton("+ 파일 추가")
        apply_qss_role(btn_add, role="secondary", obj_name="BtnAddOcrFiles")
        btn_clear = QPushButton("목록 비우기")
        apply_qss_role(btn_clear, role="outline", obj_name="BtnClearOcrFiles")
        btn_save_txt = QPushButton("결과 마크다운(.md) 저장")
        apply_qss_role(btn_save_txt, role="outline", obj_name="BtnSaveOcrMarkdown")

        self.list_ocr_files = QListWidget()
        self.list_ocr_files.setMouseTracking(True)
        self.list_ocr_files.setMinimumHeight(90)

        btn_add.clicked.connect(self._on_add_ocr_files)
        btn_clear.clicked.connect(self.list_ocr_files.clear)
        btn_save_txt.clicked.connect(self._on_save_ocr_result)

        file_toolbar.addWidget(btn_add)
        file_toolbar.addWidget(btn_clear)
        file_toolbar.addStretch()
        file_toolbar.addWidget(btn_save_txt)
        g_lay.addLayout(file_toolbar)

        g_lay.addWidget(self.list_ocr_files)

        # 보정 지침 입력란
        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        self.edit_ocr_prompt = QLineEdit(
            "스캔/추출된 문서의 오탈자를 문맥에 맞게 교정하고, 핵심 3줄 요약과 후속 조치 사항을 정리해 주세요."
        )
        self.edit_ocr_prompt.setMinimumHeight(32)
        form.addRow("AI 보정/요약 지침:", self.edit_ocr_prompt)
        g_lay.addLayout(form)

        # 결과 출력창
        self.txt_ocr_output = QTextEdit()
        apply_qss_role(self.txt_ocr_output, obj_name="WebAiInputBox")
        self.txt_ocr_output.setPlaceholderText(
            "우측 AI 실행 패널(API / 수동 복붙 / Playwright 자동)에서 실행한 결과 또는 파싱된 JSON 변환 결과가 이곳에 표시됩니다..."
        )
        g_lay.addWidget(self.txt_ocr_output, stretch=1)

        lbl_ocr_dock_hint = QLabel(
            "AI 실행(API · 수동 복붙 · Playwright 자동)은 우측 [AI 실행 패널]에서 제어합니다."
        )
        lbl_ocr_dock_hint.setWordWrap(True)
        apply_qss_role(lbl_ocr_dock_hint, role="infoBanner")
        g_lay.addWidget(lbl_ocr_dock_hint)

        lay.addWidget(self.card_ocr_main)
        scroll.setWidget(w)
        return scroll

    # =========================================================================
    # [서브탭 3] 과제 취합 및 AI 과세특·채점 자동화 UI
    # =========================================================================
    def _build_excel_aggregator_subtab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        w = QWidget()
        w.setMinimumWidth(0)
        w.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.MinimumExpanding
        )
        apply_qss_role(w, obj_name="SubTabDocxRecordAggregator")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 22, 22)
        lay.setSpacing(14)

        # ---------------------------------------------------------------------
        # 1. 상단 3-Stage Workflow Stepper (Stitch Pipeline Indicator + 상태 칩)
        # ---------------------------------------------------------------------
        stepper_frame = QFrame(w)
        apply_qss_role(stepper_frame, role="stepperBar", obj_name="AggStepperBar")
        stepper_lay = QHBoxLayout(stepper_frame)
        stepper_lay.setContentsMargins(12, 10, 12, 10)
        stepper_lay.setSpacing(10)

        self._agg_stage_boxes: List[QFrame] = []
        self._agg_stage_chips: List[QLabel] = []
        stage_defs = [
            ("1", "DOCX 일괄 파싱", "표 구조 파싱 및 엑셀 직렬화"),
            ("2", "AI 루브릭 · NEIS 세특", "글자수 예산 및 성취기준 정렬"),
            ("3", "검증 및 엑셀 내보내기", "학번순 정렬 및 결과 저장"),
        ]
        for num, title, desc in stage_defs:
            box = QFrame()
            apply_qss_role(box, role="stageBox")
            box_lay = QHBoxLayout(box)
            box_lay.setContentsMargins(10, 8, 10, 8)
            box_lay.setSpacing(10)

            lbl_num = QLabel(num)
            lbl_num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_num.setFixedSize(26, 26)
            apply_qss_role(lbl_num, role="stageNumber")

            text_col = QVBoxLayout()
            text_col.setSpacing(1)
            lbl_title = QLabel(title)
            apply_qss_role(lbl_title, role="stageTitle")
            lbl_desc = QLabel(desc)
            lbl_desc.setWordWrap(True)
            apply_qss_role(lbl_desc, role="mutedText")
            text_col.addWidget(lbl_title)
            text_col.addWidget(lbl_desc)

            chip = QLabel("대기")
            apply_qss_role(chip, role="stageBadge")

            box_lay.addWidget(lbl_num, alignment=Qt.AlignmentFlag.AlignTop)
            box_lay.addLayout(text_col, stretch=1)
            box_lay.addWidget(chip, alignment=Qt.AlignmentFlag.AlignTop)

            stepper_lay.addWidget(box, stretch=1)
            self._agg_stage_boxes.append(box)
            self._agg_stage_chips.append(chip)

        lay.addWidget(stepper_frame)

        # ---------------------------------------------------------------------
        # 2. 세로 스택(Vertical 3-Stage): 1단계 → (선택) 가로 병합 → 2단계 → 3단계, 모든 카드 너비 100%
        # ---------------------------------------------------------------------
        cards_layout = QVBoxLayout()
        cards_layout.setSpacing(14)

        # [STAGE 1 CARD] DOCX Parsing & Form Schema
        self.card_agg_step1 = HoverZoomCard(
            title="1단계 · 과제 DOCX 추출 및 양식 스키마",
            parent=w,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_agg_step1, role="hoverZoomCard", obj_name="AggStep1Card")
        lay_step1 = self.card_agg_step1.body_layout()

        form1 = QVBoxLayout()
        form1.setSpacing(10)

        # 학생 DOCX 폴더
        box_docx_dir = QHBoxLayout()
        self.edit_agg_docx_dir = QLineEdit()
        self.edit_agg_docx_dir.setPlaceholderText("학생들이 제출한 .docx 파일 폴더를 선택하세요.")
        self.edit_agg_docx_dir.setMinimumHeight(32)
        apply_qss_role(self.edit_agg_docx_dir, obj_name="EditAggDocxDir")

        btn_browse_docx = QPushButton("찾아보기...")
        btn_browse_docx.setMinimumHeight(32)
        apply_qss_role(btn_browse_docx, role="secondary")
        btn_browse_docx.clicked.connect(self._on_browse_agg_docx_dir)

        btn_open_docx = QPushButton("폴더 열기")
        btn_open_docx.setMinimumHeight(32)
        apply_qss_role(btn_open_docx, role="outline")
        btn_open_docx.clicked.connect(lambda: self._open_dir_if_exists(self.edit_agg_docx_dir.text()))

        box_docx_dir.addWidget(self.edit_agg_docx_dir, stretch=1)
        box_docx_dir.addWidget(btn_browse_docx)
        box_docx_dir.addWidget(btn_open_docx)
        form1.addLayout(self._field_block("학생 제출 폴더 (DOCX)", box_docx_dir))

        # 과제 양식(JSON 스키마) 선택 및 새 양식 생성 버튼
        box_schema = QHBoxLayout()
        self.combo_agg_schema = QComboBox()
        self.combo_agg_schema.setMinimumHeight(32)
        apply_qss_role(self.combo_agg_schema, obj_name="ComboAggSchema")

        btn_create_schema = QPushButton("+ 새 스키마 생성")
        btn_create_schema.setMinimumHeight(32)
        apply_qss_role(btn_create_schema, role="secondary")
        btn_create_schema.clicked.connect(self._on_open_create_schema_dialog)

        btn_refresh_schema = QPushButton("새로고침")
        btn_refresh_schema.setMinimumHeight(32)
        apply_qss_role(btn_refresh_schema, role="outline")
        btn_refresh_schema.clicked.connect(self._refresh_schemas_combo)

        box_schema.addWidget(self.combo_agg_schema, stretch=1)
        box_schema.addWidget(btn_create_schema)
        box_schema.addWidget(btn_refresh_schema)
        form1.addLayout(self._field_block("과제 양식 템플릿 (스키마)", box_schema))

        lay_step1.addLayout(form1)

        # 스키마 감지 및 PII 보호 안내 배지
        lbl_schema_info = QLabel("양식 분석: 표(Table) 셀 자동 추출 · 학생 개인정보 자동 마스킹")
        lbl_schema_info.setWordWrap(True)
        apply_qss_role(lbl_schema_info, role="infoBanner", obj_name="SchemaInfoBanner")
        lay_step1.addWidget(lbl_schema_info)

        # 1단계 단독 실행 버튼 (중앙 1단계 카드에 유지)
        self.btn_run_docx_only = QPushButton("1. DOCX -> 엑셀 추출 실행")
        self.btn_run_docx_only.setMinimumHeight(36)
        self.btn_run_docx_only.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_qss_role(self.btn_run_docx_only, role="primary", obj_name="BtnRunDocxOnly")
        self.btn_run_docx_only.clicked.connect(self._on_run_docx_extract)
        lay_step1.addWidget(self.btn_run_docx_only)

        cards_layout.addWidget(self.card_agg_step1)

        # [STAGE 2 CARD] AI Evaluation & NEIS Record Generation
        self.card_agg_step2 = HoverZoomCard(
            title="2단계 · AI 채점 및 과세특 생성 설정",
            parent=w,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_agg_step2, role="hoverZoomCard", obj_name="AggStep2Card")
        self.card_agg_step2.setProperty("aiTargetSelected", "false")
        self._mode_badge_hovered: bool = False
        self.btn_agg_mode_badge = QPushButton("세특 작성 모드 활성")
        self.btn_agg_mode_badge.setCheckable(True)
        self.btn_agg_mode_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_agg_mode_badge.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.btn_agg_mode_badge.setMinimumHeight(24)
        self.btn_agg_mode_badge.setToolTip(
            "클릭하여 프롬프트별 결과 파일명 접두사 규칙 및 저장 열 설명을 펼치거나 접습니다."
        )
        apply_qss_role(self.btn_agg_mode_badge, obj_name="AggModeBadge")
        self.btn_agg_mode_badge.setProperty("aggMode", "record")
        self.btn_agg_mode_badge.clicked.connect(self._on_toggle_mode_explain_accordion)
        self.card_agg_step2.header_layout.insertWidget(1, self.btn_agg_mode_badge)

        self.lbl_target_badge_step2 = QLabel("AI 대상 선택됨")
        apply_qss_role(self.lbl_target_badge_step2, obj_name="AiTargetSelectedBadge")
        self.lbl_target_badge_step2.setVisible(False)
        self.card_agg_step2.add_header_widget(self.lbl_target_badge_step2)
        lay_step2 = self.card_agg_step2.body_layout()

        # 클릭 시 펼쳐지는 모드/결과 파일명 규칙 아코디언 설명 패널 (기본 접힘)
        self.mode_explain_accordion = QFrame()
        apply_qss_role(self.mode_explain_accordion, obj_name="ModeExplainAccordion")
        self.mode_explain_accordion.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.mode_explain_accordion.setMaximumHeight(0)
        self.mode_explain_accordion.setVisible(False)

        lay_mode_explain = QVBoxLayout(self.mode_explain_accordion)
        lay_mode_explain.setContentsMargins(14, 12, 14, 12)
        lay_mode_explain.setSpacing(8)

        self.lbl_mode_explain_summary = QLabel()
        self.lbl_mode_explain_summary.setWordWrap(True)
        self.lbl_mode_explain_summary.setTextFormat(Qt.TextFormat.RichText)
        apply_qss_role(self.lbl_mode_explain_summary, obj_name="ModeExplainSummaryLabel")
        lay_mode_explain.addWidget(self.lbl_mode_explain_summary)

        self.lbl_mode_explain_rules = QLabel(
            "<b>프롬프트별 결과 파일명 자동 분기 규칙</b><br>"
            "• <b>채점 모드 (<code>채점완료_&lt;원본파일명&gt;.xlsx</code>)</b>: 선택한 프롬프트 파일명에 "
            "<code>'채점'</code>이 포함된 경우 자동 활성화됩니다. 결과는 <code>AI_채점결과</code> 열에 저장되며 "
            "NEIS 바이트 제한을 적용하지 않습니다.<br>"
            "• <b>세특 작성 모드 (<code>과세특완료_&lt;원본파일명&gt;.xlsx</code>)</b>: 그 외 세특·기본 프롬프트 "
            "선택 시 활성화됩니다. 결과는 <code>생성된_과세특</code> 및 <code>과세특_바이트</code> 열에 저장되며 "
            "설정된 NEIS 바이트 예산을 따릅니다.<br>"
            "• <b>이어쓰기(Resume) 동작</b>: 대상 폴더에 이미 해당 접두사(<code>채점완료_</code> 또는 "
            "<code>과세특완료_</code>)가 붙은 결과 파일이 있으면 그 파일을 우선 읽어 미완료 학생만 이어서 처리합니다."
        )
        self.lbl_mode_explain_rules.setWordWrap(True)
        self.lbl_mode_explain_rules.setTextFormat(Qt.TextFormat.RichText)
        apply_qss_role(self.lbl_mode_explain_rules, obj_name="ModeExplainRulesLabel")
        lay_mode_explain.addWidget(self.lbl_mode_explain_rules)

        self._mode_explain_anim = QPropertyAnimation(self.mode_explain_accordion, b"maximumHeight", self)
        self._mode_explain_anim.setDuration(220)
        self._mode_explain_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._mode_explain_anim.finished.connect(self._on_mode_explain_anim_finished)

        lay_step2.addWidget(self.mode_explain_accordion)

        # 활성 AI 공급자 및 모델 상태 배지 바
        box_ai_badge = QHBoxLayout()
        box_ai_badge.setContentsMargins(0, 0, 0, 4)
        box_ai_badge.setSpacing(8)
        self.lbl_active_ai_badge = QLabel("AI 엔진 상태 확인 중...")
        self.lbl_active_ai_badge.setWordWrap(True)
        apply_qss_role(self.lbl_active_ai_badge, role="engineBadge", obj_name="ActiveAiBadge")

        btn_go_settings = QPushButton("AI 설정 변경")
        btn_go_settings.setMinimumHeight(30)
        btn_go_settings.setToolTip("환경설정 탭으로 이동하여 AI 공급자와 모델, API 키를 변경합니다.")
        apply_qss_role(btn_go_settings, role="secondary")
        btn_go_settings.clicked.connect(self._on_go_to_settings_tab)

        btn_refresh_ai = QPushButton("상태 갱신")
        btn_refresh_ai.setMinimumHeight(30)
        apply_qss_role(btn_refresh_ai, role="subtle")
        btn_refresh_ai.clicked.connect(self._update_active_ai_display)

        box_ai_badge.addWidget(self.lbl_active_ai_badge, stretch=1)
        box_ai_badge.addWidget(btn_go_settings)
        box_ai_badge.addWidget(btn_refresh_ai)
        lay_step2.addLayout(box_ai_badge)

        form2 = QVBoxLayout()
        form2.setSpacing(10)

        # 대상 엑셀 파일 (1단계 추출 엑셀 또는 기존 엑셀)
        box_excel_file = QHBoxLayout()
        self.edit_agg_excel_file = QLineEdit()
        self.edit_agg_excel_file.setPlaceholderText("학번/이름/문항별 응답이 정리된 .xlsx 파일 (1단계 완료 시 자동 입력)")
        self.edit_agg_excel_file.setMinimumHeight(32)
        apply_qss_role(self.edit_agg_excel_file, obj_name="EditAggExcelFile")

        btn_browse_excel = QPushButton("찾아보기...")
        btn_browse_excel.setMinimumHeight(32)
        apply_qss_role(btn_browse_excel, role="secondary")
        btn_browse_excel.clicked.connect(self._on_browse_agg_excel_file)

        btn_open_excel = QPushButton("파일 열기")
        btn_open_excel.setMinimumHeight(32)
        apply_qss_role(btn_open_excel, role="outline")
        btn_open_excel.clicked.connect(lambda: self._open_file_if_exists(self.edit_agg_excel_file.text()))

        box_excel_file.addWidget(self.edit_agg_excel_file, stretch=1)
        box_excel_file.addWidget(btn_browse_excel)
        box_excel_file.addWidget(btn_open_excel)
        form2.addLayout(self._field_block("대상 엑셀 파일 (학번 · 이름 · 문항별 응답)", box_excel_file))

        # 프롬프트 선택 및 새 프롬프트 작성/폴더 열기 버튼
        box_prompt = QHBoxLayout()
        self.combo_agg_prompt = QComboBox()
        self.combo_agg_prompt.setMinimumHeight(32)
        apply_qss_role(self.combo_agg_prompt, obj_name="ComboAggPrompt")

        btn_new_prompt = QPushButton("+ 새 프롬프트")
        btn_new_prompt.setMinimumHeight(32)
        apply_qss_role(btn_new_prompt, role="secondary")
        btn_new_prompt.clicked.connect(self._on_open_new_prompt_dialog)

        btn_open_prompt_dir = QPushButton("폴더 열기")
        btn_open_prompt_dir.setMinimumHeight(32)
        apply_qss_role(btn_open_prompt_dir, role="outline")
        btn_open_prompt_dir.clicked.connect(self._on_open_prompt_dir)

        btn_refresh_prompt = QPushButton("새로고침")
        btn_refresh_prompt.setMinimumHeight(32)
        apply_qss_role(btn_refresh_prompt, role="outline")
        btn_refresh_prompt.clicked.connect(self._refresh_prompts_combo)

        box_prompt.addWidget(self.combo_agg_prompt, stretch=1)
        box_prompt.addWidget(btn_new_prompt)
        box_prompt.addWidget(btn_open_prompt_dir)
        box_prompt.addWidget(btn_refresh_prompt)
        form2.addLayout(self._field_block("세특 · 채점 프롬프트 프리셋", box_prompt))

        # 참고자료 폴더
        box_ref_dir = QHBoxLayout()
        self.edit_agg_ref_dir = QLineEdit()
        self.edit_agg_ref_dir.setText(str(Path("references").resolve()) if Path("references").exists() else "references")
        self.edit_agg_ref_dir.setPlaceholderText("수업계획서, 성취기준, 루브릭 등 참고 문서 폴더 (선택 사항)")
        self.edit_agg_ref_dir.setMinimumHeight(32)
        apply_qss_role(self.edit_agg_ref_dir, obj_name="EditAggRefDir")

        btn_browse_ref = QPushButton("찾아보기...")
        btn_browse_ref.setMinimumHeight(32)
        apply_qss_role(btn_browse_ref, role="secondary")
        btn_browse_ref.clicked.connect(self._on_browse_agg_ref_dir)

        box_ref_dir.addWidget(self.edit_agg_ref_dir, stretch=1)
        box_ref_dir.addWidget(btn_browse_ref)
        form2.addLayout(self._field_block("참고자료 폴더 (수업계획서 · 성취기준 · 루브릭)", box_ref_dir))

        # 나이스 바이트 상한선
        box_bytes = QHBoxLayout()
        self.spin_agg_max_bytes = QSpinBox()
        self.spin_agg_max_bytes.setRange(100, 3000)
        self.spin_agg_max_bytes.setSingleStep(50)
        self.spin_agg_max_bytes.setValue(1500)
        self.spin_agg_max_bytes.setSuffix(" Byte")
        self.spin_agg_max_bytes.setMinimumHeight(32)
        apply_qss_role(self.spin_agg_max_bytes, obj_name="SpinAggMaxBytes")

        lbl_byte_tip = QLabel("고등학교 나이스 교과세특 상한선은 약 1,500 Byte (한글 500자 기준)입니다.")
        lbl_byte_tip.setWordWrap(True)
        apply_qss_role(lbl_byte_tip, role="mutedText")

        box_bytes.addWidget(self.spin_agg_max_bytes)
        box_bytes.addWidget(lbl_byte_tip, stretch=1)
        form2.addLayout(self._field_block("NEIS 글자수(바이트) 예산", box_bytes))

        lay_step2.addLayout(form2)

        # ---------------------------------------------------------------------
        # [선택 단계] 다중 과제 엑셀 가로 병합 (Column Merge) 카드
        # (1단계 추출 직후 여러 과제를 병합하여 2단계 대상 엑셀로 자동 전달)
        # ---------------------------------------------------------------------
        self.card_agg_merge = HoverZoomCard(
            title="다중 과제 엑셀 가로 병합 (Column Merge) — 학번 기준 여러 과제 통합",
            parent=w,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_agg_merge, role="hoverZoomCard", obj_name="AggMergeCard")
        lay_merge = self.card_agg_merge.body_layout()

        # 안내 배지
        lbl_merge_tip = QLabel(
            "여러 개의 과제 엑셀 파일(.xlsx)을 '학번' 기준으로 가로 결합(Full Outer Join)합니다. "
            "목록 위쪽 파일의 열이 결과 엑셀의 앞쪽에 배치되며, 겹치는 열 이름은 [파일명] 접두사가 자동 부여됩니다."
        )
        lbl_merge_tip.setWordWrap(True)
        apply_qss_role(lbl_merge_tip, role="infoBanner")
        lay_merge.addWidget(lbl_merge_tip)

        # 파일 목록 + 우측 컨트롤 버튼들
        box_list_and_btns = QHBoxLayout()
        box_list_and_btns.setSpacing(10)

        self.list_merge_excels = QListWidget()
        self.list_merge_excels.setMinimumHeight(120)
        self.list_merge_excels.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_merge_excels.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        apply_qss_role(self.list_merge_excels, obj_name="ListMergeExcels")
        box_list_and_btns.addWidget(self.list_merge_excels, stretch=1)

        # 우측 버튼들 (이모지 대신 깔끔한 텍스트/기호 사용)
        box_merge_btns = QVBoxLayout()
        box_merge_btns.setSpacing(6)

        btn_merge_add = QPushButton("+ 파일 추가")
        btn_merge_add.setMinimumHeight(28)
        apply_qss_role(btn_merge_add, role="secondary")
        btn_merge_add.clicked.connect(self._on_merge_add_files)

        btn_merge_del = QPushButton("- 선택 제거")
        btn_merge_del.setMinimumHeight(28)
        apply_qss_role(btn_merge_del, role="outline")
        btn_merge_del.clicked.connect(self._on_merge_del_file)

        btn_merge_up = QPushButton("▲ 위로")
        btn_merge_up.setMinimumHeight(28)
        apply_qss_role(btn_merge_up, role="outline")
        btn_merge_up.clicked.connect(self._on_merge_move_up)

        btn_merge_down = QPushButton("▼ 아래로")
        btn_merge_down.setMinimumHeight(28)
        apply_qss_role(btn_merge_down, role="outline")
        btn_merge_down.clicked.connect(self._on_merge_move_down)

        btn_merge_clear = QPushButton("목록 비우기")
        btn_merge_clear.setMinimumHeight(28)
        apply_qss_role(btn_merge_clear, role="outline")
        btn_merge_clear.clicked.connect(self._on_merge_clear_files)

        box_merge_btns.addWidget(btn_merge_add)
        box_merge_btns.addWidget(btn_merge_del)
        box_merge_btns.addWidget(btn_merge_up)
        box_merge_btns.addWidget(btn_merge_down)
        box_merge_btns.addWidget(btn_merge_clear)
        box_merge_btns.addStretch()
        box_list_and_btns.addLayout(box_merge_btns)

        lay_merge.addLayout(box_list_and_btns)

        # 출력 파일명 설정
        box_merge_output = QHBoxLayout()
        self.edit_merge_output = QLineEdit()
        self.edit_merge_output.setText(f"통합과제_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        self.edit_merge_output.setPlaceholderText("저장될 통합 엑셀 파일 경로")
        self.edit_merge_output.setMinimumHeight(32)
        apply_qss_role(self.edit_merge_output, obj_name="EditMergeOutput")

        btn_browse_merge_output = QPushButton("저장위치 변경...")
        btn_browse_merge_output.setMinimumHeight(32)
        apply_qss_role(btn_browse_merge_output, role="secondary")
        btn_browse_merge_output.clicked.connect(self._on_browse_merge_output)

        box_merge_output.addWidget(self.edit_merge_output, stretch=1)
        box_merge_output.addWidget(btn_browse_merge_output)
        lay_merge.addLayout(self._field_block("출력 파일명", box_merge_output))

        # 하단 실행 버튼 및 피드백 레이아웃
        box_merge_action = QHBoxLayout()
        box_merge_action.setSpacing(10)

        self.btn_run_merge = QPushButton("학번 기준 다중 과제 가로 병합 실행")
        self.btn_run_merge.setMinimumHeight(38)
        self.btn_run_merge.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_qss_role(self.btn_run_merge, role="primary", obj_name="BtnRunMerge")
        self.btn_run_merge.clicked.connect(self._on_run_column_merge)

        self.lbl_merge_status = QLabel("")
        self.lbl_merge_status.setWordWrap(True)
        apply_qss_role(self.lbl_merge_status, role="mutedText")

        self.btn_open_merged_file = QPushButton("통합 엑셀 열기")
        self.btn_open_merged_file.setMinimumHeight(32)
        apply_qss_role(self.btn_open_merged_file, role="success")
        self.btn_open_merged_file.setVisible(False)
        self.btn_open_merged_file.clicked.connect(lambda: self._open_file_if_exists(self.edit_merge_output.text()))

        box_merge_action.addWidget(self.btn_run_merge, stretch=2)
        box_merge_action.addWidget(self.lbl_merge_status, stretch=3)
        box_merge_action.addWidget(self.btn_open_merged_file)
        lay_merge.addLayout(box_merge_action)

        # 세로 스택 배치: 상단(1단계 DOCX 추출 + 다중 과제 병합) → 중단(2단계 AI 설정) → 하단(3단계 실행 및 진행 상황)
        cards_layout.addWidget(self.card_agg_merge)
        cards_layout.addWidget(self.card_agg_step2)
        lay.addLayout(cards_layout)

        # ---------------------------------------------------------------------
        # 3. 하단 카드: 3단계 · 학생별 처리 상태 및 결과 미리보기
        # ---------------------------------------------------------------------
        self.card_agg_step3 = HoverZoomCard(
            title="3단계 · 학생별 처리 상태 및 결과 미리보기",
            parent=w,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_agg_step3, role="hoverZoomCard", obj_name="AggStep3Card")
        self.card_agg_step3.setProperty("aiTargetSelected", "false")
        lay_step3 = self.card_agg_step3.body_layout()

        preview_hdr = QHBoxLayout()
        preview_hdr.setSpacing(8)
        self.lbl_agg_preview_summary = QLabel(
            "엑셀 파일을 선택하거나 1단계를 실행하면 학생별 처리 상태와 생성된 세특 미리보기가 표시됩니다."
        )
        self.lbl_agg_preview_summary.setWordWrap(True)
        apply_qss_role(self.lbl_agg_preview_summary, role="mutedText")

        btn_refresh_preview = QPushButton("미리보기 갱신")
        btn_refresh_preview.setMinimumHeight(28)
        apply_qss_role(btn_refresh_preview, role="outline")
        btn_refresh_preview.clicked.connect(lambda: self._refresh_agg_student_preview_table())

        preview_hdr.addWidget(self.lbl_agg_preview_summary, stretch=1)
        preview_hdr.addWidget(btn_refresh_preview)
        lay_step3.addLayout(preview_hdr)

        self.table_agg_preview = QTableWidget(0, 5)
        self.table_agg_preview.setHorizontalHeaderLabels(
            ["학번", "이름", "응답 문항", "처리 상태", "생성된 결과 미리보기"]
        )
        self.table_agg_preview.setMinimumHeight(200)
        self.table_agg_preview.setMaximumHeight(320)
        self.table_agg_preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_agg_preview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        v_hdr = self.table_agg_preview.verticalHeader()
        if v_hdr is not None:
            v_hdr.setVisible(False)
        h_hdr = self.table_agg_preview.horizontalHeader()
        if h_hdr is not None:
            h_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            h_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            h_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            h_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            h_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        lay_step3.addWidget(self.table_agg_preview)

        lbl_dock_exec_hint = QLabel(
            "AI 실행(API 일괄 생성 · 수동 복붙 · Playwright 자동)은 우측 [AI 실행 패널]에서 실행합니다."
        )
        lbl_dock_exec_hint.setWordWrap(True)
        apply_qss_role(lbl_dock_exec_hint, role="infoBanner")
        lay_step3.addWidget(lbl_dock_exec_hint)

        # 피드백 & 결과 열기 상태 영역
        box_feedback = QHBoxLayout()
        self.lbl_agg_status = QLabel("대기 중: 1단계 추출 또는 우측 AI 실행 패널에서 작업을 실행해 주세요.")
        self.lbl_agg_status.setWordWrap(True)
        apply_qss_role(self.lbl_agg_status, role="mutedText")

        self.btn_open_result_excel = QPushButton("생성된 결과 엑셀 열기 (.xlsx)")
        self.btn_open_result_excel.setMinimumHeight(32)
        apply_qss_role(self.btn_open_result_excel, role="success")
        self.btn_open_result_excel.setVisible(False)
        self.btn_open_result_excel.clicked.connect(self._on_open_last_result_excel)

        box_feedback.addWidget(self.lbl_agg_status, stretch=1)
        box_feedback.addWidget(self.btn_open_result_excel)
        lay_step3.addLayout(box_feedback)

        lay.addWidget(self.card_agg_step3)
        lay.addStretch()

        scroll.setWidget(w)

        # 초기 콤보박스 목록 및 활성 AI 상태 로드
        self._last_result_excel_path: Optional[str] = None
        self._refresh_schemas_combo()
        self._refresh_prompts_combo()
        self.combo_agg_prompt.currentIndexChanged.connect(lambda *_: self._update_agg_mode_badge_and_accordion())
        self.edit_agg_excel_file.textChanged.connect(lambda *_: self._update_agg_mode_badge_and_accordion())
        self._update_agg_mode_badge_and_accordion()
        self._update_active_ai_display()

        return scroll

    # =========================================================================
    # [서브탭 1] 핸들러
    # =========================================================================
    def _on_browse_submission_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "학생 제출물 폴더 선택")
        if folder:
            norm = str(Path(folder).resolve())
            self.edit_submission_dir.setText(norm)
            self.edit_submission_dir.setToolTip(norm)
            folder_name = Path(norm).name
            if folder_name:
                self.edit_assignment_title.setText(folder_name)
            self.run_submission_analysis()

    def _on_open_submission_dir(self) -> None:
        folder = self.edit_submission_dir.text().strip()
        if folder and os.path.exists(folder):
            os.startfile(folder)
        else:
            QMessageBox.information(self, "안내", "먼저 유효한 제출물 폴더를 선택해 주세요.")

    def run_submission_analysis(self) -> None:
        folder = self.edit_submission_dir.text().strip()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "폴더 확인", "학생들이 제출한 파일이 들어있는 폴더를 먼저 선택해 주세요.")
            return

        roster = self.db.get_students()
        worker = StudentSubmissionAnalyzeWorker(
            folder_path=folder,
            recursive=self.chk_recursive.isChecked(),
            ext_filter=self.combo_ext_filter.currentData(),
            id_regex=self.edit_id_regex.text(),
            name_regex=self.edit_name_regex.text(),
            assignment_title=self.edit_assignment_title.text(),
            roster=roster,
        )

        self.btn_scan_analyze.setEnabled(False)
        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_ANALYZE,
            on_result=self._on_analysis_completed,
            on_finished=lambda: self.btn_scan_analyze.setEnabled(True),
        )

    def _on_analysis_completed(self, result: Dict[str, Any]) -> None:
        self._analyzed_items = result.get("files", [])
        self._missing_students = result.get("missing_students", [])
        submitted_cnt = result.get("submitted_count", 0)
        roster_cnt = result.get("roster_count", 0)

        self._populate_submissions_table()
        self._populate_missing_table()

        pdf_cnt = sum(1 for it in self._analyzed_items if it.get("ext") == ".pdf")
        if roster_cnt == 0:
            self.lbl_summary_badge.setText(
                f"분석 파일: 총 {len(self._analyzed_items)}개 (PDF {pdf_cnt}개) | "
                f"등록된 학생 명렬표가 없습니다. 환경설정에서 명렬표를 등록하면 미제출자가 자동 대조됩니다."
            )
        else:
            self.lbl_summary_badge.setText(
                f"명렬표 총원: {roster_cnt}명  |  제출 완료: {submitted_cnt}명  |  "
                f"미제출: {len(self._missing_students)}명  |  스캔 파일: {len(self._analyzed_items)}개 (PDF {pdf_cnt}개)"
            )

    def _populate_submissions_table(self) -> None:
        self.table_submissions.blockSignals(True)
        self.table_submissions.setRowCount(0)

        for row_idx, item in enumerate(self._analyzed_items):
            self.table_submissions.insertRow(row_idx)

            chk_item = QTableWidgetItem()
            chk_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            chk_item.setCheckState(
                Qt.CheckState.Checked if item.get("checked", True) else Qt.CheckState.Unchecked
            )
            chk_item.setData(Qt.ItemDataRole.UserRole, item)

            sid_str = str(item.get("student_id", ""))
            sid_item = QTableWidgetItem(sid_str)
            sid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            name_str = str(item.get("name", ""))
            name_item = QTableWidgetItem(name_str)
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            status_str = str(item.get("match_status", ""))
            status_item = QTableWidgetItem(status_str)
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if "일치" in status_str or "매칭" in status_str or "완료" in status_str:
                status_item.setForeground(QBrush(QColor("#059669")))
            elif "동명이인" in status_str or "확인" in status_str:
                status_item.setForeground(QBrush(QColor("#d97706")))
            else:
                status_item.setForeground(QBrush(QColor("#dc2626")))

            orig_name_str = str(item.get("orig_name", ""))
            orig_name_item = QTableWidgetItem(orig_name_str)
            orig_name_item.setFlags(orig_name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            new_name_str = str(item.get("new_name", ""))
            new_name_item = QTableWidgetItem(new_name_str)
            new_name_item.setForeground(QBrush(QColor("#2563eb")))

            orig_path_str = str(item.get("orig_path", ""))
            orig_path_item = QTableWidgetItem(orig_path_str)
            orig_path_item.setFlags(orig_path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            tooltip_html = build_floating_tooltip(
                f"[{sid_str or '학번미상'}] {name_str or '이름미상'}",
                [
                    f"<b>명렬표 대조 상태:</b> {html.escape(status_str)}",
                    f"<b>원본 파일명:</b> {html.escape(orig_name_str)}",
                    f"<b>변경될 표준 파일명:</b> <span style='color:#93c5fd;'>{html.escape(new_name_str)}</span>",
                    f"<b>원본 전체 경로:</b> {html.escape(orig_path_str)}",
                ],
            )

            row_items = [
                chk_item,
                sid_item,
                name_item,
                status_item,
                orig_name_item,
                new_name_item,
                orig_path_item,
            ]
            for col_idx, it in enumerate(row_items):
                it.setToolTip(tooltip_html)
                self.table_submissions.setItem(row_idx, col_idx, it)

        self.table_submissions.blockSignals(False)

    def _populate_missing_table(self) -> None:
        self.table_missing.setRowCount(0)
        for row_idx, stu in enumerate(self._missing_students):
            self.table_missing.insertRow(row_idx)

            sid_str = str(stu.get("student_id", ""))
            sid_item = QTableWidgetItem(sid_str)
            sid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            sid_item.setForeground(QBrush(QColor("#dc2626")))

            gcn = f"{stu.get('grade', 1)}-{stu.get('class_no', 1)}-{stu.get('number', 1):02d}"
            gcn_item = QTableWidgetItem(gcn)
            gcn_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            name_str = str(stu.get("name", ""))
            name_item = QTableWidgetItem(name_str)
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            contact_str = str(stu.get("contact", ""))
            contact_item = QTableWidgetItem(contact_str)

            parent_str = str(stu.get("parent_contact", ""))
            parent_item = QTableWidgetItem(parent_str)

            tooltip_html = build_floating_tooltip(
                f"미제출: [{sid_str}] {name_str}",
                [
                    f"<b>학년-반-번호:</b> {gcn}",
                    f"<b>학생 연락처:</b> {html.escape(contact_str or '-')}",
                    f"<b>학부모 연락처:</b> {html.escape(parent_str or '-')}",
                    f"<b>비고:</b> {html.escape(str(stu.get('memo', '') or '-'))}",
                ],
            )

            for col_idx, it in enumerate((sid_item, gcn_item, name_item, contact_item, parent_item)):
                it.setToolTip(tooltip_html)
                self.table_missing.setItem(row_idx, col_idx, it)

    def _on_submission_table_item_changed(self, changed_item: QTableWidgetItem) -> None:
        row = changed_item.row()
        col = changed_item.column()
        if row < 0 or row >= len(self._analyzed_items):
            return

        chk_cell = self.table_submissions.item(row, 0)
        sid_cell = self.table_submissions.item(row, 1)
        name_cell = self.table_submissions.item(row, 2)
        new_name_cell = self.table_submissions.item(row, 5)
        if not (chk_cell and sid_cell and name_cell and new_name_cell):
            return

        data = self._analyzed_items[row]
        data["checked"] = chk_cell.checkState() == Qt.CheckState.Checked

        if col in (1, 2):
            data["student_id"] = sid_cell.text().strip()
            data["name"] = name_cell.text().strip()
            updated_name = build_standard_filename(
                student_id=data["student_id"],
                student_name=data["name"],
                assignment_title=self.edit_assignment_title.text(),
                ext=data.get("ext", ""),
            )
            data["new_name"] = updated_name
            self.table_submissions.blockSignals(True)
            new_name_cell.setText(updated_name)
            self.table_submissions.blockSignals(False)
        elif col == 5:
            data["new_name"] = new_name_cell.text().strip()

    def _on_assignment_title_changed(self, new_title: str) -> None:
        if not self._analyzed_items:
            return
        self.table_submissions.blockSignals(True)
        for row_idx, data in enumerate(self._analyzed_items):
            updated_name = build_standard_filename(
                student_id=data.get("student_id", ""),
                student_name=data.get("name", ""),
                assignment_title=new_title,
                ext=data.get("ext", ""),
            )
            data["new_name"] = updated_name
            cell = self.table_submissions.item(row_idx, 5)
            if cell is not None:
                cell.setText(updated_name)
        self.table_submissions.blockSignals(False)

    def _set_all_files_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.table_submissions.blockSignals(True)
        for row_idx, data in enumerate(self._analyzed_items):
            data["checked"] = checked
            cell = self.table_submissions.item(row_idx, 0)
            if cell is not None:
                cell.setCheckState(state)
        self.table_submissions.blockSignals(False)

    def _get_checked_submission_items(self) -> List[Dict[str, Any]]:
        return [it for it in self._analyzed_items if it.get("checked", True)]

    def _on_copy_missing_to_clipboard(self) -> None:
        if not self._missing_students:
            QMessageBox.information(self, "안내", "현재 미제출자가 없거나 아직 폴더 분석을 실행하지 않았습니다.")
            return
        title = self.edit_assignment_title.text().strip() or "과제"
        lines = [f"[{title} 미제출자 명단 (총 {len(self._missing_students)}명)]"]
        for s in self._missing_students:
            lines.append(f"- {s.get('student_id')} {s.get('name')}")
        text = "\n".join(lines)
        QApplication.clipboard().setText(text)
        QMessageBox.information(
            self,
            "클립보드 복사 완료",
            f"미제출자 {len(self._missing_students)}명의 명단이 클립보드에 복사되었습니다.\n"
            "학급 공지톡이나 메모장에 붙여넣기(Ctrl+V) 하세요.",
        )

    def _on_export_submission_report_xlsx(self) -> None:
        if not self._analyzed_items and not self._missing_students:
            QMessageBox.information(self, "안내", "먼저 제출물 폴더 스캔을 실행해 주세요.")
            return

        default_name = f"제출현황_{self.edit_assignment_title.text().strip() or '과제'}_{datetime.now().strftime('%Y%m%d')}.xlsx"
        save_path, _ = QFileDialog.getSaveFileName(
            self, "제출/미제출 현황 엑셀 저장", default_name, "Excel 파일 (*.xlsx)"
        )
        if not save_path:
            return

        try:
            import openpyxl

            wb = openpyxl.Workbook()
            ws_miss = wb.active
            ws_miss.title = "미제출자_명단"
            ws_miss.append(["학번", "학년", "반", "번호", "이름", "학생연락처", "학부모연락처", "비고"])
            for s in self._missing_students:
                ws_miss.append(
                    [
                        s.get("student_id", ""),
                        s.get("grade", ""),
                        s.get("class_no", ""),
                        s.get("number", ""),
                        s.get("name", ""),
                        s.get("contact", ""),
                        s.get("parent_contact", ""),
                        s.get("memo", ""),
                    ]
                )

            ws_sub = wb.create_sheet("제출파일_목록(학번순)")
            ws_sub.append(["학번", "이름", "매칭상태", "원본파일명", "표준파일명", "원본경로"])
            for it in sorted(self._analyzed_items, key=_student_sort_key):
                ws_sub.append(
                    [
                        it.get("student_id", ""),
                        it.get("name", ""),
                        it.get("match_status", ""),
                        it.get("orig_name", ""),
                        it.get("new_name", ""),
                        it.get("orig_path", ""),
                    ]
                )

            wb.save(save_path)
            QMessageBox.information(self, "저장 완료", f"제출/미제출 현황 엑셀 파일이 저장되었습니다:\n{save_path}")
        except Exception as exc:
            QMessageBox.critical(self, "저장 오류", f"엑셀 저장 중 오류가 발생했습니다:\n{exc}")

    def run_standard_rename(self) -> None:
        selected = self._get_checked_submission_items()
        if not selected:
            QMessageBox.warning(self, "선택 확인", "표준 파일명으로 변경할 항목을 먼저 스캔하고 선택해 주세요.")
            return

        base_folder = Path(self.edit_submission_dir.text().strip())
        copy_mode = self.combo_rename_mode.currentData() == "copy"
        title = self.edit_assignment_title.text().strip() or "과제"

        if copy_mode:
            output_dir = str(base_folder / f"{title}_표준파일명_정리완료")
        else:
            ans = QMessageBox.question(
                self,
                "원본 파일명 직접 변경 확인",
                f"선택된 {len(selected)}개 파일의 이름을 원본 폴더에서 즉시 표준 형식([학번]_[이름]_{title}.확장자)으로 변경하시겠습니까?",
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            output_dir = str(base_folder)

        worker = StandardFileRenameWorker(
            items=selected,
            output_dir=output_dir,
            copy_mode=copy_mode,
        )
        self.btn_execute_rename.setEnabled(False)
        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_RENAME,
            on_result=self._on_rename_completed,
            on_finished=lambda: self.btn_execute_rename.setEnabled(True),
        )

    def _on_rename_completed(self, result: Dict[str, Any]) -> None:
        succ = result.get("success_count", 0)
        fail = result.get("fail_count", 0)
        out_dir = result.get("output_dir", "")

        self.notifier.notify(
            "학번순 표준 파일명 일괄 적용 완료",
            f"성공: {succ}건 / 실패: {fail}건\n저장 폴더: {out_dir}",
        )
        ans = QMessageBox.question(
            self,
            "표준 파일명 변경 완료",
            f"총 {succ}개 파일이 학번 순서대로 표준 파일명으로 정리되었습니다. (실패: {fail}건)\n\n"
            f"결과 폴더:\n{out_dir}\n\n지금 결과 폴더를 여시겠습니까?",
        )
        if ans == QMessageBox.StandardButton.Yes and os.path.exists(out_dir):
            os.startfile(out_dir)

        if self.combo_rename_mode.currentData() == "move":
            self.run_submission_analysis()

    def run_pdf_merge(self) -> None:
        selected = self._get_checked_submission_items()
        pdf_items = [it for it in selected if str(it.get("ext", "")).lower() == ".pdf"]

        if not pdf_items:
            QMessageBox.warning(
                self,
                "PDF 파일 없음",
                "선택된 항목 중 PDF(.pdf) 파일이 없습니다.\nPDF 제출물이 있는 폴더를 스캔했는지 확인해 주세요.",
            )
            return

        if not HAS_PYPDF:
            QMessageBox.critical(
                self,
                "pypdf 패키지 필요",
                "단일 PDF 병합 기능을 사용하려면 'pypdf' 라이브러리가 필요합니다.\n\n"
                "터미널에서 아래 명령어를 실행해 주세요:\n"
                "pip install pypdf>=4.0.0",
            )
            return

        title = self.edit_assignment_title.text().strip() or "과제"
        base_folder = Path(self.edit_submission_dir.text().strip() or ".")
        default_pdf = str(base_folder / f"[통합본]_{title}_학번순병합({len(pdf_items)}명).pdf")

        save_pdf_path, _ = QFileDialog.getSaveFileName(
            self,
            "학번순 단일 병합 PDF 저장 경로 선택",
            default_pdf,
            "PDF 문서 (*.pdf)",
        )
        if not save_pdf_path:
            return

        worker = PdfMergeByStudentWorker(
            items=pdf_items,
            output_pdf_path=save_pdf_path,
            add_bookmarks=self.chk_pdf_bookmark.isChecked(),
        )
        self.btn_execute_pdf_merge.setEnabled(False)
        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_PDF_MERGE,
            on_result=self._on_pdf_merge_completed,
            on_finished=lambda: self.btn_execute_pdf_merge.setEnabled(True),
        )

    def _on_pdf_merge_completed(self, result: Dict[str, Any]) -> None:
        out_pdf = result.get("output_pdf", "")
        merged_cnt = result.get("merged_files", 0)
        total_pages = result.get("total_pages", 0)
        skipped = result.get("skipped_files", [])

        self.notifier.notify(
            "학번순 단일 PDF 병합 완료",
            f"병합 파일: {merged_cnt}개 (총 {total_pages}페이지)\n파일명: {Path(out_pdf).name}",
        )

        skip_msg = f"\n\n[건너뛴 파일 ({len(skipped)}건)]:\n" + "\n".join(skipped[:5]) if skipped else ""
        ans = QMessageBox.question(
            self,
            "단일 PDF 병합 완료",
            f"학번 순서대로 총 {merged_cnt}개의 PDF({total_pages}페이지)가 하나로 병합되었습니다!{skip_msg}\n\n"
            f"저장 위치:\n{out_pdf}\n\n병합된 PDF 파일을 지금 바로 열어보시겠습니까?",
        )
        if ans == QMessageBox.StandardButton.Yes and os.path.exists(out_pdf):
            os.startfile(out_pdf)

    # =========================================================================
    # [서브탭 2] 하이브리드 AI OCR / 텍스트 보정 핸들러
    # =========================================================================
    def _on_add_ocr_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "OCR/보정 대상 문서 및 이미지 선택",
            "",
            "문서 및 이미지 (*.png *.jpg *.jpeg *.pdf *.txt *.md *.csv *.tif *.bmp *.webp);;모든 파일 (*)",
        )
        for f in files:
            it = QListWidgetItem(f)
            it.setToolTip(build_floating_tooltip("OCR 대상 파일 경로", [html.escape(f)]))
            self.list_ocr_files.addItem(it)

    def _on_open_manual_llm_dialog(self) -> None:
        """[수동 복붙 모드 열기] 버튼 클릭 시 ManualLlmDialog가 팝업됩니다."""
        files = [self.list_ocr_files.item(i).text() for i in range(self.list_ocr_files.count())]
        raw_text = extract_local_text_from_files(files) if files else self.txt_ocr_output.toPlainText().strip()

        dlg = ManualLlmDialog(
            self,
            task_key="ocr_correction",
            raw_content=raw_text,
            custom_instruction=self.edit_ocr_prompt.text().strip(),
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            res = dlg.get_result()
            self.txt_ocr_output.setPlainText(res.get("formatted_text", ""))
            self.notifier.notify(
                "수동 프롬프트 JSON 적용 완료",
                "외부 LLM 응답에서 JSON 데이터를 추출하여 문서 보정 결과에 반영했습니다.",
            )

    def _on_run_hybrid_ocr(self) -> None:
        """
        [AI 자동 실행 (API 사용)] 버튼 클릭 시:
        API 키가 설정되어 있고 수동 모드가 해제된 경우 -> 백그라운드 Worker로 자동 처리.
        API 키가 없거나 '수동 프롬프트 모드'가 체크된 경우 -> ManualLlmDialog 모달 자동 팝업.
        """
        client = MultiLlmClient()
        if client.is_manual_mode_active():
            self._on_open_manual_llm_dialog()
            return

        files = [self.list_ocr_files.item(i).text() for i in range(self.list_ocr_files.count())]
        if not files:
            QMessageBox.information(
                self,
                "파일 추가 또는 수동 모드 안내",
                "자동 API 보정을 수행할 파일을 먼저 목록에 추가해 주세요.\n"
                "(직접 텍스트를 붙여넣어 처리하려면 옆의 [수동 복붙 모드 열기] 버튼을 클릭하세요.)",
            )
            return

        worker = OcrAndLlmWorker(files, self.edit_ocr_prompt.text())
        self.task_runner.start_worker(
            worker,
            task_key=self.TASK_OCR,
            on_result=lambda res: self.txt_ocr_output.setPlainText(res.get("combined_text", "")),
        )

    def _on_save_ocr_result(self) -> None:
        text = self.txt_ocr_output.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "안내", "저장할 결과 텍스트가 없습니다.")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self, "AI 보정 결과 저장", "AI_문서보정결과.md", "Markdown (*.md);;텍스트 파일 (*.txt)"
        )
        if save_path:
            Path(save_path).write_text(text, encoding="utf-8")
            QMessageBox.information(self, "저장 완료", f"저장되었습니다:\n{save_path}")

    # =========================================================================
    # [서브탭 3] 과제 취합 및 AI 과세특·채점 자동화 핸들러
    # =========================================================================
    def _open_dir_if_exists(self, dir_path: str) -> None:
        p = Path(dir_path.strip()) if dir_path else None
        if p and p.exists() and p.is_dir():
            os.startfile(str(p.resolve()))
        else:
            QMessageBox.information(self, "안내", f"유효한 폴더 경로가 아닙니다:\n{dir_path}")

    def _open_file_if_exists(self, file_path: str) -> None:
        p = Path(file_path.strip()) if file_path else None
        if p and p.exists() and p.is_file():
            os.startfile(str(p.resolve()))
        else:
            QMessageBox.information(self, "안내", f"유효한 파일 경로가 아닙니다:\n{file_path}")

    def _update_active_ai_display(self) -> None:
        """현재 ConfigManager에 설정된 AI 공급자 및 모델 정보를 UI 배지에 반영합니다."""
        info = get_active_ai_info()
        p_name = info.get("provider_display", "Gemini")
        model = info.get("model", "gemini-1.5-flash")
        responded = info.get("responded_model", "")
        verified = info.get("connection_verified", False)
        has_key = info.get("has_key", False)

        model_disp = f"{model} -> {responded}" if (verified and responded and responded != model) else model

        if has_key:
            status_tag = "✓ 연결 확인됨" if verified else "✓ 정상 연결"
            self.lbl_active_ai_badge.setText(
                f"AI 엔진: <b>{p_name}</b> ({model_disp})  |  <b>{status_tag}</b>"
            )
            self.lbl_active_ai_badge.setStyleSheet(
                "background-color: #EFF6FF; color: #1E3A8A; font-size: 12px; "
                "padding: 6px 12px; border-radius: 8px; border: 1px solid #93C5FD;"
            )
        else:
            self.lbl_active_ai_badge.setText(
                f"AI 엔진: <b>{p_name}</b> ({model_disp})  |  <b>[!] API Key 미등록 (수동 배치 모드 사용 가능)</b>"
            )
            self.lbl_active_ai_badge.setStyleSheet(
                "background-color: #F8FAFC; color: #334155; font-size: 12px; "
                "padding: 6px 12px; border-radius: 8px; border: 1px solid #CBD5E1;"
            )

    def _on_open_batch_manual_dialog(self) -> None:
        """[수동 프롬프트 배치 모드 (웹 AI 복붙)] 모달을 엽니다."""
        excel_str = self.edit_agg_excel_file.text().strip()
        if not excel_str or not Path(excel_str).is_file():
            QMessageBox.warning(
                self,
                "대상 엑셀 파일 확인",
                "수동 배치 모드를 실행하려면 먼저 [대상 엑셀 파일]을 선택해 주세요.\n"
                "(1단계 DOCX 추출 완료 시 자동으로 입력됩니다.)",
            )
            return

        prompt_data = self.combo_agg_prompt.currentData()
        prompt_file = Path(prompt_data) if prompt_data else None

        ref_str = self.edit_agg_ref_dir.text().strip()
        ref_dir = Path(ref_str) if ref_str and Path(ref_str).is_dir() else None

        max_bytes = self.spin_agg_max_bytes.value()

        dlg = BatchManualPromptDialog(
            excel_path=excel_str,
            prompt_file=prompt_file,
            reference_dir=ref_dir,
            max_bytes=max_bytes,
            parent=self,
            task_runner=self.task_runner,
        )
        dlg.exec()

        if dlg.last_saved_excel_path:
            self._last_result_excel_path = dlg.last_saved_excel_path
            self.btn_open_result_excel.setVisible(True)
            self._update_agg_stepper(s1="done", s2="done", s3="done")
            self.lbl_agg_status.setText(
                f"✓ 수동 배치 반영 완료: {Path(dlg.last_saved_excel_path).name} (누적 {dlg.total_saved_count}명 저장)"
            )

    def _on_go_to_settings_tab(self) -> None:
        """메인 윈도우의 [환경설정] 탭으로 즉시 전환합니다."""
        curr: Optional[QWidget] = self
        switched = False
        while curr is not None:
            if hasattr(curr, "switch_to_settings_tab"):
                try:
                    getattr(curr, "switch_to_settings_tab")()
                    switched = True
                    break
                except Exception:
                    pass
            if hasattr(curr, "on_nav_btn_clicked"):
                try:
                    getattr(curr, "on_nav_btn_clicked")("settings")
                    switched = True
                    break
                except Exception:
                    pass
            if hasattr(curr, "main_tabs"):
                try:
                    tabs = getattr(curr, "main_tabs")
                    tabs.setCurrentIndex(tabs.count() - 1)
                    switched = True
                    break
                except Exception:
                    pass
            curr = curr.parentWidget()

        if not switched:
            QMessageBox.information(
                self,
                "환경설정 안내",
                "왼쪽 사이드바의 <b>[환경설정]</b> 메뉴를 클릭하여\n"
                "AI 공급자(Gemini, OpenAI, Claude, OpenRouter, Ollama)와 API Key를 설정해 주세요.",
            )

    def _on_open_create_schema_dialog(self) -> None:
        """새 DOCX 서식 분석 다이얼로그를 열고 생성된 스키마를 자동 선택합니다."""
        curr_docx = self.edit_agg_docx_dir.text().strip()
        dlg = DocxSchemaCreateDialog(self, default_docx=curr_docx)
        if dlg.exec():
            self._refresh_schemas_combo()
            created_path = dlg.created_schema_path
            if created_path:
                idx = self.combo_agg_schema.findData(created_path)
                if idx >= 0:
                    self.combo_agg_schema.setCurrentIndex(idx)

    def _on_open_new_prompt_dialog(self) -> None:
        """새 프롬프트 작성 다이얼로그를 열고 생성된 프롬프트를 자동 선택합니다."""
        dlg = PromptEditDialog(self)
        if dlg.exec():
            self._refresh_prompts_combo()
            saved_path = dlg.saved_prompt_path
            if saved_path:
                idx = self.combo_agg_prompt.findData(saved_path)
                if idx >= 0:
                    self.combo_agg_prompt.setCurrentIndex(idx)

    def _on_open_prompt_dir(self) -> None:
        """prompts_memory/ 디렉토리를 윈도우 탐색기에서 엽니다."""
        p = Path("prompts_memory").resolve()
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))

    def _refresh_schemas_combo(self) -> None:
        """schemas/ 폴더 내 JSON 양식 목록을 콤보박스에 로드합니다."""
        prev_data = self.combo_agg_schema.currentData()
        self.combo_agg_schema.clear()
        self.combo_agg_schema.addItem("양식 자동 판별 (탐구계획서/보고서 등 내장 지원)", "")

        schemas_dir = Path("schemas")
        if schemas_dir.exists():
            for json_path in sorted(schemas_dir.glob("*.json")):
                try:
                    name, _ = load_single_schema(json_path)
                    display = f"{name} ({json_path.name})"
                    self.combo_agg_schema.addItem(display, str(json_path.resolve()))
                except Exception:
                    self.combo_agg_schema.addItem(f"{json_path.stem} ({json_path.name})", str(json_path.resolve()))

        if prev_data:
            idx = self.combo_agg_schema.findData(prev_data)
            if idx >= 0:
                self.combo_agg_schema.setCurrentIndex(idx)

    def _refresh_prompts_combo(self) -> None:
        """prompts_memory/ 디렉토리 내 .txt 프롬프트 파일 목록을 콤보박스에 로드합니다."""
        prev_data = self.combo_agg_prompt.currentData()
        self.combo_agg_prompt.clear()
        self.combo_agg_prompt.addItem("기본 내장 과세특 프롬프트 (행동 중심 NEIS 1500B)", "")

        prompt_files = list_prompt_files(Path("prompts_memory"))
        for pf in prompt_files:
            clean_name = re.sub(r"^prompt_", "", pf.stem)
            display = f"{clean_name} ({pf.name})"
            self.combo_agg_prompt.addItem(display, str(pf.resolve()))

        if prev_data:
            idx = self.combo_agg_prompt.findData(prev_data)
            if idx >= 0:
                self.combo_agg_prompt.setCurrentIndex(idx)

    def _on_browse_agg_docx_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "학생 제출 DOCX 과제 폴더 선택")
        if folder:
            norm = str(Path(folder).resolve())
            self.edit_agg_docx_dir.setText(norm)
            self.edit_agg_docx_dir.setToolTip(norm)

    def _on_browse_agg_excel_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "세특/채점 생성 대상 엑셀 파일 선택", "", "Excel 파일 (*.xlsx *.xls);;모든 파일 (*)"
        )
        if file_path:
            norm = str(Path(file_path).resolve())
            self.edit_agg_excel_file.setText(norm)
            self.edit_agg_excel_file.setToolTip(norm)

    def _on_browse_agg_ref_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "참고자료 폴더 선택")
        if folder:
            norm = str(Path(folder).resolve())
            self.edit_agg_ref_dir.setText(norm)
            self.edit_agg_ref_dir.setToolTip(norm)

    def _check_ai_provider_key_or_warn(self) -> bool:
        """현재 활성 AI 공급자의 API 키 등록 여부를 확인하고 없으면 환경설정 이동을 유도합니다."""
        info = get_active_ai_info()
        if not info["has_key"]:
            reply = QMessageBox.warning(
                self,
                f"[{info['provider_display']}] API 키 필요",
                f"현재 선택된 AI 공급자(<b>{info['provider_display']}</b>)의 API Key가 등록되어 있지 않습니다.\n\n"
                "지금 [환경설정] 탭으로 이동하여 API Key를 설정하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._on_go_to_settings_tab()
            return False
        return True

    def _check_gemini_api_key_or_warn(self) -> bool:
        """하위 호환 래퍼"""
        return self._check_ai_provider_key_or_warn()

    def _update_agg_stepper(self, s1: str = "idle", s2: str = "idle", s3: str = "idle") -> None:
        """상단 3-Stage Stepper의 각 단계 상태('idle' | 'active' | 'done')를 시각적으로 갱신합니다."""
        if not hasattr(self, "_agg_stage_boxes") or not hasattr(self, "_agg_stage_chips"):
            return
        states = [s1, s2, s3]
        for idx, state in enumerate(states):
            if idx >= len(self._agg_stage_boxes) or idx >= len(self._agg_stage_chips):
                break
            box = self._agg_stage_boxes[idx]
            chip = self._agg_stage_chips[idx]
            if state == "active":
                box_role = "stageBoxActive"
                chip_role = "stageBadgeActive"
                chip.setText("진행 중")
            elif state == "done":
                box_role = "stageBoxDone"
                chip_role = "stageBadgeDone"
                chip.setText("완료")
            else:
                box_role = "stageBox"
                chip_role = "stageBadge"
                chip.setText("대기")
            box.setProperty("role", box_role)
            chip.setProperty("role", chip_role)
            box.style().unpolish(box)
            box.style().polish(box)
            chip.style().unpolish(chip)
            chip.style().polish(chip)

    def _set_agg_controls_busy(self, busy: bool) -> None:
        """작업 진행 중 버튼 상태 토글"""
        if hasattr(self, "btn_run_docx_only") and self.btn_run_docx_only is not None:
            self.btn_run_docx_only.setEnabled(not busy)
        if hasattr(self, "btn_run_ai_only") and self.btn_run_ai_only is not None:
            self.btn_run_ai_only.setEnabled(not busy)
        if hasattr(self, "btn_run_integrated") and self.btn_run_integrated is not None:
            self.btn_run_integrated.setEnabled(not busy)
        if hasattr(self, "btn_run_merge") and self.btn_run_merge is not None:
            self.btn_run_merge.setEnabled(not busy)
        if hasattr(self, "btn_stop_agg") and self.btn_stop_agg is not None:
            self.btn_stop_agg.setEnabled(busy)

    def _on_run_docx_extract(self) -> None:
        """1단계: DOCX 과제 파일 추출만 단독 실행"""
        folder_str = self.edit_agg_docx_dir.text().strip()
        if not folder_str or not Path(folder_str).is_dir():
            QMessageBox.warning(self, "입력 확인", "학생들의 .docx 파일이 들어있는 폴더를 먼저 선택해 주세요.")
            return

        schema_data = self.combo_agg_schema.currentData()
        schema_path = Path(schema_data) if schema_data else None

        worker = DocxToExcelWorker(docx_dir=folder_str, schema_path=schema_path)
        self._set_agg_controls_busy(True)
        self._update_agg_stepper(s1="active", s2="idle", s3="idle")
        self.lbl_agg_status.setText("DOCX 과제 추출 진행 중...")
        self.btn_open_result_excel.setVisible(False)

        def on_done(res: Dict[str, Any]):
            out_file = res.get("output_file", "")
            self._last_result_excel_path = out_file
            if out_file:
                self.edit_agg_excel_file.setText(out_file)
                self.btn_open_result_excel.setVisible(True)

            tot = res.get("total", 0)
            succ = res.get("success", 0)
            failed = res.get("failed", 0)
            errors = res.get("errors", [])

            self._update_agg_stepper(s1="done", s2="idle", s3="idle")
            self.lbl_agg_status.setText(
                f"추출 완료: {Path(out_file).name} (성공 {succ}명 / 실패 {failed}명)"
            )

            if failed > 0:
                preview_count = min(len(errors), 8)
                fail_summary_lines = [
                    f"• {e.get('파일명', '알수없음')}: {e.get('오류내용', '원인 미상')}"
                    for e in errors[:preview_count]
                ]
                if len(errors) > preview_count:
                    fail_summary_lines.append(f"... 외 {len(errors) - preview_count}건 생략")
                fail_summary_str = "\n".join(fail_summary_lines)

                QMessageBox.warning(
                    self,
                    "DOCX 추출 완료 (일부 실패)",
                    f"학생 과제 추출이 완료되었으나, {failed}건의 파일에서 오류가 발생했습니다.\n\n"
                    f"- 총 파일: {tot}개\n"
                    f"- 성공 추출: {succ}명\n"
                    f"- 오류 건수: {failed}건\n\n"
                    f"[오류 발생 파일 및 원인 (상위 {preview_count}건)]:\n{fail_summary_str}\n\n"
                    f"※ 자세한 원인은 결과 엑셀의 [오류 파일] 시트 및 하단 로그 콘솔에서 확인할 수 있습니다.\n\n"
                    f"생성된 엑셀 경로:\n{out_file}\n\n"
                    f"2단계 대상 엑셀 파일로 자동 등록되었습니다.",
                )
            else:
                QMessageBox.information(
                    self,
                    "DOCX 추출 완료",
                    f"학생 과제 추출이 성공적으로 완료되었습니다.\n\n"
                    f"- 총 파일: {tot}개\n"
                    f"- 성공 추출: {succ}명\n"
                    f"- 오류 건수: 0건\n\n"
                    f"생성된 엑셀 경로:\n{out_file}\n\n"
                    f"2단계 대상 엑셀 파일로 자동 등록되었습니다.",
                )

        self.task_runner.start_worker(
            worker,
            task_key=self.TASK_DOCX_EXTRACT,
            on_result=on_done,
            on_error=lambda err: self._handle_agg_task_error("추출 오류", err),
            on_finished=lambda: self._set_agg_controls_busy(False),
        )

    def _on_run_record_generate(self) -> None:
        """2단계: 엑셀 데이터를 기반으로 AI 과세특 생성만 실행 (멀티 LLM 지원)"""
        if not self._check_ai_provider_key_or_warn():
            return

        excel_str = self.edit_agg_excel_file.text().strip()
        if not excel_str or not Path(excel_str).is_file():
            QMessageBox.warning(self, "입력 확인", "과세특을 생성할 대상 엑셀(.xlsx) 파일을 먼저 선택해 주세요.")
            return

        prompt_data = self.combo_agg_prompt.currentData()
        prompt_file = Path(prompt_data) if prompt_data else None

        ref_str = self.edit_agg_ref_dir.text().strip()
        ref_dir = Path(ref_str) if ref_str and Path(ref_str).is_dir() else None

        max_bytes = self.spin_agg_max_bytes.value()
        info = get_active_ai_info()

        worker = RecordGenerateWorker(
            excel_path=excel_str,
            prompt_file=prompt_file,
            reference_dir=ref_dir,
            max_bytes=max_bytes,
            provider=info["provider"],
            model_name=info["model"],
        )
        self._set_agg_controls_busy(True)
        self._update_agg_stepper(s1="done", s2="active", s3="idle")
        self.lbl_agg_status.setText(
            f"AI 과세특 생성 진행 중... (공급자: {info['provider_display']}, 모델: {info['model']}, 상한: {max_bytes}B)"
        )
        self.btn_open_result_excel.setVisible(False)

        def on_done(res: Dict[str, Any]):
            out_file = res.get("output_path", "")
            self._last_result_excel_path = out_file
            if out_file:
                self.btn_open_result_excel.setVisible(True)
            self._update_agg_stepper(s1="done", s2="done", s3="done")
            self.lbl_agg_status.setText(
                f"과세특 생성 완료: {Path(out_file).name} (완료 {res.get('completed', 0)}/{res.get('total', 0)}명)"
            )
            QMessageBox.information(
                self,
                "AI 과세특 생성 완료",
                f"과세특 생성이 완료되었습니다.\n\n"
                f"- 공급자: {info['provider_display']} ({info['model']})\n"
                f"- 총원: {res.get('total', 0)}명\n"
                f"- 완료 인원: {res.get('completed', 0)}명\n\n"
                f"저장된 파일:\n{out_file}",
            )

        self.task_runner.start_worker(
            worker,
            task_key=self.TASK_RECORD_GEN,
            on_result=on_done,
            on_error=lambda err: self._handle_agg_task_error("과세특 생성 오류", err),
            on_finished=lambda: self._set_agg_controls_busy(False),
        )

    def _on_run_integrated_pipeline(self) -> None:
        """3단계: 과제 폴더에서 최종 세특 엑셀까지 원클릭 일괄 생성 실행 (멀티 LLM 지원)"""
        if not self._check_ai_provider_key_or_warn():
            return

        folder_str = self.edit_agg_docx_dir.text().strip()
        if not folder_str or not Path(folder_str).is_dir():
            QMessageBox.warning(self, "입력 확인", "1단계 학생 과제 폴더(.docx)를 먼저 선택해 주세요.")
            return

        schema_data = self.combo_agg_schema.currentData()
        schema_path = Path(schema_data) if schema_data else None

        prompt_data = self.combo_agg_prompt.currentData()
        prompt_file = Path(prompt_data) if prompt_data else None

        ref_str = self.edit_agg_ref_dir.text().strip()
        ref_dir = Path(ref_str) if ref_str and Path(ref_str).is_dir() else None

        max_bytes = self.spin_agg_max_bytes.value()
        info = get_active_ai_info()

        worker = IntegratedPipelineWorker(
            docx_dir=folder_str,
            schema_path=schema_path,
            prompt_file=prompt_file,
            reference_dir=ref_dir,
            max_bytes=max_bytes,
            provider=info["provider"],
            model_name=info["model"],
        )
        self._set_agg_controls_busy(True)
        self._update_agg_stepper(s1="active", s2="active", s3="active")
        self.lbl_agg_status.setText(
            f"원클릭 파이프라인 진행 중: 1단계 DOCX 추출 -> 2단계 AI 세특 ({info['provider_display']} / {info['model']})"
        )
        self.btn_open_result_excel.setVisible(False)

        def on_done(res: Dict[str, Any]):
            out_file = res.get("output_path", "")
            self._last_result_excel_path = out_file
            if out_file:
                self.edit_agg_excel_file.setText(out_file)
                self.btn_open_result_excel.setVisible(True)
            step1 = res.get("step1", {})
            step2 = res.get("step2", {})

            s1_succ = step1.get("success", 0)
            s1_failed = step1.get("failed", 0)
            s1_errors = step1.get("errors", [])
            s2_comp = step2.get("completed", 0) if step2 else 0

            self._update_agg_stepper(s1="done", s2="done", s3="done")
            self.lbl_agg_status.setText(
                f"전체 일괄 생성 완료! 최종 파일: {Path(out_file).name} (과제 {s1_succ}명, 세특 {s2_comp}명)"
            )

            if s1_failed > 0:
                preview_count = min(len(s1_errors), 8)
                fail_summary_lines = [
                    f"• {e.get('파일명', '알수없음')}: {e.get('오류내용', '원인 미상')}"
                    for e in s1_errors[:preview_count]
                ]
                if len(s1_errors) > preview_count:
                    fail_summary_lines.append(f"... 외 {len(s1_errors) - preview_count}건 생략")
                fail_summary_str = "\n".join(fail_summary_lines)

                QMessageBox.warning(
                    self,
                    "원클릭 일괄 생성 완료 (일부 파일 추출 실패)",
                    f"원클릭 일괄 처리가 완료되었으나, 1단계 과제 추출 중 {s1_failed}건의 파일에서 오류가 발생했습니다.\n\n"
                    f"- 사용된 AI: {info['provider_display']} ({info['model']})\n"
                    f"[1단계 과제 추출]: 성공 {s1_succ}건 / 실패 {s1_failed}건\n"
                    f"[2단계 AI 세특]: 완료 {s2_comp}건\n\n"
                    f"[1단계 추출 실패 파일 및 원인 (상위 {preview_count}건)]:\n{fail_summary_str}\n\n"
                    f"※ 자세한 원인은 결과 엑셀의 [오류 파일] 시트 및 하단 로그 콘솔에서 확인할 수 있습니다.\n\n"
                    f"최종 저장된 파일:\n{out_file}",
                )
            else:
                QMessageBox.information(
                    self,
                    "원클릭 일괄 생성 완료",
                    f"학생 DOCX 과제 취합부터 AI 과세특 생성까지 모두 완료되었습니다!\n\n"
                    f"- 사용된 AI: {info['provider_display']} ({info['model']})\n"
                    f"[1단계 과제 추출]: 성공 {s1_succ}건\n"
                    f"[2단계 AI 세특]: 완료 {s2_comp}건\n\n"
                    f"최종 저장된 파일:\n{out_file}",
                )

        self.task_runner.start_worker(
            worker,
            task_key=self.TASK_INTEGRATED,
            on_result=on_done,
            on_error=lambda err: self._handle_agg_task_error("일괄 처리 오류", err),
            on_finished=lambda: self._set_agg_controls_busy(False),
        )

    def _handle_agg_task_error(self, prefix: str, err: str) -> None:
        """태스크 오류 발생 시 상태 라벨을 빨간색으로 표시하고, 메인 윈도우의 로그 서랍을 엽니다."""
        first_line = (err or "").strip().split("\n")[0]
        self.lbl_agg_status.setText(f"<font color='#ef4444'><b>{prefix}: {first_line}</b></font>")
        main_win = self.window()
        if main_win and hasattr(main_win, "btn_toggle_log"):
            if not main_win.btn_toggle_log.isChecked():
                main_win.btn_toggle_log.setChecked(True)

    def _on_stop_agg_task(self) -> None:
        """진행 중인 과제 추출, 가로 병합 또는 AI 과세특 생성 작업을 안전하게 중단합니다."""
        if hasattr(self, "btn_stop_agg") and self.btn_stop_agg is not None:
            self.btn_stop_agg.setEnabled(False)
        self.task_runner.stop_task(self.TASK_DOCX_EXTRACT)
        self.task_runner.stop_task(self.TASK_COLUMN_MERGE)
        self.task_runner.stop_task(self.TASK_RECORD_GEN)
        self.task_runner.stop_task(self.TASK_INTEGRATED)
        self.lbl_agg_status.setText(
            "<font color='#d97706'><b>작업 중단 요청됨 (현재 학생 저장 후 즉시 안전 종료합니다...)</b></font>"
        )

    # =========================================================================
    # [다중 과제 엑셀 가로 병합] 핸들러
    # =========================================================================
    def _on_merge_add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "가로 병합할 과제 엑셀 파일 선택",
            "",
            "Excel 파일 (*.xlsx *.xls);;모든 파일 (*)",
        )
        if not files:
            return

        existing = set()
        for i in range(self.list_merge_excels.count()):
            existing.add(self.list_merge_excels.item(i).text())

        first_dir = None
        for f in files:
            norm = str(Path(f).resolve())
            if norm not in existing:
                item = QListWidgetItem(norm)
                item.setToolTip(norm)
                self.list_merge_excels.addItem(item)
                existing.add(norm)
                if first_dir is None:
                    first_dir = Path(norm).parent

        # 출력 파일 기본 경로 자동 설정 (첫 파일의 폴더에 생성)
        current_out = self.edit_merge_output.text().strip()
        if first_dir and (not current_out or not ("\\" in current_out or "/" in current_out)):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.edit_merge_output.setText(str(first_dir / f"통합과제_{ts}.xlsx"))

    def _on_merge_del_file(self) -> None:
        row = self.list_merge_excels.currentRow()
        if row >= 0:
            self.list_merge_excels.takeItem(row)

    def _on_merge_move_up(self) -> None:
        row = self.list_merge_excels.currentRow()
        if row > 0:
            item = self.list_merge_excels.takeItem(row)
            self.list_merge_excels.insertItem(row - 1, item)
            self.list_merge_excels.setCurrentRow(row - 1)

    def _on_merge_move_down(self) -> None:
        row = self.list_merge_excels.currentRow()
        if 0 <= row < self.list_merge_excels.count() - 1:
            item = self.list_merge_excels.takeItem(row)
            self.list_merge_excels.insertItem(row + 1, item)
            self.list_merge_excels.setCurrentRow(row + 1)

    def _on_merge_clear_files(self) -> None:
        self.list_merge_excels.clear()

    def _on_browse_merge_output(self) -> None:
        cur = self.edit_merge_output.text().strip()
        init_path = cur if cur else str(Path("통합과제.xlsx").resolve())
        path, _ = QFileDialog.getSaveFileName(
            self,
            "통합 결과 엑셀 저장 위치 선택",
            init_path,
            "Excel 파일 (*.xlsx)",
        )
        if path:
            self.edit_merge_output.setText(str(Path(path).resolve()))

    def _on_run_column_merge(self) -> None:
        count = self.list_merge_excels.count()
        if count < 2:
            QMessageBox.warning(
                self,
                "파일 확인",
                "가로 병합을 위해 최소 2개 이상의 엑셀 파일을 목록에 추가해 주세요.",
            )
            return

        excel_paths = [self.list_merge_excels.item(i).text() for i in range(count)]
        output_str = self.edit_merge_output.text().strip()
        output_path = Path(output_str) if output_str else None

        worker = ExcelColumnMergeWorker(
            excel_paths=excel_paths,
            output_path=output_path,
        )
        self._set_agg_controls_busy(True)
        self._update_agg_stepper(s1="active", s2="idle", s3="idle")
        self.lbl_merge_status.setText(f"{count}개 엑셀 파일 가로 병합 진행 중...")
        self.btn_open_merged_file.setVisible(False)

        def on_done(res: Dict[str, Any]):
            out_file = res.get("output_file", "")
            tot_files = res.get("total_files", 0)
            tot_students = res.get("total_students", 0)
            merged_cols = res.get("merged_columns", 0)
            rev_cnt = res.get("review_count", 0)
            err_cnt = res.get("error_count", 0)

            # 1. 2단계 AI 세특 입력창에 자동 등록
            if out_file:
                self.edit_agg_excel_file.setText(out_file)
                self.btn_open_result_excel.setVisible(True)
                self._last_result_excel_path = out_file
                self.btn_open_merged_file.setVisible(True)

            self._update_agg_stepper(s1="done", s2="idle", s3="idle")
            self.lbl_merge_status.setText(
                f"병합 완료: {Path(out_file).name} (학생 {tot_students}명, 열 {merged_cols}개)"
            )

            # 2. 완료 팝업 알림 (검토/오류 항목 요약 포함)
            base_msg = (
                f"총 {tot_files}개 파일에서 {tot_students}명의 학생이 가로 병합되었습니다.\n\n"
                f"- 통합된 문항 열 개수: {merged_cols}개\n"
                f"- 생성된 엑셀 경로:\n{out_file}\n\n"
                f"하단 [2단계: AI 채점 및 과세특 생성 설정]의 대상 파일로 자동 등록되었습니다."
            )

            if rev_cnt > 0 or err_cnt > 0:
                review_details = []
                for r in res.get("review_records", [])[:5]:
                    review_details.append(f"• [이름불일치] 학번 {r.get('학번')}: {r.get('상세원인')}")
                for e in res.get("error_records", [])[:5]:
                    review_details.append(f"• [학번오류] {e.get('파일명')}(행 {e.get('행번호')}): {e.get('상세원인')}")

                detail_str = "\n".join(review_details)
                extra_cnt = (rev_cnt + err_cnt) - len(review_details)
                if extra_cnt > 0:
                    detail_str += f"\n... 외 {extra_cnt}건 생략"

                warn_msg = (
                    f"총 {tot_files}개 파일에서 {tot_students}명의 학생이 가로 병합되었습니다.\n\n"
                    f"[확인 필요 항목]:\n"
                    f"- 이름 불일치 의심: {rev_cnt}건 (대표 이름으로 자동 병합됨)\n"
                    f"- 학번 누락/오류 제외: {err_cnt}건 (정상 행에서 제외됨)\n\n"
                    f"[주요 확인 대상 (상위 5~10건)]:\n{detail_str}\n\n"
                    f"※ 상세 내역은 생성된 엑셀의 [검토_및_오류] 시트 및 하단 로그 콘솔에서 확인할 수 있습니다.\n\n"
                    f"- 통합된 문항 열 개수: {merged_cols}개\n"
                    f"- 생성된 엑셀 경로:\n{out_file}\n\n"
                    f"하단 [2단계: AI 채점 및 과세특 생성 설정]의 대상 파일로 자동 등록되었습니다."
                )
                QMessageBox.warning(self, "다중 과제 가로 병합 완료 (검토 항목 있음)", warn_msg)
            else:
                QMessageBox.information(self, "다중 과제 가로 병합 완료", base_msg)

        self.task_runner.start_worker(
            worker,
            task_key=self.TASK_COLUMN_MERGE,
            on_result=on_done,
            on_error=lambda err: self._handle_agg_task_error("병합 오류", err),
            on_finished=lambda: self._set_agg_controls_busy(False),
        )

    def _on_open_last_result_excel(self) -> None:
        if self._last_result_excel_path and Path(self._last_result_excel_path).exists():
            os.startfile(self._last_result_excel_path)
        else:
            QMessageBox.information(self, "안내", "열 수 있는 결과 엑셀 파일이 없습니다.")

    # =========================================================================
    # AI 실행 도크 연동 (공통 컨텍스트 · 디바운스 · 카드 강조 · 결과 반영)
    # =========================================================================
    def _wire_ai_context_sync(self) -> None:
        """중앙 입력창 변경(300ms 디바운스) 및 AI 대상 카드 클릭 이벤트를 연결합니다."""
        self.card_agg_step2.get_ai_context = lambda: self.get_ai_context("agg_step2")  # type: ignore[attr-defined]
        self.card_agg_step3.get_ai_context = lambda: self.get_ai_context("agg_step2")  # type: ignore[attr-defined]
        self.card_ocr_main.get_ai_context = lambda: self.get_ai_context("ocr")  # type: ignore[attr-defined]

        self._install_target_click_filter(self.card_agg_step2, "agg_step2")
        self._install_target_click_filter(self.card_agg_step3, "agg_step2")
        self._install_target_click_filter(self.card_ocr_main, "ocr")

        # 300ms 디바운스 시그널 연결
        self.edit_agg_excel_file.textChanged.connect(lambda _t: self._schedule_context_debounce())
        self.combo_agg_prompt.currentIndexChanged.connect(lambda _i: self._schedule_context_debounce())
        self.edit_agg_ref_dir.textChanged.connect(lambda _t: self._schedule_context_debounce())
        self.spin_agg_max_bytes.valueChanged.connect(lambda _v: self._schedule_context_debounce())
        self.edit_agg_docx_dir.textChanged.connect(lambda _t: self._schedule_context_debounce())
        self.combo_agg_schema.currentIndexChanged.connect(lambda _i: self._schedule_context_debounce())

        self.edit_ocr_prompt.textChanged.connect(lambda _t: self._schedule_context_debounce())
        self.txt_ocr_output.textChanged.connect(self._schedule_context_debounce)
        ocr_model = self.list_ocr_files.model()
        if ocr_model is not None:
            ocr_model.rowsInserted.connect(lambda *_a: self._schedule_context_debounce())
            ocr_model.rowsRemoved.connect(lambda *_a: self._schedule_context_debounce())
            ocr_model.modelReset.connect(self._schedule_context_debounce)

    def _install_target_click_filter(self, root_widget: QWidget, target_id: str) -> None:
        root_widget.setProperty("_ai_target_id", target_id)
        root_widget.installEventFilter(self)
        for child in root_widget.findChildren(QWidget):
            child.setProperty("_ai_target_id", target_id)
            child.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is getattr(self, "btn_agg_mode_badge", None):
            ev_type = event.type()
            if ev_type in (QEvent.Type.Enter, QEvent.Type.HoverEnter):
                if not getattr(self, "_mode_badge_hovered", False):
                    self._mode_badge_hovered = True
                    self._update_agg_mode_badge_text()
            elif ev_type in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
                if getattr(self, "_mode_badge_hovered", False):
                    self._mode_badge_hovered = False
                    self._update_agg_mode_badge_text()
        if event.type() == QEvent.Type.MouseButtonPress:
            tid = obj.property("_ai_target_id") if obj is not None else None
            if isinstance(tid, str) and tid:
                self.ai_target_requested.emit(tid)
        return super().eventFilter(obj, event)

    def _is_agg_grading_mode(self) -> bool:
        """현재 선택된 프롬프트 파일명·경로 또는 표시명에 '채점'이 포함되어 있는지 판별합니다."""
        prompt_data = str(self.combo_agg_prompt.currentData() or "").strip()
        prompt_text = str(self.combo_agg_prompt.currentText() or "").strip()
        prompt_name = Path(prompt_data).name if prompt_data else ""
        return ("채점" in prompt_name) or ("채점" in prompt_data) or ("채점" in prompt_text)

    def _update_agg_mode_badge_text(self) -> None:
        base = "채점 모드 활성" if self._is_agg_grading_mode() else "세특 작성 모드 활성"
        if getattr(self, "_mode_badge_hovered", False):
            action_txt = "설명 접기 ▴" if self.btn_agg_mode_badge.isChecked() else "설명 보기 ▾"
            self.btn_agg_mode_badge.setText(f"{base} · {action_txt}")
        else:
            self.btn_agg_mode_badge.setText(base)

    def _update_agg_mode_badge_and_accordion(self) -> None:
        is_grading = self._is_agg_grading_mode()
        self.btn_agg_mode_badge.setProperty("aggMode", "grading" if is_grading else "record")
        st = self.btn_agg_mode_badge.style()
        if st is not None:
            st.unpolish(self.btn_agg_mode_badge)
            st.polish(self.btn_agg_mode_badge)
        self._update_agg_mode_badge_text()

        prompt_data = str(self.combo_agg_prompt.currentData() or "").strip()
        prompt_label = Path(prompt_data).name if prompt_data else (
            self.combo_agg_prompt.currentText().strip() or "기본 내장 과세특 프롬프트"
        )
        mode_name = "채점 모드" if is_grading else "세특 작성 모드"
        excel_str = self.edit_agg_excel_file.text().strip()
        out_name = resolve_batch_output_path(
            excel_str or "자유 주제 탐구 종합.xlsx",
            None,
            is_grading=is_grading,
        ).name
        prefix_note = " (예시)" if not excel_str else ""

        self.lbl_mode_explain_summary.setText(
            f"<b>현재 선택 요약</b> · 프롬프트: <code>{html.escape(prompt_label)}</code> "
            f"→ <b>{mode_name}</b> 활성 · 예상 결과 파일명{prefix_note}: "
            f"<code>{html.escape(out_name)}</code>"
        )

        if self.btn_agg_mode_badge.isChecked() and self._mode_explain_anim.state() != QPropertyAnimation.State.Running:
            self.mode_explain_accordion.setMaximumHeight(16777215)

    def _measure_mode_explain_target_height(self) -> int:
        layout = self.mode_explain_accordion.layout()
        avail_w = self.mode_explain_accordion.width()
        if avail_w <= 80:
            avail_w = max(self.card_agg_step2.width() - 36, 460)
        if layout is not None:
            layout.activate()
            if layout.hasHeightForWidth() and avail_w > 0:
                return max(
                    layout.totalHeightForWidth(avail_w) + 4,
                    self.mode_explain_accordion.sizeHint().height(),
                    110,
                )
        return max(self.mode_explain_accordion.sizeHint().height() + 4, 110)

    def _on_toggle_mode_explain_accordion(self, checked: bool) -> None:
        self._update_agg_mode_badge_text()
        current_h = self.mode_explain_accordion.height() if self.mode_explain_accordion.isVisible() else 0
        self._mode_explain_anim.stop()
        if checked:
            self.mode_explain_accordion.setMaximumHeight(current_h)
            self.mode_explain_accordion.setVisible(True)
            target_h = self._measure_mode_explain_target_height()
            self._mode_explain_anim.setStartValue(current_h)
            self._mode_explain_anim.setEndValue(target_h)
        else:
            start_h = max(current_h, 0)
            self.mode_explain_accordion.setMaximumHeight(start_h)
            self._mode_explain_anim.setStartValue(start_h)
            self._mode_explain_anim.setEndValue(0)
        self._mode_explain_anim.start()

    def _on_mode_explain_anim_finished(self) -> None:
        if self.btn_agg_mode_badge.isChecked():
            self.mode_explain_accordion.setMaximumHeight(16777215)
        else:
            self.mode_explain_accordion.setMaximumHeight(0)
            self.mode_explain_accordion.setVisible(False)

    def _schedule_context_debounce(self) -> None:
        self._context_debounce_timer.start()

    def _on_debounced_context_sync(self) -> None:
        self._refresh_agg_student_preview_table()
        self.ai_context_changed.emit(self.get_ai_context())

    def get_ai_context(self, target_id: Optional[str] = None) -> Dict[str, Any]:
        """현재 선택(또는 지정)된 중앙 AI 대상 카드의 최신 설정·입력값을 딕셔너리로 반환합니다."""
        tid = target_id or self._active_ai_target_id or "agg_step2"
        if tid == "ocr":
            files: List[str] = []
            for i in range(self.list_ocr_files.count()):
                item = self.list_ocr_files.item(i)
                if item is None:
                    continue
                val = item.data(Qt.ItemDataRole.UserRole) or item.text()
                if val:
                    files.append(str(val))
            return {
                "target_id": "ocr",
                "label": "2. AI OCR 및 텍스트 보정",
                "kind": "ocr",
                "ocr_files": files,
                "ocr_prompt": self.edit_ocr_prompt.text().strip(),
                "ocr_current_text": self.txt_ocr_output.toPlainText().strip(),
            }

        prompt_data = self.combo_agg_prompt.currentData()
        schema_data = self.combo_agg_schema.currentData()
        return {
            "target_id": "agg_step2",
            "label": "과제 파이프라인 > 2단계 세특 생성",
            "kind": "excel_batch",
            "excel_path": self.edit_agg_excel_file.text().strip(),
            "docx_dir": self.edit_agg_docx_dir.text().strip(),
            "schema_path": str(schema_data) if schema_data else "",
            "prompt_file": str(prompt_data) if prompt_data else "",
            "reference_dir": self.edit_agg_ref_dir.text().strip(),
            "max_bytes": int(self.spin_agg_max_bytes.value()),
            "last_result_excel_path": self._last_result_excel_path or "",
        }

    def set_active_ai_target(self, target_id: str) -> None:
        """활성 AI 타겟에 따라 중앙 카드 강조(1.5px solid #2563EB 및 선택 배지)를 갱신합니다."""
        self._active_ai_target_id = target_id or "agg_step2"
        is_agg = self._active_ai_target_id == "agg_step2"
        is_ocr = self._active_ai_target_id == "ocr"

        card_states = [
            (self.card_agg_step2, is_agg),
            (self.card_agg_step3, is_agg),
            (self.card_ocr_main, is_ocr),
        ]
        for card, selected in card_states:
            card.setProperty("aiTargetSelected", "true" if selected else "false")
            st = card.style()
            if st is not None:
                st.unpolish(card)
                st.polish(card)
            card.update()

        self.lbl_target_badge_step2.setVisible(is_agg)
        self.lbl_target_badge_ocr.setVisible(is_ocr)

    def _refresh_agg_student_preview_table(self, preferred_path: str = "") -> None:
        """선택된 과제/결과 엑셀을 읽어 3단계 카드의 학생별 처리 상태 미리보기를 갱신합니다."""
        prompt_data = self.combo_agg_prompt.currentData()
        is_grading = bool(prompt_data and "채점" in Path(str(prompt_data)).name)
        excel_str = self.edit_agg_excel_file.text().strip()

        target_path = ""
        if preferred_path and Path(preferred_path).is_file():
            target_path = preferred_path
        elif excel_str and Path(excel_str).is_file():
            cand = resolve_batch_output_path(excel_str, None, is_grading=is_grading)
            if cand and Path(cand).is_file():
                target_path = cand
            else:
                target_path = excel_str

        if not target_path:
            self.table_agg_preview.setRowCount(0)
            self.lbl_agg_preview_summary.setText(
                "엑셀 파일을 선택하거나 1단계를 실행하면 학생별 처리 상태와 생성된 세특 미리보기가 표시됩니다."
            )
            return

        try:
            df = pd.read_excel(target_path, dtype=str).fillna("")
        except Exception as exc:
            self.table_agg_preview.setRowCount(0)
            self.lbl_agg_preview_summary.setText(f"미리보기 로드 오류: {exc}")
            return

        cols = [str(c) for c in df.columns]
        sid_col = next((c for c in cols if "학번" in c), cols[0] if cols else "")
        name_col = next((c for c in cols if "이름" in c or "성명" in c), "")

        result_candidates = (
            ["AI_채점결과", "AI_서술형평가", "피드백", "평가결과", "생성된_과세특"]
            if is_grading
            else ["생성된_과세특", "AI_교과세특", "과세특", "생성_세특", "AI_채점결과"]
        )
        res_col = ""
        for cand_col in result_candidates:
            if cand_col in cols:
                res_col = cand_col
                break
        if not res_col:
            res_col = next(
                (
                    c
                    for c in cols
                    if ("바이트" not in c and "Byte" not in c)
                    and ("세특" in c or "채점" in c or "피드백" in c or "평가" in c or c.startswith("AI_"))
                ),
                "",
            )

        meta_skip = {sid_col, name_col, res_col, "누락 항목", "누락항목", "양식", "원본파일명", "과세특_바이트"}
        q_cols = [
            c
            for c in cols
            if c not in meta_skip
            and not c.startswith("AI_")
            and "오류" not in c
            and "바이트" not in c
            and "Byte" not in c
            and "처리상태" not in c
        ]

        total_rows = len(df)
        preview_rows = df.head(200)
        self.table_agg_preview.setRowCount(len(preview_rows))
        completed_cnt = 0

        for r_idx, (_, row) in enumerate(df.iterrows()):
            raw_res = str(row.get(res_col, "")).strip() if res_col else ""
            is_done = bool(res_col and not is_blank_or_failed(raw_res))
            if is_done:
                completed_cnt += 1

        for r_idx in range(len(preview_rows)):
            row = preview_rows.iloc[r_idx]
            sid_val = str(row.get(sid_col, "")).strip() if sid_col else ""
            name_val = str(row.get(name_col, "")).strip() if name_col else ""
            answered = sum(1 for qc in q_cols if str(row.get(qc, "")).strip())
            raw_res = str(row.get(res_col, "")).strip() if res_col else ""
            is_done = bool(res_col and not is_blank_or_failed(raw_res))
            status_txt = "완료" if is_done else ("오류/재시도" if raw_res else "대기")
            snippet = (raw_res[:90] + "...") if len(raw_res) > 90 else (raw_res or "-")

            items = [
                QTableWidgetItem(sid_val),
                QTableWidgetItem(name_val),
                QTableWidgetItem(f"{answered}/{len(q_cols)}" if q_cols else "-"),
                QTableWidgetItem(status_txt),
                QTableWidgetItem(snippet),
            ]
            items[3].setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_done:
                items[3].setForeground(QBrush(QColor("#15803D")))
            elif raw_res:
                items[3].setForeground(QBrush(QColor("#B91C1C")))
            else:
                items[3].setForeground(QBrush(QColor("#64748B")))
            if raw_res:
                items[4].setToolTip(raw_res)

            for c_idx, cell_item in enumerate(items):
                self.table_agg_preview.setItem(r_idx, c_idx, cell_item)

        suffix = " (상위 200명 표시)" if total_rows > 200 else ""
        self.lbl_agg_preview_summary.setText(
            f"대상 파일: {Path(target_path).name} · 완료 {completed_cnt}/{total_rows}명 "
            f"(미완료 {max(0, total_rows - completed_cnt)}명){suffix}"
        )

    def apply_ai_dock_result(self, payload: Dict[str, Any]) -> None:
        """우측 AiExecutionDock에서 실행된 결과를 중앙 탭 UI 및 미리보기 테이블에 반영합니다."""
        tid = str(payload.get("target_id", "") or self._active_ai_target_id)
        if tid == "ocr":
            formatted = str(payload.get("formatted_text", "") or "").strip()
            if formatted:
                self.txt_ocr_output.blockSignals(True)
                self.txt_ocr_output.setPlainText(formatted)
                self.txt_ocr_output.blockSignals(False)
            return

        out_path = str(payload.get("output_path", "") or "").strip()
        if out_path and Path(out_path).exists():
            self._last_result_excel_path = out_path
            self.btn_open_result_excel.setVisible(True)
            if payload.get("integrated"):
                self.edit_agg_excel_file.blockSignals(True)
                self.edit_agg_excel_file.setText(out_path)
                self.edit_agg_excel_file.blockSignals(False)

        mode = str(payload.get("mode", "") or "")
        applied = payload.get("applied_count", payload.get("completed", 0))
        if mode == "manual":
            self.lbl_agg_status.setText(
                f"수동 배치 반영 완료: {Path(out_path).name if out_path else ''} ({applied}명 반영)"
            )
        elif mode == "playwright":
            self.lbl_agg_status.setText(
                f"Playwright 자동화 반영 완료: {Path(out_path).name if out_path else ''} ({applied}명 반영)"
            )
        else:
            tot = payload.get("total", applied)
            self.lbl_agg_status.setText(
                f"API 일괄 생성 완료: {Path(out_path).name if out_path else ''} ({applied}/{tot}명)"
            )

        self._update_agg_stepper(s1="done", s2="done", s3="done")
        self._refresh_agg_student_preview_table(preferred_path=out_path)
