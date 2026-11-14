"""
학사일정 파일 파서(.xlsx / .csv / .ics), D-Day 계산, ICS 내보내기 및 일정 임박 알림 체크 서비스.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import openpyxl

from core.database import DatabaseManager
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

        memo = str(_cell("memo") or "").strip()

        parsed.append(
            {
                "title": title,
                "category": category,
                "start_date": s_date,
                "end_date": e_date or s_date,
                "start_time": s_time,
                "reminder_type": "none",
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
    """확장자(.xlsx, .csv, .ics)에 따라 적절한 학사일정 파서를 호출합니다."""
    ext = Path(file_path).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return parse_xlsx_schedules(file_path, default_year=default_year)
    if ext == ".csv":
        return parse_csv_schedules(file_path, default_year=default_year)
    if ext == ".ics":
        return parse_ics_schedules(file_path)
    raise ValueError(f"지원하지 않는 학사일정 파일 형식입니다: {ext} (.xlsx, .csv, .ics 지원)")


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
