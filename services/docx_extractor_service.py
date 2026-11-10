"""
학생들이 제출한 DOCX 과제 문서에서 학번, 이름, 문항별 응답을 추출하여
구조화된 단일 Excel(.xlsx) 파일로 변환하는 비즈니스 로직 서비스 모듈.
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zipfile import ZipFile

import pandas as pd

# OpenXML 네임스페이스
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

EXCEL_CELL_LIMIT = 32000  # 엑셀 셀 최대 글자 수(32767) 여유분
MIN_DETECT_SCORE = 2      # 양식 판별에 필요한 고유 항목 일치 개수
NAME_FIELD = "학번 및 이름"

# 기본 내장 양식
BUILTIN_FORM_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "탐구보고서": {
        "detect_keywords": ["탐구결과정리", "탐구결과분석", "결론도출", "고찰"],
        "fields": {
            "학번 및 이름": ["학번및이름", "학번이름"],
            "탐구 주제": ["탐구주제", "연구주제"],
            "탐구 과정": ["탐구과정", "실험과정", "탐구과정서술하기"],
            "탐구 결과 정리": ["탐구결과정리", "측정결과", "탐구결과"],
            "탐구 결과 분석": ["탐구결과분석", "결과분석"],
            "결론 도출": ["결론도출", "결론", "결론및가설검증"],
            "고찰": ["고찰", "느낀점", "탐구후느낀점"],
        },
    },
    "탐구계획서": {
        "detect_keywords": [
            "탐구주제선정이유", "가설설정", "조작변인", "통제변인", "종속변인",
        ],
        "fields": {
            "학번 및 이름": ["학번및이름", "학번이름"],
            "관련 과학 주제": ["관련과학주제", "관련과학분야", "과학분야"],
            "탐구 주제": ["탐구주제", "연구주제"],
            "탐구 주제 선정 이유": [
                "탐구주제선정이유", "주제선정이유", "탐구동기", "연구동기",
            ],
            "가설 설정": ["가설설정", "가설"],
            "조작변인": ["조작변인", "독립변인"],
            "통제변인": ["통제변인"],
            "종속변인": ["종속변인"],
            "탐구 과정 서술하기": [
                "탐구과정서술하기", "탐구과정", "실험과정", "탐구방법",
            ],
        },
    },
}


# =============================================================================
# 1. 텍스트 정제 및 학번/이름 분리 유틸
# =============================================================================
def clean_text(text: Any) -> str:
    """줄바꿈과 연속 공백을 정리합니다."""
    return " ".join(str(text).replace("\xa0", " ").split())


def normalize_heading(text: Any) -> str:
    """
    항목명 비교용 정규화.
    '탐구 주제\\n(의문문의 형태로 제기)' -> '탐구주제'
    """
    cleaned = clean_text(text)
    cleaned = re.sub(r"[\(（].*?[\)）]", "", cleaned)
    cleaned = re.sub(r"[\s:：.\-\[\]·•]", "", cleaned)
    return cleaned


def sanitize_for_excel(value: Any) -> Any:
    """엑셀에 쓸 수 없는 제어문자 제거 + 글자 수 제한."""
    if not isinstance(value, str):
        return value
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    if len(value) > EXCEL_CELL_LIMIT:
        value = value[:EXCEL_CELL_LIMIT] + "\n...(글자 수 제한으로 잘림)"
    return value


def split_student_number_and_name(value: Any) -> Tuple[str, str]:
    """
    다양한 형태의 학번/이름 문자열을 분리합니다.
    '10702 고은혁' / '10801곽대현' / '10904/김민정' / '유은정 10716'
    """
    value = clean_text(value)
    if not value:
        return "", ""

    # '이름:' 앞의 내용은 학번 열에 보존
    match = re.fullmatch(r"(.*?)\s*이름\s*[:：]\s*(.*)", value)
    if match:
        number = match.group(1).strip()
        name = match.group(2).strip()
        return number, name

    value = clean_text(value.replace("|", " ").replace("/", " "))

    match = re.match(r"^(\d{5,6})\s*([가-힣A-Za-z]+)$", value)
    if match:
        return match.group(1), match.group(2)

    match = re.match(r"^([가-힣A-Za-z]+)\s*(\d{5,6})$", value)
    if match:
        return match.group(2), match.group(1)

    number_match = re.search(r"\d{5,6}", value)
    if number_match:
        number = number_match.group()
        name = re.sub(r"[^가-힣A-Za-z]", "", value.replace(number, ""))
        return number, name

    return "", re.sub(r"[^가-힣A-Za-z]", "", value)


# =============================================================================
# 2. DOCX XML 저수준 파싱 유틸
# =============================================================================
def get_paragraph_text(paragraph: ET.Element) -> str:
    """문단 하나의 텍스트 (탭/줄바꿈은 공백으로)."""
    parts = []
    for node in paragraph.iter():
        tag = node.tag.split("}")[-1]
        if tag == "t" and node.text:
            parts.append(node.text)
        elif tag in ("tab", "br"):
            parts.append(" ")
    return clean_text("".join(parts))


def get_direct_cell_text(cell: ET.Element) -> str:
    """셀 바로 아래 문단의 텍스트만 (내부 표 제외). 문단 줄바꿈은 유지."""
    lines = []
    for paragraph in cell.findall("./w:p", NS):
        text = get_paragraph_text(paragraph)
        if text:
            lines.append(text)
    return "\n".join(lines)


def get_label_text(cell: ET.Element) -> str:
    """항목명 셀은 줄바꿈을 공백으로 합쳐서 반환."""
    return clean_text(get_direct_cell_text(cell))


def table_to_text(table: ET.Element) -> str:
    """내부 표를 ' | ' 구분 텍스트로 변환."""
    lines = []
    for row in table.findall("./w:tr", NS):
        values = [
            clean_text(get_direct_cell_text(cell))
            for cell in row.findall("./w:tc", NS)
        ]
        if any(values):
            lines.append(" | ".join(values))
    return "\n".join(lines)


def get_cell_text_with_nested_tables(cell: ET.Element) -> str:
    """셀의 직접 텍스트 + 내부 표 텍스트."""
    parts = []
    direct = get_direct_cell_text(cell)
    if direct:
        parts.append(direct)
    for nested in cell.findall("./w:tbl", NS):
        nested_text = table_to_text(nested)
        if nested_text:
            parts.append(nested_text)
    return "\n\n".join(parts).strip()


def count_images(cell: ET.Element) -> int:
    """셀 안의 그림 개수 (텍스트로는 추출 불가하므로 표시용)."""
    return len(cell.findall(".//w:drawing", NS)) + len(cell.findall(".//w:pict", NS))


def find_field_name(schema: Dict[str, Any], label: str) -> Optional[str]:
    key = normalize_heading(label)
    if not key:
        return None
    for field_name, aliases in schema["fields"].items():
        if key in {normalize_heading(a) for a in aliases}:
            return field_name
    return None


def add_value(record: Dict[str, Any], field_name: str, value: str) -> None:
    """같은 필드에 여러 번 값이 들어오면 이어 붙입니다."""
    if not value:
        return
    if record.get(field_name):
        record[field_name] += "\n\n" + value
    else:
        record[field_name] = value


# =============================================================================
# 3. 양식 로드 및 자동 판별
# =============================================================================
def load_single_schema(json_path: Path) -> Tuple[str, Dict[str, Any]]:
    """단일 JSON 파일에서 양식 이름과 스키마 딕셔너리를 로드합니다."""
    json_path = Path(json_path)
    with json_path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("JSON 최상위 데이터는 객체여야 합니다.")

    form_name = data.get("form_name")
    schema = data.get("schema")

    if not isinstance(form_name, str) or not form_name.strip():
        form_name = json_path.stem

    form_name = form_name.strip()

    if not isinstance(schema, dict):
        # 만약 data 자체가 fields를 담고 있는 경우 처리
        if "fields" in data:
            schema = data
        else:
            raise ValueError("JSON 내에 schema 객체가 없습니다.")

    return form_name, schema


def load_all_schemas(schemas_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """내장 양식과 schemas/ 폴더의 모든 양식을 합쳐서 로드합니다."""
    loaded = dict(BUILTIN_FORM_SCHEMAS)
    if schemas_dir is None:
        schemas_dir = Path("schemas")

    if schemas_dir.exists():
        for json_path in sorted(schemas_dir.glob("*.json")):
            try:
                name, schema = load_single_schema(json_path)
                loaded[name] = schema
            except Exception:
                continue

    return loaded


def collect_label_candidates(root: ET.Element) -> List[str]:
    """표의 항목명 셀과 본문 문단에서 양식 판별 후보를 수집합니다."""
    labels = []
    for table in root.findall("./w:body/w:tbl", NS):
        for row in table.findall("./w:tr", NS):
            cells = row.findall("./w:tc", NS)
            if cells:
                text = get_label_text(cells[0])
                if text:
                    labels.append(text)
            if len(cells) >= 4 and len(cells) % 2 == 0:
                for cell in cells[2::2]:
                    text = get_label_text(cell)
                    if text:
                        labels.append(text)

    for paragraph in root.findall("./w:body/w:p", NS):
        text = get_paragraph_text(paragraph)
        if text:
            labels.append(text)

    return labels


def detect_form(labels: List[str], schemas: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], Dict[str, int]]:
    normalized = {normalize_heading(label) for label in labels if label}
    scores = {}

    for form_name, schema in schemas.items():
        detect_keywords = schema.get("detect_keywords", [])
        if not detect_keywords:
            detect_keywords = list(schema.get("fields", {}).keys())
        keywords = {normalize_heading(k) for k in detect_keywords}
        scores[form_name] = len(normalized & keywords)

    if not scores:
        return None, {}

    best_form = max(scores, key=lambda form: scores[form])
    best_score = scores[best_form]

    if best_score < MIN_DETECT_SCORE:
        return None, scores

    if list(scores.values()).count(best_score) > 1:
        return None, scores

    return best_form, scores


# =============================================================================
# 4. 레이아웃별 파싱 로직
# =============================================================================
def parse_table_layout(root: ET.Element, schema: Dict[str, Any], record: Dict[str, Any]) -> Set[str]:
    found = set()
    for table in root.findall("./w:body/w:tbl", NS):
        for row in table.findall("./w:tr", NS):
            cells = row.findall("./w:tc", NS)
            if len(cells) < 2:
                continue

            field_name = find_field_name(schema, get_label_text(cells[0]))
            if not field_name:
                continue

            parts = []
            image_total = 0
            for cell in cells[1:]:
                text = get_cell_text_with_nested_tables(cell)
                if text:
                    parts.append(text)
                image_total += count_images(cell)

            value = "\n".join(parts).strip()
            if image_total:
                value = (value + f"\n[그림 {image_total}개 포함: 원본 확인 필요]").strip()

            if field_name == NAME_FIELD:
                number, name = split_student_number_and_name(value)
                record["학번"], record["이름"] = number, name
            else:
                add_value(record, field_name, value)

            found.add(field_name)

    return found


def parse_paragraph_layout(root: ET.Element, schema: Dict[str, Any], record: Dict[str, Any]) -> Set[str]:
    body = root.find("./w:body", NS)
    if body is None:
        return set()

    sections: Dict[str, List[str]] = {}
    current = None

    for paragraph in body.findall("./w:p", NS):
        text = get_paragraph_text(paragraph)
        if not text:
            continue

        field_name = find_field_name(schema, text) if len(text) <= 60 else None
        if field_name:
            current = field_name
            sections.setdefault(current, [])
        elif current:
            sections[current].append(text)

    found = set()
    for field_name, lines in sections.items():
        value = "\n".join(lines).strip()
        if not value:
            continue

        if field_name == NAME_FIELD:
            number, name = split_student_number_and_name(value)
            record["학번"], record["이름"] = number, name
        else:
            add_value(record, field_name, value)

        found.add(field_name)

    return found


def parse_generated_layout(root: ET.Element, schema: Dict[str, Any], record: Dict[str, Any]) -> Set[str]:
    body = root.find("./w:body", NS)
    if body is None:
        return set()

    found = set()
    adjacent_labels: Dict[str, str] = {}
    question_labels: Dict[str, str] = {}

    for rule in schema.get("extraction", []):
        field_name = rule["field"]
        aliases = list(schema["fields"].get(field_name, []))
        if rule.get("question"):
            aliases.append(rule["question"])

        if rule.get("reader") == "adjacent_cell":
            target = adjacent_labels
        elif rule.get("reader") == "question_then_table":
            target = question_labels
        else:
            continue

        for alias in aliases:
            key = normalize_heading(alias)
            if key:
                target[key] = field_name

    def save_value(field_name: str, value: str):
        if field_name == NAME_FIELD:
            number, name = split_student_number_and_name(value)
            if number:
                record["학번"] = number
            if name:
                record["이름"] = name
        else:
            add_value(record, field_name, value)
        found.add(field_name)

    def read_cells(cells: List[ET.Element]) -> str:
        parts = []
        image_total = 0
        for cell in cells:
            text = get_cell_text_with_nested_tables(cell)
            if text:
                parts.append(text)
            image_total += count_images(cell)

        value = "\n".join(parts).strip()
        if image_total:
            value = (value + f"\n[그림 {image_total}개 포함: 원본 확인 필요]").strip()
        return value

    pending_question = None
    pending_table_index = None
    outside_answers = []
    blocks = list(body)

    for block_index, block in enumerate(blocks):
        tag = block.tag.split("}")[-1]

        if tag == "p":
            text = get_paragraph_text(block)
            if not text:
                continue

            field_name = question_labels.get(normalize_heading(text))
            if field_name:
                pending_question = field_name
                pending_table_index = None
                outside_answers = []

                offset_rules = [
                    rule for rule in schema.get("extraction", [])
                    if rule["field"] == field_name
                    and rule["reader"] == "question_then_table"
                    and rule.get("answer_table_offset") is not None
                ]

                if offset_rules:
                    offset = offset_rules[0]["answer_table_offset"]
                    target_index = block_index + offset
                    if target_index < len(blocks) and blocks[target_index].tag.split("}")[-1] == "tbl":
                        pending_table_index = target_index

            elif re.match(r"^Q\s*\d+\s*[.．:：)\-]", text, flags=re.IGNORECASE):
                pending_question = None
                pending_table_index = None
                outside_answers = []

            elif pending_question == "Q2":
                label_pattern = r"(?:인물\s*이름|직업\s*[/／]\s*역할)\s*[:：]"
                if re.search(label_pattern, text):
                    entered_text = re.sub(label_pattern, "", text)
                    if re.search(r"\w", entered_text):
                        outside_answers.append(text)
            continue

        if tag != "tbl":
            continue

        if pending_question is not None:
            if pending_table_index is not None and block_index != pending_table_index:
                continue

            cells = [
                cell for row in block.findall("./w:tr", NS)
                for cell in row.findall("./w:tc", NS)
            ]
            table_answer = read_cells(cells)
            answer_parts = outside_answers.copy()
            if table_answer:
                answer_parts.append(table_answer)

            value = "\n".join(answer_parts).strip()
            save_value(pending_question, value)

            pending_question = None
            pending_table_index = None
            outside_answers = []
            continue

        for row in block.findall("./w:tr", NS):
            cells = row.findall("./w:tc", NS)
            cell_index = 0
            while cell_index < len(cells) - 1:
                label = get_label_text(cells[cell_index])
                field_name = adjacent_labels.get(normalize_heading(label))
                if field_name:
                    value = read_cells([cells[cell_index + 1]])
                    save_value(field_name, value)
                    cell_index += 2
                else:
                    cell_index += 1

    return found


# =============================================================================
# 5. 개별 DOCX 파일 파싱
# =============================================================================
def read_docx(
    docx_path: Path,
    *,
    form_name: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
    schemas_catalog: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """단일 DOCX 파일에서 학번, 이름, 문항별 응답을 읽어 딕셔너리로 반환합니다."""
    docx_path = Path(docx_path)
    with ZipFile(docx_path) as docx_zip:
        xml_data = docx_zip.read("word/document.xml")

    root = ET.fromstring(xml_data)

    if schemas_catalog is None:
        schemas_catalog = load_all_schemas()

    if schema is None:
        if form_name and form_name in schemas_catalog:
            schema = schemas_catalog[form_name]
        else:
            detected_name, scores = detect_form(collect_label_candidates(root), schemas_catalog)
            if not detected_name:
                raise ValueError(f"문서 양식을 자동 판별하지 못했습니다 (일치 점수: {scores}).")
            form_name = detected_name
            schema = schemas_catalog[detected_name]
    else:
        if not form_name:
            form_name = schema.get("form_name", "지정 양식")

    record: Dict[str, Any] = {
        "학번": "",
        "이름": "",
        "원본파일명": docx_path.name,
        "양식": form_name,
    }
    for field_name in schema.get("fields", {}):
        if field_name != NAME_FIELD:
            record[field_name] = ""

    if schema.get("extraction"):
        found = parse_generated_layout(root, schema, record)
    else:
        found = parse_table_layout(root, schema, record)
        expected = set(schema.get("fields", {}))
        if found != expected:
            tmp: Dict[str, Any] = {
                field_name: ""
                for field_name in schema.get("fields", {})
                if field_name != NAME_FIELD
            }
            tmp["학번"] = ""
            tmp["이름"] = ""

            fallback = parse_paragraph_layout(root, schema, tmp)
            for field_name in fallback - found:
                if field_name == NAME_FIELD:
                    record["학번"] = tmp["학번"]
                    record["이름"] = tmp["이름"]
                else:
                    record[field_name] = tmp[field_name]
            found |= fallback

    if not found:
        raise ValueError("양식은 판별했지만 입력 항목을 읽지 못했습니다.")

    # 파일명 보조 추출
    if not record["이름"]:
        match = re.search(r"제출-([^-]+?)\s*-\s*", docx_path.name)
        if match:
            record["이름"] = clean_text(match.group(1))

    # 파일명에서 학번/이름 추가 보조 추출
    if not record["학번"] or not record["이름"]:
        num, name = split_student_number_and_name(docx_path.stem)
        if not record["학번"] and num:
            record["학번"] = num
        if not record["이름"] and name:
            record["이름"] = name

    missing = [
        f for f in schema.get("fields", {})
        if f != NAME_FIELD and not record.get(f)
    ]
    record["누락 항목"] = ", ".join(missing)
    return record


# =============================================================================
# 6. 엑셀 저장
# =============================================================================
def build_final_columns(schema: Dict[str, Any]) -> List[str]:
    fields = [f for f in schema.get("fields", {}) if f != NAME_FIELD]
    return ["학번", "이름"] + fields + ["누락 항목", "원본파일명", "양식"]


def write_excel_records(
    records: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    output_path: Path,
    schemas_catalog: Dict[str, Dict[str, Any]],
) -> Path:
    """추출된 레코드들과 오류 목록을 깔끔한 엑셀 파일로 저장합니다."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # PermissionError 방지: 파일이 열려있으면 대체 이름 생성
    target_path = output_path
    max_retries = 5
    writer = None

    for attempt in range(max_retries):
        try:
            writer = pd.ExcelWriter(target_path, engine="openpyxl")
            break
        except PermissionError:
            if attempt < max_retries - 1:
                target_path = output_path.parent / f"{output_path.stem}_대체_{attempt+1}{output_path.suffix}"
            else:
                raise PermissionError(
                    f"엑셀 파일 쓰기 권한이 없습니다. 파일이 엑셀에서 열려있는지 확인하세요: {output_path.name}"
                )

    used_sheet_names: Set[str] = {"오류 파일"}

    with writer:
        # 양식별 시트 분리 작성
        all_forms = {r["양식"] for r in records if "양식" in r}
        for form_name in all_forms:
            rows = [r for r in records if r.get("양식") == form_name]
            if not rows:
                continue

            schema = schemas_catalog.get(form_name, {})
            columns = build_final_columns(schema) if schema else list(rows[0].keys())

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

            # 스타일링
            ws = writer.sheets[sheet_name]
            ws.freeze_panes = "A2"
            for col_cells in ws.columns:
                longest = max((len(str(c.value or "")) for c in col_cells), default=0)
                col_letter = col_cells[0].column_letter
                ws.column_dimensions[col_letter].width = min(max(longest + 2, 10), 50)

        if errors:
            error_df = pd.DataFrame(errors).map(sanitize_for_excel)
            error_df.to_excel(writer, index=False, sheet_name="오류 파일")
            ws_err = writer.sheets["오류 파일"]
            ws_err.freeze_panes = "A2"

        if not records and not errors:
            pd.DataFrame({"안내": ["처리 결과 없음"]}).to_excel(writer, index=False, sheet_name="결과 없음")

    return target_path


# =============================================================================
# 7. 전체 추출 메인 서비스 함수
# =============================================================================
def extract_docx_to_excel(
    docx_dir: Path,
    schema_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    지정된 디렉토리 내의 모든 DOCX 파일을 파싱하여 1개의 엑셀 파일로 병합합니다.

    :param docx_dir: 학생 DOCX 파일들이 있는 폴더
    :param schema_path: 특정 JSON 양식 파일 경로 (None이면 자동 판별)
    :param output_path: 저장할 엑셀 파일 경로 (None이면 docx_dir에 타임스탬프 파일명 자동 생성)
    :param progress_callback: (진행률 0~100, 상태 메시지) 콜백 함수
    :param stop_checker: () -> bool 사용자 작업 중단 요청 확인 함수
    :return: 처리 결과 요약 딕셔너리
    """
    docx_dir = Path(docx_dir)
    if not docx_dir.exists() or not docx_dir.is_dir():
        raise FileNotFoundError(f"DOCX 디렉토리를 찾을 수 없습니다: {docx_dir}")

    docx_files = [
        f for f in sorted(docx_dir.glob("*.docx"))
        if not f.name.startswith("~$") and not f.name.startswith(".")
    ]

    total_files = len(docx_files)
    if total_files == 0:
        return {
            "total": 0,
            "success": 0,
            "failed": 0,
            "output_file": "",
            "records": [],
            "errors": [],
        }

    # 양식 준비
    schemas_catalog = load_all_schemas()
    forced_form_name = None
    forced_schema = None

    if schema_path is not None and Path(schema_path).exists():
        forced_form_name, forced_schema = load_single_schema(Path(schema_path))
        schemas_catalog[forced_form_name] = forced_schema

    records = []
    errors = []

    for idx, docx_path in enumerate(docx_files, start=1):
        if stop_checker and stop_checker():
            break

        percent = int((idx / total_files) * 100)
        if progress_callback:
            progress_callback(percent, f"DOCX 파싱 중 ({idx}/{total_files}): {docx_path.name}")

        try:
            rec = read_docx(
                docx_path,
                form_name=forced_form_name,
                schema=forced_schema,
                schemas_catalog=schemas_catalog,
            )
            records.append(rec)
        except Exception as exc:
            errors.append({
                "파일명": docx_path.name,
                "오류내용": str(exc),
            })

    # 출력 경로 결정
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = docx_dir / f"과제취합_{ts}.xlsx"
    else:
        output_path = Path(output_path)

    final_saved_path = write_excel_records(records, errors, output_path, schemas_catalog)

    return {
        "total": total_files,
        "success": len(records),
        "failed": len(errors),
        "output_file": str(final_saved_path.resolve()),
        "records": records,
        "errors": errors,
    }


# =============================================================================
# 8. 새 DOCX 서식 양식 자동 분석 및 스키마 JSON 생성 (generate_docx_schema)
# =============================================================================
QUESTION_REGEX = re.compile(r"^Q\s*(\d+)\s*[.．:：)\-]\s*(.*)", re.IGNORECASE)


def field_title_from_label(label: str) -> str:
    compact = re.sub(r"[\s/·및]", "", label)
    if compact in ("학번이름", "이름학번"):
        return NAME_FIELD
    return clean_text(label)


def analyze_docx_schema(docx_path: Path | str, form_name: Optional[str] = None) -> Dict[str, Any]:
    """
    빈 DOCX 양식 파일을 분석하여 항목(질문)과 답변 위치 구조를 추출하고 스키마 초안을 생성합니다.
    """
    docx_path = Path(docx_path)
    if not docx_path.exists() or not docx_path.is_file():
        raise FileNotFoundError(f"DOCX 파일을 찾을 수 없습니다: {docx_path}")

    with ZipFile(docx_path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))

    body = root.find("./w:body", NS)
    if body is None:
        raise ValueError("문서 본문(w:body)을 찾을 수 없습니다.")

    blocks = list(body)
    fields: Dict[str, List[str]] = {}
    locations: List[Dict[str, Any]] = []
    warnings: List[str] = []
    structure: List[Dict[str, Any]] = []
    pending = None
    seen_questions = set()

    def add_field(title: str, alias: str, location: Dict[str, Any]):
        if title not in fields:
            fields[title] = []
        if alias not in fields[title]:
            fields[title].append(alias)
        loc = dict(field=title, **location)
        locations.append(loc)

    for index, block in enumerate(blocks):
        tag = block.tag.split("}")[-1]

        if tag == "p":
            text = get_paragraph_text(block)
            if not text:
                continue
            structure.append({"block_index": index, "type": "paragraph", "text": text})

            match = QUESTION_REGEX.match(text)
            if match:
                if pending:
                    warnings.append(f"{pending['field']}: 다음 질문 전까지 답변 표를 찾지 못했습니다.")
                title = "Q" + str(int(match.group(1)))
                if title in seen_questions:
                    warnings.append(f"{title}: 질문 번호가 중복됩니다. 위치를 확인하세요.")
                seen_questions.add(title)
                pending = {
                    "field": title,
                    "question": text,
                    "question_block": index,
                    "between_paragraph_blocks": [],
                }
                fields.setdefault(title, [text])
            elif pending:
                pending["between_paragraph_blocks"].append(index)
            continue

        if tag != "tbl":
            continue

        table_rows = [
            [get_label_text(cell) for cell in row.findall("./w:tc", NS)]
            for row in block.findall("./w:tr", NS)
        ]
        structure.append({"block_index": index, "type": "table", "rows": table_rows})

        if block.findall(".//w:tbl", NS):
            warnings.append(f"블록 {index}: 중첩 표가 있습니다. 추출 위치 확인 필요.")

        if pending:
            # 질문 뒤에 이어지는 첫 표를 답변 표 후보로 지정
            add_field(
                pending["field"],
                pending["question"],
                {
                    "reader": "question_then_table",
                    "question": pending["question"],
                    "question_block": pending["question_block"],
                    "answer_table_block": index,
                    "between_paragraph_blocks": pending["between_paragraph_blocks"],
                    "needs_review": True,
                },
            )
            pending = None
            continue

        for row_index, row in enumerate(table_rows):
            if len(row) < 2:
                continue
            if len(row) % 2:
                warnings.append(f"블록 {index}, 행 {row_index}: 홀수 개 셀이라 자동 항목 추출을 보류했습니다.")
                continue

            for cell_index in range(0, len(row), 2):
                label = row[cell_index]
                if not label or len(label) > 80:
                    continue
                title = field_title_from_label(label)
                add_field(
                    title,
                    label,
                    {
                        "reader": "adjacent_cell",
                        "table_block": index,
                        "row_index": row_index,
                        "label_cell_index": cell_index,
                        "answer_cell_index": cell_index + 1,
                        "needs_review": True,
                    },
                )

    if pending:
        warnings.append(f"{pending['field']}: 뒤따르는 답변 표가 없습니다.")

    if not fields:
        warnings.append("문서에서 항목을 찾지 못했습니다. 번호(Q1~Q8)나 표 양식인지 확인해 주세요.")

    candidates = []
    for title, aliases in fields.items():
        if title == NAME_FIELD:
            continue
        candidate = normalize_heading(aliases[0])
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    schema = {
        "detect_keywords": candidates[:4],
        "fields": fields,
        "extraction": locations,
    }

    resolved_form_name = (form_name or docx_path.stem).strip()

    return {
        "format_version": 1,
        "source_file": docx_path.name,
        "form_name": resolved_form_name,
        "schema": schema,
        "warnings": list(dict.fromkeys(warnings)),
        "structure": structure,
    }


def save_schema_json(
    schema_report: Dict[str, Any],
    schemas_dir: Optional[Path | str] = None,
    overwrite: bool = True,
) -> Path:
    """분석된 스키마 데이터를 schemas/ 디렉토리에 JSON 파일로 저장합니다."""
    if schemas_dir is None:
        schemas_dir = Path("schemas")
    else:
        schemas_dir = Path(schemas_dir)

    schemas_dir.mkdir(parents=True, exist_ok=True)

    form_name = schema_report.get("form_name", "새양식").strip()
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", form_name)
    target_path = schemas_dir / f"{safe_name}.json"

    if not overwrite and target_path.exists():
        serial = 2
        while target_path.exists():
            target_path = schemas_dir / f"{safe_name}_{serial}.json"
            serial += 1

    with target_path.open("w", encoding="utf-8") as f:
        json.dump(schema_report, f, ensure_ascii=False, indent=2)

    return target_path
