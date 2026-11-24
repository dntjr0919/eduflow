"""
학사일정 파일 파서(.xlsx / .csv / .ics), D-Day 계산, ICS 내보내기 및 일정 임박 알림 체크 서비스.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from core.database import DatabaseManager
from services.llm_client import extract_json_from_llm_response
from services.notifier import NotificationService

VALID_SCHEDULE_CATEGORIES: Tuple[str, ...] = ("학사일정", "수행평가", "업무", "개인")

CATEGORY_ALIASES: Dict[str, str] = {
    "학사": "학사일정",
    "학사일정": "학사일정",
    "학교일정": "학사일정",
    "행사": "학사일정",
    "academic": "학사일정",
    "수행": "수행평가",
    "수행평가": "수행평가",
    "평가": "수행평가",
    "지필": "수행평가",
    "고사": "수행평가",
    "시험": "수행평가",
    "exam": "수행평가",
    "업무": "업무",
    "회의": "업무",
    "공문": "업무",
    "출장": "업무",
    "연수": "업무",
    "work": "업무",
    "개인": "개인",
    "상담": "개인",
    "연가": "개인",
    "휴가": "개인",
    "personal": "개인",
}

REMINDER_LABELS: Dict[str, str] = {
    "none": "알림 없음",
    "10m": "10분 전",
    "1h": "1시간 전",
    "morning_9am": "당일 오전 09:00",
}

REMINDER_ALIASES: Dict[str, str] = {
    "none": "none",
    "없음": "none",
    "알림 없음": "none",
    "알림없음": "none",
    "10m": "10m",
    "10분": "10m",
    "10분 전": "10m",
    "10분전": "10m",
    "1h": "1h",
    "1시간": "1h",
    "1시간 전": "1h",
    "1시간전": "1h",
    "morning_9am": "morning_9am",
    "당일 오전 09:00": "morning_9am",
    "당일 오전 9시": "morning_9am",
    "오전 9시": "morning_9am",
    "오전9시": "morning_9am",
    "09:00": "morning_9am",
}

TODO_PRIORITY_ALIASES: Dict[str, str] = {
    "high": "high",
    "높음": "high",
    "긴급": "high",
    "상": "high",
    "normal": "normal",
    "medium": "normal",
    "보통": "normal",
    "중간": "normal",
    "중": "normal",
    "low": "low",
    "낮음": "low",
    "여유": "low",
    "하": "low",
}

TODO_PRIORITY_LABELS: Dict[str, str] = {
    "high": "높음",
    "normal": "보통",
    "low": "낮음",
}


def normalize_reminder_type(raw_val: Any) -> str:
    """알림 문자열을 표준 코드(none, 10m, 1h, morning_9am)로 정규화합니다."""
    s = str(raw_val or "").strip().lower()
    if not s:
        return "none"
    if s in REMINDER_ALIASES:
        return REMINDER_ALIASES[s]
    compact = s.replace(" ", "")
    if compact in REMINDER_ALIASES:
        return REMINDER_ALIASES[compact]
    if "10" in compact and ("m" in compact or "분" in compact):
        return "10m"
    if "1" in compact and ("h" in compact or "시간" in compact):
        return "1h"
    if "9" in compact or "오전" in compact or "morning" in compact:
        return "morning_9am"
    return "none"


def normalize_todo_priority(raw_val: Any) -> str:
    """할일 우선순위 문자열을 DB 표준 코드(high, normal, low)로 정규화합니다."""
    s = str(raw_val or "").strip().lower()
    if not s:
        return "normal"
    if s in TODO_PRIORITY_ALIASES:
        return TODO_PRIORITY_ALIASES[s]
    for key, mapped in TODO_PRIORITY_ALIASES.items():
        if key in s:
            return mapped
    return "normal"


def infer_schedule_category(title: str, raw_category: str = "") -> str:
    """카테고리 문자열 또는 일정 제목을 바탕으로 표준 4종 카테고리를 추론합니다."""
    raw = str(raw_category or "").strip()
    if raw in VALID_SCHEDULE_CATEGORIES:
        return raw

    combined = f"{raw} {title or ''}".strip().lower()
    for kw, mapped in CATEGORY_ALIASES.items():
        if kw in combined:
            return mapped

    if any(k in combined for k in ("수행", "평가", "지필", "고사", "모의고사", "채점", "성적")):
        return "수행평가"
    if any(k in combined for k in ("회의", "공문", "제출", "마감", "연수", "출장", "점검", "업무")):
        return "업무"
    if any(k in combined for k in ("개인", "연가", "병가", "조퇴", "상담", "병원", "휴가")):
        return "개인"
    return "학사일정"


def normalize_time_str(raw_val: Any) -> str:
    """다양한 시간 표현(HH:MM, 09시 30분, datetime/time 등)을 HH:MM으로 정규화합니다."""
    if raw_val is None:
        return ""
    if isinstance(raw_val, datetime):
        return raw_val.strftime("%H:%M")
    if isinstance(raw_val, time):
        return raw_val.strftime("%H:%M")

    s = str(raw_val).strip()
    if not s:
        return ""

    m = re.search(r"(\d{1,2})\s*[:시]\s*(\d{1,2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    m_hour = re.fullmatch(r"(\d{1,2})\s*시", s)
    if m_hour:
        hh = int(m_hour.group(1))
        if 0 <= hh <= 23:
            return f"{hh:02d}:00"
    return ""


def _parse_single_date(token: str, default_year: int) -> Optional[date]:
    s = str(token or "").strip()
    if not s:
        return None
    # 요일 표기 제거 예: 2026-11-18(수), 2026.11.18 (수)
    s = re.sub(r"\([월화수목금토일a-zA-Z]+\)", "", s).strip()

    # YYYY-MM-DD, YYYY.MM.DD, YYYY/MM/DD, YYYY년 MM월 DD일
    m_full = re.search(
        r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?",
        s,
    )
    if m_full:
        try:
            return date(int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3)))
        except ValueError:
            return None

    # YYYYMMDD
    m_compact = re.search(r"\b(20\d{2})(\d{2})(\d{2})\b", s)
    if m_compact:
        try:
            return date(int(m_compact.group(1)), int(m_compact.group(2)), int(m_compact.group(3)))
        except ValueError:
            return None

    # MM월 DD일, MM/DD, MM.DD, MM-DD
    m_short = re.search(r"(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?", s)
    if m_short:
        try:
            return date(default_year, int(m_short.group(1)), int(m_short.group(2)))
        except ValueError:
            return None

    return None


def normalize_date_str(
    raw_val: Any, default_year: Optional[int] = None
) -> Tuple[str, str]:
    """
    단일 날짜 또는 범위 날짜(예: '2026-11-18 ~ 2026-11-20', '11.20.')를
    (start_date_str, end_date_str) 'YYYY-MM-DD' 튜플로 반환합니다.
    파싱 실패 시 ("", "")를 반환합니다.
    """
    if raw_val is None:
        return "", ""
    if isinstance(raw_val, datetime):
        d = raw_val.date().isoformat()
        return d, d
    if isinstance(raw_val, date):
        d = raw_val.isoformat()
        return d, d

    yr = int(default_year or date.today().year)
    s = str(raw_val).strip()
    if not s:
        return "", ""

    # 범위 구분자 분리 (~, 〜, –, — 또는 공백 포함 -)
    parts = re.split(r"\s*(?:~|〜|–|—|\s-\s)\s*", s, maxsplit=1)
    if len(parts) == 2:
        d1 = _parse_single_date(parts[0], yr)
        d2 = _parse_single_date(parts[1], d1.year if d1 else yr)
        if d1 and d2:
            if d2 < d1:
                d2 = d1
            return d1.isoformat(), d2.isoformat()
        if d1:
            # '2026-11-18 ~ 20' 처럼 일(day)만 온 경우 처리
            m_day = re.match(r"^(\d{1,2})\s*일?$", parts[1].strip())
            if m_day:
                try:
                    d2_day = date(d1.year, d1.month, int(m_day.group(1)))
                    if d2_day >= d1:
                        return d1.isoformat(), d2_day.isoformat()
                except ValueError:
                    pass
            return d1.isoformat(), d1.isoformat()

    d_single = _parse_single_date(s, yr)
    if d_single:
        iso = d_single.isoformat()
        return iso, iso
    return "", ""


def _detect_schedule_columns(rows: Sequence[Sequence[Any]]) -> Tuple[int, Dict[str, int]]:
    """표 데이터 상단에서 헤더 행 인덱스와 컬럼 매핑을 추론합니다."""
    header_keywords = {
        "start_date": ("시작일", "날짜", "일자", "일시", "기간", "date", "start_date", "start"),
        "end_date": ("종료일", "마감일", "끝일", "end_date", "end"),
        "start_time": ("시간", "시작시간", "시각", "time", "start_time"),
        "all_day": ("종일여부", "종일", "all_day", "allday"),
        "reminder_type": ("알림", "리마인더", "reminder", "reminder_type"),
        "title": ("일정명", "일정", "행사명", "행사", "제목", "내용", "title", "summary", "event"),
        "category": ("카테고리", "구분", "분류", "유형", "category", "type"),
        "memo": ("메모", "비고", "상세", "설명", "note", "memo", "description"),
    }

    best_row = -1
    best_map: Dict[str, int] = {}
    scan_limit = min(10, len(rows))

    for r_idx in range(scan_limit):
        row = rows[r_idx]
        col_map: Dict[str, int] = {}
        for c_idx, cell in enumerate(row):
            txt = str(cell or "").strip().lower().replace(" ", "")
            if not txt:
                continue
            for field, kw_list in header_keywords.items():
                if field in col_map:
                    continue
                if any(kw == txt or kw in txt for kw in kw_list):
                    col_map[field] = c_idx
        if "start_date" in col_map and "title" in col_map:
            return r_idx, col_map
        if len(col_map) > len(best_map):
            best_map = col_map
            best_row = r_idx

    if len(best_map) >= 2:
        return best_row, best_map
    return -1, {}


def _parse_tabular_rows(
    rows: Sequence[Sequence[Any]],
    default_year: Optional[int] = None,
) -> List[Dict[str, Any]]:
    if not rows:
        return []

    hdr_idx, col_map = _detect_schedule_columns(rows)
    start_row = hdr_idx + 1 if hdr_idx >= 0 else 0
    if not col_map:
        col_map = {"start_date": 0, "title": 1, "category": 2, "memo": 3}

    yr = int(default_year or date.today().year)
    parsed: List[Dict[str, Any]] = []

    for r_idx in range(start_row, len(rows)):
        row = list(rows[r_idx])
        if not row or all(str(c or "").strip() == "" for c in row):
            continue

        def _cell(field: str) -> Any:
            idx = col_map.get(field, -1)
            if 0 <= idx < len(row):
                return row[idx]
            return None

        raw_start = _cell("start_date")
        s_date, e_date = normalize_date_str(raw_start, default_year=yr)
        if not s_date:
            continue

        raw_end = _cell("end_date")
        if raw_end is not None and str(raw_end).strip():
            end_s, end_e = normalize_date_str(raw_end, default_year=int(s_date[:4]))
            candidate_end = end_e or end_s
            if candidate_end and candidate_end >= s_date:
                e_date = candidate_end

        title = str(_cell("title") or "").strip()
        if not title:
            continue

        raw_cat = str(_cell("category") or "").strip()
        category = infer_schedule_category(title, raw_cat)

        raw_time = _cell("start_time")
        s_time = normalize_time_str(raw_time)
        if not s_time and isinstance(raw_start, datetime):
            if raw_start.hour != 0 or raw_start.minute != 0:
                s_time = raw_start.strftime("%H:%M")
        elif not s_time and isinstance(raw_start, str):
            s_time = normalize_time_str(raw_start)

        raw_all_day = str(_cell("all_day") or "").strip().lower()
        if raw_all_day in ("y", "yes", "true", "1", "예", "종일"):
            s_time = ""

        reminder_type = normalize_reminder_type(_cell("reminder_type"))
        memo = str(_cell("memo") or "").strip()

        parsed.append(
            {
                "title": title,
                "category": category,
                "start_date": s_date,
                "end_date": e_date or s_date,
                "start_time": s_time,
                "reminder_type": reminder_type,
                "memo": memo,
            }
        )

    return parsed


def parse_xlsx_schedules(
    file_path: str, default_year: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Excel(.xlsx) 학사일정 파일을 파싱하여 일정 dict 목록을 반환합니다."""
    wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    try:
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    return _parse_tabular_rows(rows, default_year=default_year)


def parse_csv_schedules(
    file_path: str, default_year: Optional[int] = None
) -> List[Dict[str, Any]]:
    """CSV(.csv) 학사일정 파일을 파싱하여 일정 dict 목록을 반환합니다."""
    path_obj = Path(file_path)
    content: Optional[str] = None
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            content = path_obj.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        content = path_obj.read_text(encoding="utf-8", errors="replace")

    reader = csv.reader(content.splitlines())
    rows = [r for r in reader]
    return _parse_tabular_rows(rows, default_year=default_year)


def _unfold_ics_lines(text: str) -> List[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized.split("\n")
    unfolded: List[str] = []
    for line in raw_lines:
        if (line.startswith(" ") or line.startswith("\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _unescape_ics_text(val: str) -> str:
    return (
        val.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _parse_ics_datetime(raw_val: str) -> Tuple[str, str, bool]:
    """(YYYY-MM-DD, HH:MM, is_all_day) 반환."""
    s = str(raw_val or "").strip()
    m_dt = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?Z?$", s)
    if m_dt:
        d_str = f"{m_dt.group(1)}-{m_dt.group(2)}-{m_dt.group(3)}"
        t_str = f"{m_dt.group(4)}:{m_dt.group(5)}"
        return d_str, t_str, False

    m_d = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m_d:
        d_str = f"{m_d.group(1)}-{m_d.group(2)}-{m_d.group(3)}"
        return d_str, "", True

    s_date, _ = normalize_date_str(s)
    return s_date, normalize_time_str(s), False


def parse_ics_schedules(file_path: str) -> List[Dict[str, Any]]:
    """iCalendar(.ics) 파일의 VEVENT 목록을 파싱하여 일정 dict 목록을 반환합니다."""
    text = Path(file_path).read_text(encoding="utf-8-sig", errors="replace")
    lines = _unfold_ics_lines(text)

    events: List[Dict[str, Any]] = []
    current: Optional[Dict[str, str]] = None

    for line in lines:
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
            continue
        if stripped == "END:VEVENT":
            if current is not None:
                s_date, s_time, is_all_day = _parse_ics_datetime(current.get("DTSTART", ""))
                e_date, _, end_all_day = _parse_ics_datetime(current.get("DTEND", ""))
                if s_date:
                    if e_date and (is_all_day or end_all_day) and e_date > s_date:
                        # RFC 5545 종일 일정의 DTEND는 배타적(exclusive) 종료일
                        try:
                            d_end = date.fromisoformat(e_date) - timedelta(days=1)
                            e_date = max(d_end, date.fromisoformat(s_date)).isoformat()
                        except ValueError:
                            pass
                    if not e_date or e_date < s_date:
                        e_date = s_date
                    title = _unescape_ics_text(current.get("SUMMARY", ""))
                    if title:
                        memo = _unescape_ics_text(current.get("DESCRIPTION", ""))
                        raw_cat = _unescape_ics_text(current.get("CATEGORIES", ""))
                        events.append(
                            {
                                "title": title,
                                "category": infer_schedule_category(title, raw_cat),
                                "start_date": s_date,
                                "end_date": e_date,
                                "start_time": s_time,
                                "reminder_type": "none",
                                "memo": memo,
                                "remote_id": current.get("UID", "").strip(),
                            }
                        )
            current = None
            continue

        if current is not None and ":" in line:
            key_part, val_part = line.split(":", 1)
            prop_name = key_part.split(";", 1)[0].strip().upper()
            current[prop_name] = val_part.strip()

    return events


def parse_schedule_file(
    file_path: str, default_year: Optional[int] = None
) -> List[Dict[str, Any]]:
    """확장자(.xlsx, .xls, .csv, .ics)에 따라 적절한 학사일정 파서를 호출합니다."""
    ext = Path(file_path).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return parse_xlsx_schedules(file_path, default_year=default_year)
    if ext == ".xls":
        import pandas as pd

        df = pd.read_excel(file_path, header=None, dtype=object)
        rows = [
            [None if pd.isna(v) else v for v in row]
            for row in df.values.tolist()
        ]
        return _parse_tabular_rows(rows, default_year=default_year)
    if ext == ".csv":
        return parse_csv_schedules(file_path, default_year=default_year)
    if ext == ".ics":
        return parse_ics_schedules(file_path)
    raise ValueError(f"지원하지 않는 학사일정 파일 형식입니다: {ext} (.xlsx, .xls, .csv, .ics 지원)")


def _escape_ics_text(val: str) -> str:
    return (
        str(val or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def export_schedules_to_ics(schedules: List[Dict[str, Any]], output_path: str) -> int:
    """일정 목록을 표준 iCalendar(.ics) 파일로 내보내고 내보낸 건수를 반환합니다."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//EduFlow//Teacher Workstation Calendar//KO",
        "CALSCALE:GREGORIAN",
    ]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    exported = 0

    for idx, sch in enumerate(schedules, start=1):
        s_date_str = str(sch.get("start_date", "")).strip()
        title = str(sch.get("title", "")).strip()
        if not s_date_str or not title:
            continue
        e_date_str = str(sch.get("end_date", "")).strip() or s_date_str
        s_time = str(sch.get("start_time", "")).strip()
        uid = str(sch.get("remote_id") or f"eduflow-sch-{sch.get('id', idx)}@eduflow.local")

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"DTSTAMP:{stamp}")

        if s_time and re.fullmatch(r"\d{2}:\d{2}", s_time):
            s_compact = s_date_str.replace("-", "")
            e_compact = e_date_str.replace("-", "")
            t_compact = s_time.replace(":", "") + "00"
            lines.append(f"DTSTART:{s_compact}T{t_compact}")
            try:
                start_dt = datetime.strptime(f"{s_date_str} {s_time}", "%Y-%m-%d %H:%M")
                end_dt = start_dt + timedelta(hours=1)
                if e_date_str > s_date_str:
                    end_dt = datetime.strptime(f"{e_date_str} {s_time}", "%Y-%m-%d %H:%M")
                lines.append(f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}")
            except ValueError:
                lines.append(f"DTEND:{e_compact}T{t_compact}")
        else:
            s_compact = s_date_str.replace("-", "")
            try:
                e_exclusive = (date.fromisoformat(e_date_str) + timedelta(days=1)).strftime("%Y%m%d")
            except ValueError:
                e_exclusive = s_compact
            lines.append(f"DTSTART;VALUE=DATE:{s_compact}")
            lines.append(f"DTEND;VALUE=DATE:{e_exclusive}")

        lines.append(f"SUMMARY:{_escape_ics_text(title)}")
        cat = str(sch.get("category", "")).strip()
        if cat:
            lines.append(f"CATEGORIES:{_escape_ics_text(cat)}")
        memo = str(sch.get("memo", "")).strip()
        if memo:
            lines.append(f"DESCRIPTION:{_escape_ics_text(memo)}")
        lines.append("END:VEVENT")
        exported += 1

    lines.append("END:VCALENDAR")
    Path(output_path).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return exported


XLSX_EXPORT_HEADERS: Tuple[str, ...] = (
    "시작일",
    "종료일",
    "일정명",
    "카테고리",
    "시작시간",
    "종일여부",
    "알림",
    "메모",
)


def export_schedules_to_xlsx(
    schedules: Sequence[Dict[str, Any]],
    out_path: str | Path,
    include_sample_when_empty: bool = True,
) -> int:
    """
    일정 목록을 parse_xlsx_schedules와 호환되는 표준 Excel(.xlsx) 파일로 내보냅니다.
    반환값은 실제 내보낸 일정 건수입니다(빈 양식 내보내기 시 0 반환).
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "학사일정"

    header_fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_align = Alignment(horizontal="center", vertical="center")

    ws.append(list(XLSX_EXPORT_HEADERS))
    for col_idx in range(1, len(XLSX_EXPORT_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "A2"

    col_widths = {
        "A": 14,  # 시작일
        "B": 14,  # 종료일
        "C": 32,  # 일정명
        "D": 14,  # 카테고리
        "E": 12,  # 시작시간
        "F": 12,  # 종일여부
        "G": 16,  # 알림
        "H": 36,  # 메모
    }
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    exported = 0
    for sch in schedules or []:
        s_date = str(sch.get("start_date", "") or "").strip()
        title = str(sch.get("title", "") or "").strip()
        if not s_date or not title:
            continue
        e_date = str(sch.get("end_date", "") or "").strip() or s_date
        if e_date < s_date:
            e_date = s_date
        s_time = normalize_time_str(sch.get("start_time"))
        all_day_str = "N" if s_time else "Y"
        cat = infer_schedule_category(title, str(sch.get("category", "") or ""))
        r_type = normalize_reminder_type(sch.get("reminder_type", sch.get("reminder", "none")))
        r_label = REMINDER_LABELS.get(r_type, "알림 없음")
        memo = str(sch.get("memo", "") or "").strip()

        ws.append(
            [
                escape_spreadsheet_formula(s_date),
                escape_spreadsheet_formula(e_date),
                escape_spreadsheet_formula(title),
                escape_spreadsheet_formula(cat),
                escape_spreadsheet_formula(s_time),
                escape_spreadsheet_formula(all_day_str),
                escape_spreadsheet_formula(r_label),
                escape_spreadsheet_formula(memo),
            ]
        )
        exported += 1

    if exported == 0 and include_sample_when_empty:
        today_str = date.today().isoformat()
        next_str = (date.today() + timedelta(days=2)).isoformat()
        sample_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
        sample_font = Font(color="475569", italic=True)
        samples = [
            [today_str, today_str, "[예시] 2학년 수학 수행평가", "수행평가", "09:30", "N", "1시간 전", "준비물: 계산기 (수정 후 사용)"],
            [next_str, next_str, "[예시] 교직원 전체 회의", "업무", "", "Y", "당일 오전 09:00", "본관 회의실"],
        ]
        for row_vals in samples:
            ws.append(row_vals)
            r_idx = ws.max_row
            for c_idx in range(1, len(XLSX_EXPORT_HEADERS) + 1):
                c = ws.cell(row=r_idx, column=c_idx)
                c.fill = sample_fill
                c.font = sample_font

        ws_guide = wb.create_sheet(title="입력안내")
        ws_guide.column_dimensions["A"].width = 18
        ws_guide.column_dimensions["B"].width = 62
        ws_guide.append(["항목", "작성 규칙 및 허용 값"])
        for col_idx in (1, 2):
            c = ws_guide.cell(row=1, column=col_idx)
            c.fill = header_fill
            c.font = header_font
            c.alignment = header_align
        guide_rows = [
            ["시작일 / 종료일", "YYYY-MM-DD 형식 권장 (예: 2026-11-18). 종료일 비움 시 시작일과 동일 처리."],
            ["일정명", "캘린더에 표시할 일정 제목 (필수)."],
            ["카테고리", f"허용 값: {', '.join(VALID_SCHEDULE_CATEGORIES)}"],
            ["시작시간", "HH:MM 24시간 형식 (예: 09:30, 14:00). 종일 일정이면 비워둡니다."],
            ["종일여부", "Y(종일) 또는 N(시간 지정)."],
            ["알림", "알림 없음 / 10분 전 / 1시간 전 / 당일 오전 09:00 (또는 none/10m/1h/morning_9am)."],
            ["메모", "세부 장소, 준비물 등 참고 메모."],
        ]
        for g_row in guide_rows:
            ws_guide.append(g_row)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    wb.close()
    return exported


_WEEKDAY_KO_MAP: Dict[str, int] = {
    "월": 0,
    "화": 1,
    "수": 2,
    "목": 3,
    "금": 4,
    "토": 5,
    "일": 6,
}


def normalize_ai_date_str(raw_val: Any, today: Optional[date] = None) -> str:
    """
    AI 응답의 날짜(YYYY-MM-DD, MM월 DD일, '오늘', '내일', '다음주 월요일' 등)를
    'YYYY-MM-DD' 문자열로 정규화합니다. 파싱 실패 시 ""를 반환합니다.
    """
    ref = today or date.today()
    s = str(raw_val or "").strip()
    if not s:
        return ""

    low = s.lower()
    compact = low.replace(" ", "")

    if compact in ("오늘", "금일", "당일", "today"):
        return ref.isoformat()
    if compact in ("내일", "명일", "tomorrow"):
        return (ref + timedelta(days=1)).isoformat()
    if compact in ("모레",):
        return (ref + timedelta(days=2)).isoformat()
    if compact in ("글피",):
        return (ref + timedelta(days=3)).isoformat()
    if compact in ("어제", "작일", "yesterday"):
        return (ref - timedelta(days=1)).isoformat()

    m_days_after = re.search(r"(\d{1,3})\s*일\s*(?:후|뒤)", s)
    if m_days_after:
        return (ref + timedelta(days=int(m_days_after.group(1)))).isoformat()

    m_week = re.search(r"(이번\s*주|금주|다음\s*주|차주)\s*([월화수목금토일])(?:요일)?", s)
    if m_week:
        week_kw = m_week.group(1).replace(" ", "")
        target_wd = _WEEKDAY_KO_MAP[m_week.group(2)]
        monday = ref - timedelta(days=ref.weekday())
        if week_kw in ("다음주", "차주"):
            monday += timedelta(days=7)
        return (monday + timedelta(days=target_wd)).isoformat()

    s_date, _ = normalize_date_str(s, default_year=ref.year)
    return s_date


def is_calendar_query_intent(text: str) -> bool:
    """
    사용자 입력이 일정·할일 '등록/추출'이 아니라 기존 일정 '조회/요약/빈 시간 확인' 질문인지 판별합니다.
    """
    s = str(text or "").strip()
    if not s or len(s) > 220:
        return False
    # JSON 직접 입력이나 등록/추가 명령어가 명확히 포함된 경우 등록으로 간주
    if s.startswith("{") or s.startswith("[") or "```" in s:
        return False
    reg_keywords = ("등록", "추가해", "넣어", "저장해", "기입", "메모해", "일괄")
    if any(kw in s for kw in reg_keywords):
        return False
    query_keywords = (
        "요약",
        "알려",
        "조회",
        "확인",
        "뭐 있",
        "무슨 일정",
        "어떤 일정",
        "빈 시간",
        "비어",
        "비었",
        "일정 있",
        "할일 있",
        "할 일 있",
        "스케줄",
        "보여",
        "몇 시",
        "언제야",
        "언제인가",
    )
    if s.endswith("?") or any(kw in s for kw in query_keywords):
        return True
    return False


def try_parse_direct_calendar_json(
    raw_text: str,
    today: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """
    사용자가 입력창에 순수 JSON 객체/배열 또는 단일 ```json ... ``` 코드블록 전체를 직접 붙여넣은 경우에만
    AI 호출 없이 즉시 parse_calendar_ai_response()로 파싱해 반환합니다.
    (긴 프롬프트 지시문이나 문서 원문 중간에 포함된 스키마 예시는 절대 가로채지 않습니다.)
    """
    s = str(raw_text or "").strip()
    if not s:
        return None
    if "--- [추출된 학사일정 원문] ---" in s or s.startswith("[원본 파일:"):
        return None

    candidate = ""
    m_block = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", s, re.IGNORECASE)
    if m_block:
        candidate = m_block.group(1).strip()
    elif (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        candidate = s
    else:
        return None

    if not candidate or not (
        (candidate.startswith("{") and candidate.endswith("}"))
        or (candidate.startswith("[") and candidate.endswith("]"))
    ):
        return None

    try:
        loaded = json.loads(candidate)
    except Exception:
        return None

    if not isinstance(loaded, (dict, list)):
        return None

    try:
        parsed = parse_calendar_ai_response(loaded, today=today)
    except Exception:
        return None
    if not parsed.get("schedules") and not parsed.get("todos"):
        return None
    return parsed


def build_calendar_ai_prompt(
    user_input: str,
    today: Optional[date] = None,
    schedule_context: str = "",
) -> Tuple[str, str]:
    """
    자연어 일정 요청·공문/공지 복붙 또는 내 일정 질문에 대응하기 위한
    (system_prompt, user_prompt) 튜플을 생성합니다.
    """
    ref = today or date.today()
    wd_names = ("월", "화", "수", "목", "금", "토", "일")
    today_label = f"{ref.isoformat()} ({wd_names[ref.weekday()]}요일)"
    categories_str = ", ".join(VALID_SCHEDULE_CATEGORIES)
    ctx_clean = str(schedule_context or "").strip()
    is_query = is_calendar_query_intent(user_input) and bool(ctx_clean)

    if is_query:
        system_prompt = (
            "당신은 한국 학교 교사의 캘린더·할일 비서입니다.\n"
            f"- 기준 오늘 날짜: {today_label} (YYYY-MM-DD: {ref.isoformat()})\n"
            "- 아래 제공된 [내 캘린더·할일 현황]을 바탕으로 사용자의 질문(일정 요약, 빈 시간 확인, 마감 할일 조회 등)에 "
            "한국어로 간결하고 정확하게 답변하세요.\n"
            "- 조회/질문 응답이므로 JSON이 아닌 읽기 쉬운 일반 텍스트(불릿·시간순 정리)로 답변하세요."
        )
        user_prompt = (
            f"[기준 날짜: {today_label}]\n\n"
            f"--- [내 캘린더·할일 현황] ---\n{ctx_clean}\n\n"
            f"--- [사용자 질문] ---\n{str(user_input or '').strip()}"
        )
        return system_prompt, user_prompt

    system_prompt = (
        "당신은 한국 학교 교사의 학사일정 및 업무 할일(To-Do) 정리 비서입니다.\n"
        f"- 기준 오늘 날짜: {today_label} (YYYY-MM-DD: {ref.isoformat()})\n"
        "- 사용자가 입력한 자연어 요청이나 긴 공지글·회의록·공문·가정통신문에서 '일정(schedule)'과 '할일(todo)'을 모두 빠짐없이 추출하세요.\n"
        "- 시간·장소가 정해진 행사/회의/수업/출장은 schedule, 기한까지 처리해야 할 업무/제출/준비는 todo로 분류하세요.\n"
        "- 상대 날짜('내일', '모레', '이번 주 금요일', '다음 주 월요일')는 반드시 기준 오늘 날짜를 바탕으로 YYYY-MM-DD 형식으로 계산하세요.\n"
        f"- 허용 카테고리(category): {categories_str} (반드시 이 4가지 중 하나만 사용, 기본: '학사일정')\n"
        "- 허용 우선순위(priority): 높음, 보통, 낮음 (반드시 이 3가지 중 하나만 사용, 기본: '보통')\n"
        "- 허용 알림(reminder): none, 10m, 1h, morning_9am 중 하나 (명시되지 않으면 \"none\")\n"
        "- 시간(start_time)은 24시간제 HH:MM 형식이며, 시간이 없는 종일 일정은 all_day=true, start_time=\"\" 로 설정하세요.\n"
        "- 반드시 설명이나 서문 없이 아래 스키마 중 하나를 따르는 단일 ```json ... ``` 코드블록(또는 순수 JSON)으로만 응답하세요.\n\n"
        "```json\n"
        "{\n"
        '  "summary": "추출 요약",\n'
        '  "schedules": [\n'
        '    {"item_type": "schedule", "title": "...", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "start_time": "HH:MM", "all_day": true, "category": "학사일정", "reminder": "none", "memo": "..."}\n'
        "  ],\n"
        '  "todos": [\n'
        '    {"item_type": "todo", "title": "...", "due_date": "YYYY-MM-DD", "priority": "보통", "memo": "..."}\n'
        "  ]\n"
        "}\n"
        "```"
    )

    ctx_block = (
        f"\n\n--- [참고: 내 캘린더·할일 현황] ---\n{ctx_clean}"
        if ctx_clean
        else ""
    )
    user_prompt = (
        f"[기준 날짜: {today_label}]\n"
        "아래 내용에서 캘린더 일정(schedule)과 할일(todo)을 추출하여 단일 ```json ... ``` 블록으로 응답해 주세요.\n\n"
        f"{str(user_input or '').strip()}{ctx_block}"
    )
    return system_prompt, user_prompt


ALLOWED_CALENDAR_AI_KEYS = frozenset(
    {
        "item_type",
        "type",
        "title",
        "summary",
        "task",
        "start_date",
        "end_date",
        "due_date",
        "start_time",
        "all_day",
        "category",
        "priority",
        "reminder",
        "reminder_type",
        "memo",
        "description",
    }
)
MAX_CALENDAR_TITLE_LEN = 200
MAX_CALENDAR_MEMO_LEN = 1000
MAX_CALENDAR_SUMMARY_LEN = 500
MAX_CALENDAR_AI_BATCH_ITEMS = 500

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script>", re.IGNORECASE)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>[\s\S]*?</style>", re.IGNORECASE)
_EVENT_ATTR_RE = re.compile(
    r"\bon[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
_DANGEROUS_URI_RE = re.compile(
    r"(?:javascript|vbscript|data\s*:\s*text/html)\s*:", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_schedule_text(raw_val: Any, max_len: int = MAX_CALENDAR_TITLE_LEN) -> str:
    """
    외부 AI/JSON 입력 문자열에서 제어문자, <script>/<style> 블록, 인라인 이벤트(onerror= 등),
    위험 URI 스킴, HTML 태그를 제거하고 최대 길이(max_len)로 제한합니다.
    """
    if raw_val is None or isinstance(raw_val, (dict, list, tuple, set)):
        return ""
    s = str(raw_val)
    s = _CONTROL_CHAR_RE.sub("", s)
    s = _SCRIPT_BLOCK_RE.sub("", s)
    s = _STYLE_BLOCK_RE.sub("", s)
    s = _EVENT_ATTR_RE.sub("", s)
    s = _DANGEROUS_URI_RE.sub("", s)
    s = _HTML_TAG_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n").strip()
    if max_len > 0 and len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def escape_spreadsheet_formula(val: Any) -> str:
    """
    Excel/CSV 내보내기 시 수식 인젝션(Formula Injection)을 방지하기 위해
    '=', '+', '-', '@', '\\t', '\\r'로 시작하는 문자열 앞에 작은따옴표(')를 붙입니다.
    """
    s = str(val if val is not None else "")
    stripped = s.lstrip(" \t\r\n")
    if stripped and stripped[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


_DUMMY_SCHEDULE_TITLES = frozenset(
    {"...", "…", "일정명", "할일", "제목", "예시", "sample", "example"}
)


def _is_dummy_schedule_title(title: str) -> bool:
    t = str(title or "").strip()
    if not t:
        return True
    if t.lower() in _DUMMY_SCHEDULE_TITLES:
        return True
    if re.fullmatch(r"[.\u2026·\-_\s]+", t):
        return True
    return False


def _is_valid_iso_date(date_str: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date_str or "")):
        return False
    try:
        date.fromisoformat(date_str)
        return True
    except ValueError:
        return False


def parse_calendar_ai_response(
    raw_response: Any,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    AI 응답(마크다운 ```json 블록, JSON 배열 또는 일반 JSON/dict)을 파싱하여
    화이트리스트 스키마 검증·새니타이징·길이/건수 제한을 거친
    정규화된 {"summary": str, "schedules": [...], "todos": [...]} 딕셔너리를 반환합니다.
    """
    ref = today or date.today()
    if isinstance(raw_response, list):
        parsed: Any = {"summary": f"학사일정 {len(raw_response)}건 추출", "schedules": raw_response, "todos": []}
    elif isinstance(raw_response, dict):
        parsed = raw_response
    else:
        parsed = extract_json_from_llm_response(str(raw_response or ""))

    if isinstance(parsed, list):
        parsed = {"summary": f"학사일정 {len(parsed)}건 추출", "schedules": parsed, "todos": []}

    if not isinstance(parsed, dict):
        raise ValueError("AI 응답이 JSON 객체 또는 배열 형식이 아닙니다.")

    summary = sanitize_schedule_text(parsed.get("summary"), max_len=MAX_CALENDAR_SUMMARY_LEN)
    raw_schedules = parsed.get("schedules")
    if not isinstance(raw_schedules, list):
        # extract_json_from_llm_response()가 최상위 JSON 배열을 {"items": [...]}로 감싸는 경우 지원
        if isinstance(parsed.get("items"), list):
            raw_schedules = parsed.get("items")
        elif parsed.get("title") and parsed.get("start_date"):
            raw_schedules = [parsed]
        else:
            raw_schedules = []
    raw_todos = parsed.get("todos")
    if not isinstance(raw_todos, list):
        raw_todos = []

    norm_schedules: List[Dict[str, Any]] = []
    norm_todos: List[Dict[str, Any]] = []

    def _whitelist_item(raw_item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw_item, dict):
            return None
        return {k: v for k, v in raw_item.items() if k in ALLOWED_CALENDAR_AI_KEYS}

    def _total_count() -> int:
        return len(norm_schedules) + len(norm_todos)

    def _append_todo_item(item: Dict[str, Any]) -> None:
        if _total_count() >= MAX_CALENDAR_AI_BATCH_ITEMS:
            return
        title = sanitize_schedule_text(
            item.get("title") or item.get("task") or item.get("summary"),
            max_len=MAX_CALENDAR_TITLE_LEN,
        )
        if _is_dummy_schedule_title(title):
            return
        raw_due = item.get("due_date") or item.get("end_date") or item.get("start_date")
        due_date = normalize_ai_date_str(raw_due, today=ref)
        if due_date and not _is_valid_iso_date(due_date):
            due_date = ""
        priority = normalize_todo_priority(item.get("priority"))
        memo = sanitize_schedule_text(
            item.get("memo") or item.get("description"),
            max_len=MAX_CALENDAR_MEMO_LEN,
        )
        norm_todos.append(
            {
                "title": title,
                "due_date": due_date,
                "priority": priority,
                "memo": memo,
            }
        )

    for raw_item in raw_schedules:
        if _total_count() >= MAX_CALENDAR_AI_BATCH_ITEMS:
            break
        item = _whitelist_item(raw_item)
        if item is None:
            continue
        item_type_raw = str(item.get("item_type") or item.get("type") or "schedule").strip().lower()
        item_type = item_type_raw if item_type_raw in ("schedule", "todo") else "schedule"
        if item_type == "todo":
            _append_todo_item(item)
            continue
        title = sanitize_schedule_text(
            item.get("title") or item.get("summary"),
            max_len=MAX_CALENDAR_TITLE_LEN,
        )
        if _is_dummy_schedule_title(title):
            continue

        s_date = normalize_ai_date_str(item.get("start_date"), today=ref)
        if not _is_valid_iso_date(s_date):
            s_date = ref.isoformat()
        e_date = normalize_ai_date_str(item.get("end_date"), today=ref)
        if not _is_valid_iso_date(e_date) or e_date < s_date:
            e_date = s_date

        all_day_raw = item.get("all_day")
        s_time = normalize_time_str(item.get("start_time"))
        if not re.fullmatch(r"\d{2}:\d{2}", s_time):
            s_time = ""
        if all_day_raw is True or (isinstance(all_day_raw, str) and all_day_raw.strip().lower() in ("true", "1", "y", "예")):
            all_day = True
            s_time = ""
        elif not s_time:
            all_day = True
        else:
            all_day = False

        cat = infer_schedule_category(title, str(item.get("category") or ""))
        if cat not in VALID_SCHEDULE_CATEGORIES:
            cat = "학사일정"
        reminder = normalize_reminder_type(item.get("reminder") or item.get("reminder_type") or "none")
        memo = sanitize_schedule_text(
            item.get("memo") or item.get("description"),
            max_len=MAX_CALENDAR_MEMO_LEN,
        )

        norm_schedules.append(
            {
                "title": title,
                "start_date": s_date,
                "end_date": e_date,
                "start_time": s_time,
                "all_day": all_day,
                "category": cat,
                "reminder_type": reminder,
                "memo": memo,
            }
        )

    for raw_item in raw_todos:
        if _total_count() >= MAX_CALENDAR_AI_BATCH_ITEMS:
            break
        item = _whitelist_item(raw_item)
        if item is None:
            continue
        _append_todo_item(item)

    return {
        "summary": summary,
        "schedules": norm_schedules,
        "todos": norm_todos,
    }


def format_hwpx_schedule_text(doc_or_path: Any) -> str:
    """
    HWPX AI 문서 에이전트의 DOM 맵 추출 로직(`extract_hwpx_dom_map`)을 재사용하여
    문단([p_...])과 표([tbl_...])의 행/열 경계(` | `)가 보존된 텍스트를 생성합니다.
    """
    from services.hwpx_agent_service import extract_hwpx_dom_map

    dom_map = extract_hwpx_dom_map(doc_or_path)
    lines: List[str] = []
    for blk in dom_map.get("blocks") or []:
        if not isinstance(blk, dict):
            continue
        blk_id = str(blk.get("id") or "").strip()
        if blk.get("type") == "table":
            if blk_id:
                lines.append(f"[{blk_id}]")
            for row in blk.get("rows") or []:
                row_txt = " | ".join(
                    str((c.get("text", "") if isinstance(c, dict) else c) or "")
                    .replace("\n", " / ")
                    .strip()
                    for c in row
                )
                if row_txt.strip(" |"):
                    lines.append(row_txt)
        else:
            txt = str(blk.get("text", "")).strip()
            if txt:
                lines.append(f"[{blk_id}] {txt}" if blk_id else txt)
    return "\n".join(lines).strip()


def extract_schedule_text_from_document(
    file_path: str | Path, max_chars: int = 24000
) -> Tuple[str, bool]:
    """
    HWP, HWPX, PDF 학사일정 문서에서 표(행/셀)와 본문 텍스트를 로컬로 1차 추출합니다.
    - .hwp 파일은 HWPX AI 문서 에이전트와 동일한 한컴 COM 변환(`convert_hwp_to_hwpx`) +
      DOM 표/문단 추출(`format_hwpx_schedule_text`)을 우선 사용하고, 불가 시 olefile로 폴백합니다.
    반환값: (extracted_text, is_scanned_pdf)
    """
    import tempfile

    from services.hwpx_agent_service import convert_hwp_to_hwpx
    from services.multi_doc_extractor_service import (
        HAS_OLEFILE,
        PDF_MIN_CHARS_PER_PAGE,
        _extract_hwp_text_olefile,
        _extract_pdf_text_layers,
        extract_raw_text_preview,
        parse_hwpx_content,
    )

    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {p}")

    ext = p.suffix.lower()
    if ext not in (".hwp", ".hwpx", ".pdf"):
        raise ValueError(
            f"지원하지 않는 파일 형식입니다 ({ext}). .hwp, .hwpx, .pdf 파일만 지원합니다."
        )

    if ext == ".pdf":
        pages, avg = _extract_pdf_text_layers(p)
        txt = "\n\n".join(pages).strip()
        if avg < PDF_MIN_CHARS_PER_PAGE or not txt:
            return (
                "(스캔/이미지 기반 PDF 문서입니다. 첨부된 PDF 파일을 직접 판독하여 학사일정을 추출해 주세요.)",
                True,
            )
        if max_chars > 0 and len(txt) > max_chars:
            txt = txt[:max_chars] + "\n... (이하 생략)"
        return txt, False

    def _extract_from_hwpx_path(hwpx_p: Path) -> str:
        try:
            dom_txt = format_hwpx_schedule_text(hwpx_p)
            if dom_txt:
                return dom_txt
        except Exception:
            pass
        parsed = parse_hwpx_content(hwpx_p)
        txt = str(parsed.get("raw_text") or "").strip()
        if not txt and parsed.get("tables"):
            lines = [
                " | ".join(str(c or "").strip() for c in row)
                for tbl in parsed["tables"]
                for row in tbl
            ]
            txt = "\n".join(ln for ln in lines if ln).strip()
        return txt

    extracted = ""
    if ext == ".hwpx":
        extracted = _extract_from_hwpx_path(p)
    else:
        # 1순위: HWP -> 임시 HWPX 변환 후 문단/표([p_...], [tbl_...]) 구조 추출
        try:
            with tempfile.TemporaryDirectory(prefix="eduflow_hwp_sched_") as tmp_dir:
                temp_hwpx = Path(tmp_dir) / f"{p.stem}.hwpx"
                conv = convert_hwp_to_hwpx(p, temp_hwpx)
                if conv and conv.exists():
                    extracted = _extract_from_hwpx_path(conv)
        except Exception:
            extracted = ""

        # 2순위(폴백): 한컴 COM 변환이 불가한 환경에서는 olefile로 추출
        if not extracted and HAS_OLEFILE:
            try:
                extracted = str(_extract_hwp_text_olefile(p) or "").strip()
            except Exception:
                extracted = ""

        if not extracted:
            preview = extract_raw_text_preview(p, max_chars=max_chars)
            if preview and not preview.startswith("("):
                extracted = preview.strip()

    if not extracted:
        raise ValueError(
            "HWP/HWPX 문서에서 텍스트 또는 표를 추출하지 못했습니다. "
            "암호화된 문서이거나 이미지 스캔본인지 확인해 주세요."
        )

    if max_chars > 0 and len(extracted) > max_chars:
        extracted = extracted[:max_chars] + "\n... (이하 생략)"
    return extracted, False


def extract_schedule_text_from_hwp_hwpx(
    file_path: str | Path, max_chars: int = 24000
) -> str:
    """HWP/HWPX 학사일정 문서에서 표와 본문 텍스트를 추출합니다."""
    text, _ = extract_schedule_text_from_document(file_path, max_chars=max_chars)
    return text


def build_hwp_schedule_ai_prompt(
    extracted_text: str,
    file_name: str = "",
    today: Optional[date] = None,
) -> str:
    """
    HWP/HWPX/PDF에서 추출된 학사일정 텍스트를 AI 실행 패널(calendar_todo)에 전달하기 위한
    구조화 JSON 배열 추출 프롬프트를 생성합니다.
    """
    ref = today or date.today()
    src_line = f"[원본 파일: {file_name}]\n" if file_name else ""
    return (
        f"{src_line}"
        f"기준 연도/날짜: {ref.isoformat()}\n"
        "아래 학사일정 문서(표 및 본문)에서 모든 학사일정을 추출하여 "
        "반드시 아래 형식의 순수 JSON 배열(또는 단일 ```json 코드블록)로만 응답해 주세요.\n"
        '- 출력 스키마: [{"title": "<일정명>", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", '
        '"category": "학사일정", "memo": "<대상 학년·장소 등 부가 메모>"}]\n'
        "- category는 '학사일정', '수행평가', '업무', '개인' 중 하나로 분류하세요 (기본: '학사일정').\n"
        "- [지침 1 - 변경/수정 문서 처리] 문서에 '변경 전'과 '변경 후' 표나 항목이 함께 있으면 '변경 전' 일정은 제외하고 최종 확정/'변경 후' 일정만 추출하세요.\n"
        "- [지침 2 - 학년도·연도 전환 처리] 2학기 학사일정 표처럼 8~12월 뒤에 1월 또는 2월이 나오면 기준 연도의 다음 해(기준 연도 + 1)로 계산해 YYYY-MM-DD를 채우세요. (문서에 명시된 학년도가 있으면 해당 학년도 기준 적용)\n"
        "- [지침 3 - 다중 일정 및 기간 분리] 한 셀에 여러 일정이 쉼표(,)나 가운뎃점(·) 등으로 묶여 있으면 각각 별도의 JSON 객체로 분리하고, '6일-8일' 또는 '5.1~5.3' 같은 기간은 start_date와 end_date로 정확히 나누세요. (단일 날짜는 start_date와 end_date를 동일하게 설정)\n"
        "- [지침 4 - 세부 정보 보존] '모의수능(3)', '전국연합(인천-1,2)' 등 대상 학년이나 부가 설명은 title 또는 memo에 누락 없이 반영하세요.\n"
        "- [지침 5 - 순수 JSON만 출력] 인사말·설명 문구·요약 해설 없이 순수 JSON 배열(또는 ```json ... ``` 코드블록)만 출력하세요.\n\n"
        "--- [추출된 학사일정 원문] ---\n"
        f"{str(extracted_text or '').strip()}"
    )


def calculate_dday_badge(
    due_date_str: str, today: Optional[date] = None
) -> Tuple[str, str]:
    """
    마감일('YYYY-MM-DD') 기준으로 (배지 텍스트, QSS role)을 계산합니다.
    """
    raw = str(due_date_str or "").strip()
    if not raw:
        return "", "badgeNeutral"
    try:
        due_d = date.fromisoformat(raw[:10])
    except ValueError:
        return "", "badgeNeutral"

    ref = today or date.today()
    delta = (due_d - ref).days
    if delta < 0:
        return f"D+{abs(delta)}", "badgeDanger"
    if delta == 0:
        return "오늘 마감", "badgeDanger"
    if delta <= 2:
        return f"D-{delta}", "badgeWarning"
    return f"D-{delta}", "badgePrimary"


class ScheduleReminderChecker:
    """임박한 일정(10분 전, 1시간 전, 당일 오전 09:00)을 점검하고 알림을 발송하는 헬퍼."""

    @staticmethod
    def _compute_trigger_window(
        start_date_str: str, start_time_str: str, reminder_type: str
    ) -> Optional[Tuple[datetime, datetime]]:
        try:
            s_date = date.fromisoformat(start_date_str[:10])
        except ValueError:
            return None

        t_clean = normalize_time_str(start_time_str)
        if t_clean:
            hh, mm = map(int, t_clean.split(":"))
            event_dt = datetime.combine(s_date, time(hh, mm))
        else:
            event_dt = datetime.combine(s_date, time(9, 0))

        if reminder_type == "10m":
            return event_dt - timedelta(minutes=10), event_dt
        if reminder_type == "1h":
            return event_dt - timedelta(hours=1), event_dt
        if reminder_type == "morning_9am":
            trigger_dt = datetime.combine(s_date, time(9, 0))
            return trigger_dt, event_dt
        return None

    @classmethod
    def check_and_trigger_reminders(
        cls,
        db: DatabaseManager,
        notifier: Optional[NotificationService] = None,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        current_now = now or datetime.now()
        pending = db.get_pending_reminder_schedules()
        triggered: List[Dict[str, Any]] = []

        for sch in pending:
            sch_id = int(sch["id"])
            r_type = str(sch.get("reminder_type", "none") or "none").strip()
            win = cls._compute_trigger_window(
                str(sch.get("start_date", "")),
                str(sch.get("start_time", "")),
                r_type,
            )
            if win is None:
                continue
            trigger_dt, event_dt = win

            # 너무 오래 지난 과거 일정은 오발송 방지
            if current_now > event_dt + timedelta(hours=12):
                continue
            if current_now < trigger_dt:
                continue

            db.mark_schedule_reminder_sent(sch_id)

            r_label = REMINDER_LABELS.get(r_type, r_type)
            cat = str(sch.get("category", "학사일정"))
            title = str(sch.get("title", "일정"))
            time_part = f" ({sch['start_time']})" if sch.get("start_time") else ""
            alert_title = f"일정 알림 ({r_label})"
            alert_msg = f"[{cat}] {title}{time_part} — {sch.get('start_date', '')}"

            db.add_notification(
                title=alert_title,
                message=alert_msg,
                channel="schedule_reminder",
                sender="캘린더 알림",
                remote_id=f"reminder_{sch_id}_{sch.get('start_date', '')}_{r_type}",
                is_read=0,
            )
            if notifier is not None:
                notifier.notify(alert_title, alert_msg)

            triggered.append(
                {
                    "schedule_id": sch_id,
                    "title": alert_title,
                    "message": alert_msg,
                    "reminder_type": r_type,
                }
            )

        return triggered
