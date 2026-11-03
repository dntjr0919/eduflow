"""
[탭 2] 📄 문서 처리 & AI OCR / 파일 병합 (ui/tabs/tab_doc_ocr.py)
- 1순위 핵심 구현: [학번/이름 기준 제출 파일 분석 · 미제출자 대조 · 표준 파일명 일괄 변경 · 학번순 단일 PDF 병합]
- 2순위 구현: [2. AI OCR 및 문서 텍스트 보정] 서브탭 하단 실행 영역에 QHBoxLayout으로
  1) [🤖 AI 자동 실행 (API 사용)] 버튼 (기존 워커 연결)
  2) [📋 수동 복붙 모드 열기] 버튼 (클릭 시 ManualLlmDialog 팝업 연결)
  을 나란히 배치
- 모든 위젯에 setObjectName / setProperty("role", ...) 및 style().polish() 강제 적용
"""

import csv
import mimetypes
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
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
from services.llm_client import (
    MultiLlmClient,
    build_structured_prompt,
    extract_json_from_llm_response,
    format_parsed_json_to_readable_text,
)
from services.notifier import NotificationService
from ui.dialogs.manual_llm_dialog import ManualLlmDialog

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
    if obj_name:
        widget.setObjectName(obj_name)
    if role:
        widget.setProperty("role", role)
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


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

            match_status = "❓ 미등록/확인필요"
            matched_student: Optional[Dict[str, Any]] = None

            if extracted_id and extracted_id in roster_by_id:
                matched_student = roster_by_id[extracted_id]
                official_name = matched_student["name"]
                if extracted_name == official_name:
                    match_status = "✅ 명렬표 일치"
                else:
                    extracted_name = official_name
                    match_status = "✅ 학번 매칭(이름보완)"
            elif extracted_name and extracted_name in roster_by_name:
                candidates = roster_by_name[extracted_name]
                if len(candidates) == 1:
                    matched_student = candidates[0]
                    extracted_id = str(matched_student["student_id"])
                    match_status = "✅ 이름 매칭(학번보완)"
                else:
                    match_status = "⚠️ 동명이인(학번확인)"
            elif extracted_id or extracted_name:
                match_status = "⚠️ 명렬표 외 추출"

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


class ReportExcelAggregatorWorker(BaseWorker):
    def __init__(self, file_paths: List[str], output_xlsx: str, skip_header_rows: int = 1):
        super().__init__(task_name="다중 보고서 엑셀 취합")
        self.file_paths = file_paths
        self.output_xlsx = Path(output_xlsx)
        self.skip_header_rows = max(0, int(skip_header_rows))

    def execute(self) -> Dict[str, Any]:
        import openpyxl

        wb_out = openpyxl.Workbook()
        ws_out = wb_out.active
        ws_out.title = "취합요약본"

        total = len(self.file_paths)
        total_rows_added = 0
        header_written = False

        self.emit_log("INFO", f"총 {total}개의 보고서 파일을 단일 엑셀로 취합합니다.")

        for idx, fp in enumerate(self.file_paths, start=1):
            if self.is_stop_requested():
                break
            p = Path(fp)
            pct = int((idx / max(1, total)) * 100)
            self.emit_progress(pct, f"보고서 취합 중 ({idx}/{total}): {p.name}")

            if not p.exists():
                continue

            try:
                rows: List[List[Any]] = []
                if p.suffix.lower() == ".xlsx":
                    wb_in = openpyxl.load_workbook(str(p), data_only=True)
                    ws_in = wb_in.active
                    for r in ws_in.iter_rows(values_only=True):
                        if any(c is not None for c in r):
                            rows.append(list(r))
                elif p.suffix.lower() == ".csv":
                    for enc in ("utf-8-sig", "cp949", "utf-8"):
                        try:
                            with open(p, "r", encoding=enc, newline="") as f:
                                rows = [row for row in csv.reader(f) if any(row)]
                            break
                        except UnicodeDecodeError:
                            continue

                if not rows:
                    continue

                if not header_written and self.skip_header_rows > 0:
                    ws_out.append(["원본파일명"] + list(rows[0]))
                    header_written = True

                data_rows = rows[self.skip_header_rows :] if self.skip_header_rows <= len(rows) else []
                for dr in data_rows:
                    ws_out.append([p.name] + list(dr))
                    total_rows_added += 1

                self.emit_log("SUCCESS", f"취합 완료: {p.name} ({len(data_rows)}행 추가)")
            except Exception as exc:
                self.emit_log("ERROR", f"보고서 읽기 실패 ({p.name}): {exc}")

        self.output_xlsx.parent.mkdir(parents=True, exist_ok=True)
        wb_out.save(str(self.output_xlsx))
        self.emit_progress(100, f"엑셀 취합 완료: 총 {total_rows_added}행 저장됨")
        return {"output_xlsx": str(self.output_xlsx.resolve()), "rows_added": total_rows_added}


# =============================================================================
# 2. [탭 2] 메인 UI 위젯 클래스 (DocOcrTab)
# =============================================================================
class DocOcrTab(QWidget):
    """[탭 2] 문서 처리 & AI OCR / 파일 병합 메인 탭 위젯."""

    TASK_ANALYZE = "doc_tab_analyze_submissions"
    TASK_RENAME = "doc_tab_standard_rename"
    TASK_PDF_MERGE = "doc_tab_pdf_merge"
    TASK_OCR = "doc_tab_ai_ocr"
    TASK_EXCEL_AGG = "doc_tab_excel_agg"

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="DocOcrTabPage")
        self.task_runner = task_runner
        self.config = ConfigManager()
        self.db = DatabaseManager()
        self.notifier = NotificationService()

        self._analyzed_items: List[Dict[str, Any]] = []
        self._missing_students: List[Dict[str, Any]] = []

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.sub_tabs = QTabWidget()
        apply_qss_role(self.sub_tabs, obj_name="DocOcrSubTabWidget")
        self.sub_tabs.addTab(
            self._build_student_merge_subtab(),
            "🗂️ 1. 학번/이름 기준 파일 취합 · 미제출자 대조 · PDF 병합",
        )
        self.sub_tabs.addTab(
            self._build_ocr_subtab(),
            "🔍 2. AI OCR 및 문서 텍스트 보정",
        )
        self.sub_tabs.addTab(
            self._build_excel_aggregator_subtab(),
            "📊 3. 다중 보고서 엑셀(.xlsx) 취합기",
        )
        layout.addWidget(self.sub_tabs)

    # =========================================================================
    # [서브탭 1] 학번/이름 기준 파일 취합 · 미제출자 확인 · 표준 이름 변경 · PDF 병합
    # =========================================================================
    def _build_student_merge_subtab(self) -> QWidget:
        page = QWidget()
        apply_qss_role(page, obj_name="SubTabStudentMerge")
        main_lay = QVBoxLayout(page)
        main_lay.setContentsMargins(15, 15, 15, 15)
        main_lay.setSpacing(10)

        cfg_group = QGroupBox("📂 1단계: 학생 제출물 폴더 선택 및 학번/이름 정규표현식(Regex) 설정")
        apply_qss_role(cfg_group, obj_name="SubmissionRegexGroup")
        cfg_lay = QVBoxLayout(cfg_group)
        cfg_lay.setContentsMargins(15, 15, 15, 15)
        cfg_lay.setSpacing(10)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("<b>제출물 폴더:</b>"))
        self.edit_submission_dir = QLineEdit()
        self.edit_submission_dir.setPlaceholderText(
            "학생들이 제출한 파일(PDF, HWP, DOCX, XLSX 등)이 들어있는 폴더를 선택하세요..."
        )
        btn_browse_sub = QPushButton("폴더 찾아보기...")
        apply_qss_role(btn_browse_sub)
        btn_browse_sub.clicked.connect(self._on_browse_submission_dir)
        btn_open_sub = QPushButton("폴더 열기")
        apply_qss_role(btn_open_sub)
        btn_open_sub.clicked.connect(self._on_open_submission_dir)

        self.chk_recursive = QCheckBox("하위 폴더까지 포함 검색")
        self.combo_ext_filter = QComboBox()
        self.combo_ext_filter.addItem("모든 제출 문서 (PDF/HWP/Office/이미지)", "all")
        self.combo_ext_filter.addItem("PDF 파일만 (.pdf)", "pdf")
        self.combo_ext_filter.addItem("한글 파일만 (.hwp, .hwpx)", "hwp")
        self.combo_ext_filter.addItem("워드/엑셀 파일만 (.docx, .xlsx 등)", "word_excel")

        folder_row.addWidget(self.edit_submission_dir, stretch=1)
        folder_row.addWidget(btn_browse_sub)
        folder_row.addWidget(btn_open_sub)
        folder_row.addSpacing(8)
        folder_row.addWidget(self.combo_ext_filter)
        folder_row.addWidget(self.chk_recursive)
        cfg_lay.addLayout(folder_row)

        regex_row = QHBoxLayout()
        regex_row.addWidget(QLabel("학번 정규식(4~5자리):"))
        self.edit_id_regex = QLineEdit(r"(?<!\d)(\d{4,5})(?!\d)")
        regex_row.addWidget(QLabel("이름 정규식:"))
        self.edit_name_regex = QLineEdit(r"([가-힣]{2,4})")
        regex_row.addWidget(QLabel("표준 파일명 과제명:"))
        self.edit_assignment_title = QLineEdit("수행평가과제")
        self.edit_assignment_title.textChanged.connect(self._on_assignment_title_changed)

        self.btn_scan_analyze = QPushButton("🔍 폴더 스캔 & 명렬표 미제출자 대조 실행")
        apply_qss_role(self.btn_scan_analyze, role="primary", obj_name="BtnScanAnalyze")
        self.btn_scan_analyze.setMinimumHeight(38)
        self.btn_scan_analyze.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_scan_analyze.clicked.connect(self.run_submission_analysis)

        regex_row.addWidget(self.edit_id_regex, stretch=1)
        regex_row.addWidget(self.edit_name_regex, stretch=1)
        regex_row.addWidget(self.edit_assignment_title, stretch=1)
        regex_row.addWidget(self.btn_scan_analyze)
        cfg_lay.addLayout(regex_row)

        main_lay.addWidget(cfg_group)

        summary_bar = QHBoxLayout()
        self.lbl_summary_badge = QLabel(
            "💡 제출물 폴더를 선택하고 [폴더 스캔 & 명렬표 미제출자 대조 실행]을 클릭하세요."
        )
        apply_qss_role(self.lbl_summary_badge, role="infoBanner", obj_name="SubmissionSummaryBanner")
        summary_bar.addWidget(self.lbl_summary_badge, stretch=1)
        main_lay.addLayout(summary_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_group = QGroupBox("📋 2단계: 제출 파일 학번순 정렬 및 표준 파일명 미리보기 (더블클릭하여 학번/이름 직접 수정 가능)")
        apply_qss_role(left_group, obj_name="SubmittedFilesGroup")
        left_lay = QVBoxLayout(left_group)
        left_lay.setContentsMargins(15, 15, 15, 15)

        left_top_bar = QHBoxLayout()
        btn_check_all = QPushButton("전체 선택")
        apply_qss_role(btn_check_all)
        btn_uncheck_all = QPushButton("전체 해제")
        apply_qss_role(btn_uncheck_all)
        btn_check_all.clicked.connect(lambda: self._set_all_files_checked(True))
        btn_uncheck_all.clicked.connect(lambda: self._set_all_files_checked(False))
        left_top_bar.addWidget(btn_check_all)
        left_top_bar.addWidget(btn_uncheck_all)
        left_top_bar.addStretch()
        left_lay.addLayout(left_top_bar)

        self.table_submissions = QTableWidget(0, 7)
        self.table_submissions.setAlternatingRowColors(True)
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
        sub_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        sub_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        sub_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        sub_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        sub_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        sub_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        sub_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)

        left_lay.addWidget(self.table_submissions)
        splitter.addWidget(left_group)

        right_group = QGroupBox("🚨 미제출자 명단 (환경설정 학생 명렬표 DB 자동 대조)")
        apply_qss_role(right_group, obj_name="MissingStudentsGroup")
        right_lay = QVBoxLayout(right_group)
        right_lay.setContentsMargins(15, 15, 15, 15)

        missing_btn_bar = QHBoxLayout()
        btn_copy_missing = QPushButton("📋 미제출자 명단 복사")
        apply_qss_role(btn_copy_missing)
        btn_export_missing = QPushButton("📤 제출/미제출 현황 엑셀(.xlsx) 저장")
        apply_qss_role(btn_export_missing)

        btn_copy_missing.clicked.connect(self._on_copy_missing_to_clipboard)
        btn_export_missing.clicked.connect(self._on_export_submission_report_xlsx)

        missing_btn_bar.addWidget(btn_copy_missing)
        missing_btn_bar.addWidget(btn_export_missing)
        missing_btn_bar.addStretch()
        right_lay.addLayout(missing_btn_bar)

        self.table_missing = QTableWidget(0, 5)
        self.table_missing.setAlternatingRowColors(True)
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

        right_lay.addWidget(self.table_missing)
        splitter.addWidget(right_group)

        splitter.setSizes([720, 440])
        main_lay.addWidget(splitter, stretch=1)

        action_group = QGroupBox("🚀 3단계: 학번순 파일명 일괄 표준화 & 단일 PDF 병합 실행 (비동기 백그라운드 처리)")
        apply_qss_role(action_group, obj_name="BatchMergeActionGroup")
        act_lay = QHBoxLayout(action_group)
        act_lay.setContentsMargins(15, 15, 15, 15)

        self.combo_rename_mode = QComboBox()
        self.combo_rename_mode.addItem("안전 모드: 하위 '_정리완료' 폴더에 표준 파일명으로 복사 생성 (원본 보존)", "copy")
        self.combo_rename_mode.addItem("직접 변경: 원본 폴더 내에서 파일명을 즉시 표준 형식으로 변경", "move")

        self.btn_execute_rename = QPushButton("✏️ 학번순 표준 파일명([학번]_[이름]_과제명) 일괄 적용")
        apply_qss_role(self.btn_execute_rename, role="teal", obj_name="BtnExecuteStandardRename")
        self.btn_execute_rename.setMinimumHeight(40)
        self.btn_execute_rename.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_execute_rename.clicked.connect(self.run_standard_rename)

        self.chk_pdf_bookmark = QCheckBox("PDF 병합 시 학생별 책갈피(목차) 자동 삽입")
        self.chk_pdf_bookmark.setChecked(True)

        self.btn_execute_pdf_merge = QPushButton("📑 학번 순서대로 단일 PDF 병합 저장 (pypdf)")
        apply_qss_role(self.btn_execute_pdf_merge, role="danger", obj_name="BtnExecutePdfMerge")
        self.btn_execute_pdf_merge.setMinimumHeight(40)
        self.btn_execute_pdf_merge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_execute_pdf_merge.clicked.connect(self.run_pdf_merge)

        act_lay.addWidget(self.combo_rename_mode, stretch=1)
        act_lay.addWidget(self.btn_execute_rename)
        act_lay.addSpacing(12)
        act_lay.addWidget(self.chk_pdf_bookmark)
        act_lay.addWidget(self.btn_execute_pdf_merge)

        main_lay.addWidget(action_group)
        return page

    # =========================================================================
    # [서브탭 2] 2. AI OCR 및 문서 텍스트 보정 (하단 QHBoxLayout 2버튼 나란히 배치)
    # =========================================================================
    def _build_ocr_subtab(self) -> QWidget:
        w = QWidget()
        apply_qss_role(w, obj_name="SubTabAiOcr")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(12)

        grp = QGroupBox("🤖 2. AI OCR 및 문서 텍스트 보정 (멀티 LLM API & 수동 프롬프트 복붙 하이브리드)")
        apply_qss_role(grp, obj_name="AiOcrHybridGroup")
        g_lay = QVBoxLayout(grp)
        g_lay.setContentsMargins(15, 15, 15, 15)
        g_lay.setSpacing(12)

        # 상단 파일 추가 및 결과 저장 툴바
        file_toolbar = QHBoxLayout()
        btn_add = QPushButton("+ 이미지/PDF/문서 파일 추가")
        apply_qss_role(btn_add, obj_name="BtnAddOcrFiles")
        btn_clear = QPushButton("목록 비우기")
        apply_qss_role(btn_clear, obj_name="BtnClearOcrFiles")
        btn_save_txt = QPushButton("💾 결과 마크다운(.md) 저장")
        apply_qss_role(btn_save_txt, obj_name="BtnSaveOcrMarkdown")

        self.list_ocr_files = QListWidget()
        self.list_ocr_files.setMaximumHeight(110)

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
        self.edit_ocr_prompt = QLineEdit(
            "스캔/추출된 문서의 오탈자를 문맥에 맞게 교정하고, 핵심 3줄 요약과 후속 조치 사항을 정리해 주세요."
        )
        form.addRow("AI 보정/요약 지침:", self.edit_ocr_prompt)
        g_lay.addLayout(form)

        # 결과 출력창
        self.txt_ocr_output = QTextEdit()
        apply_qss_role(self.txt_ocr_output, obj_name="WebAiInputBox")
        self.txt_ocr_output.setPlaceholderText(
            "AI API 자동 처리 결과 또는 [📋 수동 복붙 모드 열기]에서 파싱된 JSON 변환 결과가 이곳에 표시됩니다..."
        )
        g_lay.addWidget(self.txt_ocr_output, stretch=1)

        # 하단 실행 버튼 영역: QHBoxLayout으로 두 개의 버튼을 나란히 배치
        exec_btn_layout = QHBoxLayout()
        exec_btn_layout.setSpacing(12)

        # 1) [🤖 AI 자동 실행 (API 사용)] 버튼 (기존 워커 연결)
        self.btn_run_ai_auto = QPushButton("🤖 AI 자동 실행 (API 사용)")
        apply_qss_role(self.btn_run_ai_auto, role="primary", obj_name="BtnRunAiAuto")
        self.btn_run_ai_auto.setMinimumHeight(42)
        self.btn_run_ai_auto.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run_ai_auto.clicked.connect(self._on_run_hybrid_ocr)

        # 2) [📋 수동 복붙 모드 열기] 버튼 (클릭 시 ManualLlmDialog 팝업 연결)
        self.btn_open_manual_mode = QPushButton("📋 수동 복붙 모드 열기")
        apply_qss_role(self.btn_open_manual_mode, role="teal", obj_name="BtnOpenManualPrompt")
        self.btn_open_manual_mode.setMinimumHeight(42)
        self.btn_open_manual_mode.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_manual_mode.clicked.connect(self._on_open_manual_llm_dialog)

        exec_btn_layout.addWidget(self.btn_run_ai_auto, stretch=1)
        exec_btn_layout.addWidget(self.btn_open_manual_mode, stretch=1)
        g_lay.addLayout(exec_btn_layout)

        lay.addWidget(grp)
        return w

    # =========================================================================
    # [서브탭 3] 다중 보고서 엑셀 취합기 UI
    # =========================================================================
    def _build_excel_aggregator_subtab(self) -> QWidget:
        w = QWidget()
        apply_qss_role(w, obj_name="SubTabExcelAggregator")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(15, 15, 15, 15)

        grp = QGroupBox("다중 엑셀(.xlsx) / CSV 보고서 파일 단일 요약본 병합기")
        apply_qss_role(grp, obj_name="ExcelAggregatorGroup")
        g_lay = QVBoxLayout(grp)
        g_lay.setContentsMargins(15, 15, 15, 15)

        top_bar = QHBoxLayout()
        btn_add = QPushButton("+ 취합할 엑셀/CSV 보고서 파일 추가")
        apply_qss_role(btn_add)
        btn_clear = QPushButton("목록 비우기")
        apply_qss_role(btn_clear)
        btn_run = QPushButton("📊 단일 요약 Excel(.xlsx)로 병합 실행")
        apply_qss_role(btn_run, role="success", obj_name="BtnRunExcelAggregator")
        btn_run.setMinimumHeight(38)
        btn_run.setCursor(Qt.CursorShape.PointingHandCursor)

        self.list_excel_files = QListWidget()
        btn_add.clicked.connect(self._on_add_excel_files)
        btn_clear.clicked.connect(self.list_excel_files.clear)
        btn_run.clicked.connect(self._on_run_excel_aggregator)

        top_bar.addWidget(btn_add)
        top_bar.addWidget(btn_clear)
        top_bar.addStretch()
        top_bar.addWidget(btn_run)

        g_lay.addLayout(top_bar)
        g_lay.addWidget(self.list_excel_files)
        lay.addWidget(grp)
        return w

    # =========================================================================
    # [서브탭 1] 핸들러
    # =========================================================================
    def _on_browse_submission_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "학생 제출물 폴더 선택")
        if folder:
            norm = str(Path(folder).resolve())
            self.edit_submission_dir.setText(norm)
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
                f"📊 분석 파일: 총 {len(self._analyzed_items)}개 (PDF {pdf_cnt}개) | "
                f"⚠️ [탭 6: 환경설정]에 등록된 학생 명렬표가 없습니다. 명렬표를 등록하면 미제출자가 자동 표시됩니다."
            )
        else:
            self.lbl_summary_badge.setText(
                f"📊 명렬표 총원: {roster_cnt}명  |  ✅ 제출 완료: {submitted_cnt}명  |  "
                f"🚨 미제출: {len(self._missing_students)}명  |  📄 스캔된 제출 파일: {len(self._analyzed_items)}개 (PDF {pdf_cnt}개)"
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

            sid_item = QTableWidgetItem(str(item.get("student_id", "")))
            sid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            name_item = QTableWidgetItem(str(item.get("name", "")))
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            status_str = str(item.get("match_status", ""))
            status_item = QTableWidgetItem(status_str)
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if "✅" in status_str:
                status_item.setForeground(QBrush(QColor("#15803d")))
            elif "⚠️" in status_str:
                status_item.setForeground(QBrush(QColor("#b45309")))
            else:
                status_item.setForeground(QBrush(QColor("#dc2626")))

            orig_name_item = QTableWidgetItem(str(item.get("orig_name", "")))
            orig_name_item.setFlags(orig_name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            new_name_item = QTableWidgetItem(str(item.get("new_name", "")))
            new_name_item.setForeground(QBrush(QColor("#1d4ed8")))

            orig_path_item = QTableWidgetItem(str(item.get("orig_path", "")))
            orig_path_item.setFlags(orig_path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self.table_submissions.setItem(row_idx, 0, chk_item)
            self.table_submissions.setItem(row_idx, 1, sid_item)
            self.table_submissions.setItem(row_idx, 2, name_item)
            self.table_submissions.setItem(row_idx, 3, status_item)
            self.table_submissions.setItem(row_idx, 4, orig_name_item)
            self.table_submissions.setItem(row_idx, 5, new_name_item)
            self.table_submissions.setItem(row_idx, 6, orig_path_item)

        self.table_submissions.blockSignals(False)

    def _populate_missing_table(self) -> None:
        self.table_missing.setRowCount(0)
        for row_idx, stu in enumerate(self._missing_students):
            self.table_missing.insertRow(row_idx)

            sid_item = QTableWidgetItem(str(stu.get("student_id", "")))
            sid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            sid_item.setForeground(QBrush(QColor("#dc2626")))

            gcn = f"{stu.get('grade', 1)}-{stu.get('class_no', 1)}-{stu.get('number', 1):02d}"
            gcn_item = QTableWidgetItem(gcn)
            gcn_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            name_item = QTableWidgetItem(str(stu.get("name", "")))
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            contact_item = QTableWidgetItem(str(stu.get("contact", "")))
            parent_item = QTableWidgetItem(str(stu.get("parent_contact", "")))

            self.table_missing.setItem(row_idx, 0, sid_item)
            self.table_missing.setItem(row_idx, 1, gcn_item)
            self.table_missing.setItem(row_idx, 2, name_item)
            self.table_missing.setItem(row_idx, 3, contact_item)
            self.table_missing.setItem(row_idx, 4, parent_item)

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
        lines = [f"[🚨 {title} 미제출자 명단 (총 {len(self._missing_students)}명)]"]
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

        skip_msg = f"\n\n⚠️ 건너뛴 파일 ({len(skipped)}건):\n" + "\n".join(skipped[:5]) if skipped else ""
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
            self.list_ocr_files.addItem(f)

    def _on_open_manual_llm_dialog(self) -> None:
        """[📋 수동 복붙 모드 열기] 버튼 클릭 시 ManualLlmDialog가 팝업됩니다."""
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
        [🤖 AI 자동 실행 (API 사용)] 버튼 클릭 시:
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
                "(직접 텍스트를 붙여넣어 처리하려면 옆의 [📋 수동 복붙 모드 열기] 버튼을 클릭하세요.)",
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
    # [서브탭 3] 다중 보고서 엑셀 취합 핸들러
    # =========================================================================
    def _on_add_excel_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "취합할 보고서 선택", "", "스프레드시트 (*.xlsx *.csv);;모든 파일 (*)"
        )
        for f in files:
            self.list_excel_files.addItem(f)

    def _on_run_excel_aggregator(self) -> None:
        files = [self.list_excel_files.item(i).text() for i in range(self.list_excel_files.count())]
        if not files:
            QMessageBox.information(self, "안내", "취합할 엑셀(.xlsx) 또는 CSV 보고서 파일을 먼저 추가해 주세요.")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self, "취합 요약 엑셀 저장 경로", "다중보고서_취합요약본.xlsx", "Excel 파일 (*.xlsx)"
        )
        if not save_path:
            return
        worker = ReportExcelAggregatorWorker(files, save_path, skip_header_rows=1)
        self.task_runner.start_worker(
            worker,
            task_key=self.TASK_EXCEL_AGG,
            on_result=lambda res: QMessageBox.information(
                self,
                "취합 완료",
                f"총 {res.get('rows_added', 0)}행이 단일 엑셀 파일로 취합되었습니다:\n{res.get('output_xlsx')}",
            ),
        )
