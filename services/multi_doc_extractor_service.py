"""
다중 포맷(.docx, .hwpx, .hwp, .pdf) 과제 문서 통합 추출 및
AI·로컬 이중 '검토 필요' 탐지/검수 서비스 모듈 (services/multi_doc_extractor_service.py).
"""

import json
import os
import re
import struct
import tempfile
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zipfile import ZipFile

import pandas as pd

from services.docx_extractor_service import (
    EXCEL_CELL_LIMIT,
    MIN_DETECT_SCORE,
    NAME_FIELD,
    add_value,
    build_final_columns,
    clean_text,
    detect_form,
    find_field_name,
    load_all_schemas,
    load_single_schema,
    normalize_heading,
    read_docx,
    sanitize_for_excel,
    split_student_number_and_name,
)
from services.llm_client import MultiLlmClient, extract_json_from_llm_response

# 선택적 외부 라이브러리 안전 임포트 (미설치 시에도 앱이 죽지 않도록 처리)
try:
    from pypdf import PdfReader, PdfWriter  # type: ignore

    HAS_PYPDF = True
except ImportError:
    try:
        from PyPDF2 import PdfReader, PdfWriter  # type: ignore

        HAS_PYPDF = True
    except ImportError:
        HAS_PYPDF = False
        PdfReader = None  # type: ignore
        PdfWriter = None  # type: ignore

try:
    import fitz  # type: ignore  # PyMuPDF

    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False
    fitz = None  # type: ignore

try:
    import olefile  # type: ignore

    HAS_OLEFILE = True
except ImportError:
    HAS_OLEFILE = False
    olefile = None  # type: ignore

try:
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    HAS_WIN32COM = True
except ImportError:
    HAS_WIN32COM = False
    pythoncom = None  # type: ignore
    win32com = None  # type: ignore


SUPPORTED_DOC_EXTS = {".docx", ".hwpx", ".hwp", ".pdf"}
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
PDF_MIN_CHARS_PER_PAGE = 50
REVIEW_SHEET_NAME = "검토_필요_상세"
REVIEW_SHEET_ALIASES = ("검토_필요_상세", "검토 필요")
REVIEW_STATUS_COL = "검토 상태"

# 2차 로컬 휴리스틱 검수 패턴
_UNCERTAIN_MARKERS = (
    "[판독불가]",
    "[불명확]",
    "[인식불가]",
    "[원본 확인 필요]",
    "(판독불가)",
    "(흐림)",
)
_GARBLED_JAMO_RE = re.compile(r"[ㄱ-ㅎㅏ-ㅣ]{3,}")
_QUESTION_MARKS_RE = re.compile(r"\?{3,}")


# =============================================================================
# 1. HWPX 저수준 XML 파싱 유틸
# =============================================================================
def _local_tag(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def _hwpx_paragraph_text(p_elem: ET.Element) -> str:
    """hp:p 문단 내부의 hp:t / tab / lineBreak 텍스트를 추출합니다."""
    parts: List[str] = []
    for node in p_elem.iter():
        tag = _local_tag(node)
        if tag == "t" and node.text:
            parts.append(node.text)
        elif tag in ("tab", "lineBreak"):
            parts.append(" ")
    return clean_text("".join(parts))


def _hwpx_cell_text(tc_elem: ET.Element) -> str:
    """hp:tc 셀 내부의 문단 텍스트를 줄바꿈을 유지하여 추출합니다."""
    lines: List[str] = []
    for node in tc_elem.iter():
        if _local_tag(node) == "p":
            txt = _hwpx_paragraph_text(node)
            if txt:
                lines.append(txt)
    return "\n".join(lines).strip()


def parse_hwpx_content(hwpx_path: Path) -> Dict[str, Any]:
    """
    HWPX(ZIP) 파일 내부 Contents/section*.xml을 파싱하여
    문단 목록, 표(행/셀) 목록, 전체 원문 텍스트를 반환합니다.
    """
    hwpx_path = Path(hwpx_path)
    paragraphs: List[str] = []
    tables: List[List[List[str]]] = []
    raw_chunks: List[str] = []

    with ZipFile(hwpx_path, "r") as zf:
        section_names = sorted(
            [
                n
                for n in zf.namelist()
                if re.match(r"^Contents/section\d+\.xml$", n, re.IGNORECASE)
            ],
            key=lambda x: int(re.search(r"(\d+)", x).group(1)),  # type: ignore
        )
        if not section_names:
            raise ValueError("HWPX 내부에서 Contents/section*.xml을 찾을 수 없습니다.")

        for sec_name in section_names:
            xml_bytes = zf.read(sec_name)
            root = ET.fromstring(xml_bytes)

            def _walk_children(parent: ET.Element) -> None:
                for child in parent:
                    tag = _local_tag(child)
                    if tag == "tbl":
                        tbl_rows: List[List[str]] = []
                        for tr in child:
                            if _local_tag(tr) != "tr":
                                continue
                            row_cells: List[str] = []
                            for tc in tr:
                                if _local_tag(tc) != "tc":
                                    continue
                                row_cells.append(_hwpx_cell_text(tc))
                            if any(row_cells):
                                tbl_rows.append(row_cells)
                                raw_chunks.append(" | ".join(clean_text(c) for c in row_cells))
                        if tbl_rows:
                            tables.append(tbl_rows)
                    elif tag == "p":
                        # 문단 내부에 표(tbl)가 중첩된 경우 표도 함께 수집
                        has_nested_tbl = any(_local_tag(n) == "tbl" for n in child.iter())
                        if has_nested_tbl:
                            _walk_children(child)
                        else:
                            txt = _hwpx_paragraph_text(child)
                            if txt:
                                paragraphs.append(txt)
                                raw_chunks.append(txt)
                    else:
                        _walk_children(child)

            _walk_children(root)

    return {
        "paragraphs": paragraphs,
        "tables": tables,
        "raw_text": "\n".join(raw_chunks).strip(),
    }


def _map_hwpx_locally(
    parsed_hwpx: Dict[str, Any],
    file_path: Path,
    *,
    form_name: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
    schemas_catalog: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]], Set[str]]:
    """
    HWPX에서 추출된 표(hp:tbl)와 문단(hp:p)을 선택된 스키마에 맞춰 로컬 매핑합니다.
    """
    if schemas_catalog is None:
        schemas_catalog = load_all_schemas()

    paragraphs: List[str] = parsed_hwpx.get("paragraphs", [])
    tables: List[List[List[str]]] = parsed_hwpx.get("tables", [])

    candidates: List[str] = list(paragraphs)
    for tbl in tables:
        for row in tbl:
            if row and row[0]:
                candidates.append(clean_text(row[0]))
            if len(row) >= 4 and len(row) % 2 == 0:
                for cell in row[2::2]:
                    if cell:
                        candidates.append(clean_text(cell))

    resolved_form_name = form_name
    resolved_schema = schema

    if resolved_schema is None:
        if resolved_form_name and resolved_form_name in schemas_catalog:
            resolved_schema = schemas_catalog[resolved_form_name]
        else:
            detected_name, _ = detect_form(candidates, schemas_catalog)
            if not detected_name:
                return None, None, None, set()
            resolved_form_name = detected_name
            resolved_schema = schemas_catalog[detected_name]
    else:
        if not resolved_form_name:
            resolved_form_name = resolved_schema.get("form_name", "지정 양식")

    record: Dict[str, Any] = {
        "학번": "",
        "이름": "",
        "원본파일명": file_path.name,
        "양식": resolved_form_name,
    }
    for f_name in resolved_schema.get("fields", {}):
        if f_name != NAME_FIELD:
            record[f_name] = ""

    found: Set[str] = set()

    # 1) 표 기반 매핑 (좌측 라벨 -> 우측 값, 또는 짝수 열 쌍)
    for tbl in tables:
        for row in tbl:
            if len(row) < 2:
                continue
            if len(row) >= 4 and len(row) % 2 == 0:
                pairs = [(row[i], row[i + 1]) for i in range(0, len(row), 2)]
            else:
                pairs = [(row[0], "\n".join(c for c in row[1:] if c).strip())]

            for lbl, val in pairs:
                field_name = find_field_name(resolved_schema, clean_text(lbl))
                if not field_name:
                    continue
                val_clean = val.strip()
                if field_name == NAME_FIELD:
                    num, nm = split_student_number_and_name(val_clean)
                    if num:
                        record["학번"] = num
                    if nm:
                        record["이름"] = nm
                else:
                    add_value(record, field_name, val_clean)
                found.add(field_name)

    # 2) 문단 기반 매핑 (보완)
    expected_fields = set(resolved_schema.get("fields", {}).keys())
    if found != expected_fields:
        current_field: Optional[str] = None
        sections: Dict[str, List[str]] = {}
        for p_txt in paragraphs:
            f_name = find_field_name(resolved_schema, p_txt) if len(p_txt) <= 60 else None
            if f_name:
                current_field = f_name
                sections.setdefault(current_field, [])
            elif current_field:
                sections[current_field].append(p_txt)

        for f_name, lines in sections.items():
            if f_name in found:
                continue
            val = "\n".join(lines).strip()
            if not val:
                continue
            if f_name == NAME_FIELD:
                num, nm = split_student_number_and_name(val)
                if num:
                    record["학번"] = num
                if nm:
                    record["이름"] = nm
            else:
                add_value(record, f_name, val)
            found.add(f_name)

    _fill_student_info_from_filename(record, file_path)
    return record, resolved_form_name, resolved_schema, found


# =============================================================================
# 2. HWP (COM + olefile) & PDF 텍스트/이미지 추출 유틸
# =============================================================================
def _convert_hwp_via_com(hwp_path: Path, temp_dir: Path) -> Optional[Path]:
    """
    Windows 한컴오피스 COM(HWPFrame.HwpObject)을 이용해 .hwp를 임시 .hwpx(실패 시 .pdf)로 변환합니다.
    미설치 또는 COM 오류 시 None을 반환합니다.
    """
    if not HAS_WIN32COM or win32com is None or pythoncom is None:
        return None

    hwp = None
    com_initialized = False
    try:
        pythoncom.CoInitialize()
        com_initialized = True
        hwp = win32com.client.Dispatch("HWPFrame.HwpObject")
        try:
            hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
        except Exception:
            pass
        try:
            hwp.SetMessageBoxMode(0x00010000)  # 확인/오류 팝업 억제
        except Exception:
            pass

        opened = hwp.Open(str(hwp_path.resolve()), "HWP", "forceopen:true")
        if not opened:
            return None

        # 1순위: HWPX 변환
        out_hwpx = temp_dir / f"{hwp_path.stem}.hwpx"
        try:
            if hwp.SaveAs(str(out_hwpx.resolve()), "HWPX") and out_hwpx.exists():
                return out_hwpx
        except Exception:
            pass

        # 2순위: PDF 변환
        out_pdf = temp_dir / f"{hwp_path.stem}.pdf"
        try:
            if hwp.SaveAs(str(out_pdf.resolve()), "PDF") and out_pdf.exists():
                return out_pdf
        except Exception:
            pass

        return None
    except Exception:
        return None
    finally:
        if hwp is not None:
            try:
                hwp.Quit()
            except Exception:
                pass
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _extract_hwp_text_olefile(hwp_path: Path) -> str:
    """
    olefile을 사용하여 HWP 바이너리(BodyText/Section* 및 PrvText)에서 텍스트를 추출합니다.
    """
    if not HAS_OLEFILE or olefile is None:
        raise RuntimeError("olefile 패키지가 설치되어 있지 않습니다.")

    if not olefile.isOleFile(str(hwp_path)):
        raise ValueError("유효한 HWP(OLE2) 파일이 아닙니다.")

    chunks: List[str] = []
    with olefile.OleFileIO(str(hwp_path)) as ole:
        # 압축 여부 확인 (FileHeader 36번째 바이트 bit 0)
        is_compressed = False
        if ole.exists("FileHeader"):
            header_data = ole.openstream("FileHeader").read()
            if len(header_data) >= 40:
                flags = struct.unpack("<I", header_data[36:40])[0]
                is_compressed = bool(flags & 0x01)
                if flags & 0x02:
                    raise ValueError("암호화된 HWP 문서는 텍스트를 추출할 수 없습니다.")

        # BodyText/Section* 스트림 파싱
        section_streams = sorted(
            [
                "/".join(entry)
                for entry in ole.listdir()
                if len(entry) == 2 and entry[0] == "BodyText" and entry[1].startswith("Section")
            ],
            key=lambda s: int(re.search(r"(\d+)", s).group(1)) if re.search(r"(\d+)", s) else 0,
        )

        for stream_path in section_streams:
            raw = ole.openstream(stream_path).read()
            if is_compressed:
                try:
                    raw = zlib.decompress(raw, -15)
                except zlib.error:
                    continue

            # HWP 레코드 순회 (HWPTAG_PARA_TEXT = 67)
            pos = 0
            length = len(raw)
            while pos + 4 <= length:
                header = struct.unpack("<I", raw[pos : pos + 4])[0]
                tag_id = header & 0x3FF
                size = (header >> 20) & 0xFFF
                pos += 4
                if size == 0xFFF and pos + 4 <= length:
                    size = struct.unpack("<I", raw[pos : pos + 4])[0]
                    pos += 4
                if pos + size > length:
                    break

                record_data = raw[pos : pos + size]
                pos += size

                if tag_id == 67:  # HWPTAG_PARA_TEXT
                    text_chars: List[str] = []
                    i = 0
                    while i + 2 <= len(record_data):
                        code = struct.unpack("<H", record_data[i : i + 2])[0]
                        if code in (1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23):
                            # 확장 제어 문자 (16바이트 = UTF-16 code unit 8개)
                            i += 16
                            continue
                        elif code in (4, 5, 6, 7, 8, 9, 19, 20):
                            # 인라인 제어 문자 (tab 등)
                            if code == 9:
                                text_chars.append(" ")
                            i += 2
                            continue
                        elif code in (10, 13):
                            text_chars.append("\n")
                        elif code >= 32:
                            text_chars.append(chr(code))
                        i += 2
                    line = "".join(text_chars).strip()
                    if line:
                        chunks.append(line)

        # BodyText 추출 실패 시 PrvText(미리보기 텍스트) 폴백
        if not chunks and ole.exists("PrvText"):
            prv_raw = ole.openstream("PrvText").read()
            prv_text = prv_raw.decode("utf-16le", errors="ignore").strip()
            if prv_text:
                chunks.append(prv_text)

    return "\n".join(chunks).strip()


def _extract_pdf_text_layers(pdf_path: Path) -> Tuple[List[str], float]:
    """pypdf로 페이지별 텍스트 레이어와 페이지당 평균 글자 수를 반환합니다."""
    if not HAS_PYPDF or PdfReader is None:
        raise RuntimeError(
            "pypdf 패키지가 설치되어 있지 않습니다. 'pip install pypdf>=4.0.0' 실행 후 다시 시도해 주세요."
        )
    reader = PdfReader(str(pdf_path))
    page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
    total_chars = sum(len(t) for t in page_texts)
    avg_chars = total_chars / max(1, len(page_texts))
    return page_texts, avg_chars


def _render_pdf_pages_to_png(pdf_path: Path, max_pages: int = 8) -> List[bytes]:
    """PyMuPDF(fitz)가 설치된 경우 PDF 페이지를 PNG 바이트 목록으로 렌더링합니다."""
    if not HAS_PYMUPDF or fitz is None:
        return []
    images: List[bytes] = []
    doc = fitz.open(str(pdf_path))
    try:
        for idx, page in enumerate(doc):
            if idx >= max_pages:
                break
            pix = page.get_pixmap(dpi=150)
            images.append(pix.tobytes("png"))
    finally:
        doc.close()
    return images


# =============================================================================
# 3. AI 구조화 추출 프롬프트 & 이중 '검토 필요' 탐지 로직
# =============================================================================
def _build_ai_extraction_prompts(
    schema: Dict[str, Any],
    form_name: str,
    raw_text: Optional[str] = None,
    is_vision_ocr: bool = False,
) -> Tuple[str, str]:
    """
    AI OCR / 구조화 매핑에 사용할 (system_prompt, user_prompt)를 생성합니다.
    응답은 반드시 student_id, name, fields(value, status, reason) JSON 규격을 따릅니다.
    """
    field_names = [f for f in schema.get("fields", {}).keys() if f != NAME_FIELD]
    fields_example = {
        fname: {
            "value": "추출된 답변 텍스트 (없으면 빈 문자열)",
            "status": "ok | needs_review",
            "reason": "의심 사유 (정상일 경우 빈 문자열)",
        }
        for fname in (field_names or ["문항1"])
    }
    json_template = json.dumps(
        {
            "student_id": "10305",
            "name": "김민준",
            "fields": fields_example,
        },
        ensure_ascii=False,
        indent=2,
    )

    system_prompt = (
        "당신은 한국 학교 학생 과제 문서를 정확하게 판독·구조화하는 교무 데이터 추출 AI입니다.\n"
        "반드시 아래 규칙을 지키세요:\n"
        "1. 인사말이나 부연 설명 없이 오직 ```json ... ``` 코드 블록 하나만 출력하세요.\n"
        "2. 학생이 실제로 작성한 원문을 최대한 보존하되, 스캔 노이즈·오탈자·문맥 단절·판독 불명확·"
        "내용 누락 의심 항목은 반드시 status를 'needs_review'로 표시하고 reason에 구체적 사유를 적으세요.\n"
        "3. 정상적으로 명확히 추출된 항목만 status를 'ok', reason을 ''(빈 문자열)로 설정하세요."
    )

    mode_desc = (
        "첨부된 스캔/이미지 학생 과제 문서를 OCR 판독하여"
        if is_vision_ocr
        else "아래 학생 과제 원문 텍스트를 분석하여"
    )
    user_prompt = (
        f"### [작업 지시]\n"
        f"{mode_desc} 양식 '{form_name}'의 각 문항에 해당하는 학생 응답을 추출하세요.\n"
        f"추출 대상 문항 목록: {', '.join(field_names) if field_names else '(문서 내 주요 문항)'}\n\n"
        f"### [출력 JSON 규격 - 필수 준수]\n"
        f"```json\n{json_template}\n```\n"
    )
    if raw_text is not None:
        trimmed = raw_text.strip()
        if len(trimmed) > 15000:
            trimmed = trimmed[:15000] + "\n...(이하 생략)"
        user_prompt += f"\n### [학생 과제 원문]\n\"\"\"\n{trimmed}\n\"\"\""

    return system_prompt, user_prompt


def _fill_student_info_from_filename(record: Dict[str, Any], file_path: Path) -> None:
    if not record.get("이름"):
        match = re.search(r"제출-([^-]+?)\s*-\s*", file_path.name)
        if match:
            record["이름"] = clean_text(match.group(1))
    if not record.get("학번") or not record.get("이름"):
        num, name = split_student_number_and_name(file_path.stem)
        if not record.get("학번") and num:
            record["학번"] = num
        if not record.get("이름") and name:
            record["이름"] = name


def _detect_local_suspicion(field_name: str, value: str) -> Optional[str]:
    """
    2차 로컬 휴리스틱 검증: AI가 'ok'로 판단했거나 로컬 XML 파싱된 값에서도
    교사 검수가 필요한 이상 징후를 탐지합니다.
    """
    val = (value or "").strip()
    if not val:
        return "항목 답변이 비어 있거나 추출되지 않음"

    for marker in _UNCERTAIN_MARKERS:
        if marker in val:
            return f"판독 불명확/원본 확인 표시 포함 ({marker})"

    if "\ufffd" in val:
        return "문자 인코딩 깨짐(\\ufffd) 감지"

    if _GARBLED_JAMO_RE.search(val):
        return "자모 분리/OCR 깨짐 의심 문자열 감지"

    if _QUESTION_MARKS_RE.search(val):
        return "연속 물음표(???) 등 판독 의심 패턴 감지"

    if len(val) <= 2 and val not in ("O", "X", "o", "x", "예", "아니오", "없음"):
        return f"답변 길이가 비정상적으로 짧음 ({len(val)}자)"

    return None


def _build_record_and_review_items(
    *,
    file_path: Path,
    form_name: str,
    schema: Dict[str, Any],
    student_id: str,
    name: str,
    field_values: Dict[str, str],
    ai_flags: Optional[Dict[str, Tuple[str, str]]] = None,
    raw_text_preview: str = "",
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    추출된 필드 값 + 1차(AI 판단) + 2차(로컬 휴리스틱) 이중 탐지를 통합하여
    엑셀 레코드와 검수 항목 리스트(review_items)를 생성합니다.
    """
    ai_flags = ai_flags or {}
    expected_fields = [f for f in schema.get("fields", {}).keys() if f != NAME_FIELD]
    if not expected_fields:
        expected_fields = list(field_values.keys())

    record: Dict[str, Any] = {
        "학번": clean_text(student_id),
        "이름": clean_text(name),
        "원본파일명": file_path.name,
        "양식": form_name,
    }
    _fill_student_info_from_filename(record, file_path)

    review_items: List[Dict[str, Any]] = []

    # 학번/이름 자체 이상 징후도 2차 로컬 검증
    if not record["학번"] or not re.fullmatch(r"\d{4,6}", record["학번"]):
        review_items.append(
            {
                "student_id": record["학번"],
                "name": record["이름"],
                "file_name": file_path.name,
                "file_path": str(file_path.resolve()),
                "form_name": form_name,
                "field_name": "학번",
                "extracted_value": record["학번"],
                "status": "needs_review",
                "reason": "학번 누락 또는 4~6자리 숫자 형식 아님",
                "source": "로컬 검증",
                "raw_text_preview": raw_text_preview[:3000],
            }
        )
    if not record["이름"] or len(record["이름"]) < 2:
        review_items.append(
            {
                "student_id": record["학번"],
                "name": record["이름"],
                "file_name": file_path.name,
                "file_path": str(file_path.resolve()),
                "form_name": form_name,
                "field_name": "이름",
                "extracted_value": record["이름"],
                "status": "needs_review",
                "reason": "학생 이름 누락 또는 2자 미만",
                "source": "로컬 검증",
                "raw_text_preview": raw_text_preview[:3000],
            }
        )

    missing_fields: List[str] = []
    for fname in expected_fields:
        val = (field_values.get(fname) or "").strip()
        record[fname] = val
        if not val:
            missing_fields.append(fname)

        ai_status, ai_reason = ai_flags.get(fname, ("ok", ""))
        is_ai_flagged = (ai_status == "needs_review") or bool(
            ai_reason and ai_status != "ok"
        )
        local_reason = _detect_local_suspicion(fname, val)

        if is_ai_flagged or local_reason:
            reasons: List[str] = []
            if is_ai_flagged:
                reasons.append(ai_reason or "AI 문맥/판독 의심 판단")
            if local_reason and local_reason not in reasons:
                reasons.append(local_reason)

            if is_ai_flagged and local_reason:
                source = "AI+로컬"
            elif is_ai_flagged:
                source = "AI 판단"
            else:
                source = "로컬 검증"

            review_items.append(
                {
                    "student_id": record["학번"],
                    "name": record["이름"],
                    "file_name": file_path.name,
                    "file_path": str(file_path.resolve()),
                    "form_name": form_name,
                    "field_name": fname,
                    "extracted_value": val,
                    "status": "needs_review",
                    "reason": " / ".join(reasons),
                    "source": source,
                    "raw_text_preview": raw_text_preview[:3000],
                }
            )

    record["누락 항목"] = ", ".join(missing_fields)
    record[REVIEW_STATUS_COL] = (
        f"검토 필요 ({len(review_items)}건)" if review_items else "정상"
    )
    return record, review_items


def _parse_ai_json_to_fields(
    raw_response: str,
    schema: Dict[str, Any],
) -> Tuple[str, str, Dict[str, str], Dict[str, Tuple[str, str]]]:
    """
    extract_json_from_llm_response()로 AI 응답을 파싱하여
    (student_id, name, field_values, ai_flags)를 반환합니다.
    """
    parsed = extract_json_from_llm_response(raw_response)
    sid = clean_text(parsed.get("student_id", ""))
    sname = clean_text(parsed.get("name", ""))
    raw_fields = parsed.get("fields", {})
    if not isinstance(raw_fields, dict):
        raw_fields = {}

    field_values: Dict[str, str] = {}
    ai_flags: Dict[str, Tuple[str, str]] = {}

    for k, v in raw_fields.items():
        # 스키마 필드명과 정확히 일치하거나 별칭으로 매칭
        matched_name = find_field_name(schema, str(k)) or str(k).strip()
        if isinstance(v, dict):
            val = str(v.get("value", "") or "").strip()
            st = str(v.get("status", "ok") or "ok").strip().lower()
            if st not in ("ok", "needs_review"):
                st = "needs_review" if v.get("reason") else "ok"
            rsn = clean_text(v.get("reason", ""))
        else:
            val = str(v or "").strip()
            st = "ok"
            rsn = ""

        field_values[matched_name] = val
        ai_flags[matched_name] = (st, rsn)

    return sid, sname, field_values, ai_flags


def _ai_map_text_to_record(
    raw_text: str,
    file_path: Path,
    schema: Dict[str, Any],
    form_name: str,
    llm_client: MultiLlmClient,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    sys_prompt, usr_prompt = _build_ai_extraction_prompts(
        schema=schema,
        form_name=form_name,
        raw_text=raw_text,
        is_vision_ocr=False,
    )
    resp = llm_client.generate(prompt=usr_prompt, system_prompt=sys_prompt)
    sid, sname, field_values, ai_flags = _parse_ai_json_to_fields(resp, schema)
    return _build_record_and_review_items(
        file_path=file_path,
        form_name=form_name,
        schema=schema,
        student_id=sid,
        name=sname,
        field_values=field_values,
        ai_flags=ai_flags,
        raw_text_preview=raw_text,
    )


def _ai_vision_ocr_pdf_to_record(
    pdf_path: Path,
    schema: Dict[str, Any],
    form_name: str,
    llm_client: MultiLlmClient,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    sys_prompt, usr_prompt = _build_ai_extraction_prompts(
        schema=schema,
        form_name=form_name,
        raw_text=None,
        is_vision_ocr=True,
    )

    page_images = _render_pdf_pages_to_png(pdf_path, max_pages=8)
    if page_images:
        # 여러 페이지인 경우 각 페이지 OCR 결과를 병합
        merged_sid = ""
        merged_name = ""
        merged_values: Dict[str, str] = {}
        merged_flags: Dict[str, Tuple[str, str]] = {}
        raw_ocr_notes: List[str] = []

        for page_idx, img_bytes in enumerate(page_images, start=1):
            page_prompt = usr_prompt + f"\n(현재 페이지: {page_idx}/{len(page_images)})"
            resp = llm_client.generate(
                prompt=page_prompt,
                system_prompt=sys_prompt,
                image_bytes=img_bytes,
                mime_type="image/png",
            )
            sid, sname, f_vals, a_flags = _parse_ai_json_to_fields(resp, schema)
            if sid and not merged_sid:
                merged_sid = sid
            if sname and not merged_name:
                merged_name = sname
            for fname, val in f_vals.items():
                if not val:
                    continue
                if merged_values.get(fname):
                    merged_values[fname] += "\n" + val
                else:
                    merged_values[fname] = val
                st, rsn = a_flags.get(fname, ("ok", ""))
                prev_st, prev_rsn = merged_flags.get(fname, ("ok", ""))
                if st == "needs_review" or prev_st == "needs_review":
                    combined_rsn = " / ".join(x for x in (prev_rsn, rsn) if x)
                    merged_flags[fname] = ("needs_review", combined_rsn)
                else:
                    merged_flags[fname] = ("ok", "")
            raw_ocr_notes.append(f"[페이지 {page_idx} OCR 완료]")

        return _build_record_and_review_items(
            file_path=pdf_path,
            form_name=form_name,
            schema=schema,
            student_id=merged_sid,
            name=merged_name,
            field_values=merged_values,
            ai_flags=merged_flags,
            raw_text_preview="\n".join(raw_ocr_notes) + "\n(이미지 기반 스캔 PDF — [원본 파일 열기]로 대조 가능)",
        )

    # PyMuPDF가 없는 경우: Gemini는 application/pdf 바이트 직접 전달 지원
    if llm_client.provider == "gemini":
        pdf_bytes = pdf_path.read_bytes()
        resp = llm_client.generate(
            prompt=usr_prompt,
            system_prompt=sys_prompt,
            image_bytes=pdf_bytes,
            mime_type="application/pdf",
        )
        sid, sname, f_vals, a_flags = _parse_ai_json_to_fields(resp, schema)
        return _build_record_and_review_items(
            file_path=pdf_path,
            form_name=form_name,
            schema=schema,
            student_id=sid,
            name=sname,
            field_values=f_vals,
            ai_flags=a_flags,
            raw_text_preview="(스캔 이미지 기반 PDF — [원본 파일 열기]로 대조 가능)",
        )

    raise RuntimeError(
        "스캔 PDF Vision OCR을 실행하려면 PyMuPDF(fitz)를 설치하거나 AI 공급자를 Gemini로 설정해 주세요."
    )


def _map_plain_text_locally(
    raw_text: str,
    file_path: Path,
    *,
    form_name: Optional[str],
    schema: Optional[Dict[str, Any]],
    schemas_catalog: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """AI를 사용할 수 없을 때 일반 텍스트에서 헤딩 기반으로 로컬 매핑을 수행합니다."""
    lines = [clean_text(ln) for ln in raw_text.splitlines() if clean_text(ln)]
    if schema is None:
        detected_name, _ = detect_form(lines, schemas_catalog)
        if detected_name:
            form_name = detected_name
            schema = schemas_catalog[detected_name]
        else:
            # 첫 번째 스키마로 폴백하되 전체를 검토 필요로 표시
            form_name = form_name or next(iter(schemas_catalog.keys()), "탐구보고서")
            schema = schemas_catalog.get(form_name, {"fields": {}})

    form_name = form_name or schema.get("form_name", "지정 양식")
    sections: Dict[str, List[str]] = {}
    current_field: Optional[str] = None
    sid = ""
    sname = ""

    for line in lines:
        f_name = find_field_name(schema, line) if len(line) <= 60 else None
        if f_name:
            current_field = f_name
            sections.setdefault(current_field, [])
        elif current_field:
            sections[current_field].append(line)

    field_values: Dict[str, str] = {}
    for f_name, sec_lines in sections.items():
        val = "\n".join(sec_lines).strip()
        if f_name == NAME_FIELD:
            sid, sname = split_student_number_and_name(val)
        else:
            field_values[f_name] = val

    return _build_record_and_review_items(
        file_path=file_path,
        form_name=form_name,
        schema=schema,
        student_id=sid,
        name=sname,
        field_values=field_values,
        raw_text_preview=raw_text,
    )


# =============================================================================
# 4. 포맷별 단일 파일 통합 파서
# =============================================================================
def _resolve_target_schema(
    forced_form_name: Optional[str],
    forced_schema: Optional[Dict[str, Any]],
    schemas_catalog: Dict[str, Dict[str, Any]],
    candidate_texts: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    if forced_schema is not None:
        return (forced_form_name or forced_schema.get("form_name", "지정 양식")), forced_schema
    if forced_form_name and forced_form_name in schemas_catalog:
        return forced_form_name, schemas_catalog[forced_form_name]
    if candidate_texts:
        detected, _ = detect_form(candidate_texts, schemas_catalog)
        if detected and detected in schemas_catalog:
            return detected, schemas_catalog[detected]
    first_name = next(iter(schemas_catalog.keys()), "탐구보고서")
    return first_name, schemas_catalog[first_name]


def _get_ready_llm_client() -> Optional[MultiLlmClient]:
    try:
        client = MultiLlmClient()
        if client.is_manual_mode_active():
            return None
        return client
    except Exception:
        return None


def read_single_document(
    file_path: Path,
    *,
    form_name: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
    schemas_catalog: Optional[Dict[str, Dict[str, Any]]] = None,
    llm_client: Optional[MultiLlmClient] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    단일 문서(.docx, .hwpx, .hwp, .pdf)를 포맷별 최적 경로로 파싱하고,
    이중 탐지('검토 필요') 결과를 함께 반환합니다.
    """
    file_path = Path(file_path)
    ext = file_path.suffix.lower()
    if schemas_catalog is None:
        schemas_catalog = load_all_schemas()

    # 1) .docx -> 기존 docx_extractor_service.read_docx() 로직 그대로 사용
    if ext == ".docx":
        rec = read_docx(
            file_path,
            form_name=form_name,
            schema=schema,
            schemas_catalog=schemas_catalog,
        )
        resolved_form = rec.get("양식", form_name or "탐구보고서")
        resolved_schema = schema or schemas_catalog.get(resolved_form, {"fields": {}})
        field_vals = {
            k: str(v or "")
            for k, v in rec.items()
            if k not in ("학번", "이름", "원본파일명", "양식", "누락 항목", REVIEW_STATUS_COL)
        }
        return _build_record_and_review_items(
            file_path=file_path,
            form_name=resolved_form,
            schema=resolved_schema,
            student_id=str(rec.get("학번", "")),
            name=str(rec.get("이름", "")),
            field_values=field_vals,
            raw_text_preview="\n".join(f"[{k}] {v}" for k, v in field_vals.items() if v),
        )

    # 2) .hwpx -> XML 파싱 + 스키마 매핑 (필요 시 AI 매핑 폴백)
    if ext == ".hwpx":
        parsed_hwpx = parse_hwpx_content(file_path)
        local_res = _map_hwpx_locally(
            parsed_hwpx,
            file_path,
            form_name=form_name,
            schema=schema,
            schemas_catalog=schemas_catalog,
        )
        local_rec, det_form, det_schema, found_fields = local_res
        expected_cnt = len(det_schema.get("fields", {})) if det_schema else 0

        if local_rec is not None and found_fields and len(found_fields) >= max(1, expected_cnt // 2):
            field_vals = {
                k: str(v or "")
                for k, v in local_rec.items()
                if k not in ("학번", "이름", "원본파일명", "양식", "누락 항목")
            }
            return _build_record_and_review_items(
                file_path=file_path,
                form_name=det_form or "지정 양식",
                schema=det_schema or {"fields": {}},
                student_id=str(local_rec.get("학번", "")),
                name=str(local_rec.get("이름", "")),
                field_values=field_vals,
                raw_text_preview=parsed_hwpx.get("raw_text", ""),
            )

        # AI 매핑 폴백
        raw_text = parsed_hwpx.get("raw_text", "")
        if llm_client is not None and raw_text:
            target_form, target_schema = _resolve_target_schema(
                form_name, schema, schemas_catalog, parsed_hwpx.get("paragraphs", [])
            )
            if log_callback:
                log_callback("INFO", f"[HWPX AI 매핑 폴백] {file_path.name} -> {target_form}")
            return _ai_map_text_to_record(raw_text, file_path, target_schema, target_form, llm_client)

        if local_rec is not None:
            field_vals = {
                k: str(v or "")
                for k, v in local_rec.items()
                if k not in ("학번", "이름", "원본파일명", "양식", "누락 항목")
            }
            return _build_record_and_review_items(
                file_path=file_path,
                form_name=det_form or "지정 양식",
                schema=det_schema or {"fields": {}},
                student_id=str(local_rec.get("학번", "")),
                name=str(local_rec.get("이름", "")),
                field_values=field_vals,
                raw_text_preview=raw_text,
            )

        raise ValueError("HWPX 문서 양식을 판별하지 못했고 AI 폴백도 비활성 상태입니다.")

    # 3) .pdf -> pypdf 텍스트 레이어 확인 후 텍스트 매핑 또는 Vision OCR
    if ext == ".pdf":
        page_texts, avg_chars = _extract_pdf_text_layers(file_path)
        full_text = "\n\n".join(t for t in page_texts if t).strip()
        lines = [ln for ln in full_text.splitlines() if ln.strip()]
        target_form, target_schema = _resolve_target_schema(
            form_name, schema, schemas_catalog, lines
        )

        if avg_chars < PDF_MIN_CHARS_PER_PAGE:
            # 스캔 / 이미지 기반 PDF -> 멀티모달 Vision OCR
            if llm_client is None:
                raise RuntimeError(
                    f"'{file_path.name}'은 스캔/이미지 PDF(페이지당 평균 {avg_chars:.0f}자)입니다. "
                    "AI API 키를 설정하고 수동 모드를 해제해 주세요."
                )
            if log_callback:
                log_callback("INFO", f"[PDF Vision OCR] {file_path.name} (페이지당 {avg_chars:.0f}자)")
            return _ai_vision_ocr_pdf_to_record(file_path, target_schema, target_form, llm_client)

        # 텍스트 레이어 PDF -> AI 구조화 매핑 (미설정 시 로컬 폴백)
        if llm_client is not None:
            return _ai_map_text_to_record(full_text, file_path, target_schema, target_form, llm_client)
        return _map_plain_text_locally(
            full_text,
            file_path,
            form_name=target_form,
            schema=target_schema,
            schemas_catalog=schemas_catalog,
        )

    # 4) .hwp -> 1순위 한컴오피스 COM 변환, 2순위 olefile 텍스트 추출
    if ext == ".hwp":
        with tempfile.TemporaryDirectory(prefix="eduflow_hwp_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            converted_path = _convert_hwp_via_com(file_path, tmp_dir)
            if converted_path is not None and converted_path.exists():
                if log_callback:
                    log_callback("INFO", f"[HWP COM 변환 성공] {file_path.name} -> {converted_path.name}")
                rec, rev_items = read_single_document(
                    converted_path,
                    form_name=form_name,
                    schema=schema,
                    schemas_catalog=schemas_catalog,
                    llm_client=llm_client,
                    log_callback=log_callback,
                )
                # 원본 파일명/경로를 원래 .hwp 기준으로 복원
                rec["원본파일명"] = file_path.name
                for item in rev_items:
                    item["file_name"] = file_path.name
                    item["file_path"] = str(file_path.resolve())
                return rec, rev_items

        # 2순위: olefile 텍스트 추출 폴백
        try:
            hwp_text = _extract_hwp_text_olefile(file_path)
        except Exception as ole_exc:
            raise RuntimeError(
                f"HWP 변환 실패: 한컴오피스 COM을 사용할 수 없으며 olefile 추출도 실패했습니다 ({ole_exc})"
            ) from ole_exc

        if not hwp_text:
            raise ValueError("HWP 파일에서 추출할 수 있는 본문 텍스트가 없습니다.")

        lines = [ln for ln in hwp_text.splitlines() if ln.strip()]
        target_form, target_schema = _resolve_target_schema(
            form_name, schema, schemas_catalog, lines
        )
        if llm_client is not None:
            if log_callback:
                log_callback("INFO", f"[HWP olefile + AI 매핑] {file_path.name}")
            return _ai_map_text_to_record(hwp_text, file_path, target_schema, target_form, llm_client)
        return _map_plain_text_locally(
            hwp_text,
            file_path,
            form_name=target_form,
            schema=target_schema,
            schemas_catalog=schemas_catalog,
        )

    raise ValueError(f"지원하지 않는 파일 형식입니다: {ext}")


# =============================================================================
# 5. 엑셀 저장 및 검수 데이터 읽기/쓰기 유틸
# =============================================================================
def write_multi_doc_excel(
    records: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    review_items: List[Dict[str, Any]],
    output_path: Path,
    schemas_catalog: Dict[str, Dict[str, Any]],
) -> Path:
    """추출 결과, 오류 파일 시트, 검토 필요 시트를 통합 엑셀(.xlsx)로 저장합니다."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    target_path = output_path
    writer = None
    for attempt in range(5):
        try:
            writer = pd.ExcelWriter(target_path, engine="openpyxl")
            break
        except PermissionError:
            if attempt < 4:
                target_path = output_path.parent / f"{output_path.stem}_대체_{attempt+1}{output_path.suffix}"
            else:
                raise PermissionError(
                    f"엑셀 파일 쓰기 권한이 없습니다. 파일이 열려 있는지 확인하세요: {output_path.name}"
                )

    used_sheet_names: Set[str] = {"오류 파일", REVIEW_SHEET_NAME}

    with writer:
        all_forms = {r["양식"] for r in records if "양식" in r}
        for form_name in all_forms:
            rows = [r for r in records if r.get("양식") == form_name]
            if not rows:
                continue

            schema = schemas_catalog.get(form_name, {})
            columns = build_final_columns(schema) if schema else list(rows[0].keys())
            if REVIEW_STATUS_COL not in columns:
                columns.append(REVIEW_STATUS_COL)

            df = pd.DataFrame(rows)
            for col in columns:
                if col not in df.columns:
                    df[col] = ""

            df = df[columns].fillna("")
            df["_sort"] = pd.to_numeric(df["학번"], errors="coerce")
            df = df.sort_values(by="_sort", na_position="last").drop(columns="_sort")
            df = df.map(sanitize_for_excel)

            sheet_base = re.sub(r"[\[\]:*?/\\]", "_", form_name).strip().strip("'")[:31] or "변환 결과"
            sheet_name = sheet_base
            serial = 2
            while sheet_name.casefold() in {s.casefold() for s in used_sheet_names}:
                suffix = f"_{serial}"
                sheet_name = sheet_base[: 31 - len(suffix)] + suffix
                serial += 1

            used_sheet_names.add(sheet_name)
            df.to_excel(writer, index=False, sheet_name=sheet_name)

            ws = writer.sheets[sheet_name]
            ws.freeze_panes = "A2"
            for col_cells in ws.columns:
                longest = max((len(str(c.value or "")) for c in col_cells), default=0)
                col_letter = col_cells[0].column_letter
                ws.column_dimensions[col_letter].width = min(max(longest + 2, 10), 50)

        if review_items:
            rev_rows = [
                {
                    "학번": it.get("student_id", ""),
                    "이름": it.get("name", ""),
                    "원본파일명": it.get("file_name", ""),
                    "양식": it.get("form_name", ""),
                    "문항명": it.get("field_name", ""),
                    "추출된 값": it.get("extracted_value", ""),
                    "의심 사유": it.get("reason", ""),
                    "검출 방식": it.get("source", ""),
                    "검토 상태": "검토 완료" if it.get("status") == "reviewed" else "검토 필요",
                    "원본파일경로": it.get("file_path", ""),
                }
                for it in review_items
            ]
            rev_df = pd.DataFrame(rev_rows).map(sanitize_for_excel)
            rev_df.to_excel(writer, index=False, sheet_name=REVIEW_SHEET_NAME)
            ws_rev = writer.sheets[REVIEW_SHEET_NAME]
            ws_rev.freeze_panes = "A2"
            for col_cells in ws_rev.columns:
                longest = max((len(str(c.value or "")) for c in col_cells), default=0)
                col_letter = col_cells[0].column_letter
                ws_rev.column_dimensions[col_letter].width = min(max(longest + 2, 12), 55)

        if errors:
            error_df = pd.DataFrame(errors).map(sanitize_for_excel)
            error_df.to_excel(writer, index=False, sheet_name="오류 파일")
            ws_err = writer.sheets["오류 파일"]
            ws_err.freeze_panes = "A2"

        if not records and not errors:
            pd.DataFrame({"안내": ["처리 결과 없음"]}).to_excel(writer, index=False, sheet_name="결과 없음")

    return target_path


def extract_raw_text_preview(file_path: Path | str, max_chars: int = 4000) -> str:
    """검수 다이얼로그에서 원본 문서 대조용 텍스트 미리보기를 즉석 추출합니다."""
    p = Path(file_path)
    if not p.exists():
        return "(원본 파일을 찾을 수 없습니다.)"
    ext = p.suffix.lower()
    try:
        if ext == ".docx":
            with ZipFile(p, "r") as zf:
                root = ET.fromstring(zf.read("word/document.xml"))
            texts = [
                "".join(t.text or "" for t in node.iter() if _local_tag(t) == "t")
                for node in root.iter()
                if _local_tag(node) == "p"
            ]
            out = "\n".join(clean_text(t) for t in texts if clean_text(t))
            return out[:max_chars] if out else "(텍스트 없음)"
        if ext == ".hwpx":
            return parse_hwpx_content(p).get("raw_text", "")[:max_chars] or "(텍스트 없음)"
        if ext == ".pdf":
            pages, avg = _extract_pdf_text_layers(p)
            txt = "\n\n".join(pages).strip()
            if avg < PDF_MIN_CHARS_PER_PAGE or not txt:
                return "(스캔/이미지 기반 PDF입니다. 아래 [원본 파일 열기] 버튼으로 원본을 대조해 주세요.)"
            return txt[:max_chars]
        if ext == ".hwp":
            if HAS_OLEFILE:
                txt = _extract_hwp_text_olefile(p)
                if txt:
                    return txt[:max_chars]
            return "(HWP 바이너리 텍스트 미리보기 불가 — [원본 파일 열기] 버튼으로 한컴오피스에서 확인해 주세요.)"
    except Exception as exc:
        return f"(원본 텍스트 읽기 실패: {exc})"
    return "(미리보기를 지원하지 않는 형식입니다.)"


def load_review_items_from_excel(excel_path: Path | str) -> List[Dict[str, Any]]:
    """저장된 결과 엑셀의 [검토_필요_상세] (또는 [검토 필요]) 시트에서 검수 항목 목록을 읽어옵니다."""
    excel_path = Path(excel_path)
    if not excel_path.exists():
        return []
    try:
        with pd.ExcelFile(excel_path) as xl:
            target_sheet = next(
                (s for s in REVIEW_SHEET_ALIASES if s in xl.sheet_names), None
            )
            if not target_sheet:
                return []
            df = xl.parse(target_sheet, dtype=str).fillna("")
    except Exception:
        return []

    items: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        st_raw = str(row.get("검토 상태", "")).strip()
        items.append(
            {
                "student_id": str(row.get("학번", "")).strip(),
                "name": str(row.get("이름", "")).strip(),
                "file_name": str(row.get("원본파일명", "")).strip(),
                "form_name": str(row.get("양식", "")).strip(),
                "field_name": str(row.get("문항명", "")).strip(),
                "extracted_value": str(row.get("추출된 값", "")),
                "reason": str(row.get("의심 사유", "")).strip(),
                "source": str(row.get("검출 방식", "")).strip(),
                "status": "reviewed" if st_raw == "검토 완료" else "needs_review",
                "file_path": str(row.get("원본파일경로", "")).strip(),
            }
        )
    return items


def save_review_updates_to_excel(
    excel_path: Path | str,
    updated_items: List[Dict[str, Any]],
) -> None:
    """
    교사가 검수 다이얼로그에서 수정·확인한 결과를 엑셀의 본문 시트 및 [검토 필요] 시트에 반영합니다.
    """
    excel_path = Path(excel_path)
    if not excel_path.exists():
        raise FileNotFoundError(f"대상 엑셀 파일을 찾을 수 없습니다: {excel_path}")

    with pd.ExcelFile(excel_path) as xl:
        sheet_dfs: Dict[str, pd.DataFrame] = {
            sname: xl.parse(sname, dtype=str).fillna("")
            for sname in xl.sheet_names
        }

    # 파일별 잔여 미검토 건수 집계
    pending_by_file: Dict[str, int] = {}
    for it in updated_items:
        fname = it.get("file_name", "")
        if it.get("status") != "reviewed":
            pending_by_file[fname] = pending_by_file.get(fname, 0) + 1
        elif fname not in pending_by_file:
            pending_by_file[fname] = 0

    # 본문 시트 값 업데이트
    for sname, df in sheet_dfs.items():
        if sname in REVIEW_SHEET_ALIASES or sname in ("오류 파일", "결과 없음"):
            continue
        if "원본파일명" not in df.columns:
            continue

        for it in updated_items:
            fname = it.get("file_name", "")
            field_name = it.get("field_name", "")
            new_val = it.get("extracted_value", "")
            mask = df["원본파일명"] == fname
            if not mask.any():
                continue
            if field_name in df.columns:
                df.loc[mask, field_name] = sanitize_for_excel(new_val)

        # 누락 항목 및 검토 상태 재계산
        meta_cols = {"학번", "이름", "누락 항목", "원본파일명", "양식", REVIEW_STATUS_COL}
        q_cols = [c for c in df.columns if c not in meta_cols]
        for idx, row in df.iterrows():
            if "누락 항목" in df.columns:
                missing = [c for c in q_cols if not str(row.get(c, "")).strip()]
                df.at[idx, "누락 항목"] = ", ".join(missing)
            if REVIEW_STATUS_COL in df.columns:
                fname = str(row.get("원본파일명", ""))
                rem = pending_by_file.get(fname, 0)
                df.at[idx, REVIEW_STATUS_COL] = f"검토 필요 ({rem}건)" if rem > 0 else "검토 완료"

    # [검토_필요_상세] 시트 업데이트
    target_rev_sheet = next(
        (s for s in REVIEW_SHEET_ALIASES if s in sheet_dfs), None
    )
    if target_rev_sheet:
        rev_rows = [
            {
                "학번": it.get("student_id", ""),
                "이름": it.get("name", ""),
                "원본파일명": it.get("file_name", ""),
                "양식": it.get("form_name", ""),
                "문항명": it.get("field_name", ""),
                "추출된 값": sanitize_for_excel(it.get("extracted_value", "")),
                "의심 사유": it.get("reason", ""),
                "검출 방식": it.get("source", ""),
                "검토 상태": "검토 완료" if it.get("status") == "reviewed" else "검토 필요",
                "원본파일경로": it.get("file_path", ""),
            }
            for it in updated_items
        ]
        sheet_dfs[target_rev_sheet] = pd.DataFrame(rev_rows)

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        for sname, df in sheet_dfs.items():
            df.to_excel(writer, index=False, sheet_name=sname)
            ws = writer.sheets[sname]
            ws.freeze_panes = "A2"


# =============================================================================
# 6. 다중 포맷 통합 추출 메인 서비스 함수
# =============================================================================
def extract_documents_to_excel(
    doc_dir: Path,
    schema_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    지정된 폴더 내의 모든 .docx, .hwpx, .hwp, .pdf 파일을 스캔(임시 파일 ~$* 제외)하여
    포맷별 최적 파이프라인으로 추출하고, '검토 필요' 이중 탐지 결과와 함께 단일 엑셀로 저장합니다.
    """
    doc_dir = Path(doc_dir)
    if not doc_dir.exists() or not doc_dir.is_dir():
        raise FileNotFoundError(f"과제 문서 폴더를 찾을 수 없습니다: {doc_dir}")

    target_files = [
        f
        for f in sorted(doc_dir.iterdir(), key=lambda p: p.name.lower())
        if f.is_file()
        and not f.name.startswith("~$")
        and not f.name.startswith(".")
        and f.suffix.lower() in SUPPORTED_DOC_EXTS
    ]

    total_files = len(target_files)
    if total_files == 0:
        return {
            "total": 0,
            "success": 0,
            "failed": 0,
            "review_count": 0,
            "output_file": "",
            "records": [],
            "errors": [],
            "review_items": [],
        }

    schemas_catalog = load_all_schemas()
    forced_form_name: Optional[str] = None
    forced_schema: Optional[Dict[str, Any]] = None
    if schema_path is not None and Path(schema_path).exists():
        forced_form_name, forced_schema = load_single_schema(Path(schema_path))
        schemas_catalog[forced_form_name] = forced_schema

    llm_client = _get_ready_llm_client()
    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    all_review_items: List[Dict[str, Any]] = []

    for idx, file_path in enumerate(target_files, start=1):
        if stop_checker and stop_checker():
            break

        percent = int((idx / total_files) * 100)
        if progress_callback:
            progress_callback(percent, f"문서 추출 중 ({idx}/{total_files}): {file_path.name}")

        try:
            rec, rev_items = read_single_document(
                file_path,
                form_name=forced_form_name,
                schema=forced_schema,
                schemas_catalog=schemas_catalog,
                llm_client=llm_client,
                log_callback=log_callback,
            )
            records.append(rec)
            all_review_items.extend(rev_items)
        except Exception as exc:
            errors.append(
                {
                    "파일명": file_path.name,
                    "확장자": file_path.suffix.lower(),
                    "오류내용": str(exc),
                }
            )
            if log_callback:
                log_callback("WARN", f"[추출 실패] {file_path.name} -> 사유: {exc}")

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = doc_dir / f"과제취합_{ts}.xlsx"
    else:
        output_path = Path(output_path)

    final_saved_path = write_multi_doc_excel(
        records=records,
        errors=errors,
        review_items=all_review_items,
        output_path=output_path,
        schemas_catalog=schemas_catalog,
    )

    return {
        "total": total_files,
        "success": len(records),
        "failed": len(errors),
        "review_count": len(all_review_items),
        "output_file": str(final_saved_path.resolve()),
        "records": records,
        "errors": errors,
        "review_items": all_review_items,
    }



# =============================================================================
# 7. 일괄 스캔 PDF 명단별 분할 Vision OCR 추출
# =============================================================================
def get_pdf_page_count(pdf_path: Path | str) -> int:
    """PDF 파일의 총 페이지 수를 반환합니다."""
    p = Path(pdf_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {p}")
    if HAS_PYPDF and PdfReader is not None:
        return len(PdfReader(str(p)).pages)
    if HAS_PYMUPDF and fitz is not None:
        doc = fitz.open(str(p))
        try:
            return len(doc)
        finally:
            doc.close()
    raise RuntimeError("PDF 페이지 수를 읽으려면 pypdf 또는 PyMuPDF(fitz)가 필요합니다.")


def _render_pdf_page_range_to_png(pdf_path: Path, start_page: int, end_page: int) -> List[bytes]:
    """PyMuPDF(fitz)로 1-based [start_page, end_page] 범위만 PNG 바이트로 렌더링합니다."""
    if not HAS_PYMUPDF or fitz is None:
        return []
    images: List[bytes] = []
    doc = fitz.open(str(pdf_path))
    try:
        total = len(doc)
        for p_idx in range(max(0, start_page - 1), min(total, end_page)):
            pix = doc[p_idx].get_pixmap(dpi=150)
            images.append(pix.tobytes("png"))
    finally:
        doc.close()
    return images


def _slice_pdf_pages_to_bytes(pdf_path: Path, start_page: int, end_page: int) -> bytes:
    """pypdf로 1-based [start_page, end_page] 범위만 잘라낸 PDF 바이트를 반환합니다."""
    if not HAS_PYPDF or PdfReader is None or PdfWriter is None:
        raise RuntimeError("PDF 페이지 분할을 위해 pypdf 패키지가 필요합니다.")
    import io

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    total = len(reader.pages)
    for p_idx in range(max(0, start_page - 1), min(total, end_page)):
        writer.add_page(reader.pages[p_idx])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def extract_split_pdf_ocr_to_excel(
    pdf_path: Path | str,
    assignments: List[Dict[str, Any]],
    schema_path: Optional[Path | str] = None,
    output_path: Optional[Path | str] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    일괄 스캔 PDF를 명렬표 학생별 페이지 범위([start_page, end_page])로 분할하여
    Vision OCR을 수행하고, 배정 학생과 OCR 인식 학번/이름 불일치 시 자동으로 '검토 필요'를 부여합니다.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists() or not pdf_path.is_file():
        raise FileNotFoundError(f"스캔 PDF 파일을 찾을 수 없습니다: {pdf_path}")

    llm_client = _get_ready_llm_client()
    if llm_client is None:
        raise RuntimeError(
            "일괄 스캔 PDF Vision OCR을 실행하려면 AI API 키를 설정하고 '수동 프롬프트 모드'를 해제해 주세요."
        )

    schemas_catalog = load_all_schemas()
    if schema_path is not None and Path(schema_path).exists():
        form_name, schema = load_single_schema(Path(schema_path))
        schemas_catalog[form_name] = schema
    else:
        form_name = next(iter(schemas_catalog.keys()), "탐구보고서")
        schema = schemas_catalog[form_name]

    total_students = len(assignments)
    if total_students == 0:
        raise ValueError("OCR을 수행할 배정 학생이 없습니다.")

    sys_prompt, usr_prompt = _build_ai_extraction_prompts(
        schema=schema,
        form_name=form_name,
        raw_text=None,
        is_vision_ocr=True,
    )

    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    all_review_items: List[Dict[str, Any]] = []

    for idx, assign in enumerate(assignments, start=1):
        if stop_checker and stop_checker():
            break

        assigned_sid = clean_text(assign.get("student_id", ""))
        assigned_name = clean_text(assign.get("name", ""))
        start_page = int(assign.get("start_page", 1))
        end_page = int(assign.get("end_page", start_page))
        label_tag = f"{pdf_path.name} (p.{start_page}~{end_page})"

        pct = int((idx / total_students) * 100)
        if progress_callback:
            progress_callback(pct, f"분할 Vision OCR ({idx}/{total_students}): {assigned_sid} {assigned_name} (p.{start_page}~{end_page})")

        try:
            merged_ocr_sid = ""
            merged_ocr_name = ""
            merged_values: Dict[str, str] = {}
            merged_flags: Dict[str, Tuple[str, str]] = {}

            page_images = _render_pdf_page_range_to_png(pdf_path, start_page, end_page)
            if page_images:
                for p_offset, img_bytes in enumerate(page_images):
                    cur_page = start_page + p_offset
                    p_prompt = usr_prompt + f"\n(현재 페이지: p.{cur_page}, 배정 학생 참고: {assigned_sid} {assigned_name})"
                    resp = llm_client.generate(
                        prompt=p_prompt,
                        system_prompt=sys_prompt,
                        image_bytes=img_bytes,
                        mime_type="image/png",
                    )
                    o_sid, o_name, f_vals, a_flags = _parse_ai_json_to_fields(resp, schema)
                    if o_sid and not merged_ocr_sid:
                        merged_ocr_sid = o_sid
                    if o_name and not merged_ocr_name:
                        merged_ocr_name = o_name
                    for fname, val in f_vals.items():
                        if not val:
                            continue
                        if merged_values.get(fname):
                            merged_values[fname] += "\n" + val
                        else:
                            merged_values[fname] = val
                        st, rsn = a_flags.get(fname, ("ok", ""))
                        prev_st, prev_rsn = merged_flags.get(fname, ("ok", ""))
                        if st == "needs_review" or prev_st == "needs_review":
                            comb = " / ".join(x for x in (prev_rsn, rsn) if x)
                            merged_flags[fname] = ("needs_review", comb)
                        else:
                            merged_flags[fname] = ("ok", "")
            elif llm_client.provider == "gemini":
                sliced_pdf_bytes = _slice_pdf_pages_to_bytes(pdf_path, start_page, end_page)
                p_prompt = usr_prompt + f"\n(대상 페이지 범위: p.{start_page}~{end_page})"
                resp = llm_client.generate(
                    prompt=p_prompt,
                    system_prompt=sys_prompt,
                    image_bytes=sliced_pdf_bytes,
                    mime_type="application/pdf",
                )
                merged_ocr_sid, merged_ocr_name, merged_values, merged_flags = _parse_ai_json_to_fields(resp, schema)
            else:
                raise RuntimeError(
                    "분할 스캔 PDF Vision OCR을 위해 PyMuPDF(fitz)를 설치하거나 AI 공급자를 Gemini로 설정해 주세요."
                )

            rec, rev_items = _build_record_and_review_items(
                file_path=pdf_path,
                form_name=form_name,
                schema=schema,
                student_id=assigned_sid or merged_ocr_sid,
                name=assigned_name or merged_ocr_name,
                field_values=merged_values,
                ai_flags=merged_flags,
                raw_text_preview=(
                    f"[스캔 PDF 분할 범위] p.{start_page}~{end_page}\n"
                    f"[명렬표 배정] {assigned_sid} {assigned_name}\n"
                    f"[OCR 인식 결과] {merged_ocr_sid or '(미인식)'} {merged_ocr_name or '(미인식)'}\n"
                    f"(원본 이미지는 [원본 파일 열기]로 확인 가능)"
                ),
            )
            rec["원본파일명"] = label_tag
            for it in rev_items:
                it["file_name"] = label_tag

            # 배정 학생의 학번/이름과 OCR 인식 결과 불일치 시 자동 '검토 필요' 부여
            mismatch_reasons: List[str] = []
            if assigned_sid and merged_ocr_sid and assigned_sid != merged_ocr_sid:
                mismatch_reasons.append(f"배정 학번({assigned_sid})과 OCR 인식 학번({merged_ocr_sid}) 불일치")
            if assigned_name and merged_ocr_name and assigned_name != merged_ocr_name:
                mismatch_reasons.append(f"배정 이름({assigned_name})과 OCR 인식 이름({merged_ocr_name}) 불일치")

            if mismatch_reasons:
                rev_items.insert(
                    0,
                    {
                        "student_id": assigned_sid,
                        "name": assigned_name,
                        "file_name": label_tag,
                        "file_path": str(pdf_path.resolve()),
                        "form_name": form_name,
                        "field_name": "학번/이름 대조",
                        "extracted_value": f"{assigned_sid} {assigned_name}".strip(),
                        "status": "needs_review",
                        "reason": " / ".join(mismatch_reasons),
                        "source": "명렬표 대조",
                        "raw_text_preview": (
                            f"[스캔 PDF 분할 범위] p.{start_page}~{end_page}\n"
                            f"[명렬표 배정] {assigned_sid} {assigned_name}\n"
                            f"[OCR 인식 결과] {merged_ocr_sid} {merged_ocr_name}"
                        ),
                    },
                )
                rec[REVIEW_STATUS_COL] = f"검토 필요 ({len(rev_items)}건)"

            records.append(rec)
            all_review_items.extend(rev_items)

        except Exception as exc:
            errors.append(
                {
                    "파일명": label_tag,
                    "학번": assigned_sid,
                    "이름": assigned_name,
                    "오류내용": str(exc),
                }
            )
            if log_callback:
                log_callback("WARN", f"[분할 OCR 실패] {assigned_sid} {assigned_name} (p.{start_page}~{end_page}) -> {exc}")

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = pdf_path.parent / f"스캔분할OCR_{pdf_path.stem}_{ts}.xlsx"
    else:
        output_path = Path(output_path)

    final_saved_path = write_multi_doc_excel(
        records=records,
        errors=errors,
        review_items=all_review_items,
        output_path=output_path,
        schemas_catalog=schemas_catalog,
    )

    return {
        "total": total_students,
        "success": len(records),
        "failed": len(errors),
        "review_count": len(all_review_items),
        "output_file": str(final_saved_path.resolve()),
        "records": records,
        "errors": errors,
        "review_items": all_review_items,
    }
