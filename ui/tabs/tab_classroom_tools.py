"""
[탭 4] 학급 경영 도구함 (ui/tabs/tab_classroom_tools.py)
- EduFlow Slate Workstation 2x2 Bento Instrument Card 레이아웃 적용 (Zero-Emoji, 1px #E2E8F0 정적 카드)
- Card 1: 출결 및 결석계 관리 (Attendance Tracker) — 출석/질병결석/체험학습 배지, 증빙 대장, 결석계 생성 / 출석인정서 출력
- Card 2: 청소 당번 / 1인 1역 랜덤 배정기 (Duty Shuffler) — _on_random_assign() 연동, 학번/이름/담당구역/상태 테이블
- Card 3: 자리 배치 및 모둠 편성기 (Seating Grid 6x5) — 칠판/교탁 기준 6x5 좌석 그리드, 자리 무작위 배치 / 좌석표 내보내기
- Card 4: 학생 관찰 및 상담 메모장 (Observation Log) — 학생 선택 콤보박스, 한줄 관찰 기록 입력, 최근 상담/관찰 이력 리스트
"""

import csv
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QClipboard, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
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
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import TaskRunner
from core.database import DatabaseManager
from ui.components.hover_zoom_card import HoverZoomCard
from ui.utils import install_no_scroll_wheel_filter


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


class ClassroomToolsTab(QWidget):
    """[탭 4] 학급 경영 도구함 2x2 Instrument Station 탭 위젯."""

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="ClassroomToolsTabPage")
        self.task_runner = task_runner
        self.db = DatabaseManager()
        self._seat_labels: List[Tuple[QLabel, QLabel]] = []
        self._current_seating: List[Tuple[str, str, str]] = []

        self._init_ui()
        self._refresh_roster_dependent_views()
        self._on_random_assign()

    def _get_active_roster(self) -> List[Tuple[str, str]]:
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
        outer_lay = QVBoxLayout(self)
        outer_lay.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

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

        btn_sync_roster = QPushButton("명렬표 연동 새로고침")
        apply_qss_role(btn_sync_roster, role="secondary", obj_name="BtnSyncClassroomRoster")
        btn_sync_roster.setMinimumHeight(30)
        btn_sync_roster.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_sync_roster.clicked.connect(self._refresh_roster_dependent_views)

        hb_lay.addWidget(lbl_station_title)
        hb_lay.addWidget(lbl_mode_chip)
        hb_lay.addWidget(self.lbl_roster_status_badge)
        hb_lay.addStretch()
        hb_lay.addWidget(btn_sync_roster)
        layout.addWidget(header_bar)

        # 메인 2x2 Bento Grid
        grid = QGridLayout()
        grid.setVerticalSpacing(16)
        grid.setHorizontalSpacing(16)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)

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

        # 출결 상태 배지 3종 (Present / Sick / Field trip)
        att_badge_row = QHBoxLayout()
        att_badge_row.setSpacing(8)
        pill_present, self.lbl_att_present, _ = self._create_status_pill(
            "출석 (PRESENT)", "29 / 32명", "정상 등교", "badgeSuccess"
        )
        pill_sick, self.lbl_att_sick, _ = self._create_status_pill(
            "질병 결석 (SICK)", "1명", "증빙 대기", "badgeWarning"
        )
        pill_trip, self.lbl_att_trip, _ = self._create_status_pill(
            "체험학습 (FIELD TRIP)", "2명", "사전 승인", "badgePrimary"
        )
        att_badge_row.addWidget(pill_present, stretch=1)
        att_badge_row.addWidget(pill_sick, stretch=1)
        att_badge_row.addWidget(pill_trip, stretch=1)
        c1_lay.addLayout(att_badge_row)

        # 증빙 서류 대장 테이블
        self.table_attendance = QTableWidget(0, 5)
        self.table_attendance.setAlternatingRowColors(True)
        self.table_attendance.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_attendance.verticalHeader().setDefaultSectionSize(32)
        self.table_attendance.verticalHeader().setVisible(False)
        self.table_attendance.setHorizontalHeaderLabels(
            ["학번", "이름", "결석 구분", "적용 일자", "증빙 상태"]
        )
        att_hdr = self.table_attendance.horizontalHeader()
        att_hdr.setMinimumHeight(32)
        att_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        att_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        att_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        att_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        att_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._populate_sample_attendance()
        c1_lay.addWidget(self.table_attendance, stretch=1)

        # 하단 액션 버튼 ("결석계 생성" / "출석인정서 출력")
        att_btn_row = QHBoxLayout()
        att_btn_row.setSpacing(8)
        lbl_att_rule = QLabel("규정: 등교 후 5일 이내 증빙 첨부")
        apply_qss_role(lbl_att_rule, role="mutedText")

        self.btn_create_absence = QPushButton("결석계 생성")
        apply_qss_role(self.btn_create_absence, role="primary", obj_name="BtnCreateAbsenceForm")
        self.btn_create_absence.setMinimumHeight(34)
        self.btn_create_absence.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_create_absence.clicked.connect(self._on_create_absence_form)

        self.btn_print_cert = QPushButton("출석인정서 출력")
        apply_qss_role(self.btn_print_cert, role="secondary", obj_name="BtnPrintAttendanceCert")
        self.btn_print_cert.setMinimumHeight(34)
        self.btn_print_cert.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_print_cert.clicked.connect(self._on_print_attendance_cert)

        att_btn_row.addWidget(lbl_att_rule)
        att_btn_row.addStretch()
        att_btn_row.addWidget(self.btn_print_cert)
        att_btn_row.addWidget(self.btn_create_absence)
        c1_lay.addLayout(att_btn_row)

        # =====================================================================
        # CARD 2: 청소 당번 / 1인 1역 랜덤 배정기 (Cleaning & Duty Randomizer)
        # =====================================================================
        self.card_cleaning = HoverZoomCard(
            title="2. 청소 당번 및 1인 1역 랜덤 배정기 (Duty Shuffler)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_cleaning, role="hoverZoomCard", obj_name="CardCleaning")
        c2_lay = self.card_cleaning.body_layout()

        duty_top_bar = QHBoxLayout()
        duty_top_bar.setSpacing(8)
        self.chk_fair_rotation = QCheckBox("직전 주차 당번 제외 (공정 가중치)")
        self.chk_fair_rotation.setChecked(True)

        self.btn_assign = QPushButton("무작위 당번 추첨")
        apply_qss_role(self.btn_assign, role="primary", obj_name="BtnRandomAssign")
        self.btn_assign.setMinimumHeight(34)
        self.btn_assign.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_assign.clicked.connect(self._on_random_assign)

        self.btn_copy_duty = QPushButton("클립보드 복사")
        apply_qss_role(self.btn_copy_duty, role="secondary", obj_name="BtnCopyDutyResult")
        self.btn_copy_duty.setMinimumHeight(34)
        self.btn_copy_duty.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_duty.clicked.connect(self._on_copy_duty_clipboard)

        duty_top_bar.addWidget(self.chk_fair_rotation)
        duty_top_bar.addStretch()
        duty_top_bar.addWidget(self.btn_copy_duty)
        duty_top_bar.addWidget(self.btn_assign)
        c2_lay.addLayout(duty_top_bar)

        # 당번 배정 테이블 (학번 / 이름 / 담당 구역 / 상태)
        self.table_cleaning = QTableWidget(0, 4)
        self.table_cleaning.setAlternatingRowColors(True)
        self.table_cleaning.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_cleaning.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_cleaning.verticalHeader().setDefaultSectionSize(32)
        self.table_cleaning.verticalHeader().setVisible(False)
        self.table_cleaning.setHorizontalHeaderLabels(["학번", "이름", "담당 구역 (Duty Area)", "상태"])
        c_hdr = self.table_cleaning.horizontalHeader()
        c_hdr.setMinimumHeight(32)
        c_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        c_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        c_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        c_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        c2_lay.addWidget(self.table_cleaning, stretch=1)

        # 기존 텍스트 결과 위젯도 하위 호환을 위해 유지 (숨김 처리하여 UI 밀도 유지)
        self.txt_cleaning_result = QTextEdit()
        apply_qss_role(self.txt_cleaning_result, obj_name="WebAiInputBox")
        self.txt_cleaning_result.setPlaceholderText("명렬표 학생들을 무작위로 섞어 당번을 배정합니다...")
        self.txt_cleaning_result.setVisible(False)
        c2_lay.addWidget(self.txt_cleaning_result)

        # =====================================================================
        # CARD 3: 학급 자리 배치 및 모둠 편성기 (Seating Chart & Group Builder)
        # =====================================================================
        self.card_seating = HoverZoomCard(
            title="3. 학급 자리 배치 및 모둠 편성기 (Seating Grid 6x5)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_seating, role="hoverZoomCard", obj_name="CardSeating")
        c3_lay = self.card_seating.body_layout()

        # 칠판 / 교탁 전면 바
        podium_frame = QFrame()
        apply_qss_role(podium_frame, role="podiumBar")
        podium_lay = QHBoxLayout(podium_frame)
        podium_lay.setContentsMargins(12, 6, 12, 6)
        lbl_podium = QLabel("[ 칠판 / 교탁 (Front Podium) ]")
        apply_qss_role(lbl_podium, role="podiumLabel")
        lbl_podium.setAlignment(Qt.AlignmentFlag.AlignCenter)
        podium_lay.addWidget(lbl_podium)
        c3_lay.addWidget(podium_frame)

        # 6x5 시각적 좌석 그리드
        seat_container = QFrame()
        apply_qss_role(seat_container, role="subCard")
        self.seating_grid = QGridLayout(seat_container)
        self.seating_grid.setContentsMargins(8, 8, 8, 8)
        self.seating_grid.setHorizontalSpacing(6)
        self.seating_grid.setVerticalSpacing(6)

        self._seat_labels.clear()
        for r in range(5):
            for c in range(6):
                desk_idx = r * 6 + c + 1
                cell = QFrame()
                apply_qss_role(cell, role="seatCell")
                cell_lay = QVBoxLayout(cell)
                cell_lay.setContentsMargins(4, 4, 4, 4)
                cell_lay.setSpacing(1)

                lbl_code = QLabel(f"D-{desk_idx:02d}")
                apply_qss_role(lbl_code, role="seatCode")
                lbl_code.setAlignment(Qt.AlignmentFlag.AlignCenter)

                lbl_stu = QLabel("-")
                apply_qss_role(lbl_stu, role="seatName")
                lbl_stu.setAlignment(Qt.AlignmentFlag.AlignCenter)

                cell_lay.addWidget(lbl_code)
                cell_lay.addWidget(lbl_stu)
                self.seating_grid.addWidget(cell, r, c)
                self._seat_labels.append((lbl_code, lbl_stu))

        c3_lay.addWidget(seat_container, stretch=1)

        # 자리 배치 액션 바 ("자리 무작위 배치" / "좌석표 내보내기")
        seat_btn_row = QHBoxLayout()
        seat_btn_row.setSpacing(8)
        self.chk_front_care = QCheckBox("시력배려 앞자리 우선")
        self.chk_front_care.setChecked(True)

        self.btn_random_seats = QPushButton("자리 무작위 배치")
        apply_qss_role(self.btn_random_seats, role="primary", obj_name="BtnRandomizeSeats")
        self.btn_random_seats.setMinimumHeight(34)
        self.btn_random_seats.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_random_seats.clicked.connect(self._on_randomize_seats)

        self.btn_export_seats = QPushButton("좌석표 내보내기")
        apply_qss_role(self.btn_export_seats, role="secondary", obj_name="BtnExportSeats")
        self.btn_export_seats.setMinimumHeight(34)
        self.btn_export_seats.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export_seats.clicked.connect(self._on_export_seating_chart)

        seat_btn_row.addWidget(self.chk_front_care)
        seat_btn_row.addStretch()
        seat_btn_row.addWidget(self.btn_export_seats)
        seat_btn_row.addWidget(self.btn_random_seats)
        c3_lay.addLayout(seat_btn_row)

        # =====================================================================
        # CARD 4: 학생 관찰·상담 메모장 (Counseling & Observation Log)
        # =====================================================================
        self.card_counsel = HoverZoomCard(
            title="4. 학생 관찰 및 상담 메모장 (Observation Log)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.card_counsel, role="hoverZoomCard", obj_name="CardCounsel")
        c4_lay = self.card_counsel.body_layout()

        # 학생 선택 콤보박스 + NEIS 키워드 칩
        stu_sel_row = QHBoxLayout()
        stu_sel_row.setSpacing(8)
        lbl_stu_sel = QLabel("대상 학생:")
        apply_qss_role(lbl_stu_sel, role="fieldLabel")

        self.combo_counsel_student = QComboBox()
        self.combo_counsel_student.setMinimumHeight(34)
        install_no_scroll_wheel_filter(self.combo_counsel_student)

        stu_sel_row.addWidget(lbl_stu_sel)
        stu_sel_row.addWidget(self.combo_counsel_student, stretch=1)
        c4_lay.addLayout(stu_sel_row)

        # 빠른 키워드 태그 바
        tag_row = QHBoxLayout()
        tag_row.setSpacing(6)
        lbl_tag_guide = QLabel("NEIS 키워드:")
        apply_qss_role(lbl_tag_guide, role="mutedText")
        tag_row.addWidget(lbl_tag_guide)
        for kw in ("+ 탐구능력", "+ 자기주도성", "+ 협업태도", "+ 나눔과 배려"):
            btn_kw = QPushButton(kw)
            apply_qss_role(btn_kw, role="tagChip")
            btn_kw.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_kw.clicked.connect(lambda _checked=False, k=kw: self._append_keyword_to_note(k))
            tag_row.addWidget(btn_kw)
        tag_row.addStretch()
        c4_lay.addLayout(tag_row)

        # 한줄 관찰 입력 필드 + 기록 추가 버튼
        obs_input_row = QHBoxLayout()
        obs_input_row.setSpacing(8)
        self.edit_observation = QLineEdit()
        self.edit_observation.setMinimumHeight(34)
        self.edit_observation.setPlaceholderText(
            "수업 태도, 교우 관계, 진로 상담 등 수시 관찰 내용을 입력하세요..."
        )
        self.edit_observation.returnPressed.connect(self._on_add_observation_log)

        self.btn_save_observation = QPushButton("일지에 기록")
        apply_qss_role(self.btn_save_observation, role="primary", obj_name="BtnSaveObservation")
        self.btn_save_observation.setMinimumHeight(34)
        self.btn_save_observation.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save_observation.clicked.connect(self._on_add_observation_log)

        obs_input_row.addWidget(self.edit_observation, stretch=1)
        obs_input_row.addWidget(self.btn_save_observation)
        c4_lay.addLayout(obs_input_row)

        # 최근 관찰/상담 이력 리스트
        self.list_counsel_history = QListWidget()
        self.list_counsel_history.setAlternatingRowColors(True)
        self._populate_sample_counsel_logs()
        c4_lay.addWidget(self.list_counsel_history, stretch=1)

        grid.addWidget(self.card_attendance, 0, 0)
        grid.addWidget(self.card_cleaning, 0, 1)
        grid.addWidget(self.card_seating, 1, 0)
        grid.addWidget(self.card_counsel, 1, 1)

        layout.addLayout(grid, stretch=1)
        scroll.setWidget(container)
        outer_lay.addWidget(scroll)

    # =========================================================================
    # 데이터 연동 및 액션 핸들러
    # =========================================================================
    def _populate_sample_attendance(self) -> None:
        sample_rows = [
            ("10103", "김하은", "질병결석 (독감 A형)", "10.24 ~ 10.26 (3일)", "증빙 대기 (진단서)"),
            ("10107", "이서진", "교외체험학습 (역사탐방)", "10.25 (1일)", "신청서 승인완료"),
            ("10112", "조유진", "출석인정 (교내대회 참가)", "10.22 (3~4교시)", "공문 확인완료"),
        ]
        mono_font = QFont("JetBrains Mono", 9)
        self.table_attendance.setRowCount(0)
        for idx, (sid, name, reason, period, status) in enumerate(sample_rows):
            self.table_attendance.insertRow(idx)
            it_sid = QTableWidgetItem(sid)
            it_sid.setFont(mono_font)
            it_sid.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_name = QTableWidgetItem(name)
            it_name.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_reason = QTableWidgetItem(reason)
            it_period = QTableWidgetItem(period)
            it_period.setFont(mono_font)
            it_period.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it_status = QTableWidgetItem(status)
            it_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for c, it in enumerate((it_sid, it_name, it_reason, it_period, it_status)):
                self.table_attendance.setItem(idx, c, it)

    def _refresh_roster_dependent_views(self) -> None:
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
        total = len(roster)
        present = max(0, total - 3) if total >= 3 else total
        self.lbl_att_present.setText(f"{present} / {total}명")

        self.combo_counsel_student.blockSignals(True)
        self.combo_counsel_student.clear()
        for sid, name in roster:
            self.combo_counsel_student.addItem(f"{sid} {name}", (sid, name))
        self.combo_counsel_student.blockSignals(False)

        self._on_randomize_seats(initial=True)
        self._on_random_assign()

    def _on_create_absence_form(self) -> None:
        row = self.table_attendance.currentRow()
        if row >= 0:
            sid = self.table_attendance.item(row, 0).text()
            name = self.table_attendance.item(row, 1).text()
            reason = self.table_attendance.item(row, 2).text()
            period = self.table_attendance.item(row, 3).text()
        else:
            roster = self._get_active_roster()
            sid, name = roster[0] if roster else ("10101", "강민서")
            reason = "질병결석"
            period = datetime.now().strftime("%Y-%m-%d")

        default_name = f"결석계_{sid}_{name}.csv"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "결석계 양식 파일 생성",
            default_name,
            "CSV 파일 (*.csv);;텍스트 파일 (*.txt)",
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["항목", "내용"])
                writer.writerow(["문서종류", "학급 결석계 (NEIS 증빙용)"])
                writer.writerow(["학번", sid])
                writer.writerow(["성명", name])
                writer.writerow(["결석구분 및 사유", reason])
                writer.writerow(["결석기간", period])
                writer.writerow(["생성일시", datetime.now().strftime("%Y-%m-%d %H:%M")])
                writer.writerow(["담임교사 확인", "서명 / 날인"])
            QMessageBox.information(self, "결석계 생성 완료", f"결석계 서식이 생성되었습니다:\n{file_path}")
        except Exception as exc:
            QMessageBox.critical(self, "생성 오류", f"결석계 생성 중 오류가 발생했습니다:\n{exc}")

    def _on_print_attendance_cert(self) -> None:
        row = self.table_attendance.currentRow()
        if row >= 0:
            sid = self.table_attendance.item(row, 0).text()
            name = self.table_attendance.item(row, 1).text()
            reason = self.table_attendance.item(row, 2).text()
        else:
            sid, name, reason = "10107", "이서진", "교외체험학습 (역사탐방)"

        QMessageBox.information(
            self,
            "출석인정서 출력 준비",
            f"[{sid} {name}] 학생의 '{reason}' 출석인정 의결서 데이터가 준비되었습니다.\n"
            "결재 라인(담임 - 부장 - 교감) 양식으로 내보낼 수 있습니다.",
        )

    def _on_random_assign(self) -> None:
        """등록된 학생 명렬표(또는 샘플 명렬표)를 무작위로 섞어 당번 테이블과 텍스트 버퍼를 갱신합니다."""
        students = self.db.get_active_homeroom_students()
        if not students:
            roster_pairs = [
                ("10101", "김민준(샘플)"),
                ("10102", "이서연(샘플)"),
                ("10103", "박지호(샘플)"),
                ("10104", "최수아(샘플)"),
                ("10105", "정하준(샘플)"),
                ("10106", "강민서(샘플)"),
                ("10107", "윤지민(샘플)"),
                ("10108", "조유진(샘플)"),
            ]
        else:
            roster_pairs = [(str(s["student_id"]), str(s["name"])) for s in students]

        shuffled = list(roster_pairs)
        random.shuffle(shuffled)

        mono_font = QFont("JetBrains Mono", 9)
        self.table_cleaning.setRowCount(0)
        lines: List[str] = []

        for idx, (sid, name) in enumerate(shuffled, start=1):
            duty_area = DUTY_AREAS[(idx - 1) % len(DUTY_AREAS)]
            status_txt = "배정됨" if idx <= len(DUTY_AREAS) else "예비 순번"

            r_idx = idx - 1
            self.table_cleaning.insertRow(r_idx)

            it_sid = QTableWidgetItem(sid)
            it_sid.setFont(mono_font)
            it_sid.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            it_name = QTableWidgetItem(name)
            it_name.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            it_area = QTableWidgetItem(duty_area)
            it_status = QTableWidgetItem(status_txt)
            it_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for c_idx, it in enumerate((it_sid, it_name, it_area, it_status)):
                self.table_cleaning.setItem(r_idx, c_idx, it)

            lines.append(f"{idx}번 당번: {sid} {name} - {duty_area}")

        self.txt_cleaning_result.setPlainText("\n".join(lines))

    def _on_copy_duty_clipboard(self) -> None:
        text = self.txt_cleaning_result.toPlainText().strip()
        if not text:
            self._on_random_assign()
            text = self.txt_cleaning_result.toPlainText().strip()
        clipboard: Optional[QClipboard] = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        QMessageBox.information(self, "복사 완료", "당번 배정 결과가 클립보드에 복사되었습니다.")

    def _on_randomize_seats(self, initial: bool = False) -> None:
        roster = list(self._get_active_roster())
        if not initial:
            random.shuffle(roster)

        self._current_seating.clear()
        for idx, (lbl_code, lbl_stu) in enumerate(self._seat_labels):
            desk_code = f"D-{idx + 1:02d}"
            lbl_code.setText(desk_code)
            if idx < len(roster):
                sid, name = roster[idx]
                lbl_stu.setText(f"{sid} {name}")
                self._current_seating.append((desk_code, sid, name))
            else:
                lbl_stu.setText("공석")
                self._current_seating.append((desk_code, "-", "공석"))

    def _on_export_seating_chart(self) -> None:
        if not self._current_seating:
            self._on_randomize_seats(initial=True)

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "학급 좌석 배치표 내보내기",
            f"학급좌석표_6x5_{datetime.now().strftime('%Y%m%d')}.csv",
            "CSV 파일 (*.csv)",
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["[ 칠판 / 교탁 (Front Podium) ]"])
                writer.writerow(["1분단", "2분단", "3분단", "4분단", "5분단", "6분단"])
                for r in range(5):
                    row_cells: List[str] = []
                    for c in range(6):
                        desk_code, sid, name = self._current_seating[r * 6 + c]
                        row_cells.append(f"{desk_code}: {sid} {name}" if sid != "-" else f"{desk_code}: 공석")
                    writer.writerow(row_cells)
            QMessageBox.information(self, "좌석표 내보내기 완료", f"좌석 배치표가 저장되었습니다:\n{file_path}")
        except Exception as exc:
            QMessageBox.critical(self, "내보내기 오류", f"좌석표 저장 중 오류가 발생했습니다:\n{exc}")

    def _populate_sample_counsel_logs(self) -> None:
        samples = [
            "[10.25 10:45] [10105 서준우] 물리 탐구 발표 시 조원 간 의견 충돌을 중재하고 결론 도출을 주도함. (+협업태도)",
            "[10.24 16:20] [10102 김도윤] 진로 심층 상담: 기계·컴퓨터 융합 진로 희망에 따른 2학년 선택과목 안내.",
            "[10.23 14:10] [10108 윤지민] 학급 1인 1역 환기 및 소등 업무를 학기 내내 성실히 수행함. (+나눔과 배려)",
        ]
        self.list_counsel_history.clear()
        for entry in samples:
            self.list_counsel_history.addItem(QListWidgetItem(entry))

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

        student_label = self.combo_counsel_student.currentText().strip() or "미지정 학생"
        now_str = datetime.now().strftime("%m.%d %H:%M")
        entry_text = f"[{now_str}] [{student_label}] {note}"
        self.list_counsel_history.insertItem(0, QListWidgetItem(entry_text))
        self.edit_observation.clear()
