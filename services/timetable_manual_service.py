"""
컴시간 미지원 학교 대응을 위한 수동/엑셀 시간표 표준화 서비스.
- 시간표_기본 / 시간표_변동 Firestore 스키마 표준화 (`source`: "comcigan" | "excel" | "manual")
- 주간 시간표 엑셀(.xlsx) / CSV(.csv) 일괄 파싱 (세로형 목록 + 요일별 시간표 매트릭스 모두 지원)
- 특정 날짜·교시 변동/보강 수동 입력 빌더
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


VALID_TIMETABLE_SOURCES = ("comcigan", "excel", "manual")
WEEKDAY_MAP = {
    "월": "월",
    "월요일": "월",
    "mon": "월",
    "화": "화",
    "화요일": "화",
    "tue": "화",
    "수": "수",
    "수요일": "수",
    "wed": "수",
    "목": "목",
    "목요일": "목",
    "thu": "목",
    "금": "금",
    "금요일": "금",
    "fri": "금",
    "토": "토",
    "토요일": "토",
    "sat": "토",
}


def normalize_teacher_name(name: Any) -> str:
    """교사명 끝의 'T', '선생님', 공백 등을 정규화해 '유우석T'와 '유우석'을 일치시킵니다."""
    s = str(name or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?:선생님|T)$", "", s, flags=re.IGNORECASE).strip()
    return s


def _normalize_day_char(val: Any) -> str:
    raw = str(val or "").strip().lower()
    if not raw:
        return ""
    if raw in WEEKDAY_MAP:
        return WEEKDAY_MAP[raw]
    for k, v in WEEKDAY_MAP.items():
        if raw.startswith(k):
            return v
    return ""


def _parse_period_int(val: Any) -> Optional[int]:
    m = re.search(r"\d+", str(val or "").strip())
    if not m:
        return None
    p = int(m.group(0))
    return p if p > 0 else None


def _parse_int_opt(val: Any) -> Optional[int]:
    m = re.search(r"\d+", str(val or "").strip())
    return int(m.group(0)) if m else None


def _split_cell_subject_teacher(cell_text: str, default_teacher: str = "") -> Tuple[str, str]:
    """'수학\\n(유우석)' 또는 '수학 (유우석T)' 형태에서 과목과 교사명을 분리합니다."""
    text = str(cell_text or "").strip()
    if not text:
        return "", normalize_teacher_name(default_teacher)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        subj = lines[0]
        tchr = normalize_teacher_name(re.sub(r"[()]", "", lines[1]))
        return subj, tchr or normalize_teacher_name(default_teacher)
    m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", text)
    if m:
        return m.group(1).strip(), normalize_teacher_name(m.group(2))
    return text, normalize_teacher_name(default_teacher)


def build_manual_timetable_base_row(
    *,
    day_of_week: str,
    period: int,
    subject: str,
    grade: Optional[int] = None,
    class_no: Optional[int] = None,
    teacher_name: str = "",
    entry_type: str = "class",
    school_code: str = "",
    school_name: str = "",
    room: str = "",
    source: str = "manual",
) -> Dict[str, Any]:
    """표준화된 `시간표_기본` 문서를 생성합니다."""
    day_char = _normalize_day_char(day_of_week) or str(day_of_week or "").strip()[:1]
    p_int = int(period)
    e_type = "teacher" if str(entry_type or "").strip().lower() == "teacher" else "class"
    norm_teacher = normalize_teacher_name(teacher_name)
    sc = str(school_code or "").strip()
    sn = str(school_name or "").strip()
    src = source if source in VALID_TIMETABLE_SOURCES else "manual"

    if e_type == "teacher":
        rid = f"tb_t_{norm_teacher}_{day_char}_{p_int}"
    else:
        g_str = str(grade) if grade is not None else "0"
        c_str = str(class_no) if class_no is not None else "0"
        rid = f"tb_c_{g_str}_{c_str}_{day_char}_{p_int}"
    if sc:
        rid = f"{sc}_{rid}"

    return {
        "remote_id": rid,
        "entry_type": e_type,
        "grade": int(grade) if grade is not None else None,
        "class_no": int(class_no) if class_no is not None else None,
        "day_of_week": day_char,
        "period": p_int,
        "subject": str(subject or "").strip(),
        "teacher_name": norm_teacher,
        "teacher": norm_teacher,
        "room": str(room or "").strip(),
        "school_code": sc,
        "school_name": sn,
        "source": src,
    }


def build_manual_timetable_change(
    *,
    date_str: str,
    period: int,
    changed_subject: str,
    grade: Optional[int] = None,
    class_no: Optional[int] = None,
    original_subject: str = "",
    original_teacher: str = "",
    changed_teacher: str = "",
    reason: str = "수동 변동",
    school_code: str = "",
    school_name: str = "",
    source: str = "manual",
) -> Dict[str, Any]:
    """표준화된 `시간표_변동` 문서를 생성합니다."""
    d_clean = str(date_str or datetime.now().strftime("%Y-%m-%d")).strip()
    p_int = int(period)
    norm_orig_t = normalize_teacher_name(original_teacher)
    norm_chg_t = normalize_teacher_name(changed_teacher or original_teacher)
    sc = str(school_code or "").strip()
    sn = str(school_name or "").strip()
    src = source if source in VALID_TIMETABLE_SOURCES else "manual"

    ymd = d_clean.replace("-", "")
    if grade is not None and class_no is not None:
        rid = f"tc_{ymd}_{int(grade)}_{int(class_no)}_{p_int}"
    else:
        rid = f"tc_t_{ymd}_{norm_chg_t or 'teacher'}_{p_int}"
    if sc:
        rid = f"{sc}_{rid}"

    return {
        "remote_id": rid,
        "date": d_clean,
        "target_date": d_clean,
        "grade": int(grade) if grade is not None else None,
        "class_no": int(class_no) if class_no is not None else None,
        "period": p_int,
        "original_subject": str(original_subject or "").strip() or "(기존수업)",
        "changed_subject": str(changed_subject or "").strip(),
        "original_teacher": norm_orig_t,
        "changed_teacher": norm_chg_t,
        "reason": str(reason or "수동 변동").strip(),
        "school_code": sc,
        "school_name": sn,
        "source": src,
    }


def _read_table_rows(file_path: str | Path) -> List[List[str]]:
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".csv":
        for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
            try:
                with path.open("r", encoding=enc, newline="") as f:
                    return [[str(c or "").strip() for c in row] for row in csv.reader(f)]
            except UnicodeDecodeError:
                continue
        raise ValueError("CSV 파일 인코딩을 읽을 수 없습니다 (UTF-8 또는 CP949 권장).")

    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        rows: List[List[str]] = []
        for row in ws.iter_rows(values_only=True):
            str_row = [str(c if c is not None else "").strip() for c in row]
            if any(str_row):
                rows.append(str_row)
        return rows

    raise ValueError(f"지원하지 않는 파일 형식입니다: {ext} (.xlsx 또는 .csv만 가능)")


def parse_weekly_timetable_file(
    file_path: str | Path,
    *,
    school_code: str = "",
    school_name: str = "",
    default_grade: Optional[int] = None,
    default_class: Optional[int] = None,
    default_teacher: str = "",
    entry_type: str = "class",
) -> List[Dict[str, Any]]:
    """
    엑셀(.xlsx) 또는 CSV(.csv) 주간 시간표를 파싱해 표준 `시간표_기본` 문서 리스트를 반환합니다.
    1) 목록형 헤더: 요일, 교시, 과목 (, 학년, 반, 교사, 구분, 강의실)
    2) 시간표 매트릭스형 헤더: 교시, 월, 화, 수, 목, 금 (, 토)
    """
    raw_rows = _read_table_rows(file_path)
    if len(raw_rows) < 2:
        return []

    headers = [h.strip() for h in raw_rows[0]]
    headers_lower = [h.lower() for h in headers]

    # 매트릭스형 여부 확인 (월, 화, 수... 중 2개 이상이 헤더에 존재)
    day_col_indices: Dict[int, str] = {}
    for idx, h in enumerate(headers):
        d_char = _normalize_day_char(h)
        if d_char:
            day_col_indices[idx] = d_char

    results: List[Dict[str, Any]] = []
    if len(day_col_indices) >= 2:
        period_idx = next(
            (i for i, h in enumerate(headers_lower) if "교시" in h or h == "period"),
            0,
        )
        for row in raw_rows[1:]:
            if period_idx >= len(row):
                continue
            p_int = _parse_period_int(row[period_idx])
            if not p_int:
                continue
            for col_idx, day_char in day_col_indices.items():
                if col_idx >= len(row):
                    continue
                cell = row[col_idx].strip()
                if not cell:
                    continue
                subj, tchr = _split_cell_subject_teacher(cell, default_teacher)
                if not subj:
                    continue
                results.append(
                    build_manual_timetable_base_row(
                        day_of_week=day_char,
                        period=p_int,
                        subject=subj,
                        grade=default_grade,
                        class_no=default_class,
                        teacher_name=tchr,
                        entry_type=entry_type,
                        school_code=school_code,
                        school_name=school_name,
                        source="excel",
                    )
                )
        return results

    # 목록형 파싱
    def _find_col(*candidates: str) -> Optional[int]:
        for idx, h in enumerate(headers_lower):
            for cand in candidates:
                if cand in h:
                    return idx
        return None

    col_day = _find_col("요일", "day")
    col_period = _find_col("교시", "period")
    col_subj = _find_col("과목", "subject")
    col_teacher = _find_col("교사", "선생", "teacher")
    col_grade = _find_col("학년", "grade")
    col_class = _find_col("반", "class")
    col_type = _find_col("구분", "유형", "entry_type", "type")
    col_room = _find_col("강의실", "교실", "room")

    if col_day is None or col_period is None or col_subj is None:
        raise ValueError("엑셀/CSV 헤더에서 '요일', '교시', '과목' 또는 요일별('월','화','수','목','금') 열을 찾을 수 없습니다.")

    for row in raw_rows[1:]:
        if len(row) <= max(col_day, col_period, col_subj):
            continue
        day_char = _normalize_day_char(row[col_day])
        p_int = _parse_period_int(row[col_period])
        subj = row[col_subj].strip()
        if not day_char or not p_int or not subj:
            continue
        g_val = _parse_int_opt(row[col_grade]) if col_grade is not None and col_grade < len(row) else default_grade
        c_val = _parse_int_opt(row[col_class]) if col_class is not None and col_class < len(row) else default_class
        if g_val is None:
            g_val = default_grade
        if c_val is None:
            c_val = default_class
        t_val = row[col_teacher].strip() if col_teacher is not None and col_teacher < len(row) else default_teacher
        if not t_val:
            t_val = default_teacher
        e_val = row[col_type].strip() if col_type is not None and col_type < len(row) else entry_type
        if not e_val:
            e_val = entry_type
        r_val = row[col_room].strip() if col_room is not None and col_room < len(row) else ""

        results.append(
            build_manual_timetable_base_row(
                day_of_week=day_char,
                period=p_int,
                subject=subj,
                grade=g_val,
                class_no=c_val,
                teacher_name=t_val,
                entry_type=e_val,
                school_code=school_code,
                school_name=school_name,
                room=r_val,
                source="excel",
            )
        )
    return results
