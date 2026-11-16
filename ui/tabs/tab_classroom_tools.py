"""
[탭 4] 학급 경영 도구함 (ui/tabs/tab_classroom_tools.py)
- EduFlow Slate Workstation 2x2 Bento Instrument Card 레이아웃 적용 (Zero-Emoji, 1px #E2E8F0 정적 카드)
- Card 1: 출결 및 결석계 관리 (Attendance Tracker) — 출석/질병결석/체험학습 배지, 증빙 대장, 결석계 생성 / 출석인정서 출력
- Card 2: 청소 당번 / 1인 1역 랜덤 배정기 (Duty Shuffler) — _on_random_assign() 연동, 학번/이름/담당구역/상태 테이블
- Card 3: 자리 배치 및 모둠 편성기 (Seating Grid 6x5) — 칠판/교탁 기준 6x5 좌석 그리드, 자리 무작위 배치 / 좌석표 내보내기
- Card 4: 학생 관찰 및 상담 메모장 (Observation Log) — 학생 선택 콤보박스, 한줄 관찰 기록 입력, 최근 상담/관찰 이력 리스트
"""

import csv
import json
import os
import random
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QDate, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (
    QClipboard,
    QColor,
    QFont,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPen,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.batch_prompt_service import extract_json_from_llm_response
from services.firestore_sync_service import FirestoreSyncService, hash_password_sha256
from services.timetable_manual_service import (
    build_manual_timetable_change,
    parse_weekly_timetable_file,
)
from services.hwpx_agent_service import (
    HwpxDocument,
    apply_hwpx_operations,
    convert_hwpx_to_hwp,
    create_minimal_hwpx_document,
)
from services.roster_parser_service import parse_homeroom_roster
from ui.components.file_drop_helper import enable_file_drop
from ui.components.hover_zoom_card import HoverZoomCard
from ui.utils import install_no_scroll_wheel_filter
from utils.path_helper import get_data_path, get_resource_path


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


NOTICE_OCR_JSON_PROMPT = (
    "첨부된 가정통신문 PDF(또는 이미지)를 정확히 판독하여 아래 JSON 스키마로만 응답하세요.\n"
    "코드블록(```json ... ```)을 포함해도 되며, 다른 설명 문장은 출력하지 마세요.\n"
    "{\n"
    '  "title": "가정통신문 제목",\n'
    '  "deadline": "YYYY-MM-DD",\n'
    '  "body": "핵심 안내 본문 요약 및 원문 정리",\n'
    '  "reply_fields": ["참가 동의 여부 (동의/미동의)", "비상연락처"]\n'
    "}"
)


def parse_notice_ai_json(raw_or_dict: Any) -> Dict[str, Any]:
    """AiExecutionDock(API/수동복붙/Playwright) 가정통신문 OCR JSON 응답을 정규화합니다."""
    if isinstance(raw_or_dict, dict):
        data = dict(raw_or_dict)
    else:
        raw_text = str(raw_or_dict or "").strip()
        if not raw_text:
            raise ValueError("가정통신문 JSON 응답이 비어 있습니다.")
        try:
            data = extract_json_from_llm_response(raw_text)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", raw_text)
            if not m:
                raise ValueError("응답에서 JSON 객체를 찾을 수 없습니다.")
            data = json.loads(m.group(0))

    if not isinstance(data, dict):
        raise ValueError("가정통신문 JSON 응답은 객체(dict) 형태여야 합니다.")

    raw_fields = data.get("reply_fields")
    if isinstance(raw_fields, list):
        reply_fields = [str(x).strip() for x in raw_fields if str(x).strip()]
    elif isinstance(raw_fields, str):
        reply_fields = [x.strip() for x in raw_fields.split(",") if x.strip()]
    else:
        reply_fields = []

    return {
        "title": str(data.get("title") or "").strip(),
        "deadline": str(data.get("deadline") or data.get("due_date") or "").strip(),
        "body": str(data.get("body") or data.get("content") or "").strip(),
        "reply_fields": reply_fields,
    }


SAMPLE_ROSTER: List[Tuple[str, str]] = [
    ("10101", "강민서"),
    ("10102", "김도윤"),
    ("10103", "김하은"),
    ("10104", "박시우"),
    ("10105", "서준우"),
    ("10106", "안채원"),
    ("10107", "이서진"),
    ("10108", "윤지민"),
    ("10109", "이지훈"),
    ("10110", "장하율"),
    ("10111", "정예준"),
    ("10112", "조유진"),
    ("10113", "최민혁"),
    ("10114", "한서아"),
    ("10115", "배수아"),
    ("10116", "송우진"),
    ("10117", "곽시은"),
    ("10118", "노현우"),
]

DUTY_AREAS: List[str] = [
    "칠판 및 교탁 정돈",
    "분리수거 및 쓰레기통",
    "창문 환기 및 소등",
    "교실 앞쪽 바닥 쓸기",
    "교실 뒤쪽 바닥 쓸기",
    "복도 및 사물함 정리",
    "기자재 및 전자교탁 점검",
    "우유급식 및 배식 도우미",
]


CLEANING_DUTY_COUNT = 6
BULLETIN_DUTY_COUNT = 2
CLEANING_TEMPLATE_REL_PATH = "assets/cleaning_notice_template.hwpx"


WEEKDAY_KR = ("월", "화", "수", "목", "금", "토", "일")


def _parse_iso_dates(date_strs: List[str]) -> List[date]:
    parsed: Set[date] = set()
    for raw in date_strs or []:
        s = str(raw).strip()
        if not s:
            continue
        try:
            parsed.add(date.fromisoformat(s))
        except ValueError:
            continue
    return sorted(parsed)


def default_weekly_dates(base_dt: Optional[datetime] = None) -> List[str]:
    """기본 주간 당번 날짜 목록(월~금 5일, 토·일요일에는 다가오는 다음 주 월~금)을 ISO 문자열로 반환합니다."""
    now_d = (base_dt or datetime.now()).date()
    wd = now_d.weekday()  # 0=월 .. 6=일
    monday = now_d + timedelta(days=(7 - wd)) if wd >= 5 else now_d - timedelta(days=wd)
    return [(monday + timedelta(days=offset)).isoformat() for offset in range(5)]


def format_duty_dates_korean(date_strs: List[str]) -> str:
    """
    선택된 날짜 목록을 한글 양식용 기간 문자열로 포맷합니다.
    - 연속된 날짜(예: 월~목): '11월 16일 ~ 11월 19일'
    - 격일·개별 날짜(예: 월·수·금): '11월 16일, 18일, 20일' (월 변경 시 '11월 30일, 12월 2일')
    """
    dates = _parse_iso_dates(date_strs)
    if not dates:
        return ""
    if len(dates) == 1:
        d0 = dates[0]
        return f"{d0.month}월 {d0.day}일"

    is_consecutive = all(
        (dates[i + 1] - dates[i]).days == 1 for i in range(len(dates) - 1)
    )
    if is_consecutive:
        start_d, end_d = dates[0], dates[-1]
        return f"{start_d.month}월 {start_d.day}일 ~ {end_d.month}월 {end_d.day}일"

    parts: List[str] = []
    prev_month: Optional[int] = None
    for d in dates:
        if prev_month != d.month:
            parts.append(f"{d.month}월 {d.day}일")
            prev_month = d.month
        else:
            parts.append(f"{d.day}일")
    return ", ".join(parts)


def shift_duty_dates(date_strs: List[str], delta_days: int = 7) -> List[str]:
    """선택된 모든 날짜를 delta_days(기본 +7일)만큼 이동하여 동일한 요일·격일 패턴을 다음 주로 넘깁니다."""
    dates = _parse_iso_dates(date_strs)
    if not dates:
        dates = _parse_iso_dates(default_weekly_dates())
    return [(d + timedelta(days=delta_days)).isoformat() for d in dates]


def default_weekly_period_text(base_dt: Optional[datetime] = None) -> str:
    """주간 당번 안내용 기본 기간 문자열(월~금, 토·일요일에는 다음 주 월~금)을 생성합니다."""
    return format_duty_dates_korean(default_weekly_dates(base_dt))


def _infer_student_number(raw_number: Any, student_id: str, fallback_seq: int) -> int:
    try:
        n = int(raw_number)
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    sid_digits = "".join(ch for ch in str(student_id) if ch.isdigit())
    if len(sid_digits) >= 4:
        tail = int(sid_digits[-2:])
        if tail > 0:
            return tail
    elif sid_digits:
        val = int(sid_digits)
        if val > 0:
            return val
    return max(1, fallback_seq)


def compute_single_track_rotation(
    students: List[Dict[str, Any]],
    target_count: int,
    start_num: int,
    excluded_ids: List[str],
    reassign_ids: List[str],
) -> Dict[str, Any]:
    """
    단일 당번 트랙(교실 청소 6명 또는 게시판 2명)의 주간 배정 명단과 다음 시작 번호를 계산합니다.
    - 제외 명단(결석·열외) 학생은 이번 배정에서 건너뜁니다.
    - 추가(재배정) 학생을 우선 포함하되 총 인원(target_count)을 그대로 유지합니다.
    - 최종 배정 명단은 항상 번호순(오름차순)으로 정렬됩니다.
    """
    ordered = sorted(
        students,
        key=lambda s: (int(s.get("number", 0)), str(s.get("student_id", ""))),
    )
    if not ordered or target_count <= 0:
        return {
            "assigned": [],
            "next_start_num": max(1, int(start_num or 1)),
            "consumed_reassign_ids": [],
        }

    excl_set = {str(x).strip() for x in (excluded_ids or []) if str(x).strip()}
    re_set = {str(x).strip() for x in (reassign_ids or []) if str(x).strip()}

    eligible = [s for s in ordered if str(s["student_id"]) not in excl_set]
    forced = [s for s in eligible if str(s["student_id"]) in re_set]

    if len(forced) >= target_count:
        picked_forced = forced[:target_count]
        picked_regular: List[Dict[str, Any]] = []
        next_start = max(1, int(start_num or 1))
    else:
        picked_forced = list(forced)
        need = target_count - len(picked_forced)
        forced_ids = {str(s["student_id"]) for s in picked_forced}
        pool = [s for s in eligible if str(s["student_id"]) not in forced_ids]
        if not pool:
            picked_regular = []
            next_start = max(1, int(start_num or 1))
        else:
            idx0 = next(
                (i for i, s in enumerate(pool) if int(s["number"]) >= int(start_num or 1)),
                0,
            )
            take = min(need, len(pool))
            picked_regular = [pool[(idx0 + offset) % len(pool)] for offset in range(take)]
            last_sid = str(picked_regular[-1]["student_id"])
            pos_in_all = next(
                (i for i, s in enumerate(ordered) if str(s["student_id"]) == last_sid),
                0,
            )
            next_start = int(ordered[(pos_in_all + 1) % len(ordered)]["number"])

    combined: List[Dict[str, Any]] = []
    for s in picked_forced:
        combined.append({**s, "mode": "추가(재배정)"})
    for s in picked_regular:
        combined.append({**s, "mode": "정규 순번"})

    combined.sort(key=lambda s: (int(s["number"]), str(s["student_id"])))
    return {
        "assigned": combined,
        "next_start_num": next_start,
        "consumed_reassign_ids": [str(s["student_id"]) for s in picked_forced],
    }


def compute_weekly_duty_rotation(
    students: List[Dict[str, Any]],
    cleaning_start_num: int,
    bulletin_start_num: int,
    excluded_ids: List[str],
    cleaning_reassign_ids: List[str],
    bulletin_reassign_ids: List[str],
) -> Dict[str, Any]:
    """교실 청소(6명)와 게시판(2명)을 각각 독립된 번호순 포인터로 계산합니다."""
    clean_res = compute_single_track_rotation(
        students=students,
        target_count=CLEANING_DUTY_COUNT,
        start_num=cleaning_start_num,
        excluded_ids=excluded_ids,
        reassign_ids=cleaning_reassign_ids,
    )
    bul_res = compute_single_track_rotation(
        students=students,
        target_count=BULLETIN_DUTY_COUNT,
        start_num=bulletin_start_num,
        excluded_ids=excluded_ids,
        reassign_ids=bulletin_reassign_ids,
    )
    return {
        "cleaning": clean_res["assigned"],
        "cleaning_next_num": clean_res["next_start_num"],
        "cleaning_consumed_ids": clean_res["consumed_reassign_ids"],
        "bulletin": bul_res["assigned"],
        "bulletin_next_num": bul_res["next_start_num"],
        "bulletin_consumed_ids": bul_res["consumed_reassign_ids"],
    }


def resolve_cleaning_template_path() -> Optional[Path]:
    """assets/cleaning_notice_template.hwpx 경로를 탐색합니다."""
    for candidate in (
        Path(get_data_path(CLEANING_TEMPLATE_REL_PATH, ensure_parent=False)),
        Path(get_resource_path(CLEANING_TEMPLATE_REL_PATH)),
    ):
        if candidate.is_file():
            return candidate
    return None


def fill_cleaning_notice_hwpx(
    template_path: Path | str,
    period_text: str,
    cleaning_names: List[str],
    bulletin_names: List[str],
) -> HwpxDocument:
    """
    cleaning_notice_template.hwpx 양식에 안내 기간과 교실 청소(6칸)·게시판(2칸) 학생 이름을 채웁니다.
    원본 글자 크기(<당번 안내> 40pt, 기간 30pt, 학생 이름 50pt)와 셀 병합·배경 서식을 그대로 보존합니다.
    """
    doc = HwpxDocument.load(template_path)
    clean_slots = (list(cleaning_names) + [""] * CLEANING_DUTY_COUNT)[:CLEANING_DUTY_COUNT]
    bul_slots = (list(bulletin_names) + [""] * BULLETIN_DUTY_COUNT)[:BULLETIN_DUTY_COUNT]

    ops: List[Dict[str, Any]] = []
    clean_period = (period_text or "").strip()
    if clean_period:
        ops.append(
            {
                "op": "replace_text",
                "target_id": "p_0",
                "old_text": "기간 : ",
                "new_text": f"기간 : {clean_period}",
            }
        )
    for r_idx, stu_name in enumerate(clean_slots, start=1):
        ops.append(
            {
                "op": "update_cell",
                "target_id": f"tbl_0_r{r_idx}_c0",
                "new_text": str(stu_name or "").strip(),
            }
        )
    for r_idx, stu_name in enumerate(bul_slots, start=1):
        ops.append(
            {
                "op": "update_cell",
                "target_id": f"tbl_0_r{r_idx}_c1",
                "new_text": str(stu_name or "").strip(),
            }
        )

    touched = apply_hwpx_operations(doc, ops, record_undo=False)
    if clean_period and "p_0" not in touched:
        apply_hwpx_operations(
            doc,
            [
                {
                    "op": "replace_text",
                    "target_id": "p_0",
                    "old_text": "기간:",
                    "new_text": f"기간 : {clean_period}",
                }
            ],
            record_undo=False,
        )
    return doc


class ClickablePeriodLineEdit(QLineEdit):
    """클릭(탭) 시 즉시 달력 기간 선택 대화상자를 여는 전용 기간 입력칸."""

    def __init__(self, on_click: Callable[[], None], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._on_click = on_click
        self.setReadOnly(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: Optional[QMouseEvent]) -> None:
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
            event.accept()
            return
        super().mousePressEvent(event)


class _MultiSelectCalendarWidget(QCalendarWidget):
    """QSS 배경 무시를 우회해 다중 선택/기준일/오늘/주말 셀을 직접 렌더링하는 달력 위젯."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._selected_dates: Set[date] = set()
        self._range_anchor: Optional[date] = None
        self.setStyleSheet(
            "QCalendarWidget QAbstractItemView:enabled {"
            "  selection-background-color: transparent;"
            "  selection-color: inherit;"
            "}"
        )

    def set_selection_state(
        self,
        selected_dates: Set[date],
        range_anchor: Optional[date] = None,
    ) -> None:
        self._selected_dates = set(selected_dates)
        self._range_anchor = range_anchor
        self.updateCells()

    def paintCell(self, painter: QPainter, rect: QRect, qdate: QDate) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        py_date = date(qdate.year(), qdate.month(), qdate.day())
        is_anchor = self._range_anchor is not None and py_date == self._range_anchor
        is_selected = py_date in self._selected_dates
        is_qt_selected = qdate == self.selectedDate()
        is_today = py_date == date.today()
        is_current_month = (
            qdate.month() == self.monthShown() and qdate.year() == self.yearShown()
        )

        # 기본 셀 배경(흰색) 채우기
        painter.fillRect(rect, QColor("#FFFFFF"))

        pill_rect = rect.adjusted(3, 3, -3, -3)
        font = QFont(self.font())

        if is_anchor:
            painter.setBrush(QColor("#DBEAFE"))
            painter.setPen(QPen(QColor("#2563EB"), 1.5))
            painter.drawRoundedRect(pill_rect, 6, 6)
            font.setWeight(QFont.Weight.Bold)
            text_color = QColor("#1D4ED8")
        elif is_selected:
            painter.setBrush(QColor("#2563EB"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(pill_rect, 6, 6)
            font.setWeight(QFont.Weight.Bold)
            text_color = QColor("#FFFFFF")
        elif is_qt_selected:
            painter.setBrush(QColor("#EFF6FF"))
            painter.setPen(QPen(QColor("#93C5FD"), 1.0))
            painter.drawRoundedRect(pill_rect, 6, 6)
            font.setWeight(QFont.Weight.DemiBold)
            text_color = QColor("#1D4ED8")
        else:
            if is_today:
                painter.setBrush(QColor("#F1F5F9"))
                painter.setPen(QPen(QColor("#94A3B8"), 1.0))
                painter.drawRoundedRect(pill_rect, 6, 6)
                font.setWeight(QFont.Weight.Bold)

            if not is_current_month:
                text_color = QColor("#94A3B8")
            elif qdate.dayOfWeek() == 6:
                text_color = QColor("#2563EB")
            elif qdate.dayOfWeek() == 7:
                text_color = QColor("#DC2626")
            else:
                text_color = QColor("#0F172A")

        painter.setFont(font)
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(qdate.day()))
        painter.restore()


class DutyPeriodPickerDialog(QDialog):
    """
    당번 안내 기간 달력 선택 대화상자.
    - 연속 범위 선택(월~목, 월~금 등, 주말 자동 제외 옵션)과 개별·격일 체크(월·수·금 등) 모두 지원
    - 선택된 날짜 패턴은 다음 주 순번 이동 시 동일 요일 패턴(+7일)으로 자동 이월됨
    """

    def __init__(
        self,
        selected_dates: List[str],
        current_text: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("당번 기간 선택 (연속 범위 · 격일 체크)")
        self.setMinimumSize(540, 500)

        init_dates = _parse_iso_dates(selected_dates) or _parse_iso_dates(default_weekly_dates())
        self._selected_set: Set[date] = set(init_dates)
        self._range_anchor: Optional[date] = None
        self._painted_dates: Set[date] = set()
        self._updating_preview = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        # 1) 선택 모드 바 (연속 범위 vs 개별·격일 체크)
        mode_frame = QFrame()
        apply_qss_role(mode_frame, role="subCard")
        mode_lay = QHBoxLayout(mode_frame)
        mode_lay.setContentsMargins(12, 8, 12, 8)
        mode_lay.setSpacing(12)

        self.rb_range = QRadioButton("연속 범위 지정 (시작일 → 종료일)")
        self.rb_toggle = QRadioButton("개별·격일 체크 (날짜 클릭마다 선택/해제)")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_range)
        self.mode_group.addButton(self.rb_toggle)

        is_init_consecutive = len(init_dates) <= 1 or all(
            (init_dates[i + 1] - init_dates[i]).days == 1 for i in range(len(init_dates) - 1)
        )
        if is_init_consecutive:
            self.rb_range.setChecked(True)
        else:
            self.rb_toggle.setChecked(True)

        self.chk_skip_weekend = QCheckBox("주말(토·일) 제외")
        self.chk_skip_weekend.setChecked(True)
        self.rb_range.toggled.connect(self._on_mode_changed)

        mode_lay.addWidget(self.rb_range)
        mode_lay.addWidget(self.rb_toggle)
        mode_lay.addStretch()
        mode_lay.addWidget(self.chk_skip_weekend)
        lay.addWidget(mode_frame)

        # 2) 원클릭 요일 패턴 & 주간 이동 버튼 바
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        presets = (
            ("월~금", [0, 1, 2, 3, 4]),
            ("월~목", [0, 1, 2, 3]),
            ("화~금", [1, 2, 3, 4]),
            ("월·수·금(격일)", [0, 2, 4]),
            ("화·목(격일)", [1, 3]),
        )
        for label, offsets in presets:
            btn = QPushButton(label)
            apply_qss_role(btn, role="secondary")
            btn.setMinimumHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, ofs=offsets: self._apply_week_offsets(ofs))
            preset_row.addWidget(btn)

        preset_row.addStretch()
        btn_prev_wk = QPushButton("-1주")
        apply_qss_role(btn_prev_wk, role="secondary")
        btn_prev_wk.setMinimumHeight(30)
        btn_prev_wk.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_prev_wk.clicked.connect(lambda: self._shift_current_selection(-7))

        btn_next_wk = QPushButton("+1주")
        apply_qss_role(btn_next_wk, role="secondary")
        btn_next_wk.setMinimumHeight(30)
        btn_next_wk.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_next_wk.clicked.connect(lambda: self._shift_current_selection(7))

        preset_row.addWidget(btn_prev_wk)
        preset_row.addWidget(btn_next_wk)
        lay.addLayout(preset_row)

        # 3) 달력 위젯 (다중 선택 셀 직접 렌더링)
        self.calendar = _MultiSelectCalendarWidget(self)
        self.calendar.setGridVisible(True)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.setSelectionMode(QCalendarWidget.SelectionMode.SingleSelection)
        first_d = min(self._selected_set) if self._selected_set else date.today()
        self.calendar.setSelectedDate(QDate(first_d.year, first_d.month, first_d.day))
        self.calendar.clicked.connect(self._on_calendar_date_clicked)
        lay.addWidget(self.calendar, stretch=1)

        # 4) 선택 패턴 안내 라벨
        self.lbl_pattern_hint = QLabel("")
        apply_qss_role(self.lbl_pattern_hint, role="mutedText")
        self.lbl_pattern_hint.setWordWrap(True)
        lay.addWidget(self.lbl_pattern_hint)

        # 5) 한글 양식 출력 문구 미리보기 (직접 수정도 허용)
        prev_row = QHBoxLayout()
        prev_row.setSpacing(8)
        lbl_prev = QLabel("출력 문구:")
        apply_qss_role(lbl_prev, role="fieldLabel")
        self.edit_preview = QLineEdit()
        self.edit_preview.setMinimumHeight(34)
        prev_row.addWidget(lbl_prev)
        prev_row.addWidget(self.edit_preview, stretch=1)
        lay.addLayout(prev_row)

        # 6) 하단 버튼
        btm = QHBoxLayout()
        btn_clear = QPushButton("선택 초기화")
        apply_qss_role(btn_clear, role="secondary")
        btn_clear.setMinimumHeight(34)
        btn_clear.clicked.connect(self._clear_dates)

        btn_cancel = QPushButton("취소")
        apply_qss_role(btn_cancel, role="secondary")
        btn_cancel.setMinimumHeight(34)
        btn_cancel.clicked.connect(self.reject)

        btn_ok = QPushButton("기간 적용")
        apply_qss_role(btn_ok, role="primary")
        btn_ok.setMinimumHeight(34)
        btn_ok.clicked.connect(self._on_accept)

        btm.addWidget(btn_clear)
        btm.addStretch()
        btm.addWidget(btn_cancel)
        btm.addWidget(btn_ok)
        lay.addLayout(btm)

        self._refresh_calendar_formats(sync_text=True)
        auto_txt = format_duty_dates_korean(self.selected_iso_dates())
        if current_text.strip() and current_text.strip() != auto_txt:
            self.edit_preview.setText(current_text.strip())

    def _on_mode_changed(self) -> None:
        self._range_anchor = None
        self.chk_skip_weekend.setEnabled(self.rb_range.isChecked())
        self._refresh_calendar_formats(sync_text=False)

    def _reference_monday(self) -> date:
        if self._selected_set:
            first_d = min(self._selected_set)
        else:
            qd = self.calendar.selectedDate()
            first_d = date(qd.year(), qd.month(), qd.day())
        return first_d - timedelta(days=first_d.weekday())

    def _apply_week_offsets(self, offsets: List[int]) -> None:
        monday = self._reference_monday()
        self._range_anchor = None
        self._selected_set = {monday + timedelta(days=o) for o in offsets}
        if offsets != list(range(offsets[0], offsets[-1] + 1)):
            self.rb_toggle.setChecked(True)
        self._refresh_calendar_formats(sync_text=True)

    def _shift_current_selection(self, delta_days: int) -> None:
        if not self._selected_set:
            return
        self._range_anchor = None
        self._selected_set = {d + timedelta(days=delta_days) for d in self._selected_set}
        first_d = min(self._selected_set)
        self.calendar.setCurrentPage(first_d.year, first_d.month)
        self._refresh_calendar_formats(sync_text=True)

    def _clear_dates(self) -> None:
        self._range_anchor = None
        self._selected_set.clear()
        self._refresh_calendar_formats(sync_text=True)

    def _on_calendar_date_clicked(self, qdate: QDate) -> None:
        clicked = date(qdate.year(), qdate.month(), qdate.day())
        if self.rb_range.isChecked():
            if self._range_anchor is None:
                self._range_anchor = clicked
                self._selected_set = {clicked}
                self._refresh_calendar_formats(sync_text=True)
                self.lbl_pattern_hint.setText(
                    f"시작일 선택됨 ({clicked.month}월 {clicked.day}일) — 달력에서 종료일을 클릭하세요."
                )
                return
            start_d = min(self._range_anchor, clicked)
            end_d = max(self._range_anchor, clicked)
            self._range_anchor = None
            span_days = (end_d - start_d).days + 1
            picked: Set[date] = set()
            for offset in range(span_days):
                cur = start_d + timedelta(days=offset)
                if self.chk_skip_weekend.isChecked() and cur.weekday() >= 5:
                    continue
                picked.add(cur)
            if not picked:
                picked.add(clicked)
            self._selected_set = picked
            self._refresh_calendar_formats(sync_text=True)
        else:
            self._range_anchor = None
            if clicked in self._selected_set:
                self._selected_set.remove(clicked)
            else:
                self._selected_set.add(clicked)
            self._refresh_calendar_formats(sync_text=True)

    def _refresh_calendar_formats(self, sync_text: bool = True) -> None:
        self._painted_dates = set(self._selected_set)
        self.calendar.set_selection_state(self._selected_set, self._range_anchor)

        iso_list = self.selected_iso_dates()
        if sync_text:
            self.edit_preview.setText(format_duty_dates_korean(iso_list))

        sorted_dates = sorted(self._selected_set)
        if sorted_dates:
            wd_names = ", ".join(WEEKDAY_KR[d.weekday()] for d in sorted_dates)
            next_txt = format_duty_dates_korean(shift_duty_dates(iso_list, 7))
            self.lbl_pattern_hint.setText(
                f"선택된 날짜 ({len(sorted_dates)}일: {wd_names})  |  다음 주로 넘길 때 자동 변경: {next_txt}"
            )
        else:
            self.lbl_pattern_hint.setText("달력에서 당번을 운영할 날짜를 선택해 주세요.")

    def _on_accept(self) -> None:
        if not self._selected_set and not self.edit_preview.text().strip():
            QMessageBox.warning(self, "날짜 확인", "최소 하루 이상의 날짜를 선택해 주세요.")
            return
        self.accept()

    def selected_iso_dates(self) -> List[str]:
        return [d.isoformat() for d in sorted(self._selected_set)]

    def period_text(self) -> str:
        return self.edit_preview.text().strip()


class DutyExceptionDialog(QDialog):
    """결석생 배정 제외 및 불성실 수행 학생 다음 주 추가(재배정) 지정 대화상자."""

    def __init__(
        self,
        students: List[Dict[str, Any]],
        excluded_ids: List[str],
        cleaning_reassign_ids: List[str],
        bulletin_reassign_ids: List[str],
        focus_student_id: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("당번 예외 관리 (결석 제외 · 추가 재배정)")
        self.setMinimumSize(620, 520)
        self._students = list(students)
        self._excluded_set = set(excluded_ids or [])
        self._clean_re_set = set(cleaning_reassign_ids or [])
        self._bul_re_set = set(bulletin_reassign_ids or [])
        self._row_checks: List[Tuple[str, QCheckBox, QCheckBox, QCheckBox]] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(12)

        lbl_guide = QLabel(
            "결석·현장체험학습 등으로 배정에서 뺄 학생은 [결석·배정 제외]에 체크하고,\n"
            "불성실 수행 등으로 이번(또는 다음) 주에 한 번 더 배정할 학생은 [청소 추가] 또는 [게시판 추가]에 체크하세요.\n"
            "추가 배정 인원이 있어도 총 인원(청소 6명·게시판 2명)과 번호순 정렬은 그대로 유지됩니다."
        )
        apply_qss_role(lbl_guide, role="mutedText")
        lbl_guide.setWordWrap(True)
        lay.addWidget(lbl_guide)

        self.table = QTableWidget(len(self._students), 6)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setHorizontalHeaderLabels(
            ["번호", "학번", "이름", "결석·배정 제외", "청소 추가(재배정)", "게시판 추가(재배정)"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setMinimumHeight(32)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)

        mono_font = QFont("JetBrains Mono", 9)
        focus_row = -1
        for r_idx, stu in enumerate(self._students):
            sid = str(stu["student_id"])
            num = int(stu["number"])
            name = str(stu["name"])
            if focus_student_id and sid == focus_student_id:
                focus_row = r_idx

            it_num = QTableWidgetItem(f"{num}번")
            it_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_sid = QTableWidgetItem(sid)
            it_sid.setFont(mono_font)
            it_sid.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_name = QTableWidgetItem(name)
            it_name.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table.setItem(r_idx, 0, it_num)
            self.table.setItem(r_idx, 1, it_sid)
            self.table.setItem(r_idx, 2, it_name)

            chk_ex = QCheckBox()
            chk_ex.setChecked(sid in self._excluded_set)
            chk_cl = QCheckBox()
            chk_cl.setChecked(sid in self._clean_re_set)
            chk_bu = QCheckBox()
            chk_bu.setChecked(sid in self._bul_re_set)

            # 결석 제외 체크 시 추가배정 동시 선택 방지 안내용 상호 배제
            def _bind_mutual(c_ex: QCheckBox, c_cl: QCheckBox, c_bu: QCheckBox) -> None:
                def _on_ex(checked: bool) -> None:
                    if checked:
                        c_cl.setChecked(False)
                        c_bu.setChecked(False)
                def _on_re(checked: bool) -> None:
                    if checked:
                        c_ex.setChecked(False)
                c_ex.toggled.connect(_on_ex)
                c_cl.toggled.connect(_on_re)
                c_bu.toggled.connect(_on_re)

            _bind_mutual(chk_ex, chk_cl, chk_bu)

            for c_idx, chk in ((3, chk_ex), (4, chk_cl), (5, chk_bu)):
                wrap = QWidget()
                w_lay = QHBoxLayout(wrap)
                w_lay.setContentsMargins(0, 0, 0, 0)
                w_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
                w_lay.addWidget(chk)
                self.table.setCellWidget(r_idx, c_idx, wrap)

            self._row_checks.append((sid, chk_ex, chk_cl, chk_bu))

        lay.addWidget(self.table, stretch=1)
        if focus_row >= 0:
            self.table.selectRow(focus_row)
            self.table.scrollToItem(self.table.item(focus_row, 0))

        btm = QHBoxLayout()
        btn_clear_re = QPushButton("추가배정 전체 해제")
        apply_qss_role(btn_clear_re, role="secondary")
        btn_clear_re.setMinimumHeight(34)
        btn_clear_re.clicked.connect(self._clear_reassignments)

        btn_clear_all = QPushButton("모든 체크 초기화")
        apply_qss_role(btn_clear_all, role="secondary")
        btn_clear_all.setMinimumHeight(34)
        btn_clear_all.clicked.connect(self._clear_all)

        btn_cancel = QPushButton("취소")
        apply_qss_role(btn_cancel, role="secondary")
        btn_cancel.setMinimumHeight(34)
        btn_cancel.clicked.connect(self.reject)

        btn_ok = QPushButton("저장 및 배정 반영")
        apply_qss_role(btn_ok, role="primary")
        btn_ok.setMinimumHeight(34)
        btn_ok.clicked.connect(self.accept)

        btm.addWidget(btn_clear_re)
        btm.addWidget(btn_clear_all)
        btm.addStretch()
        btm.addWidget(btn_cancel)
        btm.addWidget(btn_ok)
        lay.addLayout(btm)

    def _clear_reassignments(self) -> None:
        for _, _, chk_cl, chk_bu in self._row_checks:
            chk_cl.setChecked(False)
            chk_bu.setChecked(False)

    def _clear_all(self) -> None:
        for _, chk_ex, chk_cl, chk_bu in self._row_checks:
            chk_ex.setChecked(False)
            chk_cl.setChecked(False)
            chk_bu.setChecked(False)

    def result_lists(self) -> Tuple[List[str], List[str], List[str]]:
        excl: List[str] = []
        clean_re: List[str] = []
        bul_re: List[str] = []
        for sid, chk_ex, chk_cl, chk_bu in self._row_checks:
            if chk_ex.isChecked():
                excl.append(sid)
            else:
                if chk_cl.isChecked():
                    clean_re.append(sid)
                if chk_bu.isChecked():
                    bul_re.append(sid)
        return excl, clean_re, bul_re



ATTENDANCE_CATEGORIES: List[str] = [
    "질병결석",
    "교외체험학습",
    "출석인정",
    "기타·미인정",
]

ATTENDANCE_PROOF_STATUSES: List[str] = [
    "증빙 대기",
    "신청서 승인완료",
    "진단서·확인서 완비",
    "공문 확인완료",
]


def build_absence_form_hwpx(
    *,
    homeroom_label: str,
    student_id: str,
    student_name: str,
    category: str,
    reason: str,
    period_text: str,
    proof_status: str,
) -> HwpxDocument:
    """출결 행 데이터를 바탕으로 결석계 또는 출석인정·체험학습 확인서 표준 HWPX 문서를 생성합니다."""
    is_cert = category in ("교외체험학습", "출석인정")
    doc_title = "교외체험학습 · 출석인정 확인서" if is_cert else "학급 결석계 (NEIS 증빙용)"
    doc = create_minimal_hwpx_document(title=doc_title, empty=True)
    today_kr = datetime.now().strftime("%Y년 %m월 %d일")
    detail_reason = f"{category} ({reason})" if reason else category

    ops: List[Dict[str, Any]] = [
        {"op": "append_paragraph", "text": doc_title, "bold": True, "font_size_pt": 18, "align": "CENTER"},
        {"op": "append_paragraph", "text": ""},
        {
            "op": "append_paragraph",
            "text": "결재: 담임 [          ]   /   부장 [          ]   /   교감 [          ]",
            "font_size_pt": 10,
            "align": "RIGHT",
        },
        {"op": "append_paragraph", "text": ""},
        {
            "op": "append_table",
            "headers": ["항목", "기재 내용"],
            "rows": [
                ["소속 학급 / 학번", f"{homeroom_label} / {student_id}"],
                ["학생 성명", student_name],
                ["출결 구분 및 사유", detail_reason],
                ["적용 기간(일시)", period_text or "-"],
                ["증빙 서류 확인", proof_status or "증빙 대기"],
                ["제출(작성) 일자", today_kr],
                [
                    "확인 서명",
                    "보호자:                  (서명/인)      /      담임교사:                  (서명/인)",
                ],
            ],
            "col_widths_ratio": [28, 72],
        },
        {"op": "append_paragraph", "text": ""},
        {
            "op": "append_paragraph",
            "text": "위와 같이 관련 증빙 서류를 첨부하여 신고(확인)합니다.",
            "font_size_pt": 11,
            "align": "CENTER",
        },
    ]
    apply_hwpx_operations(doc, ops)
    return doc


def build_seating_chart_hwpx(
    *,
    homeroom_label: str,
    seating: List[Tuple[str, str, str]],
    cols: int = 6,
) -> HwpxDocument:
    """학급 좌석 배치표를 칠판/교탁 기준 6분단 표준 HWPX 표 문서로 생성합니다."""
    total_rows = max(1, (len(seating) + cols - 1) // cols)
    title_str = f"{homeroom_label} 학급 좌석 배치표 ({cols}x{total_rows})"
    doc = create_minimal_hwpx_document(title=title_str, empty=True)

    headers = [f"{c + 1}분단" for c in range(cols)]
    table_rows: List[List[str]] = []
    for r in range(total_rows):
        row_cells: List[str] = []
        for c in range(cols):
            idx = r * cols + c
            if idx < len(seating):
                _desk, sid, name = seating[idx]
                if sid and sid != "-" and name != "공석":
                    row_cells.append(f"{name}\n({sid})")
                else:
                    row_cells.append("공석")
            else:
                row_cells.append("")
        table_rows.append(row_cells)

    ops: List[Dict[str, Any]] = [
        {"op": "append_paragraph", "text": title_str, "bold": True, "font_size_pt": 16, "align": "CENTER"},
        {"op": "append_paragraph", "text": ""},
        {
            "op": "append_paragraph",
            "text": "[ 칠판 / 교탁 (전면) ]",
            "bold": True,
            "font_size_pt": 12,
            "align": "CENTER",
        },
        {
            "op": "append_table",
            "headers": headers,
            "rows": table_rows,
            "col_widths_ratio": [1] * cols,
        },
        {"op": "append_paragraph", "text": ""},
        {
            "op": "append_paragraph",
            "text": f"출력 일자: {datetime.now().strftime('%Y-%m-%d')}",
            "font_size_pt": 10,
            "align": "RIGHT",
        },
    ]
    apply_hwpx_operations(doc, ops)
    return doc


def save_seating_chart_xlsx(
    out_path: Path,
    *,
    homeroom_label: str,
    seating: List[Tuple[str, str, str]],
    cols: int = 6,
) -> None:
    """학급 좌석 배치표를 서식이 적용된 엑셀(.xlsx) 파일로 저장합니다."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    total_rows = max(1, (len(seating) + cols - 1) // cols)
    wb = Workbook()
    ws = wb.active
    ws.title = "좌석배치표"

    thin = Side(style="thin", color="CBD5E1")
    cell_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=cols)
    t_cell = ws.cell(row=1, column=1, value=f"{homeroom_label} 학급 좌석 배치표")
    t_cell.font = Font(size=14, bold=True)
    t_cell.alignment = center_align
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=cols)
    p_cell = ws.cell(row=2, column=1, value="[ 칠판 / 교탁 (전면) ]")
    p_cell.font = Font(size=11, bold=True, color="1E3A8A")
    p_cell.fill = PatternFill("solid", fgColor="EFF6FF")
    p_cell.alignment = center_align
    ws.row_dimensions[2].height = 24

    hdr_fill = PatternFill("solid", fgColor="F1F5F9")
    for c in range(cols):
        hc = ws.cell(row=3, column=c + 1, value=f"{c + 1}분단")
        hc.font = Font(size=10, bold=True)
        hc.fill = hdr_fill
        hc.border = cell_border
        hc.alignment = center_align
        col_letter = chr(ord("A") + c)
        ws.column_dimensions[col_letter].width = 16
    ws.row_dimensions[3].height = 22

    for r in range(total_rows):
        excel_row = 4 + r
        ws.row_dimensions[excel_row].height = 38
        for c in range(cols):
            idx = r * cols + c
            val = ""
            if idx < len(seating):
                _desk, sid, name = seating[idx]
                val = f"{name}\n({sid})" if (sid and sid != "-" and name != "공석") else "공석"
            dc = ws.cell(row=excel_row, column=c + 1, value=val)
            dc.border = cell_border
            dc.alignment = center_align

    wb.save(out_path)


def build_observation_logs_hwpx(
    *,
    homeroom_label: str,
    logs: List[Dict[str, Any]],
) -> HwpxDocument:
    """학생 관찰 및 상담 누적 일지를 표준 HWPX 표 문서로 생성합니다."""
    title_str = f"{homeroom_label} 학생 관찰 및 상담 기록부"
    doc = create_minimal_hwpx_document(title=title_str, empty=True)
    rows: List[List[str]] = []
    for item in logs:
        dt_txt = str(item.get("created_at", ""))[:16]
        rows.append(
            [
                dt_txt,
                str(item.get("student_id", "")),
                str(item.get("student_name", "")),
                str(item.get("content", "")),
            ]
        )
    if not rows:
        rows = [["-", "-", "-", "기록된 관찰·상담 내역이 없습니다."]]

    ops: List[Dict[str, Any]] = [
        {"op": "append_paragraph", "text": title_str, "bold": True, "font_size_pt": 16, "align": "CENTER"},
        {"op": "append_paragraph", "text": ""},
        {
            "op": "append_table",
            "headers": ["기록 일시", "학번", "성명", "관찰 및 상담 내용"],
            "rows": rows,
            "col_widths_ratio": [20, 13, 14, 53],
        },
    ]
    apply_hwpx_operations(doc, ops)
    return doc


def save_observation_logs_xlsx(
    out_path: Path,
    *,
    homeroom_label: str,
    logs: List[Dict[str, Any]],
) -> None:
    """학생 관찰 및 상담 누적 일지를 엑셀(.xlsx) 파일로 저장합니다."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "관찰상담일지"

    headers = ["기록 일시", "학급", "학번", "성명", "관찰 및 상담 내용"]
    hdr_fill = PatternFill("solid", fgColor="F1F5F9")
    for c_idx, h_text in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c_idx, value=h_text)
        cell.font = Font(bold=True)
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 60

    for r_idx, item in enumerate(logs, start=2):
        ws.cell(row=r_idx, column=1, value=str(item.get("created_at", ""))[:16])
        ws.cell(row=r_idx, column=2, value=homeroom_label)
        ws.cell(row=r_idx, column=3, value=str(item.get("student_id", "")))
        ws.cell(row=r_idx, column=4, value=str(item.get("student_name", "")))
        c_note = ws.cell(row=r_idx, column=5, value=str(item.get("content", "")))
        c_note.alignment = Alignment(vertical="center", wrap_text=True)

    wb.save(out_path)


class AttendanceEntryDialog(QDialog):
    """출결 및 결석계·체험학습 대장 항목 등록/수정 대화상자."""

    def __init__(
        self,
        roster: List[Tuple[str, str]],
        initial_data: Optional[Dict[str, Any]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._is_edit = bool(initial_data)
        self.setWindowTitle("출결 내역 수정" if self._is_edit else "출결 및 결석계 내역 등록")
        self.setMinimumWidth(480)

        data = initial_data or {}
        self._selected_dates: List[str] = list(data.get("selected_dates") or [])
        if not self._selected_dates:
            self._selected_dates = [date.today().isoformat()]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(12)

        lbl_guide = QLabel(
            "대상 학생과 결석·인정 구분, 사유, 적용 기간, 증빙 서류 상태를 입력하세요.\n"
            "[달력 선택]을 누르면 여러 날짜 범위나 징검다리 날짜도 바로 선택할 수 있습니다."
        )
        apply_qss_role(lbl_guide, role="mutedText")
        lbl_guide.setWordWrap(True)
        lay.addWidget(lbl_guide)

        form_grid = QGridLayout()
        form_grid.setHorizontalSpacing(10)
        form_grid.setVerticalSpacing(10)

        lbl_stu = QLabel("대상 학생:")
        apply_qss_role(lbl_stu, role="fieldLabel")
        self.combo_student = QComboBox()
        self.combo_student.setMinimumHeight(34)
        target_sid = str(data.get("student_id", "")).strip()
        target_name = str(data.get("student_name", "")).strip()
        matched_idx = 0
        has_target = False
        for idx, (sid, name) in enumerate(roster):
            self.combo_student.addItem(f"{sid} {name}", (sid, name))
            if target_sid and sid == target_sid:
                matched_idx = idx
                has_target = True
        if target_sid and not has_target:
            self.combo_student.insertItem(0, f"{target_sid} {target_name}".strip(), (target_sid, target_name))
            matched_idx = 0
        if self.combo_student.count() > 0:
            self.combo_student.setCurrentIndex(matched_idx)

        lbl_cat = QLabel("출결 구분:")
        apply_qss_role(lbl_cat, role="fieldLabel")
        self.combo_category = QComboBox()
        self.combo_category.setMinimumHeight(34)
        for cat in ATTENDANCE_CATEGORIES:
            self.combo_category.addItem(cat)
        init_cat = str(data.get("category", "질병결석")).strip()
        if init_cat in ATTENDANCE_CATEGORIES:
            self.combo_category.setCurrentText(init_cat)

        lbl_reason = QLabel("세부 사유:")
        apply_qss_role(lbl_reason, role="fieldLabel")
        self.edit_reason = QLineEdit()
        self.edit_reason.setMinimumHeight(34)
        self.edit_reason.setPlaceholderText("예: 독감 진료, 가족 역사탐방, 교내 탐구대회 참가")
        self.edit_reason.setText(str(data.get("reason", "")).strip())

        lbl_period = QLabel("적용 기간:")
        apply_qss_role(lbl_period, role="fieldLabel")
        period_row = QHBoxLayout()
        period_row.setSpacing(6)
        self.edit_period = QLineEdit()
        self.edit_period.setMinimumHeight(34)
        self.edit_period.setPlaceholderText("예: 11월 16일 ~ 11월 18일 또는 11월 16일 (3~4교시)")
        init_period = str(data.get("period_text", "")).strip()
        self.edit_period.setText(init_period or format_duty_dates_korean(self._selected_dates))

        btn_pick_cal = QPushButton("달력 선택")
        apply_qss_role(btn_pick_cal, role="secondary")
        btn_pick_cal.setMinimumHeight(34)
        btn_pick_cal.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_pick_cal.clicked.connect(self._on_pick_dates)
        period_row.addWidget(self.edit_period, stretch=1)
        period_row.addWidget(btn_pick_cal)

        lbl_proof = QLabel("증빙 상태:")
        apply_qss_role(lbl_proof, role="fieldLabel")
        self.combo_proof = QComboBox()
        self.combo_proof.setMinimumHeight(34)
        self.combo_proof.setEditable(True)
        for st in ATTENDANCE_PROOF_STATUSES:
            self.combo_proof.addItem(st)
        init_proof = str(data.get("proof_status", "증빙 대기")).strip() or "증빙 대기"
        self.combo_proof.setCurrentText(init_proof)

        form_grid.addWidget(lbl_stu, 0, 0)
        form_grid.addWidget(self.combo_student, 0, 1)
        form_grid.addWidget(lbl_cat, 1, 0)
        form_grid.addWidget(self.combo_category, 1, 1)
        form_grid.addWidget(lbl_reason, 2, 0)
        form_grid.addWidget(self.edit_reason, 2, 1)
        form_grid.addWidget(lbl_period, 3, 0)
        form_grid.addLayout(period_row, 3, 1)
        form_grid.addWidget(lbl_proof, 4, 0)
        form_grid.addWidget(self.combo_proof, 4, 1)
        lay.addLayout(form_grid)

        btm = QHBoxLayout()
        btm.addStretch()
        btn_cancel = QPushButton("취소")
        apply_qss_role(btn_cancel, role="secondary")
        btn_cancel.setMinimumHeight(34)
        btn_cancel.clicked.connect(self.reject)

        btn_ok = QPushButton("저장")
        apply_qss_role(btn_ok, role="primary")
        btn_ok.setMinimumHeight(34)
        btn_ok.clicked.connect(self._on_accept)
        btm.addWidget(btn_cancel)
        btm.addWidget(btn_ok)
        lay.addLayout(btm)

    def _on_pick_dates(self) -> None:
        dlg = DutyPeriodPickerDialog(
            selected_dates=self._selected_dates,
            current_text=self.edit_period.text().strip(),
            parent=self,
        )
        dlg.setWindowTitle("출결 적용 날짜 선택")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._selected_dates = dlg.selected_iso_dates()
            self.edit_period.setText(dlg.period_text())

    def _on_accept(self) -> None:
        if self.combo_student.count() == 0:
            QMessageBox.warning(self, "학생 확인", "선택 가능한 학생이 없습니다.")
            return
        if not self.edit_period.text().strip():
            QMessageBox.warning(self, "기간 확인", "적용 기간(일자 또는 교시)을 입력해 주세요.")
            return
        self.accept()

    def result_payload(self) -> Dict[str, Any]:
        stu_data = self.combo_student.currentData()
        if isinstance(stu_data, tuple) and len(stu_data) == 2:
            sid, name = str(stu_data[0]), str(stu_data[1])
        else:
            raw_txt = self.combo_student.currentText().strip()
            parts = raw_txt.split(" ", 1)
            sid = parts[0] if parts else ""
            name = parts[1] if len(parts) > 1 else raw_txt
        return {
            "student_id": sid,
            "student_name": name,
            "category": self.combo_category.currentText().strip() or "질병결석",
            "reason": self.edit_reason.text().strip(),
            "period_text": self.edit_period.text().strip(),
            "selected_dates": list(self._selected_dates),
            "proof_status": self.combo_proof.currentText().strip() or "증빙 대기",
        }


class ClickableSeatCell(QFrame):
    """클릭하여 두 좌석을 맞바꿀 수 있는 좌석 셀 위젯."""

    def __init__(
        self,
        desk_index: int,
        on_clicked: Callable[[int], None],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._desk_index = desk_index
        self._on_clicked = on_clicked
        apply_qss_role(self, role="seatCell")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(1)

        self.lbl_code = QLabel(f"D-{desk_index + 1:02d}")
        apply_qss_role(self.lbl_code, role="seatCode")
        self.lbl_code.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_stu = QLabel("-")
        apply_qss_role(self.lbl_stu, role="seatName")
        self.lbl_stu.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay.addWidget(self.lbl_code)
        lay.addWidget(self.lbl_stu)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_clicked(self._desk_index)
            event.accept()
            return
        super().mousePressEvent(event)

    def update_visual(
        self,
        desk_code: str,
        sid: str,
        name: str,
        *,
        is_care: bool = False,
        is_selected: bool = False,
    ) -> None:
        is_empty = (not sid) or sid == "-" or name == "공석"
        code_txt = f"{desk_code} · 배려" if (is_care and not is_empty) else desk_code
        self.lbl_code.setText(code_txt)
        self.lbl_stu.setText("공석" if is_empty else f"{sid} {name}")

        if is_selected:
            self.setStyleSheet(
                "QFrame { background-color: #EFF6FF; border: 2px solid #2563EB; border-radius: 6px; }"
            )
            self.lbl_code.setStyleSheet("color: #1D4ED8; font-size: 10px; font-weight: 700; border: none; background: transparent;")
            self.lbl_stu.setStyleSheet("color: #0F172A; font-size: 11px; font-weight: 700; border: none; background: transparent;")
        elif is_empty:
            self.setStyleSheet("")
            apply_qss_role(self, role="seatCell")
            self.lbl_code.setStyleSheet("color: #94A3B8; font-size: 10px; border: none; background: transparent;")
            self.lbl_stu.setStyleSheet("color: #94A3B8; font-size: 11px; border: none; background: transparent;")
        else:
            self.setStyleSheet("")
            apply_qss_role(self, role="seatCell")
            if is_care:
                self.lbl_code.setStyleSheet("color: #004AC6; font-size: 10px; font-weight: 700; border: none; background: transparent;")
            else:
                self.lbl_code.setStyleSheet("")
                apply_qss_role(self.lbl_code, role="seatCode")
            self.lbl_stu.setStyleSheet("")
            apply_qss_role(self.lbl_stu, role="seatName")


class FrontCareStudentsDialog(QDialog):
    """시력·청력·집중 배려 등 앞자리(1~2열) 우선 배정 학생 지정 대화상자."""

    def __init__(
        self,
        students: List[Dict[str, Any]],
        selected_ids: List[str],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("앞자리 우선 배려 학생 지정 (1~2열)")
        self.setMinimumSize(460, 480)
        self._checks: List[Tuple[str, QCheckBox]] = []
        sel_set = set(selected_ids or [])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(12)

        lbl_guide = QLabel(
            "시력·청력·학습 집중 등으로 앞자리(칠판 기준 1~2열, D-01 ~ D-12) 배치가 필요한 학생을 체크하세요.\n"
            "[앞자리 배려]가 켜진 상태에서 무작위 배치를 실행하면 체크된 학생이 앞줄에 우선 배치됩니다."
        )
        apply_qss_role(lbl_guide, role="mutedText")
        lbl_guide.setWordWrap(True)
        lay.addWidget(lbl_guide)

        self.table = QTableWidget(len(students), 4)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setHorizontalHeaderLabels(["번호", "학번", "이름", "앞자리 배려(1~2열)"])
        hdr = self.table.horizontalHeader()
        hdr.setMinimumHeight(32)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        mono_font = QFont("JetBrains Mono", 9)
        for r_idx, stu in enumerate(students):
            sid = str(stu["student_id"])
            num = int(stu["number"])
            name = str(stu["name"])

            it_num = QTableWidgetItem(f"{num}번")
            it_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_sid = QTableWidgetItem(sid)
            it_sid.setFont(mono_font)
            it_sid.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_name = QTableWidgetItem(name)
            it_name.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(r_idx, 0, it_num)
            self.table.setItem(r_idx, 1, it_sid)
            self.table.setItem(r_idx, 2, it_name)

            chk = QCheckBox()
            chk.setChecked(sid in sel_set)
            wrap = QWidget()
            w_lay = QHBoxLayout(wrap)
            w_lay.setContentsMargins(0, 0, 0, 0)
            w_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            w_lay.addWidget(chk)
            self.table.setCellWidget(r_idx, 3, wrap)
            self._checks.append((sid, chk))

        lay.addWidget(self.table, stretch=1)

        btm = QHBoxLayout()
        btn_clear = QPushButton("전체 해제")
        apply_qss_role(btn_clear, role="secondary")
        btn_clear.setMinimumHeight(34)
        btn_clear.clicked.connect(self._clear_all)

        btn_cancel = QPushButton("취소")
        apply_qss_role(btn_cancel, role="secondary")
        btn_cancel.setMinimumHeight(34)
        btn_cancel.clicked.connect(self.reject)

        btn_ok = QPushButton("저장")
        apply_qss_role(btn_ok, role="primary")
        btn_ok.setMinimumHeight(34)
        btn_ok.clicked.connect(self.accept)

        btm.addWidget(btn_clear)
        btm.addStretch()
        btm.addWidget(btn_cancel)
        btm.addWidget(btn_ok)
        lay.addLayout(btm)

    def _clear_all(self) -> None:
        for _, chk in self._checks:
            chk.setChecked(False)

    def selected_ids(self) -> List[str]:
        return [sid for sid, chk in self._checks if chk.isChecked()]


class ClassroomToolsTab(QWidget):
    """[탭 4] 학급 경영 도구함 2x2 Instrument Station 탭 위젯."""

    ai_target_requested = pyqtSignal(str)
    ai_context_changed = pyqtSignal()

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="ClassroomToolsTabPage")
        self.task_runner = task_runner
        self.db = DatabaseManager()
        self.config = ConfigManager()
        self._logged_in_teacher: Optional[Dict[str, Any]] = None
        self._firebase_roster: List[Tuple[str, str]] = []
        self._selected_notice_pdf_path: str = ""
        self._parsed_excel_timetable_rows: List[Dict[str, Any]] = []
        self._seat_cells: List[ClickableSeatCell] = []
        self._seat_labels: List[Tuple[QLabel, QLabel]] = []
        self._current_seating: List[Tuple[str, str, str]] = []
        self._front_care_ids: List[str] = []
        self._selected_seat_idx: Optional[int] = None
        self._loading_seating_ui: bool = False
        self._duty_excluded_ids: List[str] = []
        self._duty_clean_reassign_ids: List[str] = []
        self._duty_bul_reassign_ids: List[str] = []
        self._duty_selected_dates: List[str] = default_weekly_dates()
        self._last_duty_plan: Dict[str, Any] = {}
        self._loading_duty_ui: bool = False
        self._custom_duty_template_path: Optional[Path] = None

        self._init_ui()
        self.restore_saved_teacher_session()
        self._refresh_roster_dependent_views()

    def _get_active_roster(self) -> List[Tuple[str, str]]:
        if self._firebase_roster:
            return list(self._firebase_roster)
        students = self.db.get_active_homeroom_students()
        if students:
            return [(str(s["student_id"]), str(s["name"])) for s in students]
        return list(SAMPLE_ROSTER)

    def _create_status_pill(
        self, title_text: str, value_text: str, badge_text: str, badge_role: str
    ) -> Tuple[QFrame, QLabel, QLabel]:
        box = QFrame()
        apply_qss_role(box, role="subCard")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)

        left_col = QVBoxLayout()
        left_col.setSpacing(2)
        lbl_title = QLabel(title_text)
        apply_qss_role(lbl_title, role="kpiLabel")
        lbl_val = QLabel(value_text)
        lbl_val.setStyleSheet("color: #0F172A; font-size: 15px; font-weight: 700;")
        left_col.addWidget(lbl_title)
        left_col.addWidget(lbl_val)

        lbl_badge = QLabel(badge_text)
        apply_qss_role(lbl_badge, role=badge_role)

        lay.addLayout(left_col)
        lay.addStretch()
        lay.addWidget(lbl_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        return box, lbl_val, lbl_badge

    def _init_ui(self) -> None:
        root_grid = QGridLayout(self)
        root_grid.setContentsMargins(0, 0, 0, 0)
        root_grid.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._main_scroll = scroll

        container = QWidget()
        container.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 20, 20)
        layout.setSpacing(14)

        # 상단 학급 운영 스테이션 상태 스트립
        header_bar = QFrame()
        apply_qss_role(header_bar, role="instrumentCard", obj_name="ClassroomHeaderBar")
        hb_lay = QHBoxLayout(header_bar)
        hb_lay.setContentsMargins(16, 10, 16, 10)
        hb_lay.setSpacing(10)

        lbl_station_title = QLabel("학급 경영 미니 툴 스테이션 (Classroom Operations Station)")
        apply_qss_role(lbl_station_title, role="cardTitle")
        lbl_mode_chip = QLabel("HOMEROOM INSTRUMENT GRID")
        apply_qss_role(lbl_mode_chip, role="badgePrimary")

        self.lbl_roster_status_badge = QLabel("연동 명렬표: [담임 학급] 등록된 담임 학급 없음")
        apply_qss_role(self.lbl_roster_status_badge, role="badgeWarning")
        self.lbl_roster_status_badge.setToolTip("환경설정 탭에서 나이스 학급담임 명렬표를 먼저 등록해주세요")

        self.btn_sync_roster = QPushButton("명렬표 연동 새로고침")
        apply_qss_role(self.btn_sync_roster, role="secondary", obj_name="BtnSyncClassroomRoster")
        self.btn_sync_roster.setMinimumHeight(30)
        self.btn_sync_roster.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sync_roster.clicked.connect(self._refresh_roster_dependent_views)

        for drop_w in (self.lbl_roster_status_badge, self.btn_sync_roster):
            enable_file_drop(
                drop_w,
                allowed_exts=[".xlsx"],
                allow_dirs=False,
                allow_files=True,
                multiple=False,
                on_files_dropped=lambda paths: self.import_homeroom_roster_file(paths[0]),
                context_label="학급담임 명렬표 (.xlsx)",
            )

        self.auth_status_widget = QWidget()
        status_row = QHBoxLayout(self.auth_status_widget)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        self.lbl_teacher_auth_status = QLabel("[교사] 미로그인")
        apply_qss_role(self.lbl_teacher_auth_status, role="badgePrimary", obj_name="LblTeacherAuthBadge")
        self.btn_teacher_refresh = QPushButton("새로고침")
        apply_qss_role(self.btn_teacher_refresh, role="secondary", obj_name="BtnTeacherRefresh")
        self.btn_teacher_refresh.setMinimumHeight(30)
        self.btn_teacher_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_teacher_refresh.clicked.connect(lambda: self.refresh_firebase_roster(silent=False))
        self.btn_teacher_logout = QPushButton("로그아웃")
        apply_qss_role(self.btn_teacher_logout, role="secondary", obj_name="BtnTeacherLogout")
        self.btn_teacher_logout.setMinimumHeight(30)
        self.btn_teacher_logout.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_teacher_logout.clicked.connect(self.logout_teacher)
        status_row.addWidget(self.lbl_teacher_auth_status)
        status_row.addWidget(self.btn_teacher_refresh)
        status_row.addWidget(self.btn_teacher_logout)
        self.auth_status_widget.setVisible(False)

        hb_lay.addWidget(lbl_station_title)
        hb_lay.addWidget(lbl_mode_chip)
        hb_lay.addWidget(self.lbl_roster_status_badge)
        hb_lay.addStretch()
        hb_lay.addWidget(self.auth_status_widget)
        hb_lay.addWidget(self.btn_sync_roster)
        layout.addWidget(header_bar)

        # 확장 가능한 상단 필(Pill) 탭 바 + 하단 단일 전체 폭 QStackedWidget
        self.pill_bar = QFrame()
        pill_bar_lay = QHBoxLayout(self.pill_bar)
        pill_bar_lay.setContentsMargins(0, 0, 0, 0)
        pill_bar_lay.setSpacing(8)
        self.pill_btn_group = QButtonGroup(self)
        self.pill_btn_group.setExclusive(True)
        self.tool_pills: List[QPushButton] = []
        pill_titles = [
            "1. 출결 및 결석계 관리",
            "2. 청소 당번 순환 배정기",
            "3. 모바일 가정통신문·서명 취합 센터",
            "4. 학급 공지·긴급 알림 일괄 발송기",
            "5. 1:1 상담 내역·누가기록 뷰어",
            "6. 시간표 관리 (수동·엑셀)",
        ]
        for idx, p_title in enumerate(pill_titles):
            pill = QPushButton(p_title)
            pill.setCheckable(True)
            pill.setChecked(idx == 0)
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.setMinimumHeight(34)
            pill.setStyleSheet(
                "QPushButton { padding: 6px 14px; border-radius: 17px; border: 1px solid #CBD5E1; "
                "background: #FFFFFF; color: #334155; font-weight: 600; font-size: 12px; } "
                "QPushButton:checked { background: #4F46E5; color: #FFFFFF; border-color: #4F46E5; } "
                "QPushButton:hover:!checked { background: #F1F5F9; }"
            )
            self.pill_btn_group.addButton(pill, idx)
            pill_bar_lay.addWidget(pill)
            self.tool_pills.append(pill)
        pill_bar_lay.addStretch(1)
        layout.addWidget(self.pill_bar)

        self.tool_stack = QStackedWidget()
        self.pill_btn_group.idClicked.connect(self.tool_stack.setCurrentIndex)

        # =====================================================================
        # CARD 1: 출결 및 결석계 관리 (Attendance Tracker)
        # =====================================================================
        self.card_attendance = HoverZoomCard(
            title="1. 출결 및 결석계 관리 (Attendance Tracker)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_attendance, role="hoverZoomCard", obj_name="CardAttendance")
        c1_lay = self.card_attendance.body_layout()

        # 출결 상태 배지 3종 (재적·대장 / 질병·기타 / 체험학습·인정)
        att_badge_row = QHBoxLayout()
        att_badge_row.setSpacing(8)
        pill_present, self.lbl_att_present, self.badge_att_present = self._create_status_pill(
            "재적 · 대장 현황", "0건", "서류 완비", "badgeSuccess"
        )
        pill_sick, self.lbl_att_sick, self.badge_att_sick = self._create_status_pill(
            "질병 · 기타 결석", "0건", "확인 완료", "badgeNeutral"
        )
        pill_trip, self.lbl_att_trip, self.badge_att_trip = self._create_status_pill(
            "체험학습 · 출석인정", "0건", "사전·공문", "badgePrimary"
        )
        att_badge_row.addWidget(pill_present, stretch=1)
        att_badge_row.addWidget(pill_sick, stretch=1)
        att_badge_row.addWidget(pill_trip, stretch=1)
        c1_lay.addLayout(att_badge_row)

        # 증빙 서류 대장 테이블 (더블클릭 수정 지원)
        self.table_attendance = QTableWidget(0, 5)
        self.table_attendance.setAlternatingRowColors(True)
        self.table_attendance.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_attendance.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_attendance.verticalHeader().setDefaultSectionSize(32)
        self.table_attendance.verticalHeader().setVisible(False)
        self.table_attendance.setHorizontalHeaderLabels(
            ["학번", "이름", "구분 · 사유", "적용 기간", "증빙 상태"]
        )
        self.table_attendance.setToolTip("행을 더블클릭하면 출결 내역과 증빙 상태를 바로 수정할 수 있습니다.")
        self.table_attendance.cellDoubleClicked.connect(self._on_attendance_row_double_clicked)
        att_hdr = self.table_attendance.horizontalHeader()
        att_hdr.setMinimumHeight(32)
        att_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        att_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        att_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        att_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        att_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        c1_lay.addWidget(self.table_attendance, stretch=1)

        # 하단 액션 버튼 (등록 / 수정 / 삭제 / 결석계·인정서 출력)
        att_btn_row = QHBoxLayout()
        att_btn_row.setSpacing(6)

        self.btn_add_attendance = QPushButton("출결 등록")
        apply_qss_role(self.btn_add_attendance, role="primary", obj_name="BtnAddAttendance")
        self.btn_add_attendance.setMinimumHeight(34)
        self.btn_add_attendance.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_attendance.clicked.connect(self._on_add_attendance_entry)

        self.btn_edit_attendance = QPushButton("수정")
        apply_qss_role(self.btn_edit_attendance, role="secondary", obj_name="BtnEditAttendance")
        self.btn_edit_attendance.setMinimumHeight(34)
        self.btn_edit_attendance.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_edit_attendance.clicked.connect(self._on_edit_attendance_entry)

        self.btn_delete_attendance = QPushButton("삭제")
        apply_qss_role(self.btn_delete_attendance, role="secondary", obj_name="BtnDeleteAttendance")
        self.btn_delete_attendance.setMinimumHeight(34)
        self.btn_delete_attendance.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete_attendance.clicked.connect(self._on_delete_attendance_entry)

        self.btn_create_absence = QPushButton("결석계·인정서 출력")
        apply_qss_role(self.btn_create_absence, role="secondary", obj_name="BtnCreateAbsenceForm")
        self.btn_create_absence.setMinimumHeight(34)
        self.btn_create_absence.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_create_absence.setToolTip("선택한 출결 행으로 한글(.hwpx) 결석계·출석인정서 또는 CSV 파일을 생성합니다.")
        self.btn_create_absence.clicked.connect(self._on_export_absence_document)

        self.btn_sync_mobile_att = QPushButton("모바일 출결 동기화")
        apply_qss_role(self.btn_sync_mobile_att, role="secondary", obj_name="BtnSyncMobileAttendance")
        self.btn_sync_mobile_att.setMinimumHeight(32)
        self.btn_sync_mobile_att.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sync_mobile_att.clicked.connect(self._on_sync_mobile_attendance)

        att_btn_row.addWidget(self.btn_add_attendance)
        att_btn_row.addWidget(self.btn_edit_attendance)
        att_btn_row.addWidget(self.btn_delete_attendance)
        att_btn_row.addWidget(self.btn_sync_mobile_att)
        att_btn_row.addStretch()
        att_btn_row.addWidget(self.btn_create_absence)
        c1_lay.addLayout(att_btn_row)

        # =====================================================================
        # CARD 2: 청소 당번(6명) 및 게시판(2명) 번호순 순환 배정기
        # =====================================================================
        self.card_cleaning = HoverZoomCard(
            title="2. 청소 당번(6명) · 게시판(2명) 순환 배정기",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_cleaning, role="hoverZoomCard", obj_name="CardCleaning")
        c2_lay = self.card_cleaning.body_layout()

        # 1줄: 안내 기간 + 청소 시작 번호 + 게시판 시작 번호
        duty_cfg_row = QHBoxLayout()
        duty_cfg_row.setSpacing(6)

        lbl_period = QLabel("기간:")
        apply_qss_role(lbl_period, role="fieldLabel")
        self.edit_duty_period = ClickablePeriodLineEdit(self._on_open_period_picker, self)
        self.edit_duty_period.setMinimumHeight(32)
        self.edit_duty_period.setPlaceholderText("클릭하여 달력에서 기간·격일 선택")
        self.edit_duty_period.setToolTip("클릭하면 달력이 열리며 평일 범위(월~목 등) 또는 격일(월·수·금 등)을 선택할 수 있습니다.")
        self.edit_duty_period.setText(format_duty_dates_korean(self._duty_selected_dates))

        lbl_cl_start = QLabel("청소 시작:")
        apply_qss_role(lbl_cl_start, role="fieldLabel")
        self.spin_clean_start = QSpinBox()
        self.spin_clean_start.setRange(1, 99)
        self.spin_clean_start.setSuffix("번")
        self.spin_clean_start.setMinimumHeight(32)
        install_no_scroll_wheel_filter(self.spin_clean_start)
        self.spin_clean_start.valueChanged.connect(self._on_duty_pointer_changed)

        lbl_bu_start = QLabel("게시판 시작:")
        apply_qss_role(lbl_bu_start, role="fieldLabel")
        self.spin_bulletin_start = QSpinBox()
        self.spin_bulletin_start.setRange(1, 99)
        self.spin_bulletin_start.setSuffix("번")
        self.spin_bulletin_start.setMinimumHeight(32)
        install_no_scroll_wheel_filter(self.spin_bulletin_start)
        self.spin_bulletin_start.valueChanged.connect(self._on_duty_pointer_changed)

        duty_cfg_row.addWidget(lbl_period)
        duty_cfg_row.addWidget(self.edit_duty_period, stretch=1)
        duty_cfg_row.addWidget(lbl_cl_start)
        duty_cfg_row.addWidget(self.spin_clean_start)
        duty_cfg_row.addWidget(lbl_bu_start)
        duty_cfg_row.addWidget(self.spin_bulletin_start)
        c2_lay.addLayout(duty_cfg_row)

        # 2줄: 예외 상태 배지 + 예외·결석 지정 + 다음 주로 넘기기 + 복사 + 한글 양식 출력
        duty_top_bar = QHBoxLayout()
        duty_top_bar.setSpacing(6)

        self.lbl_duty_exception_summary = QLabel("예외 없음")
        apply_qss_role(self.lbl_duty_exception_summary, role="badgeNeutral")
        self.lbl_duty_exception_summary.setToolTip("결석 제외 및 불성실 추가배정 학생 현황")

        self.btn_duty_exceptions = QPushButton("예외·결석 지정")
        apply_qss_role(self.btn_duty_exceptions, role="secondary", obj_name="BtnDutyExceptions")
        self.btn_duty_exceptions.setMinimumHeight(32)
        self.btn_duty_exceptions.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_duty_exceptions.setToolTip("결석생 제외 및 불성실 수행 학생 추가(재배정)를 설정합니다.")
        self.btn_duty_exceptions.clicked.connect(lambda: self._on_open_duty_exceptions(""))

        self.btn_advance_week = QPushButton("다음 주 순번으로")
        apply_qss_role(self.btn_advance_week, role="secondary", obj_name="BtnAdvanceDutyWeek")
        self.btn_advance_week.setMinimumHeight(32)
        self.btn_advance_week.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_advance_week.setToolTip("현재 주차 배정을 확정하고 다음 주 시작 번호로 넘깁니다.")
        self.btn_advance_week.clicked.connect(self._on_advance_duty_week)

        self.btn_copy_duty = QPushButton("복사")
        apply_qss_role(self.btn_copy_duty, role="secondary", obj_name="BtnCopyDutyResult")
        self.btn_copy_duty.setMinimumHeight(32)
        self.btn_copy_duty.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_duty.clicked.connect(self._on_copy_duty_clipboard)

        self.btn_assign = QPushButton("한글 양식 출력")
        apply_qss_role(self.btn_assign, role="primary", obj_name="BtnExportDutyHwpx")
        self.btn_assign.setMinimumHeight(32)
        self.btn_assign.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_assign.setToolTip("assets/cleaning_notice_template.hwpx 양식에 이름을 채워 저장합니다 (.hwpx 드래그 시 해당 양식으로 출력).")
        self.btn_assign.clicked.connect(self._on_export_duty_hwpx)

        for drop_w in (self.btn_assign, self.card_cleaning):
            enable_file_drop(
                drop_w,
                allowed_exts=[".hwpx"],
                allow_dirs=False,
                allow_files=True,
                multiple=False,
                on_files_dropped=lambda paths: self.export_duty_hwpx_with_template(paths[0]),
                context_label="당번 안내 한글 양식 (.hwpx)",
            )

        duty_top_bar.addWidget(self.lbl_duty_exception_summary)
        duty_top_bar.addWidget(self.btn_duty_exceptions)
        duty_top_bar.addStretch()
        duty_top_bar.addWidget(self.btn_copy_duty)
        duty_top_bar.addWidget(self.btn_advance_week)
        duty_top_bar.addWidget(self.btn_assign)
        c2_lay.addLayout(duty_top_bar)

        # 당번 배정 테이블 (구분 / 번호 / 학번 / 이름 / 배정 상태)
        self.table_cleaning = QTableWidget(0, 5)
        self.table_cleaning.setAlternatingRowColors(True)
        self.table_cleaning.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_cleaning.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_cleaning.verticalHeader().setDefaultSectionSize(30)
        self.table_cleaning.verticalHeader().setVisible(False)
        self.table_cleaning.setHorizontalHeaderLabels(["당번 구분", "번호", "학번", "이름", "배정 상태"])
        self.table_cleaning.setToolTip("학생 행을 더블클릭하면 해당 학생의 결석 제외·추가 재배정을 바로 설정할 수 있습니다.")
        self.table_cleaning.cellDoubleClicked.connect(self._on_duty_row_double_clicked)
        c_hdr = self.table_cleaning.horizontalHeader()
        c_hdr.setMinimumHeight(30)
        c_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        c_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        c_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        c_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        c_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        c2_lay.addWidget(self.table_cleaning, stretch=1)

        # 클립보드 복사용 숨김 텍스트 버퍼
        self.txt_cleaning_result = QTextEdit()
        apply_qss_role(self.txt_cleaning_result, obj_name="WebAiInputBox")
        self.txt_cleaning_result.setVisible(False)
        c2_lay.addWidget(self.txt_cleaning_result)

        # =====================================================================
        # TOOL 3: 모바일 가정통신문·서명 취합 센터
        # =====================================================================
        self.card_mobile_notice = HoverZoomCard(
            title="3. 모바일 가정통신문·서명 취합 센터",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        c3_lay = self.card_mobile_notice.body_layout()
        notice_top = QHBoxLayout()
        self.edit_notice_title = QLineEdit()
        self.edit_notice_title.setPlaceholderText("가정통신문 제목 (PDF 불러오기 시 자동 채움)")
        self.edit_notice_due = QLineEdit()
        self.edit_notice_due.setPlaceholderText("마감일 (예: 2026-11-25)")
        self.edit_notice_due.setFixedWidth(150)
        self.btn_load_notice_pdf = QPushButton("PDF 선택 → AI OCR 추출")
        apply_qss_role(self.btn_load_notice_pdf, role="secondary")
        self.btn_load_notice_pdf.setToolTip("PDF 파일을 선택하면 우측 AI 실행 패널(API/수동복붙/Playwright)로 구조화 JSON을 추출합니다.")
        self.btn_load_notice_pdf.clicked.connect(self._on_load_notice_pdf)
        self.btn_publish_mobile_notice = QPushButton("모바일 배포")
        apply_qss_role(self.btn_publish_mobile_notice, role="primary")
        self.btn_publish_mobile_notice.clicked.connect(self._on_publish_mobile_notice)
        notice_top.addWidget(self.edit_notice_title, stretch=1)
        notice_top.addWidget(self.edit_notice_due)
        notice_top.addWidget(self.btn_load_notice_pdf)
        notice_top.addWidget(self.btn_publish_mobile_notice)
        c3_lay.addLayout(notice_top)

        self.lbl_notice_pdf_status = QLabel("선택된 PDF: 없음 (PDF를 선택하면 우측 AI 실행 패널이 열립니다)")
        self.lbl_notice_pdf_status.setStyleSheet("color: #64748B; font-size: 11.5px;")
        c3_lay.addWidget(self.lbl_notice_pdf_status)

        self.txt_notice_content = QPlainTextEdit()
        self.txt_notice_content.setPlaceholderText("가정통신문 본문 내용이 여기에 표시됩니다.")
        self.txt_notice_content.setMinimumHeight(110)
        c3_lay.addWidget(self.txt_notice_content)

        self.edit_notice_fields = QLineEdit()
        self.edit_notice_fields.setPlaceholderText("추출된 회신 입력 항목 (쉼표 구분, 예: 참가 동의 여부, 비상연락처)")
        c3_lay.addWidget(self.edit_notice_fields)

        reply_bar = QHBoxLayout()
        self.edit_active_notice_id = QLineEdit()
        self.edit_active_notice_id.setPlaceholderText("조회할 가정통신문 ID (배포 시 자동 입력)")
        self.btn_refresh_replies = QPushButton("제출 현황 조회")
        apply_qss_role(self.btn_refresh_replies, role="secondary")
        self.btn_refresh_replies.clicked.connect(self._on_refresh_notice_replies)
        self.btn_nudge_unsubmitted = QPushButton("미제출자 일괄 알림")
        apply_qss_role(self.btn_nudge_unsubmitted, role="warningOutline")
        self.btn_nudge_unsubmitted.clicked.connect(self._on_nudge_unsubmitted_notice)
        reply_bar.addWidget(self.edit_active_notice_id, stretch=1)
        reply_bar.addWidget(self.btn_refresh_replies)
        reply_bar.addWidget(self.btn_nudge_unsubmitted)
        c3_lay.addLayout(reply_bar)

        self.table_notice_replies = QTableWidget(0, 5)
        self.table_notice_replies.setHorizontalHeaderLabels(["학번", "이름", "제출 상태", "회신/동의", "서명 URL"])
        self.table_notice_replies.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_notice_replies.setMinimumHeight(180)
        c3_lay.addWidget(self.table_notice_replies, stretch=1)

        # =====================================================================
        # TOOL 4: 학급 공지·긴급 알림 일괄 발송기
        # =====================================================================
        self.card_class_notify = HoverZoomCard(
            title="4. 학급 공지·긴급 알림 일괄 발송기",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        c4_lay = self.card_class_notify.body_layout()
        noti_top = QHBoxLayout()
        self.edit_broadcast_title = QLineEdit()
        self.edit_broadcast_title.setPlaceholderText("공지 / 긴급 알림 제목")
        self.combo_broadcast_target = QComboBox()
        self.combo_broadcast_target.addItem("학생 + 학부모 전체", "all")
        self.combo_broadcast_target.addItem("학생만", "student")
        self.combo_broadcast_target.addItem("학부모만", "parent")
        self.btn_send_broadcast = QPushButton("알림 일괄 발송")
        apply_qss_role(self.btn_send_broadcast, role="primary")
        self.btn_send_broadcast.clicked.connect(self._on_send_class_broadcast)
        self.btn_refresh_broadcast = QPushButton("이력 새로고침")
        apply_qss_role(self.btn_refresh_broadcast, role="secondary")
        self.btn_refresh_broadcast.clicked.connect(self._on_refresh_broadcast_history)
        noti_top.addWidget(self.edit_broadcast_title, stretch=1)
        noti_top.addWidget(self.combo_broadcast_target)
        noti_top.addWidget(self.btn_send_broadcast)
        noti_top.addWidget(self.btn_refresh_broadcast)
        c4_lay.addLayout(noti_top)

        self.txt_broadcast_msg = QPlainTextEdit()
        self.txt_broadcast_msg.setPlaceholderText("전달할 학급 공지 또는 긴급 안내 내용을 입력하세요.")
        self.txt_broadcast_msg.setMinimumHeight(90)
        c4_lay.addWidget(self.txt_broadcast_msg)

        self.table_broadcast_history = QTableWidget(0, 4)
        self.table_broadcast_history.setHorizontalHeaderLabels(["발송 일시", "대상", "제목", "내용"])
        self.table_broadcast_history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_broadcast_history.setMinimumHeight(200)
        c4_lay.addWidget(self.table_broadcast_history, stretch=1)

        # =====================================================================
        # TOOL 5: 1:1 상담 내역·누가기록 뷰어
        # =====================================================================
        self.card_chat_viewer = HoverZoomCard(
            title="5. 1:1 상담 내역·누가기록 뷰어",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        c5_lay = self.card_chat_viewer.body_layout()
        chat_top = QHBoxLayout()
        self.combo_chat_student = QComboBox()
        self.btn_load_chat_history = QPushButton("Firestore 상담 내역 불러오기")
        apply_qss_role(self.btn_load_chat_history, role="primary")
        self.btn_load_chat_history.clicked.connect(self._on_load_chat_history)
        self.btn_copy_chat_history = QPushButton("상담일지/생기부용 복사")
        apply_qss_role(self.btn_copy_chat_history, role="secondary")
        self.btn_copy_chat_history.clicked.connect(self._on_copy_chat_history)
        self.btn_save_chat_history = QPushButton("텍스트 저장 (.txt)")
        apply_qss_role(self.btn_save_chat_history, role="secondary")
        self.btn_save_chat_history.clicked.connect(self._on_save_chat_history)
        chat_top.addWidget(QLabel("학생 선택:"))
        chat_top.addWidget(self.combo_chat_student, stretch=1)
        chat_top.addWidget(self.btn_load_chat_history)
        chat_top.addWidget(self.btn_copy_chat_history)
        chat_top.addWidget(self.btn_save_chat_history)
        c5_lay.addLayout(chat_top)

        self.txt_chat_history_viewer = QPlainTextEdit()
        self.txt_chat_history_viewer.setReadOnly(True)
        self.txt_chat_history_viewer.setPlaceholderText("학생을 선택한 뒤 [Firestore 상담 내역 불러오기]를 클릭하세요.")
        self.txt_chat_history_viewer.setMinimumHeight(260)
        c5_lay.addWidget(self.txt_chat_history_viewer, stretch=1)

        # =====================================================================
        # TOOL 6: 시간표 관리 (컴시간 미지원 학교 수동·엑셀 업로드 & 변동/보강 입력)
        # =====================================================================
        self.card_timetable_manual = HoverZoomCard(
            title="6. 시간표 관리 (수동 · 엑셀/CSV 일괄 업로드 & 변동/보강)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        c6_lay = self.card_timetable_manual.body_layout()

        # 6-1. 주간 기본 시간표 엑셀/CSV 일괄 업로드
        tt_excel_bar = QHBoxLayout()
        tt_excel_bar.setSpacing(6)
        self.combo_tt_excel_type = QComboBox()
        self.combo_tt_excel_type.addItem("학급 기본 시간표 (class)", "class")
        self.combo_tt_excel_type.addItem("교사 개인 시간표 (teacher)", "teacher")
        self.btn_tt_load_excel = QPushButton("엑셀/CSV 파일 선택 (.xlsx, .csv)")
        apply_qss_role(self.btn_tt_load_excel, role="secondary")
        self.btn_tt_load_excel.clicked.connect(self._on_load_timetable_excel)
        self.btn_tt_upload_excel = QPushButton("Firestore 시간표_기본 업로드")
        apply_qss_role(self.btn_tt_upload_excel, role="primary")
        self.btn_tt_upload_excel.clicked.connect(self._on_upload_timetable_excel)
        self.lbl_tt_excel_status = QLabel("파일 미선택")
        self.lbl_tt_excel_status.setStyleSheet("color: #64748B; font-size: 12px;")
        tt_excel_bar.addWidget(QLabel("구분:"))
        tt_excel_bar.addWidget(self.combo_tt_excel_type)
        tt_excel_bar.addWidget(self.btn_tt_load_excel)
        tt_excel_bar.addWidget(self.btn_tt_upload_excel)
        tt_excel_bar.addWidget(self.lbl_tt_excel_status, stretch=1)
        c6_lay.addLayout(tt_excel_bar)

        self.table_tt_excel_preview = QTableWidget(0, 6)
        self.table_tt_excel_preview.setHorizontalHeaderLabels(
            ["구분", "학년-반", "요일", "교시", "과목", "교사명"]
        )
        self.table_tt_excel_preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_tt_excel_preview.setMinimumHeight(140)
        c6_lay.addWidget(self.table_tt_excel_preview, stretch=1)

        # 6-2. 특정 날짜·교시 변동/보강 수동 추가·수정
        lbl_chg_sec = QLabel("특정 날짜 · 교시 변동/보강 수동 입력 (Firestore 시간표_변동 반영)")
        lbl_chg_sec.setStyleSheet("font-weight: 700; color: #1E293B; margin-top: 6px;")
        c6_lay.addWidget(lbl_chg_sec)

        chg_row1 = QHBoxLayout()
        chg_row1.setSpacing(6)
        self.date_tt_chg = QDateEdit(QDate.currentDate())
        self.date_tt_chg.setCalendarPopup(True)
        self.date_tt_chg.setDisplayFormat("yyyy-MM-dd")
        self.spin_tt_chg_grade = QSpinBox()
        self.spin_tt_chg_grade.setRange(1, 6)
        self.spin_tt_chg_grade.setSuffix("학년")
        self.spin_tt_chg_class = QSpinBox()
        self.spin_tt_chg_class.setRange(1, 30)
        self.spin_tt_chg_class.setSuffix("반")
        self.spin_tt_chg_period = QSpinBox()
        self.spin_tt_chg_period.setRange(1, 10)
        self.spin_tt_chg_period.setValue(7)
        self.spin_tt_chg_period.setSuffix("교시")
        chg_row1.addWidget(QLabel("날짜:"))
        chg_row1.addWidget(self.date_tt_chg)
        chg_row1.addWidget(self.spin_tt_chg_grade)
        chg_row1.addWidget(self.spin_tt_chg_class)
        chg_row1.addWidget(self.spin_tt_chg_period)
        chg_row1.addStretch(1)
        c6_lay.addLayout(chg_row1)

        chg_row2 = QHBoxLayout()
        chg_row2.setSpacing(6)
        self.edit_tt_chg_orig_subj = QLineEdit()
        self.edit_tt_chg_orig_subj.setPlaceholderText("원래 과목 (보강/신규 시 비워둠)")
        self.edit_tt_chg_new_subj = QLineEdit()
        self.edit_tt_chg_new_subj.setPlaceholderText("변경/보강 과목 *")
        self.edit_tt_chg_orig_teacher = QLineEdit()
        self.edit_tt_chg_orig_teacher.setPlaceholderText("원래 교사명")
        self.edit_tt_chg_new_teacher = QLineEdit()
        self.edit_tt_chg_new_teacher.setPlaceholderText("변경 교사명")
        self.edit_tt_chg_reason = QLineEdit()
        self.edit_tt_chg_reason.setPlaceholderText("사유 (예: 보강, 자습, 출장)")
        self.btn_tt_save_change = QPushButton("변동/보강 Firestore 저장")
        apply_qss_role(self.btn_tt_save_change, role="primary")
        self.btn_tt_save_change.clicked.connect(self._on_save_manual_timetable_change)
        chg_row2.addWidget(self.edit_tt_chg_orig_subj)
        chg_row2.addWidget(self.edit_tt_chg_new_subj)
        chg_row2.addWidget(self.edit_tt_chg_orig_teacher)
        chg_row2.addWidget(self.edit_tt_chg_new_teacher)
        chg_row2.addWidget(self.edit_tt_chg_reason)
        chg_row2.addWidget(self.btn_tt_save_change)
        c6_lay.addLayout(chg_row2)

        self.tool_stack.addWidget(self.card_attendance)
        self.tool_stack.addWidget(self.card_cleaning)
        self.tool_stack.addWidget(self.card_mobile_notice)
        self.tool_stack.addWidget(self.card_class_notify)
        self.tool_stack.addWidget(self.card_chat_viewer)
        self.tool_stack.addWidget(self.card_timetable_manual)

        layout.addWidget(self.tool_stack, stretch=1)
        scroll.setWidget(container)
        root_grid.addWidget(scroll, 0, 0)

        # 미로그인 시 반투명 오버레이 + 정중앙 로그인 카드 (폭 420px)
        self.auth_overlay_backdrop = QFrame(self)
        self.auth_overlay_backdrop.setObjectName("ClassroomAuthBackdrop")
        self.auth_overlay_backdrop.setStyleSheet(
            "QFrame#ClassroomAuthBackdrop { background: rgba(15, 23, 42, 0.28); }"
        )
        backdrop_lay = QVBoxLayout(self.auth_overlay_backdrop)
        backdrop_lay.setContentsMargins(24, 24, 24, 24)
        backdrop_lay.addStretch(1)

        self.auth_login_card = QFrame(self.auth_overlay_backdrop)
        self.auth_login_card.setObjectName("ClassroomAuthLoginCard")
        self.auth_login_card.setFixedWidth(420)
        self.auth_login_card.setStyleSheet(
            "QFrame#ClassroomAuthLoginCard { "
            "background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 16px; "
            "}"
        )
        card_lay = QVBoxLayout(self.auth_login_card)
        card_lay.setContentsMargins(28, 28, 28, 28)
        card_lay.setSpacing(14)

        lbl_card_title = QLabel("EduFlow Mobile 교사 계정 로그인")
        lbl_card_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_card_title.setStyleSheet("font-size: 17px; font-weight: 700; color: #0F172A; border: none;")

        lbl_card_desc = QLabel(
            "로그인하면 소속 학교·학년·반 명렬표가 연동되고\n"
            "학급 경영 도구함(출결·청소·가정통신문·알림·상담·시간표)이 활성화됩니다."
        )
        lbl_card_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_card_desc.setWordWrap(True)
        lbl_card_desc.setStyleSheet("font-size: 12px; color: #64748B; border: none;")

        self.edit_teacher_login_id = QLineEdit()
        self.edit_teacher_login_id.setMinimumHeight(38)
        self.edit_teacher_login_id.setPlaceholderText("교사 로그인 ID (예: 유우석)")

        pw_row = QHBoxLayout()
        pw_row.setSpacing(6)
        self.edit_teacher_login_pw = QLineEdit()
        self.edit_teacher_login_pw.setMinimumHeight(38)
        self.edit_teacher_login_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_teacher_login_pw.setPlaceholderText("비밀번호")
        self.edit_teacher_login_pw.returnPressed.connect(self._on_teacher_login_clicked)

        self.btn_toggle_pw_vis = QPushButton("👁")
        self.btn_toggle_pw_vis.setCheckable(True)
        self.btn_toggle_pw_vis.setFixedSize(38, 38)
        self.btn_toggle_pw_vis.setToolTip("비밀번호 표시/숨김")
        self.btn_toggle_pw_vis.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_pw_vis.toggled.connect(
            lambda checked: self.edit_teacher_login_pw.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        pw_row.addWidget(self.edit_teacher_login_pw, stretch=1)
        pw_row.addWidget(self.btn_toggle_pw_vis)

        self.btn_teacher_login = QPushButton("로그인")
        apply_qss_role(self.btn_teacher_login, role="primary", obj_name="BtnTeacherLogin")
        self.btn_teacher_login.setMinimumHeight(40)
        self.btn_teacher_login.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_teacher_login.clicked.connect(self._on_teacher_login_clicked)

        card_lay.addWidget(lbl_card_title)
        card_lay.addWidget(lbl_card_desc)
        card_lay.addWidget(self.edit_teacher_login_id)
        card_lay.addLayout(pw_row)
        card_lay.addWidget(self.btn_teacher_login)

        backdrop_lay.addWidget(self.auth_login_card, alignment=Qt.AlignmentFlag.AlignCenter)
        backdrop_lay.addStretch(1)
        root_grid.addWidget(self.auth_overlay_backdrop, 0, 0)

    # =========================================================================
    # 교사 계정 로그인 / 세션 복원 / Firebase 명렬표 동적 연동
    # =========================================================================
    def _apply_teacher_auth_state(self) -> None:
        logged_in = bool(self._logged_in_teacher)
        self.auth_status_widget.setVisible(logged_in)
        self.auth_overlay_backdrop.setVisible(not logged_in)
        self.pill_bar.setVisible(True)
        self.tool_stack.setVisible(True)

        if not logged_in:
            blur = QGraphicsBlurEffect(self._main_scroll)
            blur.setBlurRadius(9.0)
            self._main_scroll.setGraphicsEffect(blur)
            self.auth_overlay_backdrop.raise_()
            self.lbl_teacher_auth_status.setText("[교사] 미로그인")
        else:
            self._main_scroll.setGraphicsEffect(None)
            meta = self._teacher_meta()
            name = meta["teacher_name"]
            school_name = meta["school_name"] or meta["school_code"] or "소속학교"
            g = meta["grade"]
            c = meta["class_no"]
            cls_part = f"{school_name} {g}학년 {c}반" if (g and c) else school_name
            self.lbl_teacher_auth_status.setText(f"[교사] {name} ({cls_part})")
            if hasattr(self, "spin_tt_chg_grade") and g:
                self.spin_tt_chg_grade.setValue(int(g))
            if hasattr(self, "spin_tt_chg_class") and c:
                self.spin_tt_chg_class.setValue(int(c))

    def _on_teacher_login_clicked(self) -> None:
        uid = self.edit_teacher_login_id.text().strip()
        pw = self.edit_teacher_login_pw.text()
        if not uid:
            QMessageBox.warning(self, "로그인 확인", "교사 로그인 ID를 입력해 주세요.")
            self.edit_teacher_login_id.setFocus()
            return
        ok, err = self.login_teacher(uid, pw)
        if not ok:
            QMessageBox.warning(self, "로그인 실패", err or "로그인에 실패했습니다.")
            return
        self.edit_teacher_login_pw.clear()

    def login_teacher(
        self,
        user_id: str,
        password: str,
        sync_service: Optional[FirestoreSyncService] = None,
    ) -> Tuple[bool, str]:
        svc = sync_service or FirestoreSyncService(db=self.db, config=self.config)
        try:
            ok, user_doc, err = svc.authenticate_account("teacher", user_id, password)
        except Exception as exc:
            return False, f"Firestore 인증 오류: {exc}"
        if not ok or not user_doc:
            return False, err or "교사 계정을 확인할 수 없습니다."

        school_code = str(user_doc.get("school_code") or user_doc.get("neis_school_code") or "").strip()
        school_name = str(user_doc.get("school_name") or "").strip()
        grade_val = user_doc.get("grade")
        class_val = user_doc.get("class_no")
        teacher_name = str(user_doc.get("name") or user_doc.get("teacher_id") or user_id).strip()
        teacher_id = str(user_doc.get("teacher_id") or user_doc.get("remote_id") or user_id).strip()
        login_id = str(user_doc.get("login_id") or user_id).strip()
        is_admin = bool(user_doc.get("is_admin") is True)

        self._logged_in_teacher = {
            "teacher_id": teacher_id,
            "login_id": login_id,
            "name": teacher_name,
            "school_code": school_code,
            "neis_school_code": school_code,
            "school_name": school_name,
            "grade": int(grade_val) if str(grade_val or "").isdigit() else None,
            "class_no": int(class_val) if str(class_val or "").isdigit() else None,
            "is_admin": is_admin,
        }

        tp_sec = dict(self.config.get_section("teacher_profile") or {})
        tp_sec.update(self._logged_in_teacher)
        tp_sec["session_logged_in"] = True
        tp_sec["auth_password_hash"] = hash_password_sha256(password) if password else ""
        self.config.update_section("teacher_profile", tp_sec)

        self.refresh_firebase_roster(sync_service=svc, silent=True)
        self._apply_teacher_auth_state()
        return True, ""

    def logout_teacher(self) -> None:
        self._logged_in_teacher = None
        self._firebase_roster = []
        self.config.update_section(
            "teacher_profile",
            {"session_logged_in": False, "auth_password_hash": ""},
        )
        self._apply_teacher_auth_state()
        self._refresh_roster_dependent_views()

    def restore_saved_teacher_session(
        self,
        sync_service: Optional[FirestoreSyncService] = None,
    ) -> bool:
        tp_sec = dict(self.config.get_section("teacher_profile") or {})
        if not tp_sec.get("session_logged_in"):
            self._logged_in_teacher = None
            self._apply_teacher_auth_state()
            return False
        tid = str(tp_sec.get("teacher_id") or tp_sec.get("login_id") or tp_sec.get("name") or "").strip()
        if not tid:
            self._logged_in_teacher = None
            self._apply_teacher_auth_state()
            return False

        self._logged_in_teacher = {
            "teacher_id": tid,
            "login_id": str(tp_sec.get("login_id") or tid).strip(),
            "name": str(tp_sec.get("name") or tid).strip(),
            "school_code": str(tp_sec.get("school_code") or tp_sec.get("neis_school_code") or "").strip(),
            "neis_school_code": str(tp_sec.get("neis_school_code") or tp_sec.get("school_code") or "").strip(),
            "school_name": str(tp_sec.get("school_name") or "").strip(),
            "grade": int(tp_sec["grade"]) if str(tp_sec.get("grade") or "").isdigit() else None,
            "class_no": int(tp_sec["class_no"]) if str(tp_sec.get("class_no") or "").isdigit() else None,
            "is_admin": bool(tp_sec.get("is_admin") is True),
        }
        self._apply_teacher_auth_state()
        if sync_service is not None:
            self.refresh_firebase_roster(sync_service=sync_service, silent=True)
        return True

    def refresh_firebase_roster(
        self,
        sync_service: Optional[FirestoreSyncService] = None,
        silent: bool = False,
    ) -> int:
        if not self._logged_in_teacher:
            return 0
        meta = self._teacher_meta()
        svc = sync_service or FirestoreSyncService(db=self.db, config=self.config)
        try:
            students = svc.fetch_homeroom_students(
                school_code=meta["school_code"],
                school_name=meta["school_name"],
                grade=meta["grade"],
                class_no=meta["class_no"],
            )
            self._firebase_roster = [
                (str(s.get("student_id") or s.get("remote_id") or "").strip(), str(s.get("name") or "").strip())
                for s in students
                if str(s.get("student_id") or s.get("remote_id") or "").strip()
            ]
        except Exception as exc:
            if not silent:
                QMessageBox.warning(self, "명렬표 동기화 오류", f"Firestore 명렬표 조회 실패:\n{exc}")
            return 0

        self._apply_teacher_auth_state()
        self._refresh_roster_dependent_views()
        return len(self._firebase_roster)

    # =========================================================================
    # 데이터 연동 및 액션 핸들러
    # =========================================================================
    def _current_homeroom_key(self) -> str:
        if self._logged_in_teacher:
            meta = self._teacher_meta()
            sn = meta["school_name"] or meta["school_code"] or "school"
            g = meta["grade"]
            c = meta["class_no"]
            if g and c:
                return f"{sn}_{g}학년_{c}반"
            return f"{sn}_{meta['teacher_name']}"
        hr = self.db.get_active_homeroom_group()
        if hr and str(hr.get("group_name", "")).strip():
            return str(hr["group_name"]).strip()
        return "__sample__"

    def _current_homeroom_label(self) -> str:
        if self._logged_in_teacher:
            meta = self._teacher_meta()
            g = meta["grade"]
            c = meta["class_no"]
            if g and c:
                return f"{g}학년 {c}반"
            return meta["school_name"] or meta["teacher_name"]
        hr = self.db.get_active_homeroom_group()
        if hr and str(hr.get("group_name", "")).strip():
            return str(hr["group_name"]).strip()
        return "예시 학급"

    def _refresh_roster_dependent_views(self) -> None:
        if self._logged_in_teacher and self._firebase_roster:
            label = self._current_homeroom_label()
            self.lbl_roster_status_badge.setText(
                f"연동 명렬표: [Firebase] {label} ({len(self._firebase_roster)}명)"
            )
            apply_qss_role(self.lbl_roster_status_badge, role="badgeSuccess")
            self.lbl_roster_status_badge.setToolTip("로그인한 교사 계정의 Firestore 학급 명렬표와 연동되었습니다.")
        else:
            hr_group = self.db.get_active_homeroom_group()
            homeroom_students = self.db.get_active_homeroom_students()

            if hr_group and homeroom_students:
                self.lbl_roster_status_badge.setText(
                    f"연동 명렬표: [담임 학급] {hr_group['group_name']} ({len(homeroom_students)}명)"
                )
                apply_qss_role(self.lbl_roster_status_badge, role="badgeSuccess")
                self.lbl_roster_status_badge.setToolTip("나이스 학급담임 명렬표와 정상 연동되었습니다.")
            elif hr_group:
                self.lbl_roster_status_badge.setText(
                    f"연동 명렬표: [담임 학급] {hr_group['group_name']} (0명)"
                )
                apply_qss_role(self.lbl_roster_status_badge, role="badgeWarning")
                self.lbl_roster_status_badge.setToolTip("환경설정 탭에서 나이스 학급담임 명렬표를 먼저 등록해주세요")
            else:
                self.lbl_roster_status_badge.setText("연동 명렬표: [담임 학급] 등록된 담임 학급 없음")
                apply_qss_role(self.lbl_roster_status_badge, role="badgeWarning")
                self.lbl_roster_status_badge.setToolTip("환경설정 탭에서 나이스 학급담임 명렬표를 먼저 등록해주세요")

        roster = self._get_active_roster()
        self.combo_chat_student.blockSignals(True)
        self.combo_chat_student.clear()
        self.combo_chat_student.addItem("우리 반 전체", "")
        for sid, name in roster:
            self.combo_chat_student.addItem(f"{sid} {name}", sid)
        self.combo_chat_student.blockSignals(False)

        self._refresh_attendance_view()
        self._load_duty_state_into_ui()
        self._update_duty_preview()

    def _teacher_meta(self) -> Dict[str, Any]:
        if self._logged_in_teacher:
            t = self._logged_in_teacher
            return {
                "teacher_id": str(t.get("teacher_id") or "").strip(),
                "grade": t.get("grade"),
                "class_no": t.get("class_no"),
                "school_code": str(t.get("school_code") or t.get("neis_school_code") or "").strip(),
                "school_name": str(t.get("school_name") or "").strip(),
                "teacher_name": str(t.get("name") or "담임교사").strip(),
                "is_admin": bool(t.get("is_admin") is True),
            }
        tp = self.config.get_section("teacher_profile") or {}
        return {
            "teacher_id": str(tp.get("teacher_id") or "").strip(),
            "grade": tp.get("grade"),
            "class_no": tp.get("class_no"),
            "school_code": str(tp.get("school_code") or tp.get("neis_school_code") or "").strip(),
            "school_name": str(tp.get("school_name") or "").strip(),
            "teacher_name": str(tp.get("name") or "담임교사").strip(),
            "is_admin": bool(tp.get("is_admin") is True),
        }

    def _on_sync_mobile_attendance(self) -> None:
        meta = self._teacher_meta()
        svc = FirestoreSyncService(db=self.db, config=self.config)
        try:
            rows = svc.fetch_homeroom_attendance(
                grade=meta["grade"],
                class_no=meta["class_no"],
                school_code=meta["school_code"],
                school_name=meta["school_name"],
            )
            QMessageBox.information(self, "모바일 출결 동기화", f"Firestore에서 {len(rows)}건의 출결 기록을 조회했습니다.")
        except Exception as exc:
            QMessageBox.warning(self, "모바일 출결 동기화 오류", str(exc))

    # =========================================================================
    # 가정통신문 PDF -> AiExecutionDock 구조화 JSON OCR 연동 (pypdf 미사용)
    # =========================================================================
    def get_ai_context(self) -> Dict[str, Any]:
        files = [self._selected_notice_pdf_path] if self._selected_notice_pdf_path else []
        return {
            "target_id": "notice_ocr",
            "kind": "ocr",
            "task_label": "가정통신문 PDF 구조화 JSON 추출",
            "ocr_files": files,
            "attachment_paths": files,
            "ocr_prompt": NOTICE_OCR_JSON_PROMPT,
        }

    def apply_ai_dock_result(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        raw_source = payload.get("parsed_json")
        if not isinstance(raw_source, dict):
            raw_source = payload.get("raw_response") or payload.get("formatted_text") or ""
        if not raw_source:
            return
        try:
            parsed = parse_notice_ai_json(raw_source)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "가정통신문 JSON 파싱 오류",
                f"AI 응답에서 가정통신문 JSON을 해석하지 못했습니다:\n{exc}",
            )
            return

        if parsed["title"]:
            self.edit_notice_title.setText(parsed["title"])
        if parsed["deadline"]:
            self.edit_notice_due.setText(parsed["deadline"])
        if parsed["body"]:
            self.txt_notice_content.setPlainText(parsed["body"])
        if parsed["reply_fields"]:
            self.edit_notice_fields.setText(", ".join(parsed["reply_fields"]))
        self.lbl_notice_pdf_status.setText("AI 구조화 JSON 반영 완료 — 내용 확인 후 [모바일 배포]를 클릭하세요.")

    def _on_load_notice_pdf(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "가정통신문 PDF 선택",
            "",
            "PDF & Image Files (*.pdf *.png *.jpg *.jpeg *.webp)",
        )
        if not file_path:
            return
        self._selected_notice_pdf_path = str(file_path).strip()
        self.lbl_notice_pdf_status.setText(
            f"선택된 파일: {Path(file_path).name} → 우측 AI 실행 패널에서 API / 수동 복붙 / Playwright로 추출하세요."
        )
        self.ai_context_changed.emit()
        self.ai_target_requested.emit("notice_ocr")

    def _on_load_timetable_excel(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "주간 시간표 엑셀/CSV 선택",
            "",
            "Spreadsheet Files (*.xlsx *.xlsm *.csv)",
        )
        if not file_path:
            return
        meta = self._teacher_meta()
        entry_type = str(self.combo_tt_excel_type.currentData() or "class")
        try:
            rows = parse_weekly_timetable_file(
                file_path,
                school_code=meta["school_code"],
                school_name=meta["school_name"],
                default_grade=meta["grade"],
                default_class=meta["class_no"],
                default_teacher=meta["teacher_name"],
                entry_type=entry_type,
            )
        except Exception as exc:
            QMessageBox.warning(self, "시간표 파일 파싱 오류", str(exc))
            return

        self._parsed_excel_timetable_rows = rows
        self.table_tt_excel_preview.setRowCount(0)
        for item in rows:
            r = self.table_tt_excel_preview.rowCount()
            self.table_tt_excel_preview.insertRow(r)
            gc = f"{item.get('grade') or '-'}-{item.get('class_no') or '-'}"
            self.table_tt_excel_preview.setItem(r, 0, QTableWidgetItem(str(item.get("entry_type") or "")))
            self.table_tt_excel_preview.setItem(r, 1, QTableWidgetItem(gc))
            self.table_tt_excel_preview.setItem(r, 2, QTableWidgetItem(str(item.get("day_of_week") or "")))
            self.table_tt_excel_preview.setItem(r, 3, QTableWidgetItem(str(item.get("period") or "")))
            self.table_tt_excel_preview.setItem(r, 4, QTableWidgetItem(str(item.get("subject") or "")))
            self.table_tt_excel_preview.setItem(r, 5, QTableWidgetItem(str(item.get("teacher_name") or "")))
        self.lbl_tt_excel_status.setText(f"{Path(file_path).name} ({len(rows)}건 파싱됨)")

    def _on_upload_timetable_excel(self) -> None:
        if not self._parsed_excel_timetable_rows:
            QMessageBox.information(self, "업로드 안내", "먼저 엑셀/CSV 시간표 파일을 선택해 주세요.")
            return
        svc = FirestoreSyncService(db=self.db, config=self.config)
        ok = svc.upsert_timetable_base(self._parsed_excel_timetable_rows)
        if ok:
            QMessageBox.information(
                self,
                "업로드 완료",
                f"Firestore `시간표_기본`에 {len(self._parsed_excel_timetable_rows)}건(source=excel)이 반영되었습니다.",
            )
        else:
            QMessageBox.warning(self, "업로드 실패", "Firestore 시간표_기본 반영에 실패했습니다.")

    def _on_save_manual_timetable_change(self) -> None:
        new_subj = self.edit_tt_chg_new_subj.text().strip()
        if not new_subj:
            QMessageBox.warning(self, "입력 확인", "변경/보강 과목명을 입력해 주세요.")
            self.edit_tt_chg_new_subj.setFocus()
            return
        meta = self._teacher_meta()
        row = build_manual_timetable_change(
            date_str=self.date_tt_chg.date().toString("yyyy-MM-dd"),
            period=self.spin_tt_chg_period.value(),
            changed_subject=new_subj,
            grade=self.spin_tt_chg_grade.value(),
            class_no=self.spin_tt_chg_class.value(),
            original_subject=self.edit_tt_chg_orig_subj.text().strip(),
            original_teacher=self.edit_tt_chg_orig_teacher.text().strip() or meta["teacher_name"],
            changed_teacher=self.edit_tt_chg_new_teacher.text().strip() or meta["teacher_name"],
            reason=self.edit_tt_chg_reason.text().strip() or "수동 변동",
            school_code=meta["school_code"],
            school_name=meta["school_name"],
            source="manual",
        )
        svc = FirestoreSyncService(db=self.db, config=self.config)
        ok = svc.push_timetable_changes([row])
        if ok:
            QMessageBox.information(
                self,
                "저장 완료",
                f"{row['date']} {row['period']}교시 변동/보강({row['changed_subject']})이 Firestore에 저장되었습니다.",
            )
        else:
            QMessageBox.warning(self, "저장 실패", "Firestore 시간표_변동 저장에 실패했습니다.")

    def _on_publish_mobile_notice(self) -> None:
        title = self.edit_notice_title.text().strip()
        if not title:
            QMessageBox.warning(self, "입력 확인", "가정통신문 제목을 입력해 주세요.")
            return
        meta = self._teacher_meta()
        fields = [f.strip() for f in self.edit_notice_fields.text().split(",") if f.strip()]
        svc = FirestoreSyncService(db=self.db)
        ok, rid = svc.publish_mobile_notice({
            "title": title,
            "content": self.txt_notice_content.toPlainText().strip(),
            "due_date": self.edit_notice_due.text().strip(),
            "reply_fields": fields,
            "target_grade": meta["grade"],
            "target_class": meta["class_no"],
            "school_code": meta["school_code"],
            "school_name": meta["school_name"],
            "author_name": meta["teacher_name"],
        })
        if ok:
            self.edit_active_notice_id.setText(rid)
            QMessageBox.information(self, "배포 완료", f"모바일 가정통신문이 배포되었습니다.\n(ID: {rid})")

    def _on_refresh_notice_replies(self) -> None:
        nid = self.edit_active_notice_id.text().strip()
        if not nid:
            QMessageBox.information(self, "조회 안내", "먼저 가정통신문 ID를 입력하거나 배포해 주세요.")
            return
        svc = FirestoreSyncService(db=self.db)
        replies = svc.fetch_notice_replies(nid)
        rep_map = {str(r.get("student_id") or ""): r for r in replies}
        roster = self._get_active_roster()
        self.table_notice_replies.setRowCount(0)
        for sid, name in roster:
            r = self.table_notice_replies.rowCount()
            self.table_notice_replies.insertRow(r)
            rep = rep_map.get(str(sid))
            self.table_notice_replies.setItem(r, 0, QTableWidgetItem(str(sid)))
            self.table_notice_replies.setItem(r, 1, QTableWidgetItem(str(name)))
            self.table_notice_replies.setItem(r, 2, QTableWidgetItem("제출완료" if rep else "미제출"))
            self.table_notice_replies.setItem(r, 3, QTableWidgetItem(str(rep.get("reply_choice", "")) if rep else "-"))
            self.table_notice_replies.setItem(r, 4, QTableWidgetItem(str(rep.get("signature_drive_url") or rep.get("signature_url") or "") if rep else ""))

    def _on_nudge_unsubmitted_notice(self) -> None:
        nid = self.edit_active_notice_id.text().strip()
        title = self.edit_notice_title.text().strip() or "가정통신문"
        if not nid:
            return
        meta = self._teacher_meta()
        svc = FirestoreSyncService(db=self.db)
        svc.send_class_notification(
            title=f"[미제출 안내] {title}",
            message="가정통신문 전자서명 회신이 아직 완료되지 않았습니다. 확인 후 제출해 주세요.",
            target_role="all",
            grade=meta["grade"],
            class_no=meta["class_no"],
            school_code=meta["school_code"],
            school_name=meta["school_name"],
            sender=meta["teacher_name"],
        )
        QMessageBox.information(self, "알림 발송 완료", "미제출 안내 알림이 Firestore에 등록되었습니다.")

    def _on_send_class_broadcast(self) -> None:
        title = self.edit_broadcast_title.text().strip()
        msg = self.txt_broadcast_msg.toPlainText().strip()
        if not title or not msg:
            QMessageBox.warning(self, "입력 확인", "알림 제목과 내용을 모두 입력해 주세요.")
            return
        meta = self._teacher_meta()
        svc = FirestoreSyncService(db=self.db)
        svc.send_class_notification(
            title=title,
            message=msg,
            target_role=str(self.combo_broadcast_target.currentData() or "all"),
            grade=meta["grade"],
            class_no=meta["class_no"],
            school_code=meta["school_code"],
            school_name=meta["school_name"],
            sender=meta["teacher_name"],
        )
        self.edit_broadcast_title.clear()
        self.txt_broadcast_msg.clear()
        self._on_refresh_broadcast_history()
        QMessageBox.information(self, "발송 완료", "학급 공지·긴급 알림이 발송되었습니다.")

    def _on_refresh_broadcast_history(self) -> None:
        meta = self._teacher_meta()
        svc = FirestoreSyncService(db=self.db, config=self.config)
        rows = svc.fetch_sent_notifications(
            grade=meta["grade"],
            class_no=meta["class_no"],
            school_code=meta["school_code"],
            school_name=meta["school_name"],
        )
        self.table_broadcast_history.setRowCount(0)
        for item in rows:
            r = self.table_broadcast_history.rowCount()
            self.table_broadcast_history.insertRow(r)
            self.table_broadcast_history.setItem(r, 0, QTableWidgetItem(str(item.get("created_at") or "")))
            self.table_broadcast_history.setItem(r, 1, QTableWidgetItem(str(item.get("target_role") or "all")))
            self.table_broadcast_history.setItem(r, 2, QTableWidgetItem(str(item.get("title") or "")))
            self.table_broadcast_history.setItem(r, 3, QTableWidgetItem(str(item.get("message") or "")))

    def _on_load_chat_history(self) -> None:
        sid = str(self.combo_chat_student.currentData() or "").strip()
        meta = self._teacher_meta()
        svc = FirestoreSyncService(db=self.db, config=self.config)
        rows = svc.fetch_chat_history(
            grade=meta["grade"],
            class_no=meta["class_no"],
            student_id=sid,
            school_code=meta["school_code"],
            school_name=meta["school_name"],
        )
        if not rows:
            self.txt_chat_history_viewer.setPlainText("조회된 1:1 상담 대화 내역이 없습니다.")
            return
        lines = []
        for m in rows:
            ts = str(m.get("created_at") or "")
            sender = m.get("sender_name") or m.get("sender_role") or ""
            lines.append(f"[{ts}] {sender}: {m.get('message', '')}")
        self.txt_chat_history_viewer.setPlainText("\n".join(lines))

    def _on_copy_chat_history(self) -> None:
        txt = self.txt_chat_history_viewer.toPlainText().strip()
        if txt:
            QGuiApplication.clipboard().setText(txt)
            QMessageBox.information(self, "복사 완료", "상담 내역이 클립보드에 복사되었습니다.")

    def _on_save_chat_history(self) -> None:
        txt = self.txt_chat_history_viewer.toPlainText().strip()
        if not txt:
            return
        path, _ = QFileDialog.getSaveFileName(self, "상담 내역 저장", "1대1_상담내역.txt", "Text Files (*.txt)")
        if path:
            Path(path).write_text(txt, encoding="utf-8")
            QMessageBox.information(self, "저장 완료", f"저장되었습니다:\n{path}")

    # -------------------------------------------------------------------------
    # CARD 1: 출결 및 결석계 대장 (SQLite 연동 + 한글 .hwpx 출력)
    # -------------------------------------------------------------------------
    def _refresh_attendance_view(self, select_log_id: Optional[int] = None) -> None:
        roster = self._get_active_roster()
        total = len(roster)
        logs = self.db.get_attendance_logs(self._current_homeroom_key())

        pending_cnt = sum(1 for r in logs if "대기" in str(r.get("proof_status", "")))
        sick_cnt = sum(1 for r in logs if str(r.get("category", "")) in ("질병결석", "기타·미인정"))
        sick_pending = sum(
            1
            for r in logs
            if str(r.get("category", "")) in ("질병결석", "기타·미인정")
            and "대기" in str(r.get("proof_status", ""))
        )
        trip_cnt = sum(1 for r in logs if str(r.get("category", "")) in ("교외체험학습", "출석인정"))

        self.lbl_att_present.setText(f"재적 {total}명 · {len(logs)}건")
        if pending_cnt > 0:
            self.badge_att_present.setText(f"미완비 {pending_cnt}건")
            apply_qss_role(self.badge_att_present, role="badgeWarning")
        else:
            self.badge_att_present.setText("서류 완비")
            apply_qss_role(self.badge_att_present, role="badgeSuccess")

        self.lbl_att_sick.setText(f"{sick_cnt}건")
        if sick_pending > 0:
            self.badge_att_sick.setText(f"대기 {sick_pending}건")
            apply_qss_role(self.badge_att_sick, role="badgeWarning")
        else:
            self.badge_att_sick.setText("확인 완료")
            apply_qss_role(self.badge_att_sick, role="badgeNeutral")

        self.lbl_att_trip.setText(f"{trip_cnt}건")
        self.badge_att_trip.setText("체험·인정")
        apply_qss_role(self.badge_att_trip, role="badgePrimary")

        mono_font = QFont("JetBrains Mono", 9)
        self.table_attendance.setRowCount(0)
        target_row = -1
        for idx, row_data in enumerate(logs):
            self.table_attendance.insertRow(idx)
            log_id = int(row_data["id"])
            sid = str(row_data["student_id"])
            name = str(row_data["student_name"])
            cat = str(row_data["category"])
            reason = str(row_data["reason"])
            cat_reason = f"{cat} ({reason})" if reason else cat
            period = str(row_data["period_text"])
            status = str(row_data["proof_status"])

            it_sid = QTableWidgetItem(sid)
            it_sid.setFont(mono_font)
            it_sid.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_sid.setData(Qt.ItemDataRole.UserRole, log_id)
            it_sid.setData(Qt.ItemDataRole.UserRole + 1, row_data)

            it_name = QTableWidgetItem(name)
            it_name.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_reason = QTableWidgetItem(cat_reason)
            it_period = QTableWidgetItem(period)
            it_period.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_status = QTableWidgetItem(status)
            it_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for c_idx, it in enumerate((it_sid, it_name, it_reason, it_period, it_status)):
                self.table_attendance.setItem(idx, c_idx, it)
            if select_log_id is not None and log_id == select_log_id:
                target_row = idx

        if target_row >= 0:
            self.table_attendance.selectRow(target_row)
        elif self.table_attendance.rowCount() > 0:
            self.table_attendance.selectRow(0)

    def _selected_attendance_record(self) -> Optional[Dict[str, Any]]:
        row = self.table_attendance.currentRow()
        if row < 0:
            return None
        it = self.table_attendance.item(row, 0)
        if it is None:
            return None
        data = it.data(Qt.ItemDataRole.UserRole + 1)
        return data if isinstance(data, dict) else None

    def _on_add_attendance_entry(self) -> None:
        dlg = AttendanceEntryDialog(roster=self._get_active_roster(), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dlg.result_payload()
        new_id = self.db.add_attendance_log(
            self._current_homeroom_key(),
            student_id=payload["student_id"],
            student_name=payload["student_name"],
            category=payload["category"],
            reason=payload["reason"],
            period_text=payload["period_text"],
            selected_dates=payload["selected_dates"],
            proof_status=payload["proof_status"],
        )
        self._refresh_attendance_view(select_log_id=new_id)

    def _on_attendance_row_double_clicked(self, _row: int, _col: int) -> None:
        self._on_edit_attendance_entry()

    def _on_edit_attendance_entry(self) -> None:
        rec = self._selected_attendance_record()
        if rec is None:
            QMessageBox.information(self, "항목 선택", "수정할 출결 내역을 표에서 선택해 주세요.")
            return
        dlg = AttendanceEntryDialog(
            roster=self._get_active_roster(),
            initial_data=rec,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dlg.result_payload()
        log_id = int(rec["id"])
        self.db.update_attendance_log(
            log_id,
            student_id=payload["student_id"],
            student_name=payload["student_name"],
            category=payload["category"],
            reason=payload["reason"],
            period_text=payload["period_text"],
            selected_dates=payload["selected_dates"],
            proof_status=payload["proof_status"],
        )
        self._refresh_attendance_view(select_log_id=log_id)

    def _on_delete_attendance_entry(self) -> None:
        rec = self._selected_attendance_record()
        if rec is None:
            QMessageBox.information(self, "항목 선택", "삭제할 출결 내역을 표에서 선택해 주세요.")
            return
        ans = QMessageBox.question(
            self,
            "출결 내역 삭제",
            f"[{rec['student_id']} {rec['student_name']}] ({rec['category']}) 내역을 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.db.delete_attendance_log(int(rec["id"]))
            self._refresh_attendance_view()

    def _on_export_absence_document(self) -> None:
        rec = self._selected_attendance_record()
        if rec is None:
            QMessageBox.information(
                self,
                "출결 내역 선택",
                "출력할 출결 내역이 없습니다. 먼저 [출결 등록]으로 항목을 추가하거나 행을 선택해 주세요.",
            )
            return

        sid = str(rec["student_id"])
        name = str(rec["student_name"])
        category = str(rec["category"])
        reason = str(rec["reason"])
        period = str(rec["period_text"])
        proof_status = str(rec["proof_status"])
        doc_prefix = "출석인정확인서" if category in ("교외체험학습", "출석인정") else "결석계"

        default_name = f"{doc_prefix}_{sid}_{name}.hwpx"
        file_path_str, selected_filter = QFileDialog.getSaveFileName(
            self,
            "결석계 · 출석인정서 파일 저장",
            default_name,
            "한글 표준 문서 (*.hwpx);;한글 문서 (*.hwp);;CSV 파일 (*.csv)",
        )
        if not file_path_str:
            return

        out_path = Path(file_path_str)
        if out_path.suffix.lower() not in (".hwpx", ".hwp", ".csv"):
            if "*.hwp)" in selected_filter:
                out_path = out_path.with_suffix(".hwp")
            elif "*.csv)" in selected_filter:
                out_path = out_path.with_suffix(".csv")
            else:
                out_path = out_path.with_suffix(".hwpx")

        try:
            ext = out_path.suffix.lower()
            if ext == ".csv":
                detail_reason = f"{category} ({reason})" if reason else category
                with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["항목", "내용"])
                    writer.writerow(["학급", self._current_homeroom_label()])
                    writer.writerow(["학번", sid])
                    writer.writerow(["성명", name])
                    writer.writerow(["출결 구분 및 사유", detail_reason])
                    writer.writerow(["적용 기간", period])
                    writer.writerow(["증빙 상태", proof_status])
                    writer.writerow(["생성 일시", datetime.now().strftime("%Y-%m-%d %H:%M")])
            else:
                doc = build_absence_form_hwpx(
                    homeroom_label=self._current_homeroom_label(),
                    student_id=sid,
                    student_name=name,
                    category=category,
                    reason=reason,
                    period_text=period,
                    proof_status=proof_status,
                )
                if ext == ".hwp":
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        tmp_hwpx = Path(tmp_dir) / "absence_form.hwpx"
                        doc.save(tmp_hwpx)
                        convert_hwpx_to_hwp(tmp_hwpx, out_path)
                else:
                    doc.save(out_path)
        except Exception as exc:
            QMessageBox.critical(self, "문서 생성 오류", f"출결 서식 생성 중 오류가 발생했습니다:\n{exc}")
            return

        open_ans = QMessageBox.question(
            self,
            "문서 저장 완료",
            f"파일이 저장되었습니다:\n{out_path}\n\n저장된 파일을 바로 열어 확인하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if open_ans == QMessageBox.StandardButton.Yes:
            try:
                os.startfile(str(out_path))
            except Exception:
                pass

    def _on_create_absence_form(self) -> None:
        self._on_export_absence_document()

    def _on_print_attendance_cert(self) -> None:
        self._on_export_absence_document()

    def _get_ordered_duty_students(self) -> List[Dict[str, Any]]:
        students = self.db.get_active_homeroom_students()
        out: List[Dict[str, Any]] = []
        if students:
            for idx, s in enumerate(students, start=1):
                sid = str(s.get("student_id", "")).strip()
                name = str(s.get("name", "")).strip()
                num = _infer_student_number(s.get("number"), sid, idx)
                out.append({"number": num, "student_id": sid, "name": name})
        else:
            for idx, (sid, name) in enumerate(SAMPLE_ROSTER, start=1):
                num = _infer_student_number(None, sid, idx)
                out.append({"number": num, "student_id": sid, "name": name})
        out.sort(key=lambda item: (int(item["number"]), str(item["student_id"])))
        return out

    def _load_duty_state_into_ui(self) -> None:
        state = self.db.get_duty_rotation_state(self._current_homeroom_key())
        students = self._get_ordered_duty_students()
        max_num = max((int(s["number"]) for s in students), default=50)
        valid_ids = {str(s["student_id"]) for s in students}

        self._duty_excluded_ids = [x for x in state["excluded_ids"] if x in valid_ids]
        self._duty_clean_reassign_ids = [
            x for x in state["cleaning_reassign_ids"] if x in valid_ids and x not in self._duty_excluded_ids
        ]
        self._duty_bul_reassign_ids = [
            x for x in state["bulletin_reassign_ids"] if x in valid_ids and x not in self._duty_excluded_ids
        ]

        saved_dates = [d.isoformat() for d in _parse_iso_dates(state.get("selected_dates", []))]
        self._duty_selected_dates = saved_dates if saved_dates else default_weekly_dates()

        self._loading_duty_ui = True
        try:
            self.spin_clean_start.setMaximum(max(99, max_num))
            self.spin_bulletin_start.setMaximum(max(99, max_num))
            self.spin_clean_start.setValue(max(1, int(state["cleaning_next_num"])))
            self.spin_bulletin_start.setValue(max(1, int(state["bulletin_next_num"])))
            saved_period = str(state.get("last_period_text", "")).strip()
            if saved_period:
                self.edit_duty_period.setText(saved_period)
            else:
                self.edit_duty_period.setText(format_duty_dates_korean(self._duty_selected_dates))
        finally:
            self._loading_duty_ui = False

    def _save_current_duty_state(self) -> None:
        if self._loading_duty_ui:
            return
        self.db.save_duty_rotation_state(
            self._current_homeroom_key(),
            cleaning_next_num=self.spin_clean_start.value(),
            bulletin_next_num=self.spin_bulletin_start.value(),
            excluded_ids=self._duty_excluded_ids,
            cleaning_reassign_ids=self._duty_clean_reassign_ids,
            bulletin_reassign_ids=self._duty_bul_reassign_ids,
            last_period_text=self.edit_duty_period.text().strip(),
            selected_dates=self._duty_selected_dates,
        )

    def _on_open_period_picker(self) -> None:
        dlg = DutyPeriodPickerDialog(
            selected_dates=self._duty_selected_dates,
            current_text=self.edit_duty_period.text().strip(),
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._duty_selected_dates = dlg.selected_iso_dates()
            self.edit_duty_period.setText(dlg.period_text())
            self._save_current_duty_state()
            self._update_duty_preview()

    def _on_duty_pointer_changed(self) -> None:
        if self._loading_duty_ui:
            return
        self._save_current_duty_state()
        self._update_duty_preview()

    def _refresh_exception_badge(self) -> None:
        parts: List[str] = []
        if self._duty_excluded_ids:
            parts.append(f"제외 {len(self._duty_excluded_ids)}명")
        if self._duty_clean_reassign_ids:
            parts.append(f"청소추가 {len(self._duty_clean_reassign_ids)}명")
        if self._duty_bul_reassign_ids:
            parts.append(f"게시판추가 {len(self._duty_bul_reassign_ids)}명")
        if parts:
            self.lbl_duty_exception_summary.setText(" · ".join(parts))
            apply_qss_role(self.lbl_duty_exception_summary, role="badgeWarning")
        else:
            self.lbl_duty_exception_summary.setText("예외 없음")
            apply_qss_role(self.lbl_duty_exception_summary, role="badgeNeutral")

    def _update_duty_preview(self) -> None:
        """현재 시작 번호와 결석 제외·재배정 명단을 바탕으로 청소(6명)·게시판(2명) 번호순 명단을 갱신합니다."""
        students = self._get_ordered_duty_students()
        plan = compute_weekly_duty_rotation(
            students=students,
            cleaning_start_num=self.spin_clean_start.value(),
            bulletin_start_num=self.spin_bulletin_start.value(),
            excluded_ids=self._duty_excluded_ids,
            cleaning_reassign_ids=self._duty_clean_reassign_ids,
            bulletin_reassign_ids=self._duty_bul_reassign_ids,
        )
        self._last_duty_plan = plan
        self._refresh_exception_badge()

        mono_font = QFont("JetBrains Mono", 9)
        self.table_cleaning.setRowCount(0)

        display_rows: List[Tuple[str, Dict[str, Any]]] = []
        for stu in plan["cleaning"]:
            display_rows.append(("교실 청소", stu))
        for stu in plan["bulletin"]:
            display_rows.append(("게시판", stu))

        for r_idx, (role_name, stu) in enumerate(display_rows):
            self.table_cleaning.insertRow(r_idx)
            sid = str(stu["student_id"])
            num = int(stu["number"])
            name = str(stu["name"])
            mode = str(stu["mode"])

            it_role = QTableWidgetItem(role_name)
            it_role.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_role.setData(Qt.ItemDataRole.UserRole, sid)

            it_num = QTableWidgetItem(f"{num}번")
            it_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            it_sid = QTableWidgetItem(sid)
            it_sid.setFont(mono_font)
            it_sid.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            it_name = QTableWidgetItem(name)
            it_name.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            it_mode = QTableWidgetItem(mode)
            it_mode.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for c_idx, it in enumerate((it_role, it_num, it_sid, it_name, it_mode)):
                self.table_cleaning.setItem(r_idx, c_idx, it)

        period_str = self.edit_duty_period.text().strip()
        clean_names = ", ".join(f"{s['number']}번 {s['name']}" for s in plan["cleaning"])
        bul_names = ", ".join(f"{s['number']}번 {s['name']}" for s in plan["bulletin"])
        summary_lines = [
            f"<당번 안내> 기간 : {period_str}",
            f"- 교실 청소 ({len(plan['cleaning'])}명): {clean_names}",
            f"- 게시판 ({len(plan['bulletin'])}명): {bul_names}",
        ]
        self.txt_cleaning_result.setPlainText("\n".join(summary_lines))

    def _on_random_assign(self) -> None:
        """하위 호환용 별칭: 번호순 순환 미리보기를 갱신합니다."""
        self._update_duty_preview()

    def _on_duty_row_double_clicked(self, row: int, _col: int) -> None:
        it = self.table_cleaning.item(row, 0)
        sid = str(it.data(Qt.ItemDataRole.UserRole) or "") if it is not None else ""
        self._on_open_duty_exceptions(sid)

    def _on_open_duty_exceptions(self, focus_student_id: str = "") -> None:
        students = self._get_ordered_duty_students()
        dlg = DutyExceptionDialog(
            students=students,
            excluded_ids=self._duty_excluded_ids,
            cleaning_reassign_ids=self._duty_clean_reassign_ids,
            bulletin_reassign_ids=self._duty_bul_reassign_ids,
            focus_student_id=focus_student_id,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            excl, clean_re, bul_re = dlg.result_lists()
            self._duty_excluded_ids = excl
            self._duty_clean_reassign_ids = clean_re
            self._duty_bul_reassign_ids = bul_re
            self._save_current_duty_state()
            self._update_duty_preview()

    def _commit_duty_week_advance(self) -> Tuple[int, int]:
        """다음 시작 번호로 포인터를 이동하고, 선택된 요일·격일 패턴 그대로 날짜를 +7일 이동합니다."""
        if not self._last_duty_plan:
            self._update_duty_preview()
        next_clean = int(self._last_duty_plan.get("cleaning_next_num", self.spin_clean_start.value()))
        next_bul = int(self._last_duty_plan.get("bulletin_next_num", self.spin_bulletin_start.value()))
        used_clean = set(self._last_duty_plan.get("cleaning_consumed_ids", []))
        used_bul = set(self._last_duty_plan.get("bulletin_consumed_ids", []))

        self._duty_clean_reassign_ids = [x for x in self._duty_clean_reassign_ids if x not in used_clean]
        self._duty_bul_reassign_ids = [x for x in self._duty_bul_reassign_ids if x not in used_bul]
        self._duty_selected_dates = shift_duty_dates(self._duty_selected_dates, delta_days=7)
        next_period_str = format_duty_dates_korean(self._duty_selected_dates)

        self._loading_duty_ui = True
        try:
            self.spin_clean_start.setValue(max(1, next_clean))
            self.spin_bulletin_start.setValue(max(1, next_bul))
            if next_period_str:
                self.edit_duty_period.setText(next_period_str)
        finally:
            self._loading_duty_ui = False

        self._save_current_duty_state()
        self._update_duty_preview()
        return next_clean, next_bul

    def _on_advance_duty_week(self) -> None:
        if not self._last_duty_plan:
            self._update_duty_preview()
        next_clean = int(self._last_duty_plan.get("cleaning_next_num", 1))
        next_bul = int(self._last_duty_plan.get("bulletin_next_num", 1))
        next_period = format_duty_dates_korean(shift_duty_dates(self._duty_selected_dates, delta_days=7))
        ans = QMessageBox.question(
            self,
            "다음 주 순번으로 넘기기",
            f"현재 화면의 당번 배정을 마치고 다음 주 순번·기간으로 넘길까요?\n\n"
            f"- 다음 안내 기간: {next_period}\n"
            f"- 다음 청소 시작 번호: {next_clean}번\n"
            f"- 다음 게시판 시작 번호: {next_bul}번\n"
            f"(지정한 요일·격일 패턴 그대로 +7일 이동하며, 이번 주 추가 재배정 체크는 해제됩니다.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._commit_duty_week_advance()

    def import_homeroom_roster_file(self, file_path: str) -> int:
        if not file_path:
            return 0
        try:
            group_info, student_list = parse_homeroom_roster(file_path)
            group_id = self.db.create_roster_group(
                group_type=group_info["group_type"],
                group_name=group_info["group_name"],
                extra_info=group_info.get("extra_info", ""),
                columns_json=group_info.get("columns_json", "[]"),
                is_active=1,
            )
            self.db.add_roster_students(group_id, student_list)
            for s in student_list:
                self.db.upsert_student(
                    student_id=s["student_id"],
                    grade=s["grade"],
                    class_no=s["class_no"],
                    number=s["number"],
                    name=s["name"],
                    contact=s.get("contact", ""),
                    parent_contact=s.get("parent_contact", ""),
                    memo=s.get("memo", ""),
                )
            self._refresh_roster_dependent_views()
            QMessageBox.information(
                self,
                "학급담임 명렬표 연동 완료",
                f"[{group_info['group_name']}] 총 {len(student_list)}명의 학생 명렬표를 반영했습니다.",
            )
            return len(student_list)
        except Exception as exc:
            QMessageBox.critical(self, "명렬표 가져오기 오류", f"명렬표를 읽는 중 오류가 발생했습니다:\n{exc}")
            return 0

    def export_duty_hwpx_with_template(self, template_path: str) -> None:
        if not template_path:
            return
        self._custom_duty_template_path = Path(template_path).resolve()
        self._export_duty_hwpx(template_override=self._custom_duty_template_path)

    def _on_export_duty_hwpx(self) -> None:
        self._export_duty_hwpx(template_override=self._custom_duty_template_path)

    def _export_duty_hwpx(self, template_override: Optional[Path] = None) -> None:
        """cleaning_notice_template.hwpx 양식에 기간·이름을 채워 저장하고 다음 주 순번 이동 여부를 묻습니다."""
        if not self._last_duty_plan:
            self._update_duty_preview()

        tmpl_path = template_override if (template_override and template_override.is_file()) else resolve_cleaning_template_path()
        if tmpl_path is None:
            picked, _ = QFileDialog.getOpenFileName(
                self,
                "당번 안내 한글 양식(.hwpx) 선택",
                "",
                "한글 표준 문서 (*.hwpx)",
            )
            if not picked:
                return
            tmpl_path = Path(picked)

        period_str = self.edit_duty_period.text().strip()
        clean_names = [str(s["name"]) for s in self._last_duty_plan.get("cleaning", [])]
        bul_names = [str(s["name"]) for s in self._last_duty_plan.get("bulletin", [])]

        default_file = f"당번안내_{datetime.now().strftime('%Y%m%d')}.hwpx"
        save_path_str, selected_filter = QFileDialog.getSaveFileName(
            self,
            "당번 안내 한글 파일 저장",
            default_file,
            "한글 표준 문서 (*.hwpx);;한글 문서 (*.hwp)",
        )
        if not save_path_str:
            return

        out_path = Path(save_path_str)
        if out_path.suffix.lower() not in (".hwpx", ".hwp"):
            out_path = out_path.with_suffix(".hwp" if "*.hwp)" in selected_filter else ".hwpx")

        try:
            doc = fill_cleaning_notice_hwpx(
                template_path=tmpl_path,
                period_text=period_str,
                cleaning_names=clean_names,
                bulletin_names=bul_names,
            )
            if out_path.suffix.lower() == ".hwp":
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_hwpx = Path(tmp_dir) / "duty_filled.hwpx"
                    doc.save(tmp_hwpx)
                    convert_hwpx_to_hwp(tmp_hwpx, out_path)
            else:
                doc.save(out_path)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "한글 양식 출력 오류",
                f"당번 안내 파일을 생성하는 중 오류가 발생했습니다:\n{exc}",
            )
            return

        self._save_current_duty_state()
        next_clean = int(self._last_duty_plan.get("cleaning_next_num", self.spin_clean_start.value()))
        next_bul = int(self._last_duty_plan.get("bulletin_next_num", self.spin_bulletin_start.value()))
        next_period = format_duty_dates_korean(shift_duty_dates(self._duty_selected_dates, delta_days=7))

        ans = QMessageBox.question(
            self,
            "당번 안내 파일 저장 완료",
            f"파일이 저장되었습니다:\n{out_path}\n\n"
            f"이번 주 배정을 확정하고 다음 주 순번·기간으로 미리 넘겨 둘까요?\n"
            f"- 다음 안내 기간: {next_period}\n"
            f"- 다음 시작 번호: 청소 {next_clean}번 / 게시판 {next_bul}번\n"
            f"(같은 주 문서를 다시 출력할 예정이면 [아니오]를 누르세요.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._commit_duty_week_advance()

        open_ans = QMessageBox.question(
            self,
            "저장된 파일 열기",
            "방금 생성한 한글 파일을 바로 열어 확인하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if open_ans == QMessageBox.StandardButton.Yes:
            try:
                os.startfile(str(out_path))
            except Exception:
                pass

    def _on_copy_duty_clipboard(self) -> None:
        text = self.txt_cleaning_result.toPlainText().strip()
        if not text:
            self._update_duty_preview()
            text = self.txt_cleaning_result.toPlainText().strip()
        clipboard: Optional[QClipboard] = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        QMessageBox.information(self, "복사 완료", "당번 배정 결과가 클립보드에 복사되었습니다.")

    # -------------------------------------------------------------------------
    # CARD 3: 학급 자리 배치 (SQLite 영구 저장 + 클릭 맞교환 + 앞자리 배려 + 출력)
    # -------------------------------------------------------------------------
    def _active_desk_count(self, student_count: int) -> int:
        return 36 if student_count > 30 else 30

    def _load_seating_into_ui(self) -> None:
        roster = list(self._get_active_roster())
        roster_map = {sid: name for sid, name in roster}
        desk_count = self._active_desk_count(len(roster))

        state = self.db.get_seating_state(self._current_homeroom_key())
        self._front_care_ids = [sid for sid in state["front_care_ids"] if sid in roster_map]
        self._selected_seat_idx = None

        self._loading_seating_ui = True
        try:
            self.chk_front_care.setChecked(bool(state.get("use_front_care", True)))
        finally:
            self._loading_seating_ui = False

        saved_items = state.get("seating") or []
        reconciled: List[Tuple[str, str, str]] = []
        placed_ids: Set[str] = set()

        if isinstance(saved_items, list) and saved_items:
            for idx in range(desk_count):
                desk_code = f"D-{idx + 1:02d}"
                item = saved_items[idx] if idx < len(saved_items) and isinstance(saved_items[idx], dict) else {}
                sid = str(item.get("student_id", "-")).strip() or "-"
                if sid != "-" and sid in roster_map and sid not in placed_ids:
                    reconciled.append((desk_code, sid, roster_map[sid]))
                    placed_ids.add(sid)
                else:
                    reconciled.append((desk_code, "-", "공석"))

            missing_students = [(sid, name) for sid, name in roster if sid not in placed_ids]
            for idx in range(desk_count):
                if not missing_students:
                    break
                desk_code, sid, _ = reconciled[idx]
                if sid == "-":
                    m_sid, m_name = missing_students.pop(0)
                    reconciled[idx] = (desk_code, m_sid, m_name)
        else:
            for idx in range(desk_count):
                desk_code = f"D-{idx + 1:02d}"
                if idx < len(roster):
                    sid, name = roster[idx]
                    reconciled.append((desk_code, sid, name))
                else:
                    reconciled.append((desk_code, "-", "공석"))

        self._current_seating = reconciled
        self._refresh_seating_cells()
        self._save_current_seating_state()

    def _save_current_seating_state(self) -> None:
        if self._loading_seating_ui:
            return
        payload = [
            {"desk": desk_code, "student_id": sid, "name": name}
            for desk_code, sid, name in self._current_seating
        ]
        self.db.save_seating_state(
            self._current_homeroom_key(),
            seating=payload,
            front_care_ids=self._front_care_ids,
            use_front_care=self.chk_front_care.isChecked(),
        )

    def _refresh_seating_cells(self) -> None:
        care_set = set(self._front_care_ids)
        desk_count = len(self._current_seating) or 30
        if self._front_care_ids:
            self.btn_front_care_cfg.setText(f"배려 지정 ({len(self._front_care_ids)}명)")
        else:
            self.btn_front_care_cfg.setText("배려 지정")

        if self._selected_seat_idx is not None and 0 <= self._selected_seat_idx < len(self._current_seating):
            sel_code, sel_sid, sel_name = self._current_seating[self._selected_seat_idx]
            disp = "공석" if sel_sid == "-" else f"{sel_sid} {sel_name}"
            self.lbl_seat_hint.setText(f"선택됨 [{sel_code} {disp}] — 바꿀 다른 좌석을 클릭하세요")
            self.lbl_seat_hint.setStyleSheet("color: #1D4ED8; font-size: 11px; font-weight: 700;")
        else:
            self.lbl_seat_hint.setText("좌석 두 칸을 차례로 클릭하면 자리를 맞바꿉니다")
            self.lbl_seat_hint.setStyleSheet("")
            apply_qss_role(self.lbl_seat_hint, role="mutedText")

        for idx, cell in enumerate(self._seat_cells):
            is_visible = idx < desk_count
            cell.setVisible(is_visible)
            if not is_visible:
                continue
            desk_code, sid, name = self._current_seating[idx]
            cell.update_visual(
                desk_code,
                sid,
                name,
                is_care=(sid in care_set),
                is_selected=(self._selected_seat_idx == idx),
            )

    def _on_seat_cell_clicked(self, desk_idx: int) -> None:
        if desk_idx < 0 or desk_idx >= len(self._current_seating):
            return
        if self._selected_seat_idx is None:
            self._selected_seat_idx = desk_idx
            self._refresh_seating_cells()
            return
        if self._selected_seat_idx == desk_idx:
            self._selected_seat_idx = None
            self._refresh_seating_cells()
            return

        first_idx = self._selected_seat_idx
        desk_a, sid_a, name_a = self._current_seating[first_idx]
        desk_b, sid_b, name_b = self._current_seating[desk_idx]
        self._current_seating[first_idx] = (desk_a, sid_b, name_b)
        self._current_seating[desk_idx] = (desk_b, sid_a, name_a)
        self._selected_seat_idx = None
        self._refresh_seating_cells()
        self._save_current_seating_state()

    def _on_front_care_toggled(self, _checked: bool) -> None:
        self._save_current_seating_state()

    def _on_open_front_care_dialog(self) -> None:
        students = self._get_ordered_duty_students()
        dlg = FrontCareStudentsDialog(
            students=students,
            selected_ids=self._front_care_ids,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._front_care_ids = dlg.selected_ids()
            self._save_current_seating_state()
            self._refresh_seating_cells()

    def _on_randomize_seats(self, initial: bool = False) -> None:
        if initial:
            self._load_seating_into_ui()
            return

        roster = list(self._get_active_roster())
        desk_count = self._active_desk_count(len(roster))
        student_slots = min(len(roster), desk_count)
        self._selected_seat_idx = None

        ordered_students: List[Tuple[str, str]] = []
        care_set = set(self._front_care_ids)
        if self.chk_front_care.isChecked() and care_set and student_slots > 0:
            seen_sids: Set[str] = set()
            uniq_care: List[Tuple[str, str]] = []
            for stu in roster[:desk_count]:
                if stu[0] in care_set and stu[0] not in seen_sids:
                    uniq_care.append(stu)
                    seen_sids.add(stu[0])
            others = [stu for stu in roster[:desk_count] if stu[0] not in seen_sids]
            random.shuffle(uniq_care)
            random.shuffle(others)

            front_limit = min(12, student_slots)
            assigned: List[Optional[Tuple[str, str]]] = [None] * student_slots
            if len(uniq_care) <= front_limit:
                front_positions = random.sample(range(front_limit), len(uniq_care))
                for pos, stu in zip(front_positions, uniq_care):
                    assigned[pos] = stu
                rem_iter = iter(others)
                for idx in range(student_slots):
                    if assigned[idx] is None:
                        assigned[idx] = next(rem_iter, ("-", "공석"))
            else:
                for idx in range(front_limit):
                    assigned[idx] = uniq_care[idx]
                rest_pool = uniq_care[front_limit:] + others
                random.shuffle(rest_pool)
                for offset, idx in enumerate(range(front_limit, student_slots)):
                    assigned[idx] = rest_pool[offset] if offset < len(rest_pool) else ("-", "공석")

            ordered_students = [item if item is not None else ("-", "공석") for item in assigned]
        else:
            shuffled = list(roster[:desk_count])
            random.shuffle(shuffled)
            ordered_students = shuffled

        self._current_seating = []
        for idx in range(desk_count):
            desk_code = f"D-{idx + 1:02d}"
            if idx < len(ordered_students):
                sid, name = ordered_students[idx]
                self._current_seating.append((desk_code, sid, name))
            else:
                self._current_seating.append((desk_code, "-", "공석"))

        self._refresh_seating_cells()
        self._save_current_seating_state()

    def _on_export_seating_chart(self) -> None:
        if not self._current_seating:
            self._load_seating_into_ui()

        total_rows = max(1, (len(self._current_seating) + 5) // 6)
        default_name = f"학급좌석표_6x{total_rows}_{datetime.now().strftime('%Y%m%d')}.hwpx"
        file_path_str, selected_filter = QFileDialog.getSaveFileName(
            self,
            "학급 좌석 배치표 내보내기",
            default_name,
            "한글 표준 문서 (*.hwpx);;엑셀 파일 (*.xlsx);;CSV 파일 (*.csv);;한글 문서 (*.hwp)",
        )
        if not file_path_str:
            return

        out_path = Path(file_path_str)
        if out_path.suffix.lower() not in (".hwpx", ".xlsx", ".csv", ".hwp"):
            if "*.xlsx)" in selected_filter:
                out_path = out_path.with_suffix(".xlsx")
            elif "*.csv)" in selected_filter:
                out_path = out_path.with_suffix(".csv")
            elif "*.hwp)" in selected_filter:
                out_path = out_path.with_suffix(".hwp")
            else:
                out_path = out_path.with_suffix(".hwpx")

        try:
            ext = out_path.suffix.lower()
            hr_label = self._current_homeroom_label()
            if ext == ".csv":
                with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([f"{hr_label} 학급 좌석 배치표"])
                    writer.writerow(["[ 칠판 / 교탁 (전면) ]"])
                    writer.writerow(["1분단", "2분단", "3분단", "4분단", "5분단", "6분단"])
                    for r in range(total_rows):
                        row_cells: List[str] = []
                        for c in range(6):
                            idx = r * 6 + c
                            if idx < len(self._current_seating):
                                desk_code, sid, name = self._current_seating[idx]
                                row_cells.append(f"{desk_code}: {sid} {name}" if sid != "-" else f"{desk_code}: 공석")
                            else:
                                row_cells.append("")
                        writer.writerow(row_cells)
            elif ext == ".xlsx":
                save_seating_chart_xlsx(
                    out_path,
                    homeroom_label=hr_label,
                    seating=self._current_seating,
                    cols=6,
                )
            else:
                doc = build_seating_chart_hwpx(
                    homeroom_label=hr_label,
                    seating=self._current_seating,
                    cols=6,
                )
                if ext == ".hwp":
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        tmp_hwpx = Path(tmp_dir) / "seating_chart.hwpx"
                        doc.save(tmp_hwpx)
                        convert_hwpx_to_hwp(tmp_hwpx, out_path)
                else:
                    doc.save(out_path)
        except Exception as exc:
            QMessageBox.critical(self, "내보내기 오류", f"좌석표 저장 중 오류가 발생했습니다:\n{exc}")
            return

        open_ans = QMessageBox.question(
            self,
            "좌석표 저장 완료",
            f"좌석 배치표가 저장되었습니다:\n{out_path}\n\n저장된 파일을 바로 열어 확인하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if open_ans == QMessageBox.StandardButton.Yes:
            try:
                os.startfile(str(out_path))
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # CARD 4: 학생 관찰 및 상담 메모장 (SQLite 영구 저장 + 필터 + 수정/삭제 + 내보내기)
    # -------------------------------------------------------------------------
    def _format_obs_timestamp(self, raw_dt: str) -> str:
        s = (raw_dt or "").strip()
        if len(s) >= 16 and s[4] == "-" and s[7] == "-":
            return f"{s[5:7]}.{s[8:10]} {s[11:16]}"
        return s[:16] if s else datetime.now().strftime("%m.%d %H:%M")

    def _filtered_observation_logs(self) -> List[Dict[str, Any]]:
        all_logs = self.db.get_observation_logs(self._current_homeroom_key())
        filter_sid = ""
        if self.chk_counsel_filter_student.isChecked():
            stu_data = self.combo_counsel_student.currentData()
            if isinstance(stu_data, tuple) and len(stu_data) == 2:
                filter_sid = str(stu_data[0])

        keyword = self.edit_counsel_search.text().strip().lower()
        out: List[Dict[str, Any]] = []
        for item in all_logs:
            if filter_sid and str(item.get("student_id", "")) != filter_sid:
                continue
            if keyword:
                haystack = f"{item.get('student_id', '')} {item.get('student_name', '')} {item.get('content', '')}".lower()
                if keyword not in haystack:
                    continue
            out.append(item)
        return out

    def _refresh_observation_view(self, select_log_id: Optional[int] = None) -> None:
        logs = self._filtered_observation_logs()
        self.list_counsel_history.clear()
        target_row = -1
        for idx, row_data in enumerate(logs):
            log_id = int(row_data["id"])
            dt_short = self._format_obs_timestamp(str(row_data.get("created_at", "")))
            sid = str(row_data.get("student_id", ""))
            name = str(row_data.get("student_name", ""))
            content = str(row_data.get("content", ""))
            stu_tag = f"{sid} {name}".strip() or "미지정"
            entry_text = f"[{dt_short}] [{stu_tag}] {content}"
            lw_item = QListWidgetItem(entry_text)
            lw_item.setData(Qt.ItemDataRole.UserRole, row_data)
            self.list_counsel_history.addItem(lw_item)
            if select_log_id is not None and log_id == select_log_id:
                target_row = idx

        if target_row >= 0:
            self.list_counsel_history.setCurrentRow(target_row)

    def _on_counsel_filter_changed(self) -> None:
        self._refresh_observation_view()

    def _append_keyword_to_note(self, keyword_chip: str) -> None:
        clean_kw = keyword_chip.replace("+", "").strip()
        cur = self.edit_observation.text().strip()
        suffix = f" ({clean_kw} 우수)"
        self.edit_observation.setText(f"{cur}{suffix}".strip())
        self.edit_observation.setFocus()

    def _on_add_observation_log(self) -> None:
        note = self.edit_observation.text().strip()
        if not note:
            QMessageBox.warning(self, "입력 확인", "기록할 학생 관찰 또는 상담 내용을 입력해 주세요.")
            return

        stu_data = self.combo_counsel_student.currentData()
        if isinstance(stu_data, tuple) and len(stu_data) == 2:
            sid, name = str(stu_data[0]), str(stu_data[1])
        else:
            raw_txt = self.combo_counsel_student.currentText().strip()
            parts = raw_txt.split(" ", 1)
            sid = parts[0] if parts else ""
            name = parts[1] if len(parts) > 1 else (raw_txt or "미지정 학생")

        new_id = self.db.add_observation_log(
            self._current_homeroom_key(),
            student_id=sid,
            student_name=name,
            content=note,
        )
        self.edit_observation.clear()
        self._refresh_observation_view(select_log_id=new_id)

    def _on_edit_observation_item(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        log_id = int(data["id"])
        old_content = str(data.get("content", ""))
        stu_label = f"{data.get('student_id', '')} {data.get('student_name', '')}".strip()
        new_text, ok = QInputDialog.getMultiLineText(
            self,
            "관찰 · 상담 기록 수정",
            f"[{stu_label}] 학생의 기록 내용을 수정하세요:",
            old_content,
        )
        if not ok:
            return
        cleaned = new_text.strip()
        if not cleaned:
            QMessageBox.warning(self, "내용 확인", "빈 내용으로는 저장할 수 없습니다.")
            return
        self.db.update_observation_log(log_id, cleaned)
        self._refresh_observation_view(select_log_id=log_id)

    def _on_delete_observation_log(self) -> None:
        item = self.list_counsel_history.currentItem()
        if item is None:
            QMessageBox.information(self, "항목 선택", "삭제할 관찰·상담 기록을 목록에서 선택해 주세요.")
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        stu_label = f"{data.get('student_id', '')} {data.get('student_name', '')}".strip()
        ans = QMessageBox.question(
            self,
            "관찰 · 상담 기록 삭제",
            f"[{stu_label}] 기록을 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.db.delete_observation_log(int(data["id"]))
            self._refresh_observation_view()

    def _on_export_observation_logs(self) -> None:
        logs = self._filtered_observation_logs()
        if not logs:
            QMessageBox.information(self, "내보내기 안내", "저장할 관찰·상담 기록이 없습니다.")
            return

        default_name = f"학생관찰상담일지_{datetime.now().strftime('%Y%m%d')}.xlsx"
        file_path_str, selected_filter = QFileDialog.getSaveFileName(
            self,
            "학생 관찰 · 상담 일지 내보내기",
            default_name,
            "엑셀 파일 (*.xlsx);;한글 표준 문서 (*.hwpx);;CSV 파일 (*.csv)",
        )
        if not file_path_str:
            return

        out_path = Path(file_path_str)
        if out_path.suffix.lower() not in (".xlsx", ".hwpx", ".csv"):
            if "*.hwpx)" in selected_filter:
                out_path = out_path.with_suffix(".hwpx")
            elif "*.csv)" in selected_filter:
                out_path = out_path.with_suffix(".csv")
            else:
                out_path = out_path.with_suffix(".xlsx")

        try:
            ext = out_path.suffix.lower()
            hr_label = self._current_homeroom_label()
            if ext == ".csv":
                with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["기록 일시", "학급", "학번", "성명", "관찰 및 상담 내용"])
                    for item in logs:
                        writer.writerow(
                            [
                                str(item.get("created_at", ""))[:16],
                                hr_label,
                                str(item.get("student_id", "")),
                                str(item.get("student_name", "")),
                                str(item.get("content", "")),
                            ]
                        )
            elif ext == ".hwpx":
                doc = build_observation_logs_hwpx(homeroom_label=hr_label, logs=logs)
                doc.save(out_path)
            else:
                save_observation_logs_xlsx(out_path, homeroom_label=hr_label, logs=logs)
        except Exception as exc:
            QMessageBox.critical(self, "내보내기 오류", f"관찰·상담 일지 저장 중 오류가 발생했습니다:\n{exc}")
            return

        open_ans = QMessageBox.question(
            self,
            "일지 저장 완료",
            f"관찰·상담 일지가 저장되었습니다 ({len(logs)}건):\n{out_path}\n\n저장된 파일을 바로 열어 확인하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if open_ans == QMessageBox.StandardButton.Yes:
            try:
                os.startfile(str(out_path))
            except Exception:
                pass

