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
import socket
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QDate, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QClipboard,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
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

from core.base_worker import FunctionWorker, TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.batch_prompt_service import extract_json_from_llm_response
from services.class_notice_digest_service import (
    build_kakao_notice_digest_prompt,
    build_schoolbell_summary_prompt,
    digest_kakao_text_with_ai,
    parse_kakao_chat_text,
    parse_notice_digest_ai_response,
    read_kakao_export_file,
    summarize_schoolbell_notice_with_ai,
)
from services.firestore_sync_service import FirestoreSyncService, hash_password_sha256
from services.schoolbell_crawler_service import SchoolbellCrawlWorker
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
from services.llm_client import MultiLlmClient
from services.notice_parser_service import (
    NOTICE_CATEGORIES,
    NOTICE_OCR_JSON_PROMPT,
    VALID_REPLY_FIELD_TYPES,
    extract_notice_from_pdf,
    extract_notice_from_text,
    extract_notice_source_text,
    inspect_notice_file_locally,
    normalize_reply_schema,
    parse_notice_ai_json,
)
from services.roster_parser_service import parse_homeroom_roster
from ui.components.file_drop_helper import enable_file_drop
from ui.components.hover_zoom_card import HoverZoomCard
from ui.dialogs.student_face_photo_dialog import StudentFacePhotoBatchDialog
from ui.utils import install_no_scroll_wheel_filter
from utils.path_helper import get_data_path, get_resource_path


def _build_refresh_icon(size: int = 18, color: str = "#334155") -> QIcon:
    """QPainter로 선명한 원형 새로고침 화살표 아이콘을 생성합니다."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    margin = 3.2
    rect = QRectF(margin, margin, size - margin * 2, size - margin * 2)
    # 45도 위치에서 시작해 285도 원호를 그림
    p.drawArc(rect, 45 * 16, 285 * 16)
    # 우상단 끝점에 작은 삼각형 화살촉 그리기
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    cx = size - margin - 1.2
    cy = margin + (size - margin * 2) * 0.28
    arrow = QPolygonF([
        QPointF(cx + 2.0, cy - 3.0),
        QPointF(cx + 2.4, cy + 2.6),
        QPointF(cx - 3.2, cy + 1.4),
    ])
    p.drawPolygon(arrow)
    p.end()
    return QIcon(pix)


def _build_spinner_icon(angle_deg: int, size: int = 18, color: str = "#2563EB") -> QIcon:
    """QTimer 회전에 맞춰 각도가 변하는 로딩 링 QIcon을 생성합니다."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    margin = 3.0
    rect = QRectF(margin, margin, size - margin * 2, size - margin * 2)
    track_pen = QPen(QColor("#CBD5E1"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(track_pen)
    p.drawEllipse(rect)
    arc_pen = QPen(QColor(color), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(arc_pen)
    p.drawArc(rect, -int(angle_deg) * 16, 110 * 16)
    p.end()
    return QIcon(pix)


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

    PC_AGENT_HEARTBEAT_INTERVAL_MS = 90_000
    REMOTE_TASK_POLL_INTERVAL_MS = 60_000
    PC_AGENT_APP_VERSION = "2.0"

    ai_target_requested = pyqtSignal(str)
    ai_context_changed = pyqtSignal()
    schoolbell_login_required = pyqtSignal(object, str)
    schoolbell_login_cleared = pyqtSignal(str)
    teacher_auth_changed = pyqtSignal()

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="ClassroomToolsTabPage")
        self.task_runner = task_runner
        self.db = DatabaseManager()
        self.config = ConfigManager()
        self._logged_in_teacher: Optional[Dict[str, Any]] = None
        self._firebase_roster: List[Tuple[str, str]] = []
        self._pc_agent_sync_service: Optional[FirestoreSyncService] = None
        self._heartbeat_running: bool = False
        self._remote_task_poll_running: bool = False
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(self.PC_AGENT_HEARTBEAT_INTERVAL_MS)
        self._heartbeat_timer.timeout.connect(self._on_pc_agent_heartbeat_tick)
        self._remote_task_timer = QTimer(self)
        self._remote_task_timer.setInterval(self.REMOTE_TASK_POLL_INTERVAL_MS)
        self._remote_task_timer.timeout.connect(self._on_remote_task_poll_tick)
        self._selected_notice_pdf_path: str = ""
        self._selected_notice_attachment_file_id: str = ""
        self._notice_pdf_field_map: List[Dict[str, Any]] = []
        self._last_notice_replies: List[Dict[str, Any]] = []
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
        self._roster_sync_running: bool = False
        self._notice_review_cards: List[Dict[str, Any]] = []
        self._notice_review_seq: int = 0
        self._active_notice_digest_ctx: Dict[str, Any] = {}
        self._active_schoolbell_worker: Optional[SchoolbellCrawlWorker] = None

        self._init_ui()
        self.restore_saved_teacher_session()
        self._refresh_roster_dependent_views()

    def _local_homeroom_matches_logged_in_teacher(
        self,
        hr_group: Optional[Dict[str, Any]],
        students: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """로그인한 교사의 학년·반과 로컬 SQLite 담임 명렬표의 학년·반이 일치하는지 확인합니다."""
        if not self._logged_in_teacher:
            return True
        def _to_int(val: Any) -> Optional[int]:
            if val is None or isinstance(val, bool):
                return None
            try:
                s = str(val).strip()
                return int(float(s)) if s else None
            except (ValueError, TypeError):
                return None

        meta = self._teacher_meta()
        t_grade = _to_int(meta.get("grade"))
        t_class = _to_int(meta.get("class_no"))
        if t_grade is None or t_class is None:
            return True

        saw_class_info = False
        for stu in (students or []):
            if not isinstance(stu, dict):
                continue
            sg = _to_int(stu.get("grade"))
            sc = _to_int(stu.get("class_no"))
            if sg is not None or sc is not None:
                saw_class_info = True
                if sg != t_grade or sc != t_class:
                    return False

        if isinstance(hr_group, dict):
            label = f"{hr_group.get('group_name', '')} {hr_group.get('extra_info', '')}".strip()
            m = re.search(r"(\d+)\s*학년\s*(\d+)\s*반", label) or re.search(r"(\d+)\s*-\s*(\d+)", label)
            if m:
                saw_class_info = True
                if int(m.group(1)) != t_grade or int(m.group(2)) != t_class:
                    return False

        return saw_class_info

    def _get_active_roster(self) -> List[Tuple[str, str]]:
        if self._firebase_roster:
            return list(self._firebase_roster)
        hr_group = self.db.get_active_homeroom_group()
        students = self.db.get_active_homeroom_students()
        if students and self._local_homeroom_matches_logged_in_teacher(hr_group, students):
            return [(str(s["student_id"]), str(s["name"])) for s in students]
        return []

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
        hb_lay.setContentsMargins(12, 8, 12, 8)
        hb_lay.setSpacing(8)

        lbl_station_title = QLabel("학급 경영 미니 툴 스테이션 (Classroom Operations Station)")
        apply_qss_role(lbl_station_title, role="cardTitle")

        self.lbl_roster_status_badge = QLabel("연동 명렬표: [담임 학급] 등록된 담임 학급 없음")
        apply_qss_role(self.lbl_roster_status_badge, role="badgeWarning")
        self.lbl_roster_status_badge.setToolTip("환경설정 탭에서 나이스 학급담임 명렬표를 먼저 등록해주세요")

        self.btn_sync_roster = QPushButton("명렬표 연동 새로고침")
        apply_qss_role(self.btn_sync_roster, role="secondary", obj_name="BtnSyncClassroomRoster")
        self.btn_sync_roster.setMinimumHeight(30)
        self.btn_sync_roster.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sync_roster.clicked.connect(self._on_sync_roster_clicked)

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
        self.btn_teacher_refresh = QPushButton()
        apply_qss_role(self.btn_teacher_refresh, role="secondary", obj_name="BtnTeacherRefresh")
        self.btn_teacher_refresh.setFixedSize(34, 34)
        self.btn_teacher_refresh.setIcon(_build_refresh_icon())
        self.btn_teacher_refresh.setIconSize(QSize(18, 18))
        self.btn_teacher_refresh.setToolTip("교사 계정 및 명렬표 새로고침")
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

        # 상단 모바일 출결 보고 새로고침 + 미확인(대기중) 건수 바
        att_mobile_bar = QHBoxLayout()
        att_mobile_bar.setSpacing(8)
        self.lbl_mobile_att_pending = QLabel("미확인(대기중) 출결 보고: 0건")
        apply_qss_role(self.lbl_mobile_att_pending, role="badgeNeutral", obj_name="LblMobileAttPending")

        self.btn_refresh_mobile_att = QPushButton("모바일 출결 보고 새로고침")
        apply_qss_role(self.btn_refresh_mobile_att, role="primary", obj_name="BtnRefreshMobileAttendance")
        self.btn_refresh_mobile_att.setMinimumHeight(32)
        self.btn_refresh_mobile_att.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_mobile_att.setToolTip(
            "학생·학부모가 모바일에서 보고한 출결 내역(미확인·전체 날짜 포함)을 불러와 대장에 병합합니다."
        )
        self.btn_refresh_mobile_att.clicked.connect(self._on_sync_mobile_attendance)

        self.chk_include_simple_present = QCheckBox("단순 출석 포함")
        self.chk_include_simple_present.setChecked(False)
        self.chk_include_simple_present.setToolTip(
            "체크 해제 시 교사가 모바일에서 체크한 단순 '출석' 건은 숨기고 결석·지각·조퇴·체험학습 등 변동 건만 표시합니다."
        )
        self.chk_include_simple_present.stateChanged.connect(lambda _s: self._refresh_attendance_view())

        self.btn_batch_face_photos = QPushButton("학생 얼굴 사진 일괄 등록")
        apply_qss_role(self.btn_batch_face_photos, role="secondary", obj_name="BtnClassroomBatchFacePhotos")
        self.btn_batch_face_photos.setMinimumHeight(32)
        self.btn_batch_face_photos.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_batch_face_photos.setToolTip(
            "사진 폴더 또는 다중 이미지 파일명(학번/이름)으로 현재 담임학급 명렬표와 자동 매칭하여 Google Drive 및 학생_안면데이터에 업로드합니다."
        )
        self.btn_batch_face_photos.clicked.connect(self._on_open_batch_face_photos)

        att_mobile_bar.addWidget(self.lbl_mobile_att_pending)
        att_mobile_bar.addWidget(self.chk_include_simple_present)
        att_mobile_bar.addStretch(1)
        att_mobile_bar.addWidget(self.btn_batch_face_photos)
        att_mobile_bar.addWidget(self.btn_refresh_mobile_att)
        c1_lay.addLayout(att_mobile_bar)

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

        # 1) 문서(PDF/HWP/HWPX/이미지) 선택 및 드래그앤드롭 영역
        self.notice_pdf_dropzone = QFrame()
        self.notice_pdf_dropzone.setObjectName("noticePdfDropzone")
        self.notice_pdf_dropzone.setStyleSheet(
            "#noticePdfDropzone { border: 1.5px dashed #94A3B8; border-radius: 8px; background: #F8FAFC; padding: 6px; }"
        )
        drop_lay = QHBoxLayout(self.notice_pdf_dropzone)
        drop_lay.setContentsMargins(10, 8, 10, 8)
        drop_lay.setSpacing(8)
        self.lbl_notice_pdf_status = QLabel(
            "가정통신문 PDF / HWP / HWPX / 이미지를 여기에 드래그앤드롭하거나 [파일 선택]을 누르세요. (필요 시 [AI OCR 추출]로 구조화)"
        )
        self.lbl_notice_pdf_status.setStyleSheet("color: #475569; font-size: 12px;")
        self.lbl_notice_pdf_status.setWordWrap(True)
        self.btn_load_notice_pdf = QPushButton("파일 선택")
        apply_qss_role(self.btn_load_notice_pdf, role="secondary")
        self.btn_load_notice_pdf.setToolTip(
            "PDF, HWP, HWPX 또는 이미지 파일을 선택합니다. HWP/HWPX 및 텍스트 PDF는 로컬에서 1차 파싱되며, 스캔 PDF·이미지는 AI OCR을 실행합니다."
        )
        self.btn_load_notice_pdf.clicked.connect(self._on_load_notice_pdf)
        self.btn_reextract_notice_pdf = QPushButton("AI OCR 추출")
        apply_qss_role(self.btn_reextract_notice_pdf, role="secondary")
        self.btn_reextract_notice_pdf.setToolTip(
            "현재 파일 또는 본문 텍스트를 AI로 분석해 제목·마감일·요약 및 회신 입력칸(reply_schema)을 추출합니다."
        )
        self.btn_reextract_notice_pdf.clicked.connect(self._on_reextract_notice_pdf)
        drop_lay.addWidget(self.lbl_notice_pdf_status, stretch=1)
        drop_lay.addWidget(self.btn_load_notice_pdf)
        drop_lay.addWidget(self.btn_reextract_notice_pdf)
        c3_lay.addWidget(self.notice_pdf_dropzone)
        enable_file_drop(
            self.notice_pdf_dropzone,
            allowed_exts=[".pdf", ".hwp", ".hwpx", ".png", ".jpg", ".jpeg", ".webp"],
            allow_dirs=False,
            multiple=False,
            on_files_dropped=self._on_notice_pdf_dropped,
            context_label="가정통신문 문서(PDF/HWP/HWPX/이미지)",
        )

        # 2) 제목 · 분류 · 마감일 · 회신필요 여부
        self._notice_top_widget = QWidget()
        notice_top = QHBoxLayout(self._notice_top_widget)
        notice_top.setContentsMargins(0, 0, 0, 0)
        notice_top.setSpacing(8)
        self.edit_notice_title = QLineEdit()
        self.edit_notice_title.setPlaceholderText("가정통신문 제목 (문서 불러오기 시 자동 채움)")
        self.combo_notice_category = QComboBox()
        self.combo_notice_category.setEditable(True)
        self.combo_notice_category.addItems(list(NOTICE_CATEGORIES))
        self.combo_notice_category.setFixedWidth(120)
        install_no_scroll_wheel_filter(self.combo_notice_category)
        self.edit_notice_due = QLineEdit()
        self.edit_notice_due.setPlaceholderText("마감일 (YYYY-MM-DD)")
        self.edit_notice_due.setFixedWidth(135)
        self.chk_notice_requires_reply = QCheckBox("회신·서명 필요")
        self.chk_notice_requires_reply.setChecked(True)
        self.combo_notice_signature_mode = QComboBox()
        self.combo_notice_signature_mode.addItem("서명 없음 (열람용)", "none")
        self.combo_notice_signature_mode.addItem("서명 1칸 (학생만)", "student")
        self.combo_notice_signature_mode.addItem("서명 1칸 (학부모만)", "parent")
        self.combo_notice_signature_mode.addItem("서명 2칸 (학생·학부모)", "both")
        self.combo_notice_signature_mode.setCurrentIndex(2)
        self.combo_notice_signature_mode.setFixedWidth(180)
        self.combo_notice_signature_mode.setToolTip(
            "서명 방식을 선택합니다. 회신·서명이 필요 없으면 '서명 없음 (열람용)'이 선택됩니다."
        )
        install_no_scroll_wheel_filter(self.combo_notice_signature_mode)
        self._last_active_signature_mode = "parent"
        self.chk_notice_requires_reply.toggled.connect(self._on_notice_requires_reply_toggled)
        self.combo_notice_signature_mode.currentIndexChanged.connect(self._on_notice_signature_mode_changed)
        notice_top.addWidget(self.edit_notice_title, stretch=1)
        notice_top.addWidget(self.combo_notice_category)
        notice_top.addWidget(self.edit_notice_due)
        notice_top.addWidget(self.chk_notice_requires_reply)
        notice_top.addWidget(self.combo_notice_signature_mode)
        c3_lay.addWidget(self._notice_top_widget)

        # 3) 대상 학년/반(정수) · 첨부 URL · 후보 저장 / 바로 게시 버튼
        self._notice_meta_widget = QWidget()
        notice_meta_row = QHBoxLayout(self._notice_meta_widget)
        notice_meta_row.setContentsMargins(0, 0, 0, 0)
        notice_meta_row.setSpacing(8)
        self.spin_notice_target_grade = QSpinBox()
        self.spin_notice_target_grade.setRange(1, 6)
        self.spin_notice_target_grade.setSuffix("학년")
        self.spin_notice_target_grade.setMinimumWidth(84)
        install_no_scroll_wheel_filter(self.spin_notice_target_grade)
        self.spin_notice_target_class = QSpinBox()
        self.spin_notice_target_class.setRange(0, 30)
        self.spin_notice_target_class.setSuffix("반")
        self.spin_notice_target_class.setSpecialValueText("전체 반")
        self.spin_notice_target_class.setMinimumWidth(96)
        install_no_scroll_wheel_filter(self.spin_notice_target_class)
        init_meta = self._teacher_meta()
        if init_meta.get("grade"):
            self.spin_notice_target_grade.setValue(int(init_meta["grade"]))
        if init_meta.get("class_no") is not None:
            self.spin_notice_target_class.setValue(int(init_meta["class_no"]))

        self.edit_notice_attachment_url = QLineEdit()
        self.edit_notice_attachment_url.setMinimumWidth(180)
        self.edit_notice_attachment_url.setPlaceholderText(
            "원본 attachment_url (스토리지 URL이 없으면 빈칸 유지)"
        )
        self.btn_save_notice_candidate = QPushButton("모바일 후보로 저장")
        apply_qss_role(self.btn_save_notice_candidate, role="secondary")
        self.btn_save_notice_candidate.setToolTip(
            "is_published=false로 저장하여 모바일 교사 화면에서 확인 후 게시할 수 있게 합니다."
        )
        self.btn_save_notice_candidate.clicked.connect(lambda: self._save_mobile_notice(is_published=False))
        self.btn_publish_mobile_notice = QPushButton("바로 게시하기")
        apply_qss_role(self.btn_publish_mobile_notice, role="primary")
        self.btn_publish_mobile_notice.setToolTip(
            "is_published=true로 저장하고 Notifications 컬렉션에 대상 학급 알림을 함께 등록합니다."
        )
        self.btn_publish_mobile_notice.clicked.connect(self._on_publish_mobile_notice)

        lbl_target = QLabel("대상:")
        lbl_target.setFixedWidth(36)
        notice_meta_row.addWidget(lbl_target)
        notice_meta_row.addWidget(self.spin_notice_target_grade)
        notice_meta_row.addWidget(self.spin_notice_target_class)
        notice_meta_row.addWidget(self.edit_notice_attachment_url, stretch=1)
        notice_meta_row.addWidget(self.btn_save_notice_candidate)
        notice_meta_row.addWidget(self.btn_publish_mobile_notice)
        c3_lay.addWidget(self._notice_meta_widget)

        # 4) 본문 요약 · 편집 헤더 및 본문 입력창
        self._notice_content_bar_widget = QWidget()
        content_bar = QHBoxLayout(self._notice_content_bar_widget)
        content_bar.setContentsMargins(0, 0, 0, 0)
        content_bar.setSpacing(6)
        lbl_notice_content = QLabel("본문 미리보기 · 편집")
        lbl_notice_content.setStyleSheet("font-weight: 600; color: #334155;")
        self.lbl_notice_content_state = QLabel("")
        self.lbl_notice_content_state.setStyleSheet("color: #64748B; font-size: 12px;")
        self.btn_notice_content_fullscreen = QPushButton("전체화면 보기")
        apply_qss_role(self.btn_notice_content_fullscreen, role="secondary")
        self.btn_notice_content_fullscreen.setToolTip(
            "본문 미리보기·편집 영역을 넓은 전체화면 창에서 조회하고 편집합니다."
        )
        self.btn_notice_content_fullscreen.clicked.connect(self._on_open_notice_content_fullscreen)
        content_bar.addWidget(lbl_notice_content)
        content_bar.addWidget(self.lbl_notice_content_state)
        content_bar.addStretch(1)
        content_bar.addWidget(self.btn_notice_content_fullscreen)
        c3_lay.addWidget(self._notice_content_bar_widget)

        self.txt_notice_content = QPlainTextEdit()
        self.txt_notice_content.setPlaceholderText("학생·학부모용 핵심 요약 본문 내용이 여기에 표시됩니다.")
        self.txt_notice_content.setMinimumHeight(95)
        c3_lay.addWidget(self.txt_notice_content)

        # 5) 회신 입력칸 (reply_schema) 편집 UI
        schema_bar = QHBoxLayout()
        schema_bar.setSpacing(6)
        lbl_schema = QLabel("회신 입력칸 (reply_schema)")
        lbl_schema.setStyleSheet("font-weight: 600; color: #334155;")
        self.btn_toggle_reply_schema_fullscreen = QPushButton("전체화면 보기")
        self.btn_toggle_reply_schema_fullscreen.setCheckable(True)
        apply_qss_role(self.btn_toggle_reply_schema_fullscreen, role="secondary")
        self.btn_toggle_reply_schema_fullscreen.setToolTip(
            "다른 입력 영역을 잠시 접고 회신 입력칸 테이블을 넓게 펼쳐 편집합니다."
        )
        self.btn_toggle_reply_schema_fullscreen.toggled.connect(self._on_toggle_reply_schema_fullscreen)
        self.btn_add_reply_field = QPushButton("+ 입력칸 추가")
        apply_qss_role(self.btn_add_reply_field, role="secondary")
        self.btn_add_reply_field.clicked.connect(self._on_add_reply_field)
        self.btn_del_reply_field = QPushButton("- 선택 삭제")
        apply_qss_role(self.btn_del_reply_field, role="secondary")
        self.btn_del_reply_field.clicked.connect(self._on_del_reply_field)
        schema_bar.addWidget(lbl_schema)
        schema_bar.addStretch(1)
        schema_bar.addWidget(self.btn_toggle_reply_schema_fullscreen)
        schema_bar.addWidget(self.btn_add_reply_field)
        schema_bar.addWidget(self.btn_del_reply_field)
        c3_lay.addLayout(schema_bar)

        self.table_reply_schema = QTableWidget(0, 5)
        self.table_reply_schema.setHorizontalHeaderLabels(["키 (key)", "항목명 (label)", "유형 (type)", "선택지 (쉼표 구분)", "필수"])
        self.table_reply_schema.verticalHeader().setDefaultSectionSize(46)
        self.table_reply_schema.verticalHeader().setMinimumSectionSize(46)
        self.table_reply_schema.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table_reply_schema.setColumnWidth(0, 115)
        self.table_reply_schema.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_reply_schema.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table_reply_schema.setColumnWidth(2, 155)
        self.table_reply_schema.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table_reply_schema.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table_reply_schema.setColumnWidth(4, 56)
        self.table_reply_schema.setMinimumHeight(140)
        self.table_reply_schema.itemChanged.connect(self._sync_notice_fields_summary)
        c3_lay.addWidget(self.table_reply_schema)

        # 레거시 호환용 요약 필드 (reply_schema 변경 시 자동 동기화)
        self.edit_notice_fields = QLineEdit()
        self.edit_notice_fields.setReadOnly(True)
        self.edit_notice_fields.setVisible(False)
        c3_lay.addWidget(self.edit_notice_fields)

        self._notice_picker_bar_widget = QWidget()
        picker_bar = QHBoxLayout(self._notice_picker_bar_widget)
        picker_bar.setContentsMargins(0, 0, 0, 0)
        picker_bar.setSpacing(8)
        picker_bar.addWidget(QLabel("게시한 가정통신문 선택:"))
        self.combo_published_notices = QComboBox()
        self.combo_published_notices.addItem("— 게시한 가정통신문 선택 —", "")
        self.combo_published_notices.currentIndexChanged.connect(self._on_published_notice_selected)
        self.btn_refresh_published_notices = QPushButton("목록 새로고침")
        apply_qss_role(self.btn_refresh_published_notices, role="secondary")
        self.btn_refresh_published_notices.clicked.connect(lambda: self._on_refresh_published_notices())
        picker_bar.addWidget(self.combo_published_notices, stretch=1)
        picker_bar.addWidget(self.btn_refresh_published_notices)
        c3_lay.addWidget(self._notice_picker_bar_widget)

        self._notice_reply_bar_widget = QWidget()
        reply_bar = QHBoxLayout(self._notice_reply_bar_widget)
        reply_bar.setContentsMargins(0, 0, 0, 0)
        reply_bar.setSpacing(8)
        self.edit_active_notice_id = QLineEdit()
        self.edit_active_notice_id.setPlaceholderText("조회할 가정통신문 ID (배포 시 자동 입력)")
        self.btn_refresh_replies = QPushButton("제출 현황 조회")
        apply_qss_role(self.btn_refresh_replies, role="secondary")
        self.btn_refresh_replies.clicked.connect(self._on_refresh_notice_replies)
        self.btn_nudge_unsubmitted = QPushButton("미제출자 일괄 알림")
        apply_qss_role(self.btn_nudge_unsubmitted, role="warningOutline")
        self.btn_nudge_unsubmitted.clicked.connect(self._on_nudge_unsubmitted_notice)
        self.btn_map_notice_pdf = QPushButton("PDF 양식 위치 지정")
        apply_qss_role(self.btn_map_notice_pdf, role="secondary")
        self.btn_map_notice_pdf.setToolTip("PDF/HWP/HWPX 양식 위에 회신 항목·체크박스·서명 좌표를 지정합니다.")
        self.btn_map_notice_pdf.clicked.connect(self._on_open_notice_pdf_mapper)
        self.btn_export_filled_pdfs = QPushButton("응답 취합 PDF 일괄 생성")
        apply_qss_role(self.btn_export_filled_pdfs, role="primary")
        self.btn_export_filled_pdfs.setToolTip("학생별 회신과 서명 이미지를 원본 PDF 양식에 합성해 일괄 출력합니다.")
        self.btn_export_filled_pdfs.clicked.connect(self._on_export_filled_notice_pdfs)
        reply_bar.addWidget(self.edit_active_notice_id, stretch=1)
        reply_bar.addWidget(self.btn_refresh_replies)
        reply_bar.addWidget(self.btn_nudge_unsubmitted)
        reply_bar.addWidget(self.btn_map_notice_pdf)
        reply_bar.addWidget(self.btn_export_filled_pdfs)
        c3_lay.addWidget(self._notice_reply_bar_widget)

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
        self._c4_broadcast_top_widget = QWidget()
        c4_top_lay = QVBoxLayout(self._c4_broadcast_top_widget)
        c4_top_lay.setContentsMargins(0, 0, 0, 0)
        c4_top_lay.setSpacing(6)
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
        c4_top_lay.addLayout(noti_top)

        self.txt_broadcast_msg = QPlainTextEdit()
        self.txt_broadcast_msg.setPlaceholderText("전달할 학급 공지 또는 긴급 안내 내용을 입력하세요.")
        self.txt_broadcast_msg.setMinimumHeight(80)
        self.txt_broadcast_msg.setMaximumHeight(110)
        c4_top_lay.addWidget(self.txt_broadcast_msg)
        c4_lay.addWidget(self._c4_broadcast_top_widget)

        # --- 4-1. 학교종이 공지 수집 & 카톡 대화 AI 공지 요약 패널 ---
        ingest_frame = QFrame()
        self._c4_ingest_frame = ingest_frame
        ingest_frame.setStyleSheet(
            "QFrame { background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; }"
        )
        ingest_lay = QVBoxLayout(ingest_frame)
        ingest_lay.setContentsMargins(10, 10, 10, 10)
        ingest_lay.setSpacing(8)

        sb_row = QHBoxLayout()
        sb_row.setSpacing(8)
        self.btn_fetch_schoolbell = QPushButton("학교종이 최근 공지 가져오기")
        apply_qss_role(self.btn_fetch_schoolbell, role="secondary")
        self.btn_fetch_schoolbell.clicked.connect(self._on_fetch_schoolbell_notices)
        self.btn_schoolbell_confirm_login = QPushButton("로그인 완료 · 계속")
        apply_qss_role(self.btn_schoolbell_confirm_login, role="primary")
        self.btn_schoolbell_confirm_login.setVisible(False)
        self.btn_schoolbell_confirm_login.clicked.connect(self._on_schoolbell_confirm_login)
        self.lbl_schoolbell_status = QLabel("학교종이(schoolbell-e.com) 연동 대기 (최초 1회 브라우저 로그인 필요)")
        self.lbl_schoolbell_status.setStyleSheet("color: #64748B; font-size: 12px; border: none;")
        sb_row.addWidget(self.btn_fetch_schoolbell)
        sb_row.addWidget(self.btn_schoolbell_confirm_login)
        sb_row.addWidget(self.lbl_schoolbell_status, stretch=1)
        ingest_lay.addLayout(sb_row)

        kk_bar = QHBoxLayout()
        kk_bar.setSpacing(6)
        self.btn_load_kakao_txt = QPushButton("카톡 .txt 불러오기")
        apply_qss_role(self.btn_load_kakao_txt, role="secondary")
        self.btn_load_kakao_txt.clicked.connect(self._on_load_kakao_txt_file)
        self.edit_kakao_source_label = QLineEdit("카카오톡 대화")
        self.edit_kakao_source_label.setPlaceholderText("출처명 (예: 학부모 단톡방)")
        self.edit_kakao_source_label.setMaximumWidth(170)
        self.btn_digest_kakao = QPushButton("AI 공지 추출·요약")
        apply_qss_role(self.btn_digest_kakao, role="primary")
        self.btn_digest_kakao.clicked.connect(self._on_digest_kakao_notices)
        self.btn_kakao_input_fullscreen = QPushButton("전체화면 보기")
        apply_qss_role(self.btn_kakao_input_fullscreen, role="secondary")
        self.btn_kakao_input_fullscreen.setToolTip("카톡·메신저 입력 원문을 넓은 전체화면 창에서 조회·편집합니다.")
        self.btn_kakao_input_fullscreen.clicked.connect(self._on_open_kakao_input_fullscreen)
        self.lbl_kakao_status = QLabel("카톡 내보내기(.txt)를 드롭하거나 대화·공지 텍스트를 붙여넣으세요.")
        self.lbl_kakao_status.setStyleSheet("color: #64748B; font-size: 12px; border: none;")
        kk_bar.addWidget(self.btn_load_kakao_txt)
        kk_bar.addWidget(QLabel("출처:"))
        kk_bar.addWidget(self.edit_kakao_source_label)
        kk_bar.addWidget(self.btn_digest_kakao)
        kk_bar.addWidget(self.btn_kakao_input_fullscreen)
        kk_bar.addWidget(self.lbl_kakao_status, stretch=1)
        ingest_lay.addLayout(kk_bar)

        self.txt_kakao_input = QPlainTextEdit()
        self.txt_kakao_input.setPlaceholderText(
            "PC 카카오톡 대화 내보내기(.txt) 파일을 여기에 드래그·드롭하거나 대화/안내문을 붙여넣으세요."
        )
        self.txt_kakao_input.setMinimumHeight(80)
        self.txt_kakao_input.setMaximumHeight(120)
        enable_file_drop(
            self.txt_kakao_input,
            allowed_exts=[".txt"],
            allow_dirs=False,
            allow_files=True,
            on_files_dropped=self._on_kakao_file_dropped,
        )
        ingest_lay.addWidget(self.txt_kakao_input)
        c4_lay.addWidget(ingest_frame)

        # --- 4-2. 발송 전 검수 목록 & 학급_공지게시판 게시 ---
        review_bar = QHBoxLayout()
        review_bar.setSpacing(6)
        lbl_rev_title = QLabel("발송 전 검수 목록 (선택 항목만 게시·발송)")
        lbl_rev_title.setStyleSheet("font-weight: 700; color: #1E293B; font-size: 13px;")
        self.lbl_review_count = QLabel("0건")
        self.lbl_review_count.setStyleSheet("color: #64748B; font-size: 12px;")
        self.btn_toggle_review_select = QPushButton("전체 선택/해제")
        apply_qss_role(self.btn_toggle_review_select, role="secondary")
        self.btn_toggle_review_select.clicked.connect(self._on_toggle_all_review_cards)
        self.btn_delete_selected_review = QPushButton("선택 삭제")
        apply_qss_role(self.btn_delete_selected_review, role="secondary")
        self.btn_delete_selected_review.setToolTip("체크된 항목만 발송 전 검수 목록에서 일괄 제거합니다.")
        self.btn_delete_selected_review.clicked.connect(self._on_delete_selected_review_cards)
        self.btn_toggle_review_fullscreen = QPushButton("전체화면 보기")
        apply_qss_role(self.btn_toggle_review_fullscreen, role="secondary")
        self.btn_toggle_review_fullscreen.setCheckable(True)
        self.btn_toggle_review_fullscreen.setToolTip("상단 입력 영역을 숨기고 검수 목록을 전체 높이로 확장합니다.")
        self.btn_toggle_review_fullscreen.clicked.connect(self._on_toggle_review_fullscreen)
        self.btn_publish_board_only = QPushButton("공지 게시판 게시")
        apply_qss_role(self.btn_publish_board_only, role="secondary")
        self.btn_publish_board_only.clicked.connect(lambda: self._publish_selected_review_cards(False))
        self.btn_publish_board_and_notify = QPushButton("게시 + 모바일 알림 동시 발송")
        apply_qss_role(self.btn_publish_board_and_notify, role="primary")
        self.btn_publish_board_and_notify.clicked.connect(lambda: self._publish_selected_review_cards(True))
        review_bar.addWidget(lbl_rev_title)
        review_bar.addWidget(self.lbl_review_count)
        review_bar.addStretch()
        review_bar.addWidget(self.btn_toggle_review_select)
        review_bar.addWidget(self.btn_delete_selected_review)
        review_bar.addWidget(self.btn_toggle_review_fullscreen)
        review_bar.addWidget(self.btn_publish_board_only)
        review_bar.addWidget(self.btn_publish_board_and_notify)
        c4_lay.addLayout(review_bar)

        self.scroll_review_cards = QScrollArea()
        self.scroll_review_cards.setWidgetResizable(True)
        self.scroll_review_cards.setMinimumHeight(180)
        self.scroll_review_cards.setMaximumHeight(320)
        self.scroll_review_cards.setStyleSheet(
            "QScrollArea { background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; }"
        )
        review_container = QWidget()
        self.review_cards_layout = QVBoxLayout(review_container)
        self.review_cards_layout.setContentsMargins(8, 8, 8, 8)
        self.review_cards_layout.setSpacing(8)
        self.lbl_review_empty = QLabel(
            "수집·요약된 공지가 없습니다. 위에서 학교종이 공지를 가져오거나 카톡 대화를 요약해 주세요."
        )
        self.lbl_review_empty.setStyleSheet("color: #94A3B8; font-size: 12px; padding: 16px;")
        self.lbl_review_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.review_cards_layout.addWidget(self.lbl_review_empty)
        self.review_cards_layout.addStretch()
        self.scroll_review_cards.setWidget(review_container)
        c4_lay.addWidget(self.scroll_review_cards)

        self.table_broadcast_history = QTableWidget(0, 4)
        self.table_broadcast_history.setHorizontalHeaderLabels(["발송 일시", "대상", "제목", "내용"])
        self.table_broadcast_history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_broadcast_history.setMinimumHeight(150)
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
        self.edit_teacher_login_id.setPlaceholderText("교사 로그인 ID (예: teacher01)")

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
    def _apply_teacher_auth_state(self, trigger_heartbeat: bool = True) -> None:
        logged_in = bool(self._logged_in_teacher)
        self.auth_status_widget.setVisible(logged_in)
        self.auth_overlay_backdrop.setVisible(not logged_in)
        self.pill_bar.setVisible(True)
        self.tool_stack.setVisible(True)

        if not logged_in:
            self._stop_pc_agent_timers()
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
            if hasattr(self, "spin_notice_target_grade") and g:
                self.spin_notice_target_grade.setValue(int(g))
            if hasattr(self, "spin_notice_target_class") and c is not None:
                self.spin_notice_target_class.setValue(int(c))
            self._start_pc_agent_timers(send_immediate=trigger_heartbeat)
        self.teacher_auth_changed.emit()

    def _start_pc_agent_timers(self, send_immediate: bool = True) -> None:
        if not self._logged_in_teacher:
            return
        if not self._heartbeat_timer.isActive():
            self._heartbeat_timer.start()
        if not self._remote_task_timer.isActive():
            self._remote_task_timer.start()
        if send_immediate:
            self._on_pc_agent_heartbeat_tick()

    def _stop_pc_agent_timers(self) -> None:
        if hasattr(self, "_heartbeat_timer") and self._heartbeat_timer.isActive():
            self._heartbeat_timer.stop()
        if hasattr(self, "_remote_task_timer") and self._remote_task_timer.isActive():
            self._remote_task_timer.stop()

    def _on_pc_agent_heartbeat_tick(
        self,
        sync_service: Optional[FirestoreSyncService] = None,
    ) -> bool:
        if not self._logged_in_teacher:
            return False
        meta = self._teacher_meta()
        tid = str(meta.get("teacher_id") or "").strip()
        sc = str(meta.get("school_code") or "").strip()
        if not tid:
            return False
        hostname = socket.gethostname()
        app_ver = self.PC_AGENT_APP_VERSION

        injected_svc = sync_service or self._pc_agent_sync_service
        if injected_svc is not None:
            try:
                return bool(
                    injected_svc.update_pc_agent_status(
                        online=True,
                        hostname=hostname,
                        app_version=app_ver,
                        teacher_id=tid,
                        school_code=sc,
                    )
                )
            except Exception as exc:
                self.db.add_log("WARN", "ClassroomTools", f"PC 에이전트 하트비트 갱신 실패: {exc}")
                return False

        if self._heartbeat_running:
            return False
        self._heartbeat_running = True

        def _work(_worker: Any) -> bool:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return svc.update_pc_agent_status(
                online=True,
                hostname=hostname,
                app_version=app_ver,
                teacher_id=tid,
                school_code=sc,
            )

        def _on_finished() -> None:
            self._heartbeat_running = False

        worker = FunctionWorker("PC 에이전트 온라인 하트비트", _work)
        self.task_runner.start_worker(
            worker,
            task_key="classroom_pc_agent_heartbeat",
            on_finished=_on_finished,
        )
        return True

    def _on_remote_task_poll_tick(
        self,
        sync_service: Optional[FirestoreSyncService] = None,
    ) -> None:
        if not self._logged_in_teacher or self._remote_task_poll_running:
            return
        from services.remote_task_executor import RemoteTaskExecutor

        injected_svc = sync_service or self._pc_agent_sync_service
        if injected_svc is not None:
            executor = RemoteTaskExecutor(db=self.db, config=self.config, sync_service=injected_svc)
            executor.poll_and_execute_due_tasks()
            return

        self._remote_task_poll_running = True

        def _work(worker: Any) -> List[Dict[str, Any]]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            executor = RemoteTaskExecutor(db=self.db, config=self.config, sync_service=svc)
            results = executor.poll_and_execute_due_tasks()
            for item in results or []:
                if not isinstance(item, dict):
                    continue
                ok = bool(item.get("ok", True))
                task_type = str(item.get("task_type") or "remote").strip()
                summary = str(item.get("summary") or item.get("error") or "").strip()
                if not ok or str(item.get("exec_status") or "").lower() == "error":
                    worker.emit_log("ERROR", f"[{task_type}] {summary or '원격 작업 실행 실패'}")
                else:
                    worker.emit_log("SUCCESS", f"[{task_type}] {summary or '원격 작업 실행 완료'}")
            return results

        def _on_finished() -> None:
            self._remote_task_poll_running = False

        worker = FunctionWorker("모바일 원격 작업 큐 실행", _work)
        self.task_runner.start_worker(
            worker,
            task_key="classroom_remote_task_poll",
            on_finished=_on_finished,
        )

    def notify_pc_agent_offline(
        self,
        sync_service: Optional[FirestoreSyncService] = None,
    ) -> bool:
        """로그아웃 또는 앱 종료 시 PC 에이전트 상태를 즉시 오프라인(`online=False`)으로 보고합니다."""
        self._stop_pc_agent_timers()
        if not self._logged_in_teacher:
            return False
        meta = self._teacher_meta()
        tid = str(meta.get("teacher_id") or "").strip()
        sc = str(meta.get("school_code") or "").strip()
        if not tid:
            return False
        try:
            svc = sync_service or self._pc_agent_sync_service or FirestoreSyncService(
                db=self.db, config=self.config
            )
            return bool(
                svc.update_pc_agent_status(
                    online=False,
                    hostname=socket.gethostname(),
                    app_version=self.PC_AGENT_APP_VERSION,
                    teacher_id=tid,
                    school_code=sc,
                )
            )
        except Exception as exc:
            self.db.add_log("WARN", "ClassroomTools", f"PC 에이전트 오프라인 보고 실패: {exc}")
            return False

    def _start_spinner_for_buttons(
        self,
        items: List[Tuple[QPushButton, str]],
    ) -> Callable[[], None]:
        """버튼 목록에 QTimer 회전 스피너 QIcon을 적용하고, 호출 시 원래 아이콘·텍스트·활성 상태를 복구하는 콜백을 반환합니다."""
        tracked: List[Tuple[QPushButton, str, QIcon]] = []
        for btn, loading_text in items:
            if btn is None:
                continue
            tracked.append((btn, btn.text(), QIcon(btn.icon())))
            btn.setEnabled(False)
            if loading_text is not None and btn.text():
                btn.setText(loading_text)
            if btn.iconSize().width() <= 0:
                btn.setIconSize(QSize(16, 16))
            btn.setIcon(_build_spinner_icon(0))

        angle_state = {"deg": 0}
        timer = QTimer(self)
        timer.setInterval(50)

        def _tick() -> None:
            angle_state["deg"] = (angle_state["deg"] + 30) % 360
            icon = _build_spinner_icon(angle_state["deg"])
            for btn, _orig_txt, _orig_icon in tracked:
                try:
                    btn.setIcon(icon)
                except RuntimeError:
                    pass

        timer.timeout.connect(_tick)
        timer.start()

        def _restore() -> None:
            try:
                timer.stop()
                timer.deleteLater()
            except RuntimeError:
                pass
            for btn, orig_text, orig_icon in tracked:
                try:
                    btn.setIcon(orig_icon)
                    btn.setText(orig_text)
                    btn.setEnabled(True)
                except RuntimeError:
                    pass

        return _restore

    def _run_async_button_task(
        self,
        button: Optional[QPushButton],
        loading_text: str,
        task_name: str,
        task_key: str,
        work_fn: Callable[[], Any],
        on_result: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        extra_buttons: Optional[List[Tuple[QPushButton, str]]] = None,
    ) -> None:
        """버튼 비활성화·회전 스피너·로딩 텍스트 표시와 완료 시 복구를 보장하는 TaskRunner 헬퍼."""
        if button is not None and not button.isEnabled():
            return

        items: List[Tuple[QPushButton, str]] = []
        if button is not None:
            items.append((button, loading_text))
        for extra_btn, extra_loading in (extra_buttons or []):
            if extra_btn is not None:
                items.append((extra_btn, extra_loading))

        _restore_buttons = self._start_spinner_for_buttons(items)

        worker = FunctionWorker(task_name, lambda _w: work_fn())
        self.task_runner.start_worker(
            worker,
            task_key=task_key,
            on_result=on_result,
            on_error=on_error,
            on_finished=_restore_buttons,
        )

    def _on_sync_roster_clicked(self) -> None:
        if self._logged_in_teacher:
            self.refresh_firebase_roster(silent=False)
        else:
            self._refresh_roster_dependent_views()

    def _on_teacher_login_clicked(self) -> None:
        uid = self.edit_teacher_login_id.text().strip()
        pw = self.edit_teacher_login_pw.text()
        if not uid:
            QMessageBox.warning(self, "로그인 확인", "교사 로그인 ID를 입력해 주세요.")
            self.edit_teacher_login_id.setFocus()
            return

        def _do_login() -> Dict[str, Any]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            ok, user_doc, err = svc.authenticate_account("teacher", uid, pw)
            if not ok or not user_doc:
                return {"ok": False, "error": err or "교사 계정을 확인할 수 없습니다."}
            school_code = str(user_doc.get("school_code") or user_doc.get("neis_school_code") or "").strip()
            school_name = str(user_doc.get("school_name") or "").strip()
            grade_val = user_doc.get("grade")
            class_val = user_doc.get("class_no")
            g_int = int(grade_val) if str(grade_val or "").isdigit() else None
            c_int = int(class_val) if str(class_val or "").isdigit() else None
            roster_rows: List[Tuple[str, str]] = []
            try:
                students = svc.fetch_homeroom_students(
                    school_code=school_code,
                    school_name=school_name,
                    grade=g_int,
                    class_no=c_int,
                )
                roster_rows = [
                    (str(s.get("student_id") or s.get("remote_id") or "").strip(), str(s.get("name") or "").strip())
                    for s in students
                    if str(s.get("student_id") or s.get("remote_id") or "").strip()
                ]
            except Exception:
                roster_rows = []
            return {"ok": True, "user_doc": user_doc, "roster": roster_rows}

        def _on_login_result(res: Dict[str, Any]) -> None:
            if not isinstance(res, dict) or not res.get("ok"):
                QMessageBox.warning(self, "로그인 실패", str((res or {}).get("error") or "로그인에 실패했습니다."))
                return
            user_doc = res["user_doc"]
            school_code = str(user_doc.get("school_code") or user_doc.get("neis_school_code") or "").strip()
            school_name = str(user_doc.get("school_name") or "").strip()
            grade_val = user_doc.get("grade")
            class_val = user_doc.get("class_no")
            teacher_name = str(user_doc.get("name") or user_doc.get("teacher_id") or uid).strip()
            teacher_id = str(user_doc.get("teacher_id") or user_doc.get("remote_id") or uid).strip()
            login_id = str(user_doc.get("login_id") or uid).strip()
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
            tp_sec["auth_password_hash"] = hash_password_sha256(pw) if pw else ""
            self.config.update_section("teacher_profile", tp_sec)

            self._firebase_roster = list(res.get("roster") or [])
            self._apply_teacher_auth_state()
            self._refresh_roster_dependent_views()
            self._on_refresh_published_notices()
            self.edit_teacher_login_pw.clear()

        def _on_login_error(err_msg: str) -> None:
            first_line = str(err_msg or "").splitlines()[0] if err_msg else "알 수 없는 오류"
            QMessageBox.warning(self, "로그인 실패", f"Firestore 인증 오류: {first_line}")

        self._run_async_button_task(
            button=self.btn_teacher_login,
            loading_text="로그인 중...",
            task_name="교사 계정 로그인",
            task_key="classroom_teacher_login",
            work_fn=_do_login,
            on_result=_on_login_result,
            on_error=_on_login_error,
        )

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

        if sync_service is not None:
            self._pc_agent_sync_service = sync_service

        self.refresh_firebase_roster(sync_service=svc, silent=True)
        self._apply_teacher_auth_state(trigger_heartbeat=True)
        return True, ""

    def logout_teacher(self, sync_service: Optional[FirestoreSyncService] = None) -> None:
        self.notify_pc_agent_offline(sync_service=sync_service)
        self._logged_in_teacher = None
        self._firebase_roster = []
        self.config.update_section(
            "teacher_profile",
            {"session_logged_in": False, "auth_password_hash": ""},
        )
        self._apply_teacher_auth_state(trigger_heartbeat=False)
        self._refresh_roster_dependent_views()

    def restore_saved_teacher_session(
        self,
        sync_service: Optional[FirestoreSyncService] = None,
    ) -> bool:
        if sync_service is not None:
            self._pc_agent_sync_service = sync_service
        tp_sec = dict(self.config.get_section("teacher_profile") or {})
        if not tp_sec.get("session_logged_in"):
            self._logged_in_teacher = None
            self._apply_teacher_auth_state(trigger_heartbeat=False)
            return False
        tid = str(tp_sec.get("teacher_id") or tp_sec.get("login_id") or tp_sec.get("name") or "").strip()
        if not tid:
            self._logged_in_teacher = None
            self._apply_teacher_auth_state(trigger_heartbeat=False)
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
        self._apply_teacher_auth_state(trigger_heartbeat=True)
        if sync_service is not None:
            self.refresh_firebase_roster(sync_service=sync_service, silent=True)
        return True

    def sync_teacher_profile_from_config(self) -> None:
        """config의 teacher_profile 변경 사항을 현재 탭의 로그인/학반 상태에 반영합니다."""
        if getattr(self, "_syncing_profile_from_cfg", False):
            return
        self._syncing_profile_from_cfg = True
        try:
            tp_sec = dict(self.config.get_section("teacher_profile") or {})
            if not tp_sec.get("session_logged_in"):
                if self._logged_in_teacher is not None:
                    self._logged_in_teacher = None
                    self._firebase_roster = []
                    self._apply_teacher_auth_state(trigger_heartbeat=False)
                    self._refresh_roster_dependent_views()
                return

            tid = str(tp_sec.get("teacher_id") or tp_sec.get("login_id") or tp_sec.get("name") or "").strip()
            if not tid:
                return
            sc = str(tp_sec.get("school_code") or tp_sec.get("neis_school_code") or "").strip()
            new_teacher = {
                "teacher_id": tid,
                "login_id": str(tp_sec.get("login_id") or tid).strip(),
                "name": str(tp_sec.get("name") or tid).strip(),
                "school_code": sc,
                "neis_school_code": sc,
                "school_name": str(tp_sec.get("school_name") or "").strip(),
                "grade": int(tp_sec["grade"]) if str(tp_sec.get("grade") or "").isdigit() else None,
                "class_no": int(tp_sec["class_no"]) if str(tp_sec.get("class_no") or "").isdigit() else None,
                "is_admin": bool(tp_sec.get("is_admin") is True),
            }
            if self._logged_in_teacher != new_teacher:
                prev = self._logged_in_teacher or {}
                scope_changed = (
                    prev.get("teacher_id") != new_teacher["teacher_id"]
                    or prev.get("school_code") != new_teacher["school_code"]
                    or prev.get("grade") != new_teacher["grade"]
                    or prev.get("class_no") != new_teacher["class_no"]
                )
                self._logged_in_teacher = new_teacher
                if scope_changed:
                    self._firebase_roster = []
                self._apply_teacher_auth_state(trigger_heartbeat=False)
                self._refresh_roster_dependent_views()
        finally:
            self._syncing_profile_from_cfg = False

    def refresh_firebase_roster(
        self,
        sync_service: Optional[FirestoreSyncService] = None,
        silent: bool = False,
    ) -> int:
        if not self._logged_in_teacher:
            return 0
        meta = self._teacher_meta()

        # 테스트 등에서 sync_service를 직접 주입한 경우 동기 실행 유지
        if sync_service is not None:
            try:
                students = sync_service.fetch_homeroom_students(
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

            self._apply_teacher_auth_state(trigger_heartbeat=False)
            self._refresh_roster_dependent_views()
            return len(self._firebase_roster)

        if self._roster_sync_running:
            return 0
        self._roster_sync_running = True

        self.lbl_roster_status_badge.setText("연동 명렬표: [Firebase] 동기화 중...")
        apply_qss_role(self.lbl_roster_status_badge, role="badgePrimary")

        spinner_items: List[Tuple[QPushButton, str]] = []
        if getattr(self, "btn_teacher_refresh", None) is not None:
            spinner_items.append((self.btn_teacher_refresh, ""))
        if getattr(self, "btn_sync_roster", None) is not None:
            spinner_items.append((self.btn_sync_roster, "명렬표 동기화 중..."))
        _restore_spinner = self._start_spinner_for_buttons(spinner_items)

        def _fetch_roster(_worker: Any) -> List[Tuple[str, str]]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            students = svc.fetch_homeroom_students(
                school_code=meta["school_code"],
                school_name=meta["school_name"],
                grade=meta["grade"],
                class_no=meta["class_no"],
            )
            return [
                (str(s.get("student_id") or s.get("remote_id") or "").strip(), str(s.get("name") or "").strip())
                for s in students
                if str(s.get("student_id") or s.get("remote_id") or "").strip()
            ]

        def _on_result(rows: Any) -> None:
            self._firebase_roster = list(rows or [])
            self._apply_teacher_auth_state(trigger_heartbeat=False)
            self._refresh_roster_dependent_views()

        def _on_error(err_msg: str) -> None:
            self._refresh_roster_dependent_views()
            if not silent:
                first_line = str(err_msg or "").splitlines()[0] if err_msg else "알 수 없는 오류"
                QMessageBox.warning(self, "명렬표 동기화 오류", f"Firestore 명렬표 조회 실패:\n{first_line}")

        def _on_finished() -> None:
            self._roster_sync_running = False
            _restore_spinner()

        worker = FunctionWorker("Firestore 명렬표 새로고침", _fetch_roster)
        self.task_runner.start_worker(
            worker,
            task_key="classroom_roster_refresh",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=_on_finished,
        )
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
        return "미등록 학급"

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

            if self._logged_in_teacher and not self._local_homeroom_matches_logged_in_teacher(
                hr_group, homeroom_students
            ):
                label = self._current_homeroom_label()
                local_name = str((hr_group or {}).get("group_name") or "타 학반").strip()
                self.lbl_roster_status_badge.setText(
                    f"연동 명렬표: [Firebase] {label} (0명)"
                )
                apply_qss_role(self.lbl_roster_status_badge, role="badgeWarning")
                self.lbl_roster_status_badge.setToolTip(
                    f"로그인한 교사({label})와 로컬 SQLite 담임 명렬표({local_name})의 학반이 달라 "
                    "타 학반 명렬표 자동 폴백을 차단했습니다. (로컬 데이터는 보존됩니다)"
                )
            elif hr_group and homeroom_students:
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

    def _on_open_batch_face_photos(self) -> None:
        meta = self._teacher_meta()
        roster = self._get_active_roster()
        if not roster:
            QMessageBox.warning(
                self,
                "학생 얼굴 사진 일괄 등록",
                "연동된 담임 학급 명렬표가 없습니다. 명렬표를 먼저 등록하거나 연동해 주세요.",
            )
            return

        dlg = StudentFacePhotoBatchDialog(
            self,
            roster=roster,
            school_code=str(meta.get("school_code") or ""),
            school_name=str(meta.get("school_name") or ""),
            grade=meta.get("grade"),
            class_no=meta.get("class_no"),
            db=self.db,
            config=self.config,
            task_runner=self.task_runner,
        )
        dlg.exec()

    def _merge_mobile_attendance_rows(self, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
        """Firestore `일일_출결`에서 조회한 모바일 출결 보고를 로컬 출결 대장에 병합합니다."""
        hk = self._current_homeroom_key()
        include_simple_present = (
            self.chk_include_simple_present.isChecked()
            if getattr(self, "chk_include_simple_present", None) is not None
            else False
        )
        added = 0
        updated = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            sid_raw = str(row.get("student_id") or "").strip()
            sid = re.sub(r"^(\d+)\.0+$", r"\1", sid_raw)
            sname = str(row.get("student_name") or row.get("name") or "").strip()
            if not sid and not sname:
                continue
            rid = str(row.get("remote_id") or row.get("id") or "").strip()
            cat = str(row.get("attendance_type") or row.get("category") or row.get("type") or "질병결석").strip()
            if cat == "출석" and not include_simple_present:
                continue
            reason = str(row.get("reason") or row.get("memo") or "").strip()
            date_str = str(row.get("date") or row.get("start_date") or "").strip()
            raw_dates = row.get("selected_dates")
            if isinstance(raw_dates, list) and raw_dates:
                sel_dates = [str(d).strip() for d in raw_dates if str(d).strip()]
            elif date_str:
                sel_dates = [date_str]
            else:
                sel_dates = []
            period_text = str(row.get("period_text") or "").strip()
            if not period_text:
                period_val = str(row.get("period") or "").strip()
                if date_str and period_val:
                    period_text = f"{date_str} ({period_val}교시)"
                elif date_str:
                    period_text = date_str
                elif sel_dates:
                    period_text = ", ".join(sel_dates[:3])

            confirmed = bool(row.get("teacher_confirmed")) or str(row.get("status") or "").strip() in (
                "confirmed",
                "확인 완료",
                "서류 완비",
            )
            raw_proof = str(row.get("proof_status") or "").strip()
            if confirmed:
                proof_status = raw_proof if raw_proof and "대기" not in raw_proof and "미확인" not in raw_proof else "확인 완료"
            else:
                proof_status = raw_proof or "미확인(대기중)"

            _, created = self.db.upsert_remote_attendance_log(
                hk,
                remote_id=rid,
                student_id=sid,
                student_name=sname or sid,
                category=cat,
                reason=reason,
                period_text=period_text,
                selected_dates=sel_dates,
                proof_status=proof_status,
                updated_at=str(row.get("updated_at") or "").strip(),
            )
            if created:
                added += 1
            else:
                updated += 1
        return added, updated

    def _on_sync_mobile_attendance(self) -> None:
        meta = self._teacher_meta()

        def _work() -> List[Dict[str, Any]]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return svc.fetch_homeroom_attendance(
                grade=meta["grade"],
                class_no=meta["class_no"],
                school_code=meta["school_code"],
                school_name=meta["school_name"],
            )

        def _on_result(rows: Any) -> None:
            row_list = list(rows or [])
            added, updated = self._merge_mobile_attendance_rows(row_list)
            self._refresh_attendance_view()
            pending_cnt = sum(
                1
                for r in self.db.get_attendance_logs(self._current_homeroom_key())
                if "대기" in str(r.get("proof_status", "")) or "미확인" in str(r.get("proof_status", ""))
            )
            QMessageBox.information(
                self,
                "모바일 출결 동기화",
                f"Firestore에서 {len(row_list)}건의 출결 보고를 조회하여 병합했습니다.\n"
                f"신규 반영: {added}건 · 갱신: {updated}건 · 현재 미확인(대기중): {pending_cnt}건",
            )

        self._run_async_button_task(
            button=getattr(self, "btn_refresh_mobile_att", None) or getattr(self, "btn_sync_mobile_att", None),
            loading_text="새로고침 중...",
            task_name="모바일 출결 동기화",
            task_key="classroom_sync_mobile_attendance",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(
                self, "모바일 출결 동기화 오류", str(err or "").splitlines()[0]
            ),
        )

    # =========================================================================
    # 가정통신문 PDF -> AiExecutionDock 구조화 JSON OCR 연동 (pypdf 미사용)
    # =========================================================================
    def get_ai_context(self) -> Dict[str, Any]:
        files = [self._selected_notice_pdf_path] if self._selected_notice_pdf_path else []
        content_text = self.txt_notice_content.toPlainText().strip()
        prompt = NOTICE_OCR_JSON_PROMPT
        if content_text:
            prompt = (
                f"{NOTICE_OCR_JSON_PROMPT}\n\n"
                f"[가정통신문 본문(사용자 확인·편집 텍스트)]\n{content_text[:12000]}"
            )
        elif self._selected_notice_pdf_path and Path(self._selected_notice_pdf_path).suffix.lower() in (".hwp", ".hwpx", ".pdf"):
            extracted = extract_notice_source_text(self._selected_notice_pdf_path)
            if extracted:
                prompt = f"{NOTICE_OCR_JSON_PROMPT}\n\n[문서 본문(로컬 추출 텍스트)]\n{extracted}"
        return {
            "target_id": "notice_ocr",
            "kind": "ocr",
            "task_label": "가정통신문(PDF/HWP/HWPX) 구조화 JSON 추출",
            "ocr_files": files,
            "attachment_paths": files,
            "ocr_current_text": content_text,
            "ocr_prompt": prompt,
        }

    def apply_ai_dock_result(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("pdf_path"):
            self._selected_notice_pdf_path = str(payload.get("pdf_path") or "").strip()
            if self._selected_notice_pdf_path:
                self.lbl_notice_pdf_status.setText(
                    f"선택된 파일: {Path(self._selected_notice_pdf_path).name}"
                )
        raw_source = payload.get("parsed_notice") or payload.get("parsed_json")
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

        if parsed.get("title"):
            self.edit_notice_title.setText(parsed["title"])
        if parsed.get("category"):
            self.combo_notice_category.setCurrentText(parsed["category"])
        self.edit_notice_due.setText(parsed.get("due_date") or "")
        notice_text = (
            str(parsed.get("corrected_text") or payload.get("corrected_text") or "").strip()
            or str(parsed.get("content") or "").strip()
        )
        if notice_text:
            self.txt_notice_content.setPlainText(notice_text)
        if hasattr(self, "lbl_notice_content_state"):
            self.lbl_notice_content_state.setText("AI 정리 완료")
        requires_reply = bool(parsed.get("requires_reply"))
        self.chk_notice_requires_reply.setChecked(requires_reply)
        if hasattr(self, "combo_notice_signature_mode"):
            sig_mode = str(parsed.get("signature_mode") or ("parent" if requires_reply else "none")).strip().lower()
            if sig_mode == "single":
                sig_mode = "parent"
            if not requires_reply and sig_mode not in ("none", "student", "parent", "both"):
                sig_mode = "none"
            sig_idx = self.combo_notice_signature_mode.findData(sig_mode)
            if sig_idx < 0:
                sig_idx = self.combo_notice_signature_mode.findData("parent" if requires_reply else "none")
            self.combo_notice_signature_mode.setCurrentIndex(sig_idx if sig_idx >= 0 else 0)
        self._populate_reply_schema_table(parsed.get("reply_schema") or [])
        if parsed.get("attachment_url"):
            self.edit_notice_attachment_url.setText(str(parsed["attachment_url"]))
        self.lbl_notice_pdf_status.setText(
            "AI 구조화 JSON 반영 완료 — 검수 후 [모바일 후보로 저장] 또는 [바로 게시하기]를 클릭하세요."
        )

    def _populate_reply_schema_table(self, schema: List[Dict[str, Any]]) -> None:
        normalized = normalize_reply_schema(schema)
        self.table_reply_schema.blockSignals(True)
        try:
            self.table_reply_schema.setRowCount(0)
            for item in normalized:
                self._append_reply_schema_row(item)
        finally:
            self.table_reply_schema.blockSignals(False)
        self._sync_notice_fields_summary()

    def _append_reply_schema_row(self, item: Dict[str, Any]) -> None:
        row = self.table_reply_schema.rowCount()
        self.table_reply_schema.insertRow(row)

        key_item = QTableWidgetItem(str(item.get("key") or f"field_{row + 1}").strip())
        label_item = QTableWidgetItem(str(item.get("label") or "").strip())

        type_container = QWidget()
        type_lay = QHBoxLayout(type_container)
        type_lay.setContentsMargins(6, 5, 6, 5)
        type_lay.setSpacing(0)
        type_lay.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        type_combo = QComboBox(type_container)
        type_labels = {
            "text": "단답형 입력",
            "textarea": "서술형 입력",
            "radio": "객관식 (단일 선택)",
            "checkbox": "체크박스 (다중 선택)",
        }
        for ftype_key in VALID_REPLY_FIELD_TYPES:
            type_combo.addItem(type_labels.get(ftype_key, ftype_key), ftype_key)
        type_combo.setFixedHeight(32)
        type_combo.setStyleSheet(
            "QComboBox {"
            " border: 1px solid #cbd5e1;"
            " border-radius: 6px;"
            " background: #ffffff;"
            " padding: 0px 22px 2px 8px;"
            " min-height: 0px;"
            " font-size: 12px;"
            "}"
            "QComboBox::drop-down {"
            " subcontrol-origin: padding;"
            " subcontrol-position: center right;"
            " width: 20px;"
            " border: none;"
            "}"
            "QComboBox::down-arrow {"
            " subcontrol-position: center;"
            "}"
        )
        ftype = str(item.get("type") or "text").strip().lower()
        type_idx = type_combo.findData(ftype)
        if type_idx < 0:
            type_idx = type_combo.findData("text")
        if type_idx >= 0:
            type_combo.setCurrentIndex(type_idx)
        install_no_scroll_wheel_filter(type_combo)
        type_lay.addWidget(type_combo)

        opts = item.get("options") or []
        opts_text = ", ".join(str(o).strip() for o in opts if str(o).strip())
        opts_item = QTableWidgetItem(opts_text)

        req_item = QTableWidgetItem("")
        req_item.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        req_item.setCheckState(
            Qt.CheckState.Checked if item.get("required", True) else Qt.CheckState.Unchecked
        )

        self.table_reply_schema.setRowHeight(row, 46)
        self.table_reply_schema.setItem(row, 0, key_item)
        self.table_reply_schema.setItem(row, 1, label_item)
        self.table_reply_schema.setCellWidget(row, 2, type_container)
        self.table_reply_schema.setItem(row, 3, opts_item)
        self.table_reply_schema.setItem(row, 4, req_item)

    def _collect_reply_schema_from_table(self) -> List[Dict[str, Any]]:
        raw_items: List[Dict[str, Any]] = []
        for r in range(self.table_reply_schema.rowCount()):
            key_item = self.table_reply_schema.item(r, 0)
            label_item = self.table_reply_schema.item(r, 1)
            type_widget = self.table_reply_schema.cellWidget(r, 2)
            opts_item = self.table_reply_schema.item(r, 3)
            req_item = self.table_reply_schema.item(r, 4)

            key = key_item.text().strip() if key_item else f"field_{r + 1}"
            label = label_item.text().strip() if label_item else ""
            combo = (
                type_widget
                if isinstance(type_widget, QComboBox)
                else (type_widget.findChild(QComboBox) if type_widget is not None else None)
            )
            ftype = (
                str(combo.currentData() or "text").strip()
                if isinstance(combo, QComboBox)
                else "text"
            )
            opts_raw = opts_item.text().strip() if opts_item else ""
            options = [o.strip() for o in re.split(r"[,/|]", opts_raw) if o.strip()]
            required = req_item.checkState() == Qt.CheckState.Checked if req_item else True
            if not label and not key:
                continue
            raw_items.append(
                {
                    "key": key or f"field_{r + 1}",
                    "label": label or key,
                    "type": ftype,
                    "options": options,
                    "required": required,
                }
            )
        return normalize_reply_schema(raw_items)

    def _sync_notice_fields_summary(self, *_args: Any) -> None:
        schema = self._collect_reply_schema_from_table()
        labels = [str(item.get("label") or item.get("key") or "").strip() for item in schema]
        self.edit_notice_fields.setText(", ".join(x for x in labels if x))

    def _on_add_reply_field(self) -> None:
        idx = self.table_reply_schema.rowCount() + 1
        self.table_reply_schema.blockSignals(True)
        try:
            self._append_reply_schema_row(
                {
                    "key": f"field_{idx}",
                    "label": f"입력 항목 {idx}",
                    "type": "text",
                    "options": [],
                    "required": True,
                }
            )
        finally:
            self.table_reply_schema.blockSignals(False)
        self._sync_notice_fields_summary()

    def _on_toggle_reply_schema_fullscreen(self, checked: bool) -> None:
        is_full = bool(checked)
        for w in (
            getattr(self, "notice_pdf_dropzone", None),
            getattr(self, "_notice_top_widget", None),
            getattr(self, "_notice_meta_widget", None),
            getattr(self, "_notice_content_bar_widget", None),
            getattr(self, "txt_notice_content", None),
            getattr(self, "_notice_reply_bar_widget", None),
            getattr(self, "table_notice_replies", None),
        ):
            if w is not None:
                w.setVisible(not is_full)
        if is_full:
            self.btn_toggle_reply_schema_fullscreen.setText("원래대로")
            self.table_reply_schema.setMinimumHeight(460)
        else:
            self.btn_toggle_reply_schema_fullscreen.setText("전체화면 보기")
            self.table_reply_schema.setMinimumHeight(140)

    def _on_open_notice_content_fullscreen(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("가정통신문 본문 미리보기 · 편집 (전체화면)")
        dlg.setWindowFlags(
            dlg.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        dlg.resize(920, 660)

        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        header = QHBoxLayout()
        lbl_info = QLabel("본문 내용을 넓게 조회·편집한 뒤 [적용 후 닫기]를 누르면 편집 내용이 반영됩니다.")
        lbl_info.setStyleSheet("color: #475569; font-size: 12px;")
        header.addWidget(lbl_info)
        header.addStretch(1)
        lay.addLayout(header)

        editor = QPlainTextEdit(dlg)
        editor.setPlainText(self.txt_notice_content.toPlainText())
        editor.setPlaceholderText("학생·학부모용 핵심 요약 또는 추출된 본문 내용이 여기에 표시됩니다.")
        lay.addWidget(editor, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_cancel = QPushButton("취소", dlg)
        apply_qss_role(btn_cancel, role="secondary")
        btn_cancel.clicked.connect(dlg.reject)
        btn_apply = QPushButton("적용 후 닫기", dlg)
        apply_qss_role(btn_apply, role="primary")
        btn_apply.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_apply)
        lay.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            updated_text = editor.toPlainText()
            if updated_text != self.txt_notice_content.toPlainText():
                self.txt_notice_content.setPlainText(updated_text)
                self.ai_context_changed.emit()

    def _on_del_reply_field(self) -> None:
        rows = sorted({idx.row() for idx in self.table_reply_schema.selectedIndexes()}, reverse=True)
        if not rows and self.table_reply_schema.rowCount() > 0:
            rows = [self.table_reply_schema.rowCount() - 1]
        for r in rows:
            self.table_reply_schema.removeRow(r)
        self._sync_notice_fields_summary()

    def _on_notice_pdf_dropped(self, paths: List[str]) -> None:
        if not paths:
            return
        self._set_notice_pdf_file(paths[0], force_ai=False)

    def _on_load_notice_pdf(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "가정통신문 문서 선택",
            "",
            "가정통신문 파일 (*.pdf *.hwp *.hwpx *.png *.jpg *.jpeg *.webp)",
        )
        if not file_path:
            return
        self._set_notice_pdf_file(file_path, force_ai=False)

    def _on_reextract_notice_pdf(self) -> None:
        content_text = self.txt_notice_content.toPlainText().strip()
        if not self._selected_notice_pdf_path and not content_text:
            QMessageBox.information(
                self,
                "추출 안내",
                "먼저 가정통신문 문서(PDF/HWP/HWPX/이미지)를 선택하거나 본문 내용을 입력해 주세요.",
            )
            return
        self._run_notice_ai_extraction(
            file_path=self._selected_notice_pdf_path,
            override_text=content_text,
        )

    def _set_notice_pdf_file(self, file_path: str, *, force_ai: bool = False) -> None:
        self._selected_notice_pdf_path = str(file_path).strip()
        self._selected_notice_attachment_file_id = ""
        self._notice_pdf_field_map = []
        if hasattr(self, "edit_notice_attachment_url"):
            self.edit_notice_attachment_url.clear()
        fname = Path(self._selected_notice_pdf_path).name

        if not force_ai:
            try:
                info = inspect_notice_file_locally(self._selected_notice_pdf_path)
            except Exception as exc:
                QMessageBox.warning(self, "파일 파싱 오류", str(exc))
                return

            if not info.get("needs_ai_ocr"):
                local_text = str(info.get("text") or "").strip()
                self.txt_notice_content.setPlainText(local_text)
                if info.get("title"):
                    self.edit_notice_title.setText(str(info["title"]))
                if info.get("due_date"):
                    self.edit_notice_due.setText(str(info["due_date"]))
                if info.get("category"):
                    self.combo_notice_category.setCurrentText(str(info["category"]))

                if hasattr(self, "lbl_notice_content_state"):
                    self.lbl_notice_content_state.setText("로컬 파싱 완료" if local_text else "로컬 텍스트 없음")
                if local_text:
                    self.lbl_notice_pdf_status.setText(
                        f"로컬 파싱 완료: {fname} — 본문 확인·수정 후 필요 시 [AI OCR 추출]을 누르세요."
                    )
                else:
                    self.lbl_notice_pdf_status.setText(
                        f"파일 선택됨: {fname} — 로컬 텍스트를 추출하지 못했습니다. 본문을 입력하거나 [AI OCR 추출]을 누르세요."
                    )
                self.ai_context_changed.emit()
                return

            # 이미지 파일 또는 스캔/이미지 PDF -> 본문 비우고 즉시 AI OCR 흐름 진입
            self.txt_notice_content.clear()
            if hasattr(self, "lbl_notice_content_state"):
                self.lbl_notice_content_state.setText("AI OCR 대기")
            self._run_notice_ai_extraction(file_path=self._selected_notice_pdf_path, override_text="")
            return

        self._run_notice_ai_extraction(
            file_path=self._selected_notice_pdf_path,
            override_text=self.txt_notice_content.toPlainText().strip(),
        )

    def _dispatch_to_ai_dock_if_non_api(
        self,
        *,
        target_id: str,
        status_label: Optional[QLabel],
        manual_msg: str,
        pw_msg: str,
        empty_msg: str = "",
    ) -> bool:
        """AI 실행 패널이 수동 복붙(1) 또는 Playwright(2) 모드이면 패널 실행으로 위임하고 True를 반환합니다."""
        win = self.window()
        ai_dock = getattr(win, "ai_dock", None) if win is not None else None
        if ai_dock is not None and hasattr(ai_dock, "current_mode_index"):
            mode_idx = int(ai_dock.current_mode_index())
        else:
            mode_idx = 1 if MultiLlmClient().is_manual_mode_active() else 0

        if mode_idx == 1:
            if ai_dock is not None and hasattr(ai_dock, "prepare_manual_prompt_for_target"):
                if hasattr(win, "toggle_ai_dock") and not getattr(win, "_ai_dock_expanded", True):
                    win.toggle_ai_dock(True)
                prompt_text = ai_dock.prepare_manual_prompt_for_target(target_id)
                if status_label is not None:
                    status_label.setText(manual_msg if prompt_text or not empty_msg else empty_msg)
            elif status_label is not None:
                status_label.setText(manual_msg)
            return True

        if mode_idx == 2 and ai_dock is not None:
            if hasattr(win, "toggle_ai_dock") and not getattr(win, "_ai_dock_expanded", True):
                win.toggle_ai_dock(True)
            ai_dock.set_target(target_id, force=True)
            ai_dock._on_pw_start_clicked()
            if status_label is not None:
                status_label.setText(pw_msg)
            return True

        return False

    def _run_notice_ai_extraction(self, *, file_path: str = "", override_text: str = "") -> None:
        self.ai_context_changed.emit()
        self.ai_target_requested.emit("notice_ocr")

        fname = Path(file_path).name if file_path else "직접 입력 본문"
        if self._dispatch_to_ai_dock_if_non_api(
            target_id="notice_ocr",
            status_label=self.lbl_notice_pdf_status,
            manual_msg=f"수동 복붙 대기: {fname} — 우측 AI 실행 패널에 프롬프트를 띄웠습니다. 복붙 후 [응답 회수]를 눌러 반영하세요.",
            pw_msg=f"Playwright 자동 추출 실행: {fname} — 우측 AI 실행 패널 진행 상태를 확인하세요.",
            empty_msg=f"선택된 대상: {fname} — 프롬프트를 생성할 본문 또는 파일이 없습니다.",
        ):
            return

        client = MultiLlmClient()
        if client.is_manual_mode_active():
            self.lbl_notice_pdf_status.setText(
                f"선택된 대상: {fname} → API Key가 없어 수동 모드로 대기 중입니다. 우측 AI 실행 패널에서 진행하세요."
            )
            return

        self.lbl_notice_pdf_status.setText(f"AI 분석 중: {fname} ...")
        pdf_snapshot = file_path
        text_snapshot = str(override_text or "").strip()

        def _work() -> Dict[str, Any]:
            if text_snapshot:
                if pdf_snapshot:
                    return extract_notice_from_pdf(
                        pdf_snapshot, llm_client=client, override_text=text_snapshot
                    )
                return extract_notice_from_text(text_snapshot, llm_client=client)
            return extract_notice_from_pdf(pdf_snapshot, llm_client=client)

        def _on_result(parsed: Any) -> None:
            if isinstance(parsed, dict):
                self.apply_ai_dock_result({"target_id": "notice_ocr", "mode": "api", "parsed_notice": parsed})

        self._run_async_button_task(
            button=getattr(self, "btn_reextract_notice_pdf", None),
            loading_text="AI 추출 중...",
            task_name="가정통신문 AI OCR·구조화 추출",
            task_key="classroom_extract_notice_pdf",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: (
                self.lbl_notice_pdf_status.setText(f"AI 추출 오류 ({fname}): {str(err or '').splitlines()[0]}"),
                QMessageBox.warning(self, "가정통신문 AI 추출 오류", str(err or "").splitlines()[0]),
            ),
        )

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
        rows_snapshot = list(self._parsed_excel_timetable_rows)

        def _work() -> bool:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return bool(svc.upsert_timetable_base(rows_snapshot))

        def _on_result(ok: Any) -> None:
            if ok:
                QMessageBox.information(
                    self,
                    "업로드 완료",
                    f"Firestore `시간표_기본`에 {len(rows_snapshot)}건(source=excel)이 반영되었습니다.",
                )
            else:
                QMessageBox.warning(self, "업로드 실패", "Firestore 시간표_기본 반영에 실패했습니다.")

        self._run_async_button_task(
            button=getattr(self, "btn_tt_upload_excel", None),
            loading_text="업로드 중...",
            task_name="시간표_기본 업로드",
            task_key="classroom_upload_timetable_excel",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "업로드 실패", str(err or "").splitlines()[0]),
        )

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

        def _work() -> bool:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return bool(svc.push_timetable_changes([row]))

        def _on_result(ok: Any) -> None:
            if ok:
                QMessageBox.information(
                    self,
                    "저장 완료",
                    f"{row['date']} {row['period']}교시 변동/보강({row['changed_subject']})이 Firestore에 저장되었습니다.",
                )
            else:
                QMessageBox.warning(self, "저장 실패", "Firestore 시간표_변동 저장에 실패했습니다.")

        self._run_async_button_task(
            button=getattr(self, "btn_tt_save_change", None),
            loading_text="저장 중...",
            task_name="시간표_변동 저장",
            task_key="classroom_save_timetable_change",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "저장 실패", str(err or "").splitlines()[0]),
        )

    def _on_notice_requires_reply_toggled(self, checked: bool) -> None:
        if not hasattr(self, "combo_notice_signature_mode"):
            return
        self.combo_notice_signature_mode.blockSignals(True)
        try:
            if not checked:
                cur_mode = str(self.combo_notice_signature_mode.currentData() or "").strip().lower()
                if cur_mode in ("student", "parent", "both"):
                    self._last_active_signature_mode = cur_mode
                idx = self.combo_notice_signature_mode.findData("none")
                if idx >= 0:
                    self.combo_notice_signature_mode.setCurrentIndex(idx)
            else:
                cur_mode = str(self.combo_notice_signature_mode.currentData() or "none").strip().lower()
                if cur_mode == "none":
                    target_mode = getattr(self, "_last_active_signature_mode", "parent")
                    if target_mode not in ("student", "parent", "both"):
                        target_mode = "parent"
                    idx = self.combo_notice_signature_mode.findData(target_mode)
                    if idx >= 0:
                        self.combo_notice_signature_mode.setCurrentIndex(idx)
        finally:
            self.combo_notice_signature_mode.blockSignals(False)

    def _on_notice_signature_mode_changed(self, _index: int) -> None:
        if not hasattr(self, "chk_notice_requires_reply") or not hasattr(self, "combo_notice_signature_mode"):
            return
        mode = str(self.combo_notice_signature_mode.currentData() or "none").strip().lower()
        if mode in ("student", "parent", "both"):
            self._last_active_signature_mode = mode
        self.chk_notice_requires_reply.blockSignals(True)
        try:
            self.chk_notice_requires_reply.setChecked(mode != "none")
        finally:
            self.chk_notice_requires_reply.blockSignals(False)

    def _build_notice_payload(self, *, is_published: bool) -> Dict[str, Any]:
        meta = self._teacher_meta()
        reply_schema = self._collect_reply_schema_from_table()
        reply_fields = [
            str(item.get("label") or item.get("key") or "").strip()
            for item in reply_schema
            if str(item.get("label") or item.get("key") or "").strip()
        ]
        target_grade = int(self.spin_notice_target_grade.value())
        target_class = int(self.spin_notice_target_class.value())
        requires_reply = bool(self.chk_notice_requires_reply.isChecked())
        sig_mode = (
            str(self.combo_notice_signature_mode.currentData() or ("parent" if requires_reply else "none")).strip().lower()
            if hasattr(self, "combo_notice_signature_mode")
            else ("parent" if requires_reply else "none")
        )
        if not requires_reply:
            sig_mode = "none"
        elif sig_mode == "none":
            sig_mode = "parent"
        return {
            "title": self.edit_notice_title.text().strip(),
            "category": self.combo_notice_category.currentText().strip() or "가정통신문",
            "content": self.txt_notice_content.toPlainText().strip(),
            "due_date": self.edit_notice_due.text().strip(),
            "requires_reply": requires_reply,
            "signature_mode": sig_mode,
            "reply_schema": reply_schema,
            "reply_schema_json": json.dumps(reply_schema, ensure_ascii=False),
            "reply_fields": reply_fields,
            "pdf_field_map": list(getattr(self, "_notice_pdf_field_map", []) or []),
            "pdf_field_map_json": json.dumps(
                list(getattr(self, "_notice_pdf_field_map", []) or []), ensure_ascii=False
            ),
            "attachment_url": self.edit_notice_attachment_url.text().strip(),
            "attachment_file_id": getattr(self, "_selected_notice_attachment_file_id", ""),
            "target_grade": target_grade,
            "target_class": target_class,
            "grade": target_grade,
            "class_no": target_class,
            "school_code": meta["school_code"],
            "school_name": meta["school_name"],
            "author_name": meta["teacher_name"],
            "is_published": bool(is_published),
        }

    def _has_primary_drive_config(self) -> bool:
        api = self.config.get_section("api_and_integrations") or {}
        drive_priority = str(api.get("drive_priority") or "workspace").strip().lower()
        if drive_priority == "personal":
            personal_gas_url = str(
                api.get("personal_gas_url") or api.get("google_apps_script_url") or ""
            ).strip()
            return bool(personal_gas_url)
        ws_folder_id = str(api.get("workspace_drive_folder_id") or "").strip()
        return bool(ws_folder_id)

    def _on_publish_mobile_notice(self) -> None:
        requires_reply = bool(
            hasattr(self, "chk_notice_requires_reply") and self.chk_notice_requires_reply.isChecked()
        )
        sig_mode = (
            str(
                self.combo_notice_signature_mode.currentData()
                or ("parent" if requires_reply else "none")
            ).strip().lower()
            if hasattr(self, "combo_notice_signature_mode")
            else ("parent" if requires_reply else "none")
        )
        if not requires_reply:
            sig_mode = "none"
        elif sig_mode == "none":
            sig_mode = "parent"

        if sig_mode != "none" and not self._has_primary_drive_config():
            reply = QMessageBox.question(
                self,
                "Google Drive 서명 저장 설정 확인",
                "Google Drive 서명 저장 설정(공유 드라이브 폴더 ID 또는 개인 GAS URL)이 되어 있지 않아 "
                "학생·학부모 서명 제출 및 가정통신문 사본 서명 인쇄 기능이 작동하지 않을 수 있습니다. "
                "환경설정에서 먼저 설정해 주세요. 그래도 게시하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._save_mobile_notice(is_published=True)

    def _save_mobile_notice(self, *, is_published: bool, skip_attachment: bool = False) -> None:
        title = self.edit_notice_title.text().strip()
        if not title:
            QMessageBox.warning(self, "입력 확인", "가정통신문 제목을 입력해 주세요.")
            return
        payload = self._build_notice_payload(is_published=is_published)
        local_src_path = "" if skip_attachment else str(getattr(self, "_selected_notice_pdf_path", "") or "").strip()
        target_btn = (
            getattr(self, "btn_publish_mobile_notice", None)
            if is_published
            else getattr(self, "btn_save_notice_candidate", None)
        )

        def _work() -> Tuple[bool, str, str, str, str]:
            import shutil
            from services.notice_parser_service import convert_hwp_to_pdf_with_reason

            svc = FirestoreSyncService(db=self.db, config=self.config)
            uploaded_url = str(payload.get("attachment_url") or "").strip()
            uploaded_id = str(payload.get("attachment_file_id") or "").strip()
            upload_err = ""
            if not skip_attachment and not uploaded_url and local_src_path and Path(local_src_path).is_file():
                converted_pdf: Optional[Path] = None
                upload_target_path = local_src_path
                try:
                    if Path(local_src_path).suffix.lower() in (".hwp", ".hwpx"):
                        converted_pdf, conv_err = convert_hwp_to_pdf_with_reason(local_src_path)
                        if converted_pdf is None or not converted_pdf.is_file():
                            reason_txt = conv_err or "한컴오피스 PDF 변환에 실패했습니다."
                            return False, "", "", "", f"PDF_CONVERT_FAILED:{reason_txt}"
                        upload_target_path = str(converted_pdf)

                    up_res = svc.upload_notice_attachment(upload_target_path)
                    if up_res.get("ok") and up_res.get("attachment_url"):
                        uploaded_url = str(up_res["attachment_url"]).strip()
                        uploaded_id = str(up_res.get("attachment_file_id") or "").strip()
                        payload["attachment_url"] = uploaded_url
                        payload["attachment_file_id"] = uploaded_id
                    else:
                        upload_err = str(up_res.get("error") or "원본 파일 업로드에 실패했습니다.").strip()
                except Exception as exc:
                    upload_err = str(exc)
                finally:
                    if converted_pdf is not None:
                        shutil.rmtree(converted_pdf.parent, ignore_errors=True)

            ok, rid = svc.publish_mobile_notice(payload)
            if ok and is_published:
                due_txt = f" (마감: {payload['due_date']})" if payload.get("due_date") else ""
                svc.send_class_notification(
                    title=f"[{payload['category']}] {title}",
                    message=(payload.get("content") or title)[:120] + due_txt,
                    target_role="all",
                    grade=payload["target_grade"],
                    class_no=payload["target_class"],
                    school_code=payload["school_code"],
                    school_name=payload["school_name"],
                    sender=payload["author_name"],
                    channel="notice_publish",
                    notice_id=rid,
                )
            return ok, rid, uploaded_url, uploaded_id, upload_err

        def _on_result(res: Any) -> None:
            upload_err = ""
            if isinstance(res, tuple) and len(res) >= 5:
                ok, rid, uploaded_url, uploaded_id, upload_err = res[:5]
            elif isinstance(res, tuple) and len(res) >= 4:
                ok, rid, uploaded_url, uploaded_id = res[:4]
            elif isinstance(res, tuple) and len(res) >= 2:
                ok, rid, uploaded_url, uploaded_id = res[0], res[1], "", ""
            else:
                ok, rid, uploaded_url, uploaded_id = False, "", "", ""

            if str(upload_err or "").startswith("PDF_CONVERT_FAILED:"):
                conv_reason = str(upload_err).split(":", 1)[1].strip()
                self.lbl_notice_pdf_status.setText(f"PDF 변환 실패 — 업로드 중단 ({conv_reason})")
                ans = QMessageBox.question(
                    self,
                    "PDF 변환 실패",
                    "모바일에서 원문 PDF 보기를 위해 PDF 변환이 필요합니다. "
                    "한컴오피스 변환에 실패했으므로 PDF로 직접 변환해 첨부하거나 다시 시도해 주세요.\n\n"
                    f"실패 사유: {conv_reason}\n\n"
                    "원문 PDF 첨부 없이 본문·회신 양식만 저장할까요?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if ans == QMessageBox.StandardButton.Yes:
                    self._save_mobile_notice(is_published=is_published, skip_attachment=True)
                return

            if uploaded_url:
                self.edit_notice_attachment_url.setText(uploaded_url)
                self._selected_notice_attachment_file_id = uploaded_id

            if ok:
                self.edit_active_notice_id.setText(str(rid or ""))
                self._on_refresh_published_notices(select_notice_id=str(rid or ""))
                warn_tail = (
                    f"\n\n※ 단, 원본 파일 Google Drive 업로드는 실패하여 본문·회신 양식만 저장되었습니다.\n(사유: {upload_err})"
                    if upload_err
                    else ""
                )
                if is_published:
                    self.lbl_notice_pdf_status.setText(f"바로 게시 완료 (ID: {rid}) — Notifications 알림 등록됨")
                    QMessageBox.information(
                        self,
                        "게시 완료",
                        f"모바일 가정통신문이 즉시 게시되고 대상 학급 알림이 등록되었습니다.\n(ID: {rid}){warn_tail}",
                    )
                else:
                    self.lbl_notice_pdf_status.setText(f"모바일 후보 저장 완료 (ID: {rid}, is_published=false)")
                    QMessageBox.information(
                        self,
                        "후보 저장 완료",
                        f"가정통신문이 모바일 후보(is_published=false)로 저장되었습니다.\n(ID: {rid}){warn_tail}",
                    )
            else:
                QMessageBox.warning(self, "저장 실패", "가정통신문 저장에 실패했습니다.")

        self._run_async_button_task(
            button=target_btn,
            loading_text="게시 중..." if is_published else "저장 중...",
            task_name="가정통신문 바로 게시" if is_published else "가정통신문 후보 저장",
            task_key="classroom_publish_mobile_notice" if is_published else "classroom_save_notice_candidate",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "저장 오류", str(err or "").splitlines()[0]),
        )

    def _apply_loaded_notice_doc(self, notice_doc: Dict[str, Any]) -> None:
        """Firestore `가정통신문_목록` 문서 내용을 데스크톱 UI에 복원합니다."""
        if not isinstance(notice_doc, dict) or not notice_doc:
            return
        if hasattr(self, "edit_notice_title"):
            self.edit_notice_title.setText(str(notice_doc.get("title") or "").strip())
        if hasattr(self, "edit_notice_due"):
            self.edit_notice_due.setText(str(notice_doc.get("due_date") or "").strip())
        if hasattr(self, "combo_notice_category"):
            cat = str(notice_doc.get("category") or "가정통신문").strip() or "가정통신문"
            idx = self.combo_notice_category.findText(cat)
            if idx >= 0:
                self.combo_notice_category.setCurrentIndex(idx)
            elif self.combo_notice_category.isEditable():
                self.combo_notice_category.setCurrentText(cat)
        if hasattr(self, "txt_notice_content"):
            self.txt_notice_content.setPlainText(str(notice_doc.get("content") or "").strip())

        schema = notice_doc.get("reply_schema")
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except Exception:
                schema = []
        if not isinstance(schema, list):
            schema = []
        self._populate_reply_schema_table(schema)

        raw_sig_mode = str(notice_doc.get("signature_mode") or "").strip().lower()
        requires_reply = bool(notice_doc.get("requires_reply", bool(schema)))
        if not requires_reply and not raw_sig_mode:
            raw_sig_mode = "none"
        target_sig_mode = raw_sig_mode or ("parent" if requires_reply else "none")
        if hasattr(self, "combo_notice_signature_mode"):
            self.combo_notice_signature_mode.blockSignals(True)
            try:
                sig_idx = self.combo_notice_signature_mode.findData(target_sig_mode)
                if sig_idx >= 0:
                    self.combo_notice_signature_mode.setCurrentIndex(sig_idx)
            finally:
                self.combo_notice_signature_mode.blockSignals(False)
        if hasattr(self, "chk_notice_requires_reply"):
            self.chk_notice_requires_reply.blockSignals(True)
            try:
                self.chk_notice_requires_reply.setChecked(requires_reply)
            finally:
                self.chk_notice_requires_reply.blockSignals(False)

        if hasattr(self, "edit_notice_attachment_url"):
            self.edit_notice_attachment_url.setText(str(notice_doc.get("attachment_url") or "").strip())
        self._selected_notice_attachment_file_id = str(notice_doc.get("attachment_file_id") or "").strip()

        pdf_map = notice_doc.get("pdf_field_map")
        if isinstance(pdf_map, str):
            try:
                pdf_map = json.loads(pdf_map)
            except Exception:
                pdf_map = []
        if not isinstance(pdf_map, list):
            pdf_map = []
        self._notice_pdf_field_map = list(pdf_map)
        if hasattr(self, "lbl_notice_pdf_status"):
            self.lbl_notice_pdf_status.setText(
                f"게시된 통신문 불러옴 — PDF 양식 좌표 {len(self._notice_pdf_field_map)}개 · 회신 항목 {len(schema)}개 복원됨"
            )

    def _populate_published_notices_combo(
        self,
        notices: List[Dict[str, Any]],
        select_notice_id: str = "",
    ) -> None:
        if not hasattr(self, "combo_published_notices"):
            return
        self.combo_published_notices.blockSignals(True)
        try:
            self.combo_published_notices.clear()
            self.combo_published_notices.addItem("— 게시한 가정통신문 선택 —", "")
            for n in list(notices or []):
                if not isinstance(n, dict):
                    continue
                nid = str(n.get("remote_id") or "").strip()
                if not nid:
                    continue
                title = str(n.get("title") or "(제목 없음)").strip()
                date_part = str(
                    n.get("due_date") or n.get("updated_at") or n.get("created_at") or ""
                ).strip()[:10]
                status_txt = "게시됨" if n.get("is_published", True) else "후보"
                meta_txt = f"{date_part}/{status_txt}" if date_part else status_txt
                self.combo_published_notices.addItem(f"[{title}] ({meta_txt})", nid)
            if select_notice_id:
                idx = self.combo_published_notices.findData(select_notice_id)
                if idx >= 0:
                    self.combo_published_notices.setCurrentIndex(idx)
        finally:
            self.combo_published_notices.blockSignals(False)

    def _on_refresh_published_notices(self, *, select_notice_id: str = "") -> None:
        if not hasattr(self, "combo_published_notices"):
            return
        target_id = str(
            select_notice_id
            or (self.edit_active_notice_id.text().strip() if hasattr(self, "edit_active_notice_id") else "")
        ).strip()

        def _work() -> List[Dict[str, Any]]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return svc.fetch_published_notices(limit=30)

        def _on_result(rows: Any) -> None:
            self._populate_published_notices_combo(
                list(rows or []),
                select_notice_id=target_id,
            )

        self._run_async_button_task(
            button=getattr(self, "btn_refresh_published_notices", None),
            loading_text="불러오는 중...",
            task_name="게시한 가정통신문 목록 조회",
            task_key="classroom_refresh_published_notices",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda _err: None,
        )

    def _on_published_notice_selected(self, _index: int) -> None:
        if not hasattr(self, "combo_published_notices"):
            return
        nid = str(self.combo_published_notices.currentData() or "").strip()
        if not nid:
            return
        if hasattr(self, "edit_active_notice_id"):
            self.edit_active_notice_id.setText(nid)
        self._on_refresh_notice_replies()

    def _on_refresh_notice_replies(self) -> None:
        nid = self.edit_active_notice_id.text().strip()
        if not nid:
            QMessageBox.information(self, "조회 안내", "먼저 가정통신문 ID를 입력하거나 배포해 주세요.")
            return

        def _work() -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            replies = svc.fetch_notice_replies(nid)
            notice_doc = svc.fetch_notice_detail(nid)
            return replies, notice_doc

        def _on_result(res: Any) -> None:
            if isinstance(res, tuple) and len(res) >= 2:
                replies, notice_doc = res[0], res[1]
            else:
                replies, notice_doc = res, None

            if isinstance(notice_doc, dict) and notice_doc:
                self._apply_loaded_notice_doc(notice_doc)

            self._last_notice_replies = list(replies or [])
            rep_map = {str(r.get("student_id") or ""): r for r in self._last_notice_replies}
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
                self.table_notice_replies.setItem(
                    r,
                    4,
                    QTableWidgetItem(
                        str(
                            rep.get("signature_drive_url")
                            or rep.get("student_signature_drive_url")
                            or rep.get("signature_url")
                            or ""
                        )
                        if rep
                        else ""
                    ),
                )

        self._run_async_button_task(
            button=getattr(self, "btn_refresh_replies", None),
            loading_text="조회 중...",
            task_name="가정통신문 회신 조회",
            task_key="classroom_refresh_notice_replies",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "회신 조회 오류", str(err or "").splitlines()[0]),
        )

    def _open_notice_pdf_mapper_with_path(self, src: str) -> None:
        from ui.dialogs.notice_pdf_mapper_dialog import NoticePdfMapperDialog

        schema = self._collect_reply_schema_from_table()
        win = self.window()
        ai_dock = getattr(win, "ai_dock", None) if win is not None else None
        try:
            dlg = NoticePdfMapperDialog(
                pdf_path=src,
                reply_schema=schema,
                initial_field_map=self._notice_pdf_field_map,
                task_runner=self.task_runner,
                ai_dock=ai_dock,
                parent=self,
            )
        except Exception as exc:
            QMessageBox.warning(self, "양식 열기 오류", str(exc))
            return

        def _on_accepted() -> None:
            self._notice_pdf_field_map = dlg.get_field_map()
            updated_schema = dlg.get_reply_schema()
            self._populate_reply_schema_table(updated_schema)
            if updated_schema:
                self.chk_notice_requires_reply.setChecked(True)
            active_nid = (
                self.edit_active_notice_id.text().strip()
                if hasattr(self, "edit_active_notice_id")
                else ""
            )
            if active_nid:
                def _save_map_work() -> bool:
                    svc = FirestoreSyncService(db=self.db, config=self.config)
                    return svc.update_notice_pdf_field_map(
                        active_nid,
                        self._notice_pdf_field_map,
                        reply_schema=updated_schema,
                    )

                def _save_map_done(saved: Any) -> None:
                    if saved:
                        self.lbl_notice_pdf_status.setText(
                            f"PDF 양식 좌표 {len(self._notice_pdf_field_map)}개 · 회신 항목 {len(updated_schema)}개 Firestore 반영 완료 (ID: {active_nid})"
                        )
                    else:
                        self.lbl_notice_pdf_status.setText(
                            f"PDF 양식 좌표 {len(self._notice_pdf_field_map)}개 동기화됨 (Firestore 저장 실패 — 게시/저장으로 재시도 가능)"
                        )

                self._run_async_button_task(
                    button=getattr(self, "btn_map_notice_pdf", None),
                    loading_text="좌표 저장 중...",
                    task_name="PDF 양식 좌표 저장",
                    task_key="classroom_save_notice_pdf_map",
                    work_fn=_save_map_work,
                    on_result=_save_map_done,
                    on_error=lambda err: QMessageBox.warning(
                        self, "좌표 저장 오류", str(err or "").splitlines()[0]
                    ),
                )
            else:
                self.lbl_notice_pdf_status.setText(
                    f"PDF 양식 좌표 {len(self._notice_pdf_field_map)}개 · 회신 항목 {len(updated_schema)}개 동기화 완료 — 저장/게시 시 함께 반영됩니다."
                )

        dlg.accepted.connect(_on_accepted)
        dlg.setModal(False)
        self._active_pdf_mapper_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_open_notice_pdf_mapper(self) -> None:
        src = str(getattr(self, "_selected_notice_pdf_path", "") or "").strip()
        if src and Path(src).is_file():
            self._open_notice_pdf_mapper_with_path(src)
            return

        att_url = self.edit_notice_attachment_url.text().strip() if hasattr(self, "edit_notice_attachment_url") else ""
        att_fid = str(getattr(self, "_selected_notice_attachment_file_id", "") or "").strip()
        if not att_url and not att_fid:
            QMessageBox.information(
                self,
                "양식 파일 필요",
                "먼저 PDF, HWP 또는 HWPX 가정통신문 양식 파일을 선택해 주세요.",
            )
            return

        def _dl_work() -> Optional[Path]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return svc.download_notice_pdf(attachment_url=att_url, attachment_file_id=att_fid)

        def _dl_done(pdf_path: Any) -> None:
            if pdf_path is not None and Path(pdf_path).is_file():
                self._selected_notice_pdf_path = str(pdf_path)
                self._open_notice_pdf_mapper_with_path(str(pdf_path))
            else:
                QMessageBox.warning(
                    self,
                    "원본 PDF 다운로드 실패",
                    "Google Drive에서 원본 PDF를 내려받지 못했습니다. 로컬 PDF/HWP 파일을 직접 선택해 주세요.",
                )

        self._run_async_button_task(
            button=getattr(self, "btn_map_notice_pdf", None),
            loading_text="PDF 내려받는 중...",
            task_name="가정통신문 원본 PDF 다운로드",
            task_key="classroom_download_notice_pdf_mapper",
            work_fn=_dl_work,
            on_result=_dl_done,
            on_error=lambda err: QMessageBox.warning(self, "다운로드 오류", str(err or "").splitlines()[0]),
        )

    def _on_export_filled_notice_pdfs(self) -> None:
        src = str(getattr(self, "_selected_notice_pdf_path", "") or "").strip()
        att_url = self.edit_notice_attachment_url.text().strip() if hasattr(self, "edit_notice_attachment_url") else ""
        att_fid = str(getattr(self, "_selected_notice_attachment_file_id", "") or "").strip()
        if (not src or not Path(src).is_file()) and not (att_url or att_fid):
            QMessageBox.information(
                self,
                "양식 파일 필요",
                "합성에 사용할 PDF/HWP/HWPX 원본 양식 파일을 먼저 선택해 주세요.",
            )
            return
        if not self._notice_pdf_field_map:
            ans = QMessageBox.question(
                self,
                "좌표 매핑 없음",
                "저장된 PDF 양식 좌표가 없습니다. 지금 [PDF 양식 위치 지정] 창을 열까요?",
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._on_open_notice_pdf_mapper()
            if not self._notice_pdf_field_map:
                return

        nid = self.edit_active_notice_id.text().strip()
        if not nid and not self._last_notice_replies:
            QMessageBox.information(
                self,
                "회신 내역 필요",
                "먼저 가정통신문 ID를 입력해 [제출 현황 조회]를 실행해 주세요.",
            )
            return

        box = QMessageBox(self)
        box.setWindowTitle("응답 취합 PDF 출력 방식 선택")
        box.setText("학급 응답을 어떤 형식으로 출력할까요?")
        btn_indiv = box.addButton("학생별 개별 PDF 폴더 저장", QMessageBox.ButtonRole.AcceptRole)
        btn_merged = box.addButton("전체 학급 통합 PDF 1개 저장", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (btn_indiv, btn_merged):
            return

        if clicked == btn_indiv:
            export_mode = "individual"
            out_target = QFileDialog.getExistingDirectory(self, "학생별 개별 PDF 저장 폴더 선택")
        else:
            export_mode = "merged"
            default_name = f"{self.edit_notice_title.text().strip() or '가정통신문'}_취합본.pdf"
            out_target, _ = QFileDialog.getSaveFileName(
                self, "통합 PDF 저장", default_name, "PDF Files (*.pdf)"
            )
        if not out_target:
            return

        meta = self._teacher_meta()
        cached_replies = list(self._last_notice_replies or [])
        field_map = list(self._notice_pdf_field_map)
        sig_mode = (
            str(self.combo_notice_signature_mode.currentData() or "parent").strip().lower()
            if hasattr(self, "combo_notice_signature_mode")
            else "parent"
        )

        def _work() -> Dict[str, Any]:
            import shutil
            from services.notice_parser_service import convert_hwp_to_pdf_with_reason
            from services.notice_pdf_fill_service import batch_export_notice_pdfs

            replies = cached_replies
            if not replies and nid:
                svc = FirestoreSyncService(db=self.db, config=self.config)
                replies = svc.fetch_notice_replies(nid)
            if not replies:
                raise ValueError("출력할 제출 완료 회신 내역이 없습니다.")

            temp_dir: Optional[Path] = None
            pdf_src = Path(src) if src else Path("")
            try:
                if not src or not pdf_src.is_file():
                    svc_dl = FirestoreSyncService(db=self.db, config=self.config)
                    downloaded = svc_dl.download_notice_pdf(
                        attachment_url=att_url,
                        attachment_file_id=att_fid,
                    )
                    if downloaded is None or not downloaded.is_file():
                        raise RuntimeError("Google Drive에서 원본 PDF를 내려받지 못했습니다.")
                    temp_dir = downloaded.parent
                    pdf_src = downloaded
                elif pdf_src.suffix.lower() in (".hwp", ".hwpx"):
                    conv, conv_err = convert_hwp_to_pdf_with_reason(pdf_src)
                    if conv is None or not conv.is_file():
                        detail = f" ({conv_err})" if conv_err else ""
                        raise RuntimeError(f"HWP/HWPX 파일을 PDF로 변환하지 못했습니다.{detail}")
                    temp_dir = conv.parent
                    pdf_src = conv

                return batch_export_notice_pdfs(
                    source_pdf_path=pdf_src,
                    field_map=field_map,
                    replies=replies,
                    output_target=out_target,
                    mode=export_mode,
                    class_meta={
                        "grade": meta.get("grade"),
                        "class_no": meta.get("class_no"),
                    },
                    signature_mode=sig_mode,
                )
            finally:
                if temp_dir is not None:
                    shutil.rmtree(temp_dir, ignore_errors=True)

        def _on_result(res: Any) -> None:
            if not isinstance(res, dict):
                return
            cnt = int(res.get("count", 0))
            if res.get("mode") == "merged":
                QMessageBox.information(
                    self,
                    "통합 PDF 생성 완료",
                    f"총 {cnt}명 응답이 합쳐진 통합 PDF를 생성했습니다.\n\n저장 경로:\n{res.get('output_file')}",
                )
            else:
                QMessageBox.information(
                    self,
                    "개별 PDF 생성 완료",
                    f"총 {cnt}명의 학생별 PDF를 생성했습니다.\n\n저장 폴더:\n{res.get('output_dir')}",
                )

        self._run_async_button_task(
            button=getattr(self, "btn_export_filled_pdfs", None),
            loading_text="PDF 생성 중...",
            task_name="가정통신문 응답 취합 PDF 일괄 생성",
            task_key="classroom_export_filled_notice_pdfs",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "PDF 생성 오류", str(err or "").splitlines()[0]),
        )

    def _on_nudge_unsubmitted_notice(self) -> None:
        nid = self.edit_active_notice_id.text().strip()
        title = self.edit_notice_title.text().strip() or "가정통신문"
        if not nid:
            return
        meta = self._teacher_meta()

        def _work() -> Any:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return svc.send_class_notification(
                title=f"[미제출 안내] {title}",
                message="가정통신문 전자서명 회신이 아직 완료되지 않았습니다. 확인 후 제출해 주세요.",
                target_role="all",
                grade=meta["grade"],
                class_no=meta["class_no"],
                school_code=meta["school_code"],
                school_name=meta["school_name"],
                sender=meta["teacher_name"],
            )

        self._run_async_button_task(
            button=getattr(self, "btn_nudge_unsubmitted", None),
            loading_text="발송 중...",
            task_name="미제출 안내 알림 발송",
            task_key="classroom_nudge_unsubmitted_notice",
            work_fn=_work,
            on_result=lambda _res: QMessageBox.information(
                self, "알림 발송 완료", "미제출 안내 알림이 Firestore에 등록되었습니다."
            ),
            on_error=lambda err: QMessageBox.warning(self, "알림 발송 오류", str(err or "").splitlines()[0]),
        )

    def _on_send_class_broadcast(self) -> None:
        title = self.edit_broadcast_title.text().strip()
        msg = self.txt_broadcast_msg.toPlainText().strip()
        if not title or not msg:
            QMessageBox.warning(self, "입력 확인", "알림 제목과 내용을 모두 입력해 주세요.")
            return
        meta = self._teacher_meta()
        target_role = str(self.combo_broadcast_target.currentData() or "all")

        def _work() -> Any:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return svc.send_class_notification(
                title=title,
                message=msg,
                target_role=target_role,
                grade=meta["grade"],
                class_no=meta["class_no"],
                school_code=meta["school_code"],
                school_name=meta["school_name"],
                sender=meta["teacher_name"],
            )

        def _on_result(_res: Any) -> None:
            self.edit_broadcast_title.clear()
            self.txt_broadcast_msg.clear()
            self._on_refresh_broadcast_history()
            QMessageBox.information(self, "발송 완료", "학급 공지·긴급 알림이 발송되었습니다.")

        self._run_async_button_task(
            button=getattr(self, "btn_send_broadcast", None),
            loading_text="발송 중...",
            task_name="학급 공지·긴급 알림 발송",
            task_key="classroom_send_class_broadcast",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "발송 오류", str(err or "").splitlines()[0]),
        )

    def _on_refresh_broadcast_history(self) -> None:
        meta = self._teacher_meta()

        def _work() -> List[Dict[str, Any]]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return svc.fetch_sent_notifications(
                grade=meta["grade"],
                class_no=meta["class_no"],
                school_code=meta["school_code"],
                school_name=meta["school_name"],
            )

        def _on_result(rows: Any) -> None:
            self.table_broadcast_history.setRowCount(0)
            for item in (rows or []):
                r = self.table_broadcast_history.rowCount()
                self.table_broadcast_history.insertRow(r)
                self.table_broadcast_history.setItem(r, 0, QTableWidgetItem(str(item.get("created_at") or "")))
                self.table_broadcast_history.setItem(r, 1, QTableWidgetItem(str(item.get("target_role") or "all")))
                self.table_broadcast_history.setItem(r, 2, QTableWidgetItem(str(item.get("title") or "")))
                self.table_broadcast_history.setItem(r, 3, QTableWidgetItem(str(item.get("message") or "")))

        self._run_async_button_task(
            button=getattr(self, "btn_refresh_broadcast", None),
            loading_text="새로고침 중...",
            task_name="발송 이력 조회",
            task_key="classroom_refresh_broadcast_history",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "이력 조회 오류", str(err or "").splitlines()[0]),
        )

    # =========================================================================
    # 학교종이 수집 · 카톡 AI 공지 요약 · 검수 목록 · 학급_공지게시판 게시
    # =========================================================================
    def _on_fetch_schoolbell_notices(self) -> None:
        if self._active_schoolbell_worker is not None:
            return
        self.btn_fetch_schoolbell.setEnabled(False)
        self.btn_fetch_schoolbell.setText("수집 중...")
        self.btn_schoolbell_confirm_login.setVisible(False)
        self.lbl_schoolbell_status.setText("학교종이(schoolbell-e.com) 브라우저를 여는 중...")

        meta = self._teacher_meta()
        worker = SchoolbellCrawlWorker(
            max_items=15,
            login_timeout_sec=180,
            school_name=str(meta.get("school_name") or ""),
        )
        self._active_schoolbell_worker = worker

        def _on_login_req(msg: str) -> None:
            self.lbl_schoolbell_status.setText(msg)
            self.btn_schoolbell_confirm_login.setVisible(True)
            self.btn_schoolbell_confirm_login.setEnabled(True)
            self.schoolbell_login_required.emit(worker, msg)

        def _on_progress(_pct: int, msg: str) -> None:
            if msg:
                self.lbl_schoolbell_status.setText(msg)

        def _on_result(res: Any) -> None:
            data = res if isinstance(res, dict) else {}
            if data.get("missing_playwright"):
                self.lbl_schoolbell_status.setText("Playwright가 설치되지 않았습니다.")
                QMessageBox.warning(self, "Playwright 필요", str(data.get("error") or ""))
                return
            if not data.get("ok"):
                err = str(data.get("error") or "학교종이 공지를 가져오지 못했습니다.")
                self.lbl_schoolbell_status.setText(err)
                QMessageBox.warning(self, "학교종이 수집 실패", err)
                return
            notices = list(data.get("notices") or [])
            for n in notices:
                self._add_review_notice_card(
                    {
                        "title": n.get("title") or "",
                        "summary": n.get("summary") or "",
                        "content": n.get("content") or "",
                        "source_type": "schoolbell",
                        "source_label": n.get("source_label") or "학교종이",
                        "source_url": n.get("source_url") or "",
                        "published_at": n.get("published_at") or "",
                        "is_pinned": False,
                    }
                )
            self.lbl_schoolbell_status.setText(
                f"학교종이 공지 {len(notices)}건을 검수 목록에 추가했습니다 (자동 발송 안 됨)."
            )

        def _on_error(err: str) -> None:
            first = str(err or "").splitlines()[0]
            self.lbl_schoolbell_status.setText(f"학교종이 수집 오류: {first}")
            QMessageBox.warning(self, "학교종이 수집 오류", first)

        def _on_finished() -> None:
            self._active_schoolbell_worker = None
            self.btn_fetch_schoolbell.setEnabled(True)
            self.btn_fetch_schoolbell.setText("학교종이 최근 공지 가져오기")
            self.btn_schoolbell_confirm_login.setVisible(False)
            self.schoolbell_login_cleared.emit(self.lbl_schoolbell_status.text())

        worker.login_required.connect(_on_login_req)
        worker.signals.progress.connect(_on_progress)
        self.task_runner.start_worker(
            worker=worker,
            task_key="classroom_fetch_schoolbell",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=_on_finished,
        )

    def _on_schoolbell_confirm_login(self) -> None:
        if self._active_schoolbell_worker is not None:
            self._active_schoolbell_worker.confirm_login()
            self.btn_schoolbell_confirm_login.setEnabled(False)
            self.lbl_schoolbell_status.setText("로그인 확인 완료 — 공지 목록 추출 진행 중...")

    def _on_open_kakao_input_fullscreen(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("카톡 대화 · 공지 원문 전체화면 보기 / 편집")
        dlg.setWindowFlags(
            dlg.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        dlg.resize(920, 660)

        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        header = QHBoxLayout()
        lbl_info = QLabel("카톡·메신저 대화 원문을 넓게 조회·편집한 뒤 [적용 후 닫기]를 누르면 반영됩니다.")
        lbl_info.setStyleSheet("color: #475569; font-size: 12px;")
        header.addWidget(lbl_info)
        header.addStretch(1)
        lay.addLayout(header)

        editor = QPlainTextEdit(dlg)
        editor.setPlainText(self.txt_kakao_input.toPlainText())
        editor.setPlaceholderText("PC 카카오톡 대화 내보내기(.txt) 내용이나 공지 원문을 여기에 입력·편집하세요.")
        lay.addWidget(editor, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_cancel = QPushButton("취소", dlg)
        apply_qss_role(btn_cancel, role="secondary")
        btn_cancel.clicked.connect(dlg.reject)
        btn_apply = QPushButton("적용 후 닫기", dlg)
        apply_qss_role(btn_apply, role="primary")
        btn_apply.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_apply)
        lay.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            updated_text = editor.toPlainText()
            if updated_text != self.txt_kakao_input.toPlainText():
                self.txt_kakao_input.setPlainText(updated_text)
                self.lbl_kakao_status.setText(f"원문 편집 반영됨 ({len(updated_text):,}자)")
                self.ai_context_changed.emit()

    def _on_toggle_review_fullscreen(self, checked: Optional[bool] = None) -> None:
        is_full = bool(checked) if checked is not None else self.btn_toggle_review_fullscreen.isChecked()
        for w in (
            getattr(self, "_c4_broadcast_top_widget", None),
            getattr(self, "_c4_ingest_frame", None),
            getattr(self, "table_broadcast_history", None),
        ):
            if w is not None:
                w.setVisible(not is_full)
        if is_full:
            self.btn_toggle_review_fullscreen.setText("원래 크기로")
            self.scroll_review_cards.setMinimumHeight(460)
            self.scroll_review_cards.setMaximumHeight(16777215)
        else:
            self.btn_toggle_review_fullscreen.setText("전체화면 보기")
            self.scroll_review_cards.setMinimumHeight(180)
            self.scroll_review_cards.setMaximumHeight(320)

    def _load_kakao_file_path(self, file_path: str) -> None:
        try:
            text = read_kakao_export_file(file_path)
            self.txt_kakao_input.setPlainText(text)
            self.lbl_kakao_status.setText(f"불러옴: {Path(file_path).name} ({len(text):,}자)")
        except Exception as exc:
            QMessageBox.warning(self, "파일 읽기 오류", f"카톡 .txt 파일을 읽지 못했습니다:\n{exc}")

    def _on_load_kakao_txt_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "카카오톡 대화 내보내기(.txt) 선택", "", "텍스트 파일 (*.txt);;모든 파일 (*.*)"
        )
        if path:
            self._load_kakao_file_path(path)

    def _on_kakao_file_dropped(self, paths: List[str]) -> None:
        for p in paths or []:
            if str(p).lower().endswith(".txt"):
                self._load_kakao_file_path(p)
                break

    def _on_digest_kakao_notices(self) -> None:
        raw_text = self.txt_kakao_input.toPlainText().strip()
        if not raw_text:
            QMessageBox.warning(self, "입력 확인", "카톡 .txt 파일을 불러오거나 대화·공지 텍스트를 입력해 주세요.")
            return
        cleaned = parse_kakao_chat_text(raw_text)
        if not cleaned:
            QMessageBox.warning(self, "추출 불가", "시스템 메시지·감탄사를 제외하면 추출할 대화 내용이 없습니다.")
            return
        source_label = self.edit_kakao_source_label.text().strip() or "카카오톡 대화"
        sys_p, usr_p = build_kakao_notice_digest_prompt(cleaned, source_label=source_label)
        self._active_notice_digest_ctx = {
            "target_id": "class_notice_digest",
            "kind": "class_notice_digest",
            "task_label": "카톡 대화 AI 공지 추출·요약",
            "system_prompt": sys_p,
            "user_prompt": usr_p,
            "source_label": source_label,
            "source_type": "kakao",
            "digest_action": "kakao_digest",
            "review_card_id": "",
        }
        self.ai_context_changed.emit()
        self.ai_target_requested.emit("class_notice_digest")

        if self._dispatch_to_ai_dock_if_non_api(
            target_id="class_notice_digest",
            status_label=self.lbl_kakao_status,
            manual_msg="수동 복붙 대기 — 우측 AI 실행 패널에 프롬프트를 띄우고 웹 AI를 열었습니다. [응답 회수]를 눌러 반영하세요.",
            pw_msg="Playwright 자동 공지 추출 실행 — 우측 AI 실행 패널 진행 상태를 확인하세요.",
        ):
            return

        client = MultiLlmClient()
        if client.is_manual_mode_active():
            self.lbl_kakao_status.setText(
                "프롬프트 준비 완료 → 우측 AI 실행 패널(수동 복붙/Playwright)에서 실행해 주세요."
            )
            return

        self.lbl_kakao_status.setText("AI 공지 추출·요약 중...")

        def _work() -> List[Dict[str, Any]]:
            return digest_kakao_text_with_ai(raw_text, source_label=source_label, llm_client=client)

        def _on_result(items: Any) -> None:
            item_list = list(items or [])
            for it in item_list:
                self._add_review_notice_card(
                    {
                        "title": it.get("title") or "",
                        "summary": it.get("summary") or "",
                        "content": it.get("content") or "",
                        "source_type": "kakao",
                        "source_label": it.get("source_label") or source_label,
                        "source_url": "",
                        "published_at": it.get("event_date") or "",
                        "is_pinned": False,
                    }
                )
            self.lbl_kakao_status.setText(
                f"AI 공지 {len(item_list)}건 추출 완료 — 아래 검수 목록에서 확인 후 게시하세요."
            )

        self._run_async_button_task(
            button=getattr(self, "btn_digest_kakao", None),
            loading_text="AI 요약 중...",
            task_name="카톡 대화 AI 공지 요약",
            task_key="classroom_digest_kakao_notices",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: (
                self.lbl_kakao_status.setText(f"AI 요약 오류: {str(err or '').splitlines()[0]}"),
                QMessageBox.warning(self, "AI 공지 요약 오류", str(err or "").splitlines()[0]),
            ),
        )

    def _on_summarize_review_card(self, card_id: str) -> None:
        entry = next((c for c in self._notice_review_cards if c["id"] == card_id), None)
        if not entry:
            return
        title = entry["edit_title"].text().strip()
        content = entry["txt_content"].toPlainText().strip()
        source_label = entry["edit_source_label"].text().strip() or "학교종이"
        published_at = str(entry.get("published_at") or "")
        if not content:
            QMessageBox.warning(self, "내용 확인", "요약할 공지 본문이 비어 있습니다.")
            return

        sys_p, usr_p = build_schoolbell_summary_prompt(
            title=title, content=content, source_label=source_label, published_at=published_at
        )
        self._active_notice_digest_ctx = {
            "target_id": "class_notice_digest",
            "kind": "class_notice_digest",
            "task_label": f"공지 3줄 요약 ({title[:20]})",
            "system_prompt": sys_p,
            "user_prompt": usr_p,
            "source_label": source_label,
            "source_type": entry.get("source_type") or "schoolbell",
            "digest_action": "schoolbell_summary",
            "review_card_id": card_id,
        }
        self.ai_context_changed.emit()
        self.ai_target_requested.emit("class_notice_digest")

        if self._dispatch_to_ai_dock_if_non_api(
            target_id="class_notice_digest",
            status_label=self.lbl_schoolbell_status,
            manual_msg="3줄 요약 수동 복붙 대기 — 우측 AI 실행 패널에 프롬프트를 띄우고 웹 AI를 열었습니다. [응답 회수]를 눌러 반영하세요.",
            pw_msg="3줄 요약 Playwright 자동 실행 — 우측 AI 실행 패널 진행 상태를 확인하세요.",
        ):
            return

        client = MultiLlmClient()
        if client.is_manual_mode_active():
            self.lbl_schoolbell_status.setText(
                "3줄 요약 프롬프트 준비됨 → 우측 AI 실행 패널(수동 복붙/Playwright)에서 실행하세요."
            )
            return

        btn = entry.get("btn_summarize")

        def _work() -> Dict[str, Any]:
            return summarize_schoolbell_notice_with_ai(
                {
                    "title": title,
                    "content": content,
                    "source_label": source_label,
                    "published_at": published_at,
                },
                llm_client=client,
            )

        def _on_result(res: Any) -> None:
            if isinstance(res, dict):
                self._apply_summary_to_card(card_id, res)

        self._run_async_button_task(
            button=btn,
            loading_text="요약 중...",
            task_name="공지 3줄 요약",
            task_key=f"classroom_summarize_card_{card_id}",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "3줄 요약 오류", str(err or "").splitlines()[0]),
        )

    def get_class_notice_ai_context(self) -> Dict[str, Any]:
        if self._active_notice_digest_ctx:
            return dict(self._active_notice_digest_ctx)
        raw_text = self.txt_kakao_input.toPlainText().strip() if hasattr(self, "txt_kakao_input") else ""
        cleaned = parse_kakao_chat_text(raw_text) if raw_text else ""
        source_label = (
            self.edit_kakao_source_label.text().strip()
            if hasattr(self, "edit_kakao_source_label")
            else "카카오톡 대화"
        ) or "카카오톡 대화"
        sys_p, usr_p = (
            build_kakao_notice_digest_prompt(cleaned, source_label=source_label)
            if cleaned
            else ("", "")
        )
        return {
            "target_id": "class_notice_digest",
            "kind": "class_notice_digest",
            "task_label": "카톡·학교종이 AI 공지 요약",
            "system_prompt": sys_p,
            "user_prompt": usr_p,
            "source_label": source_label,
            "source_type": "kakao",
            "digest_action": "kakao_digest",
            "review_card_id": "",
        }

    def apply_class_notice_ai_result(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        src_label = str(payload.get("source_label") or "카카오톡 대화").strip() or "카카오톡 대화"
        src_type = str(payload.get("source_type") or "kakao").strip().lower()
        action = str(payload.get("digest_action") or self._active_notice_digest_ctx.get("digest_action") or "kakao_digest")
        card_id = str(payload.get("review_card_id") or self._active_notice_digest_ctx.get("review_card_id") or "")

        items = payload.get("items")
        if not isinstance(items, list):
            raw = payload.get("raw_response") or payload.get("parsed_json") or ""
            if not raw:
                return
            try:
                items = parse_notice_digest_ai_response(raw, default_source_label=src_label)
            except Exception as exc:
                QMessageBox.warning(self, "공지 JSON 파싱 오류", f"AI 응답에서 공지 JSON을 해석하지 못했습니다:\n{exc}")
                return

        if action == "schoolbell_summary" and card_id and items:
            self._apply_summary_to_card(card_id, items[0])
            self.lbl_schoolbell_status.setText("선택한 공지 카드에 3줄 요약이 반영되었습니다.")
            return

        for it in items:
            self._add_review_notice_card(
                {
                    "title": it.get("title") or "",
                    "summary": it.get("summary") or "",
                    "content": it.get("content") or "",
                    "source_type": src_type if src_type in ("schoolbell", "kakao", "teacher") else "kakao",
                    "source_label": it.get("source_label") or src_label,
                    "source_url": "",
                    "published_at": it.get("event_date") or "",
                    "is_pinned": False,
                }
            )
        self.lbl_kakao_status.setText(
            f"AI 공지 {len(items)}건이 검수 목록에 추가되었습니다 (자동 발송 안 됨)."
        )

    def _apply_summary_to_card(self, card_id: str, data: Dict[str, Any]) -> None:
        entry = next((c for c in self._notice_review_cards if c["id"] == card_id), None)
        if not entry:
            return
        if data.get("title"):
            entry["edit_title"].setText(str(data["title"]))
        if data.get("summary"):
            entry["txt_summary"].setPlainText(str(data["summary"]))
        if data.get("content"):
            entry["txt_content"].setPlainText(str(data["content"]))

    def _update_review_count_label(self) -> None:
        total = len(self._notice_review_cards)
        checked = sum(1 for c in self._notice_review_cards if c["chk_select"].isChecked())
        self.lbl_review_count.setText(f"{checked}/{total}건 선택")
        self.lbl_review_empty.setVisible(total == 0)

    def _add_review_notice_card(self, item: Dict[str, Any]) -> str:
        self._notice_review_seq += 1
        card_id = f"rev_{self._notice_review_seq}"
        src_type = str(item.get("source_type") or "teacher").strip().lower()
        if src_type not in ("schoolbell", "kakao", "teacher"):
            src_type = "teacher"
        badge_map = {
            "schoolbell": ("학교종이", "#DBEAFE", "#1D4ED8"),
            "kakao": ("카카오톡", "#FEF3C7", "#92400E"),
            "teacher": ("교사작성", "#E2E8F0", "#334155"),
        }
        badge_text, bg_col, fg_col = badge_map[src_type]

        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #F8FAFC; border: 1px solid #CBD5E1; border-radius: 8px; }"
        )
        vlay = QVBoxLayout(card)
        vlay.setContentsMargins(10, 8, 10, 8)
        vlay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        chk_select = QCheckBox("게시 선택")
        chk_select.setChecked(True)
        chk_select.toggled.connect(lambda _v: self._update_review_count_label())

        lbl_badge = QLabel(badge_text)
        lbl_badge.setStyleSheet(
            f"background: {bg_col}; color: {fg_col}; padding: 2px 8px; border-radius: 4px; "
            "font-size: 11px; font-weight: 700; border: none;"
        )
        published_at = str(item.get("published_at") or "").strip()
        lbl_date = QLabel(published_at or "날짜 미상")
        lbl_date.setStyleSheet("color: #64748B; font-size: 11px; border: none;")

        chk_pinned = QCheckBox("상단 고정")
        chk_pinned.setChecked(bool(item.get("is_pinned")))

        btn_summarize = QPushButton("3줄 요약")
        apply_qss_role(btn_summarize, role="secondary")
        btn_summarize.clicked.connect(lambda _c=False, cid=card_id: self._on_summarize_review_card(cid))

        btn_remove = QPushButton("삭제")
        apply_qss_role(btn_remove, role="secondary")
        btn_remove.clicked.connect(lambda _c=False, cid=card_id: self._remove_review_notice_card(cid))

        top.addWidget(chk_select)
        top.addWidget(lbl_badge)
        top.addWidget(lbl_date)
        top.addStretch()
        top.addWidget(chk_pinned)
        top.addWidget(btn_summarize)
        top.addWidget(btn_remove)
        vlay.addLayout(top)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        edit_title = QLineEdit(str(item.get("title") or ""))
        edit_title.setPlaceholderText("공지 제목")
        edit_source_label = QLineEdit(str(item.get("source_label") or badge_text))
        edit_source_label.setPlaceholderText("출처명")
        edit_source_label.setMaximumWidth(160)
        meta_row.addWidget(QLabel("제목:"))
        meta_row.addWidget(edit_title, stretch=1)
        meta_row.addWidget(QLabel("출처명:"))
        meta_row.addWidget(edit_source_label)
        vlay.addLayout(meta_row)

        txt_summary = QPlainTextEdit(str(item.get("summary") or ""))
        txt_summary.setPlaceholderText("모바일 공지 요약 (2~3줄)")
        txt_summary.setMaximumHeight(56)
        vlay.addWidget(txt_summary)

        txt_content = QPlainTextEdit(str(item.get("content") or ""))
        txt_content.setPlaceholderText("상세 본문")
        txt_content.setMaximumHeight(78)
        vlay.addWidget(txt_content)

        insert_pos = max(0, self.review_cards_layout.count() - 1)
        self.review_cards_layout.insertWidget(insert_pos, card)
        self._notice_review_cards.append(
            {
                "id": card_id,
                "widget": card,
                "chk_select": chk_select,
                "chk_pinned": chk_pinned,
                "edit_title": edit_title,
                "edit_source_label": edit_source_label,
                "txt_summary": txt_summary,
                "txt_content": txt_content,
                "btn_summarize": btn_summarize,
                "source_type": src_type,
                "source_url": str(item.get("source_url") or ""),
                "published_at": published_at,
            }
        )
        self._update_review_count_label()
        return card_id

    def _remove_review_notice_card(self, card_id: str) -> None:
        idx = next((i for i, c in enumerate(self._notice_review_cards) if c["id"] == card_id), None)
        if idx is None:
            return
        entry = self._notice_review_cards.pop(idx)
        widget = entry.get("widget")
        if widget is not None:
            self.review_cards_layout.removeWidget(widget)
            widget.deleteLater()
        self._update_review_count_label()

    def _on_toggle_all_review_cards(self) -> None:
        if not self._notice_review_cards:
            return
        all_checked = all(c["chk_select"].isChecked() for c in self._notice_review_cards)
        for c in self._notice_review_cards:
            c["chk_select"].setChecked(not all_checked)
        self._update_review_count_label()

    def _on_delete_selected_review_cards(self) -> None:
        selected_ids = [c["id"] for c in self._notice_review_cards if c["chk_select"].isChecked()]
        for cid in selected_ids:
            self._remove_review_notice_card(cid)
        self._update_review_count_label()

    def _collect_selected_review_payloads(self) -> List[Tuple[str, Dict[str, Any]]]:
        meta = self._teacher_meta()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        selected: List[Tuple[str, Dict[str, Any]]] = []
        for idx, c in enumerate(self._notice_review_cards):
            if not c["chk_select"].isChecked():
                continue
            title = c["edit_title"].text().strip()
            summary = c["txt_summary"].toPlainText().strip()
            content = c["txt_content"].toPlainText().strip() or summary
            if not title or not (summary or content):
                continue
            rid = f"board_{int(datetime.now().timestamp() * 1000)}_{idx}"
            doc = {
                "remote_id": rid,
                "title": title,
                "summary": summary or content[:180].replace("\n", " ").strip(),
                "content": content,
                "source_type": c.get("source_type") or "teacher",
                "source_label": c["edit_source_label"].text().strip(),
                "source_url": str(c.get("source_url") or ""),
                "target_grade": meta["grade"],
                "target_class": meta["class_no"],
                "school_code": meta["school_code"],
                "school_name": meta["school_name"],
                "author_name": meta["teacher_name"],
                "is_pinned": bool(c["chk_pinned"].isChecked()),
                "published_at": str(c.get("published_at") or "") or now_str,
                "updated_at": now_str,
            }
            selected.append((c["id"], doc))
        return selected

    def _publish_selected_review_cards(self, also_send_notification: bool) -> None:
        selected = self._collect_selected_review_payloads()
        if not selected:
            QMessageBox.warning(self, "선택 확인", "게시할 공지 카드를 1건 이상 선택하고 제목·내용을 입력해 주세요.")
            return
        meta = self._teacher_meta()
        target_role = str(self.combo_broadcast_target.currentData() or "all")
        btn = (
            getattr(self, "btn_publish_board_and_notify", None)
            if also_send_notification
            else getattr(self, "btn_publish_board_only", None)
        )

        def _work() -> Dict[str, Any]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            published_ids: List[str] = []
            notified = 0
            for card_id, doc in selected:
                ok, rid = svc.upsert_board_notice(doc)
                if ok:
                    published_ids.append(card_id)
                    if also_send_notification:
                        svc.send_class_notification(
                            title=doc["title"],
                            message=doc["summary"] or doc["content"],
                            target_role=target_role,
                            grade=meta["grade"],
                            class_no=meta["class_no"],
                            school_code=meta["school_code"],
                            school_name=meta["school_name"],
                            sender=meta["teacher_name"],
                            notice_id=rid,
                        )
                        notified += 1
            return {"published_ids": published_ids, "notified": notified}

        def _on_result(res: Any) -> None:
            data = res if isinstance(res, dict) else {}
            pub_ids = list(data.get("published_ids") or [])
            for cid in pub_ids:
                self._remove_review_notice_card(cid)
            if also_send_notification:
                self._on_refresh_broadcast_history()
                QMessageBox.information(
                    self,
                    "게시 및 알림 발송 완료",
                    f"학급 공지 게시판에 {len(pub_ids)}건 게시하고 모바일 알림 {data.get('notified', 0)}건을 발송했습니다.",
                )
            else:
                QMessageBox.information(
                    self,
                    "공지 게시판 게시 완료",
                    f"학급 공지 게시판(학급_공지게시판)에 {len(pub_ids)}건을 게시했습니다.",
                )

        self._run_async_button_task(
            button=btn,
            loading_text="게시 중...",
            task_name="학급 공지 게시판 게시",
            task_key="classroom_publish_board_notices",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "게시 오류", str(err or "").splitlines()[0]),
        )

    def _on_load_chat_history(self) -> None:
        sid = str(self.combo_chat_student.currentData() or "").strip()
        meta = self._teacher_meta()

        def _work() -> List[Dict[str, Any]]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            return svc.fetch_chat_history(
                grade=meta["grade"],
                class_no=meta["class_no"],
                student_id=sid,
                school_code=meta["school_code"],
                school_name=meta["school_name"],
            )

        def _on_result(rows: Any) -> None:
            if not rows:
                self.txt_chat_history_viewer.setPlainText("조회된 1:1 상담 대화 내역이 없습니다.")
                return
            lines = []
            for m in rows:
                ts = str(m.get("created_at") or "")
                sender = m.get("sender_name") or m.get("sender_role") or ""
                lines.append(f"[{ts}] {sender}: {m.get('message', '')}")
            self.txt_chat_history_viewer.setPlainText("\n".join(lines))

        self._run_async_button_task(
            button=getattr(self, "btn_load_chat_history", None),
            loading_text="불러오는 중...",
            task_name="1:1 상담 내역 조회",
            task_key="classroom_load_chat_history",
            work_fn=_work,
            on_result=_on_result,
            on_error=lambda err: QMessageBox.warning(self, "상담 내역 조회 오류", str(err or "").splitlines()[0]),
        )

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
        all_logs = self.db.get_attendance_logs(self._current_homeroom_key())
        include_simple_present = (
            self.chk_include_simple_present.isChecked()
            if getattr(self, "chk_include_simple_present", None) is not None
            else False
        )
        logs = (
            all_logs
            if include_simple_present
            else [r for r in all_logs if str(r.get("category") or "").strip() != "출석"]
        )

        pending_cnt = sum(
            1
            for r in logs
            if "대기" in str(r.get("proof_status", "")) or "미확인" in str(r.get("proof_status", ""))
        )
        if getattr(self, "lbl_mobile_att_pending", None) is not None:
            self.lbl_mobile_att_pending.setText(f"미확인(대기중) 출결 보고: {pending_cnt}건")
            apply_qss_role(
                self.lbl_mobile_att_pending,
                role="badgeWarning" if pending_cnt > 0 else "badgeSuccess",
            )
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
            sid = re.sub(r"^(\d+)\.0+$", r"\1", str(row_data["student_id"] or "").strip())
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
        out: List[Dict[str, Any]] = []
        if self._logged_in_teacher and self._firebase_roster:
            for idx, (sid, name) in enumerate(self._firebase_roster, start=1):
                num = _infer_student_number(None, sid, idx)
                out.append({"number": num, "student_id": sid, "name": name})
        else:
            hr_group = self.db.get_active_homeroom_group()
            students = self.db.get_active_homeroom_students()
            if students and self._local_homeroom_matches_logged_in_teacher(hr_group, students):
                for idx, s in enumerate(students, start=1):
                    sid = str(s.get("student_id", "")).strip()
                    name = str(s.get("name", "")).strip()
                    num = _infer_student_number(s.get("number"), sid, idx)
                    out.append({"number": num, "student_id": sid, "name": name})
            elif self._firebase_roster:
                for idx, (sid, name) in enumerate(self._firebase_roster, start=1):
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
        if not students:
            self._last_duty_plan = {"cleaning": [], "bulletin": []}
            self._refresh_exception_badge()
            self.table_cleaning.setRowCount(0)
            self.txt_cleaning_result.setPlainText(
                "등록된 담임 학급 명렬표가 없습니다. 환경설정 탭에서 명렬표를 등록하거나 교사 계정으로 로그인해 주세요."
            )
            return
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

