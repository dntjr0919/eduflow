"""
여러 개의 엑셀 과제 파일(.xlsx)을 '학번' 기준으로 가로 결합(Column Merge)하는 비즈니스 로직 서비스 모듈.
- 학번(숫자 4~5자리) 절대 1순위 고유 키(Key) 매칭
- Full Outer Join (특정 과제 미제출 학생도 누락 없이 포함)
- 열 이름 중복 시에만 [파일명] 접두사 부여
- 메인 데이터 시트('통합과제') 및 검토/오류 시트('검토_및_오류') 생성
- PermissionError 방지 safe_save_excel 로직
"""

from collections import Counter
from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

from services.docx_extractor_service import EXCEL_CELL_LIMIT, sanitize_for_excel

# 병합 시 자동으로 제외할 시스템 메타데이터 컬럼명 목록 (소문자/공백정제 기준)
SYSTEM_METADATA_COLUMNS = {
    "누락항목", "누락 항목", "양식", "원본파일명", "생성된_과세특", "과세특_바이트",
    "생성된과세특", "과세특바이트", "오류내용", "사유", "비고_시스템",
}


def clean_header_name(header: Any) -> str:
    """헤더 텍스트의 불필요한 줄바꿈 및 연속 공백을 정리합니다."""
    if header is None:
        return ""
    return " ".join(str(header).replace("\xa0", " ").split()).strip()


def normalize_student_id(val: Any) -> str:
    """
    다양한 형식(정수, 부동소수점, 문자열)의 학번 값을 숫자 문자열(예: '10101')로 정규화합니다.
    유효한 4~6자리 숫자가 없으면 빈 문자열을 반환합니다.
    """
    if val is None or pd.isna(val):
        return ""

    if isinstance(val, (int, float)):
        try:
            val_int = int(val)
            val_str = str(val_int)
            if 1000 <= val_int <= 999999:
                return val_str
        except (ValueError, OverflowError):
            pass

    s = str(val).strip()
    # 20101.0 형태 처리
    if s.endswith(".0"):
        s = s[:-2].strip()

    # 순수 4~6자리 숫자
    if re.fullmatch(r"\d{4,6}", s):
        return s

    # 문자열 내 4~6자리 연속 숫자 탐색
    m = re.search(r"(?<!\d)(\d{4,6})(?!\d)", s)
    if m:
        return m.group(1)

    return ""


def identify_student_columns(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    """
    DataFrame에서 학번 열과 이름 열의 컬럼명을 식별합니다.
    식별하지 못한 경우 (첫 번째 열, 두 번째 열)을 기본으로 반환합니다.
    """
    cols = list(df.columns)
    if not cols:
        return None, None

    id_col = None
    name_col = None

    # 1. 컬럼명 정밀 검색
    for c in cols:
        c_clean = clean_header_name(c).replace(" ", "")
        if not id_col and any(k in c_clean for k in ["학번", "학생번호", "번호"]):
            if "이름" not in c_clean or c_clean.startswith("학번"):
                id_col = c
        if not name_col and any(k in c_clean for k in ["이름", "성명", "학생명"]):
            if "학번" not in c_clean:
                name_col = c

    # 2. 찾지 못한 경우 인덱스 기준 (0번=학번, 1번=이름)
    if not id_col and len(cols) >= 1:
        id_col = cols[0]
    if not name_col and len(cols) >= 2:
        name_col = cols[1] if cols[1] != id_col else None

    return id_col, name_col


def read_valid_sheet_dataframe(excel_path: Path) -> Tuple[str, pd.DataFrame]:
    """
    엑셀 파일에서 실제 과제 데이터가 들어있는 시트를 찾아 DataFrame으로 반환합니다.
    ('오류 파일', '검토_및_오류', '안내' 등 제외)
    """
    xl = pd.ExcelFile(excel_path, engine="openpyxl")
    sheet_names = xl.sheet_names

    ignore_sheet_keywords = ["오류", "검토", "안내", "결과 없음", "설명", "readme"]

    selected_sheet = sheet_names[0]
    for s in sheet_names:
        s_lower = s.lower()
        if not any(k in s_lower for k in ignore_sheet_keywords):
            selected_sheet = s
            break

    df = pd.read_excel(excel_path, sheet_name=selected_sheet, engine="openpyxl")
    return selected_sheet, df


def safe_save_excel(
    writer_func: Callable[[pd.ExcelWriter], None],
    output_path: Path,
    max_retries: int = 5,
) -> Path:
    """
    PermissionError가 발생할 경우 대체 파일명(_대체_1 등)으로 안전하게 엑셀을 저장합니다.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    target_path = output_path
    writer = None

    for attempt in range(max_retries):
        try:
            with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
                writer_func(writer)
            return target_path
        except PermissionError:
            if attempt < max_retries - 1:
                target_path = output_path.parent / f"{output_path.stem}_대체_{attempt+1}{output_path.suffix}"
            else:
                raise PermissionError(
                    f"엑셀 파일 쓰기 권한이 없습니다. 파일이 엑셀에서 열려있는지 확인하세요: {output_path.name}"
                )
    return target_path


def merge_excel_assignments(
    excel_paths: List[Path | str],
    output_path: Optional[Path | str] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    여러 개의 엑셀 과제 파일을 '학번' 기준으로 가로 결합(Full Outer Join)합니다.

    :param excel_paths: 순서가 지정된 엑셀 파일 경로 목록
    :param output_path: 저장할 엑셀 파일 경로 (None이면 첫 파일 경로 기반 생성)
    :param log_callback: (레벨, 메시지) 로그 콜백
    :param progress_callback: (진행률 0~100, 메시지) 콜백
    :param stop_checker: () -> bool 작업 중단 요청 확인 함수
    :return: 작업 결과 딕셔너리
    """
    if not excel_paths:
        raise ValueError("병합할 엑셀 파일이 지정되지 않았습니다.")

    valid_paths: List[Path] = []
    for p in excel_paths:
        pp = Path(p)
        if pp.exists() and pp.is_file():
            valid_paths.append(pp)
        else:
            if log_callback:
                log_callback("WARN", f"파일을 찾을 수 없어 건너뜁니다: {p}")

    if not valid_paths:
        raise FileNotFoundError("유효한 엑셀 파일이 존재하지 않습니다.")

    if log_callback:
        log_callback("INFO", f"총 {len(valid_paths)}개 엑셀 파일 가로 병합 준비 중...")

    # -------------------------------------------------------------------------
    # 1단계: 각 파일 파싱 및 데이터 수집
    # -------------------------------------------------------------------------
    parsed_files_info: List[Dict[str, Any]] = []
    all_content_column_names: List[str] = []

    for f_idx, fpath in enumerate(valid_paths, start=1):
        if stop_checker and stop_checker():
            break

        if progress_callback:
            progress_callback(
                int((f_idx / (len(valid_paths) * 2)) * 100),
                f"엑셀 파일 읽는 중 ({f_idx}/{len(valid_paths)}): {fpath.name}",
            )

        sheet_name, df = read_valid_sheet_dataframe(fpath)
        id_col, name_col = identify_student_columns(df)

        if not id_col:
            if log_callback:
                log_callback("WARN", f"[{fpath.name}] 학번 열을 식별할 수 없어 건너뜁니다.")
            continue

        # 문항 응답 열 선정 (학번, 이름, 시스템 메타데이터 열 제외)
        content_cols = []
        for col in df.columns:
            if col in (id_col, name_col):
                continue
            clean_c = clean_header_name(col)
            if clean_c in SYSTEM_METADATA_COLUMNS or clean_c.replace(" ", "") in SYSTEM_METADATA_COLUMNS:
                continue
            content_cols.append(col)
            all_content_column_names.append(clean_c)

        parsed_files_info.append({
            "path": fpath,
            "stem": fpath.stem,
            "sheet": sheet_name,
            "df": df,
            "id_col": id_col,
            "name_col": name_col,
            "content_cols": content_cols,
        })

    if not parsed_files_info:
        raise ValueError("병합 가능한 유효한 데이터가 담긴 엑셀 파일이 없습니다.")

    # -------------------------------------------------------------------------
    # 2단계: 헤더(열 이름) 중복 검사 및 최종 컬럼명 결정
    # -------------------------------------------------------------------------
    col_counter = Counter(all_content_column_names)
    final_columns: List[str] = ["학번", "이름"]
    file_col_mapping: List[Dict[str, str]] = []  # f_idx -> {orig_col: final_col_name}

    for f_info in parsed_files_info:
        mapping = {}
        file_stem = f_info["stem"]
        for orig_col in f_info["content_cols"]:
            clean_c = clean_header_name(orig_col)
            if col_counter[clean_c] > 1:
                # 겹치는 열 이름: [파일명] 열이름 부여
                final_col_name = f"[{file_stem}] {clean_c}"
            else:
                # 고유 열 이름: 원래 이름 유지
                final_col_name = clean_c

            # 만약 같은 파일 내에서 이름이 겹치거나 할 경우 중복 방지
            unique_name = final_col_name
            serial = 2
            while unique_name in final_columns:
                unique_name = f"{final_col_name}_{serial}"
                serial += 1

            mapping[orig_col] = unique_name
            final_columns.append(unique_name)
        file_col_mapping.append(mapping)

    # -------------------------------------------------------------------------
    # 3단계: 학생별 Full Outer Join 데이터 결합 & 오류/검토 분리
    # -------------------------------------------------------------------------
    students_data: Dict[str, Dict[str, Any]] = {}
    review_records: List[Dict[str, Any]] = []
    error_records: List[Dict[str, Any]] = []

    for f_idx, f_info in enumerate(parsed_files_info):
        if stop_checker and stop_checker():
            break

        fpath = f_info["path"]
        df = f_info["df"]
        id_col = f_info["id_col"]
        name_col = f_info["name_col"]
        mapping = file_col_mapping[f_idx]

        for row_idx, row in df.iterrows():
            raw_id = row.get(id_col)
            raw_name = clean_header_name(row.get(name_col)) if name_col and name_col in df.columns else ""

            sid = normalize_student_id(raw_id)
            if not sid:
                # 학번 누락 또는 유효하지 않은 학번 -> 오류 분리
                error_records.append({
                    "구분": "학번 오류",
                    "파일명": fpath.name,
                    "행번호": row_idx + 2,  # 엑셀 기준 행번호
                    "입력_학번": str(raw_id) if pd.notna(raw_id) else "(빈값)",
                    "입력_이름": raw_name,
                    "상세원인": "학번이 누락되었거나 유효한 4~6자리 숫자가 아닙니다.",
                })
                continue

            # 학생 데이터 초기화 또는 가져오기
            if sid not in students_data:
                students_data[sid] = {
                    "학번": sid,
                    "이름": raw_name,
                    "first_file": fpath.name,
                    "seen_names": {fpath.name: raw_name},
                    "values": {},
                }
            else:
                # 이미 존재하는 학생: 이름 일치 여부 확인
                first_name = students_data[sid]["이름"]
                if raw_name and first_name and raw_name != first_name:
                    review_records.append({
                        "구분": "이름 불일치",
                        "학번": sid,
                        "대표이름": first_name,
                        "기준_파일": students_data[sid]["first_file"],
                        "불일치_파일": fpath.name,
                        "해당파일_이름": raw_name,
                        "상세원인": f"동일 학번에 서로 다른 이름 기재 (대표: '{first_name}' vs 해당: '{raw_name}')",
                    })
                    if log_callback:
                        log_callback(
                            "WARN",
                            f"[이름 불일치 의심] 학번 {sid}: '{first_name}'({students_data[sid]['first_file']}) != '{raw_name}'({fpath.name})"
                        )
                elif not first_name and raw_name:
                    students_data[sid]["이름"] = raw_name

                students_data[sid]["seen_names"][fpath.name] = raw_name

            # 문항 열 응답 데이터 저장
            for orig_col, final_col in mapping.items():
                val = row.get(orig_col)
                if pd.isna(val) or val is None:
                    str_val = ""
                else:
                    str_val = str(val).strip()
                students_data[sid]["values"][final_col] = str_val

    # -------------------------------------------------------------------------
    # 4단계: 최종 테이블(DataFrame) 구성 및 학번 오름차순 정렬
    # -------------------------------------------------------------------------
    merged_rows: List[Dict[str, Any]] = []

    def get_sort_key(s_id: str) -> Tuple[int, int, str]:
        if s_id.isdigit():
            return (0, int(s_id), s_id)
        return (1, 999999, s_id)

    sorted_sids = sorted(students_data.keys(), key=get_sort_key)

    for sid in sorted_sids:
        s_info = students_data[sid]
        row_dict: Dict[str, Any] = {
            "학번": s_info["학번"],
            "이름": s_info["이름"],
        }
        for col in final_columns[2:]:
            row_dict[col] = s_info["values"].get(col, "")
        merged_rows.append(row_dict)

    df_main = pd.DataFrame(merged_rows)
    for col in final_columns:
        if col not in df_main.columns:
            df_main[col] = ""
    df_main = df_main[final_columns].map(sanitize_for_excel)

    # -------------------------------------------------------------------------
    # 5단계: 엑셀 파일 저장 (시트1: 통합과제, 시트2: 검토_및_오류)
    # -------------------------------------------------------------------------
    if output_path is None:
        first_dir = valid_paths[0].parent
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = first_dir / f"통합과제_{ts}.xlsx"
    else:
        output_path = Path(output_path)

    combined_review_error: List[Dict[str, Any]] = []
    for r in review_records:
        combined_review_error.append({
            "유형": r["구분"],
            "학번": r["학번"],
            "이름": f"{r['대표이름']} (해당: {r['해당파일_이름']})",
            "파일명": r["불일치_파일"],
            "상세내용": r["상세원인"],
        })
    for e in error_records:
        combined_review_error.append({
            "유형": e["구분"],
            "학번": e["입력_학번"],
            "이름": e["입력_이름"],
            "파일명": e["파일명"],
            "상세내용": f"행 {e['행번호']}: {e['상세원인']}",
        })

    def write_sheets(writer: pd.ExcelWriter) -> None:
        # 시트 1: 메인 통합과제 시트
        sheet_name_main = "통합과제"
        df_main.to_excel(writer, index=False, sheet_name=sheet_name_main)
        ws_main = writer.sheets[sheet_name_main]
        ws_main.freeze_panes = "C2"  # 학번, 이름 열 고정

        for col_cells in ws_main.columns:
            longest = max((len(str(c.value or "")) for c in col_cells), default=0)
            col_letter = col_cells[0].column_letter
            ws_main.column_dimensions[col_letter].width = min(max(longest + 2, 10), 50)

        # 시트 2: 검토 및 오류 시트 (해당 사항이 있을 때만 생성)
        if combined_review_error:
            df_err = pd.DataFrame(combined_review_error).map(sanitize_for_excel)
            sheet_name_err = "검토_및_오류"
            df_err.to_excel(writer, index=False, sheet_name=sheet_name_err)
            ws_err = writer.sheets[sheet_name_err]
            ws_err.freeze_panes = "A2"
            for col_cells in ws_err.columns:
                longest = max((len(str(c.value or "")) for c in col_cells), default=0)
                col_letter = col_cells[0].column_letter
                ws_err.column_dimensions[col_letter].width = min(max(longest + 2, 12), 60)

    final_saved_path = safe_save_excel(write_sheets, output_path)

    if progress_callback:
        progress_callback(100, f"가로 병합 완료! (총 {len(merged_rows)}명 통합)")

    if log_callback:
        log_callback(
            "SUCCESS",
            f"엑셀 가로 병합 완료: 총 {len(valid_paths)}개 파일 -> 학생 {len(merged_rows)}명 / 열 {len(final_columns)}개",
        )
        if review_records:
            log_callback("WARN", f"이름 불일치 검토 필요 항목 {len(review_records)}건 발견")
        if error_records:
            log_callback("WARN", f"학번 누락/오류로 제외된 행 {len(error_records)}건 분리됨")

    return {
        "total_files": len(valid_paths),
        "total_students": len(merged_rows),
        "merged_columns": len(final_columns),
        "review_count": len(review_records),
        "error_count": len(error_records),
        "review_records": review_records,
        "error_records": error_records,
        "output_file": str(final_saved_path.resolve()),
    }
