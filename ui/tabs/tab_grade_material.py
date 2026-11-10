"""
[탭 3] 성적 및 수업 자료 관리 (ui/tabs/tab_grade_material.py)
- EduFlow Slate Workstation 디자인 시스템 적용 (Zero-Emoji, 정적 Instrument Card, 1px #E2E8F0 보더)
- 상단 KPI 요약 카드 (재적 인원, 채점 완료 인원, 미입력 특기사항 인원)
- 좌우 분할(QSplitter): 좌측 성적/수행평가 데이터 그리드 + 우측 단원별 수업자료 버전 트리 및 관리 도구
"""

import csv
import html
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
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


DEFAULT_UNIT_CURRICULUM: List[Dict[str, Any]] = [
    {
        "unit_code": "Unit 1",
        "title": "I. 역학과 에너지",
        "status": "완료 (3 items)",
        "folder": "01_역학과_에너지",
        "files": [
            {
                "name": "Lesson_01_Dynamics_Newton_v2.4.pptx",
                "meta": "14.2 MB · Updated 10.12",
                "type": "PPTX",
            },
            {
                "name": "Student_Lab_Worksheet_Newton3rd.docx",
                "meta": "420 KB · Template Synced",
                "type": "DOCX",
            },
            {
                "name": "NEIS_Rubric_Standards_Mechanics.pdf",
                "meta": "1.1 MB · Verified",
                "type": "PDF",
            },
        ],
    },
    {
        "unit_code": "Unit 2",
        "title": "II. 물질과 전자기장",
        "status": "진도 중 (3 items)",
        "folder": "02_물질과_전자기장",
        "files": [
            {
                "name": "SlideDeck_Electromagnetism_Intro.pptx",
                "meta": "22.8 MB · 수업 진행본",
                "type": "PPTX",
            },
            {
                "name": "Experiment_Induction_Handout.docx",
                "meta": "650 KB · 학생 배포용 양식",
                "type": "DOCX",
            },
            {
                "name": "Evaluation_Rubric_FaradayLaw.pdf",
                "meta": "890 KB · 수행평가 채점 기준표",
                "type": "RUBRIC",
            },
        ],
    },
    {
        "unit_code": "Unit 3",
        "title": "III. 파동과 정보 통신",
        "status": "예정 (2 items)",
        "folder": "03_파동과_정보통신",
        "files": [
            {
                "name": "Wave_Optics_Interference_Draft.pptx",
                "meta": "9.4 MB · 초안 작성 완료",
                "type": "PPTX",
            },
            {
                "name": "Performance_Task_Guide_Optics.hwp",
                "meta": "310 KB · 2차 수행 안내문",
                "type": "HWP",
            },
        ],
    },
]


class GradeMaterialTab(QWidget):
    """[탭 3] 성적 및 수업 자료 관리 워크스테이션 탭 위젯."""

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="GradeMaterialTabPage")
        self.task_runner = task_runner
        self.db = DatabaseManager()
        self._focused_card: Optional[str] = None
        self._updating_table: bool = False
        self._grade_cache: Dict[str, Dict[str, str]] = {}
        self.materials_base_dir = Path("workspace") / "course_materials"

        self._init_ui()
        self._populate_unit_tree()
        self.refresh_student_preview()

    def _create_kpi_card(
        self,
        kor_label: str,
        eng_sub: str,
        badge_text: str,
        badge_role: str,
        value_role: str = "kpiValue",
        bar_color: str = "#004AC6",
    ) -> tuple[QFrame, QLabel, QLabel, QProgressBar]:
        card = QFrame()
        apply_qss_role(card, role="kpiCard")
        card.setStyleSheet(
            "QFrame[role='kpiCard'] { background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 10px; }"
        )
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(16, 14, 16, 12)
        c_lay.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        lbl_kor = QLabel(kor_label)
        apply_qss_role(lbl_kor, role="kpiLabel")
        lbl_eng = QLabel(eng_sub)
        apply_qss_role(lbl_eng, role="kpiSubLabel")
        title_col.addWidget(lbl_kor)
        title_col.addWidget(lbl_eng)

        badge = QLabel(badge_text)
        apply_qss_role(badge, role=badge_role)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        top_row.addLayout(title_col)
        top_row.addStretch()
        top_row.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)
        c_lay.addLayout(top_row)

        val_row = QHBoxLayout()
        val_row.setSpacing(6)
        lbl_val = QLabel("0")
        apply_qss_role(lbl_val, role=value_role)
        lbl_unit = QLabel("명")
        apply_qss_role(lbl_unit, role="kpiUnit")
        val_row.addWidget(lbl_val)
        val_row.addWidget(lbl_unit, alignment=Qt.AlignmentFlag.AlignBottom)
        val_row.addStretch()
        c_lay.addLayout(val_row)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(100)
        bar.setTextVisible(False)
        bar.setFixedHeight(4)
        bar.setStyleSheet(
            f"QProgressBar {{ background-color: #E2E8F0; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background-color: {bar_color}; border-radius: 2px; }}"
        )
        c_lay.addWidget(bar)
        return card, lbl_val, lbl_unit, bar

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

        # -----------------------------------------------------------------
        # 1. 상단 KPI 메트릭 카드 Row (재적 인원 / 채점 완료 / 미입력 특기사항)
        # -----------------------------------------------------------------
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)

        (
            self.kpi_card_total,
            self.lbl_kpi_registered,
            self.lbl_kpi_registered_unit,
            self.bar_kpi_registered,
        ) = self._create_kpi_card(
            kor_label="재적 인원 (REGISTERED)",
            eng_sub="Enrolled Students",
            badge_text="NEIS ROSTER",
            badge_role="badgePrimary",
            value_role="kpiValue",
            bar_color="#004AC6",
        )

        (
            self.kpi_card_graded,
            self.lbl_kpi_graded,
            self.lbl_kpi_graded_unit,
            self.bar_kpi_graded,
        ) = self._create_kpi_card(
            kor_label="채점 완료 (GRADED)",
            eng_sub="Exam & Performance",
            badge_text="COMPLETED",
            badge_role="badgeSuccess",
            value_role="kpiValuePrimary",
            bar_color="#10B981",
        )

        (
            self.kpi_card_unentered,
            self.lbl_kpi_unentered,
            self.lbl_kpi_unentered_unit,
            self.bar_kpi_unentered,
        ) = self._create_kpi_card(
            kor_label="특기사항 미입력 (UNENTERED NOTES)",
            eng_sub="Unentered Remarks",
            badge_text="ACTION REQUIRED",
            badge_role="badgeDanger",
            value_role="kpiValueDanger",
            bar_color="#BA1A1A",
        )

        kpi_row.addWidget(self.kpi_card_total, stretch=1)
        kpi_row.addWidget(self.kpi_card_graded, stretch=1)
        kpi_row.addWidget(self.kpi_card_unentered, stretch=1)
        layout.addLayout(kpi_row)

        # -----------------------------------------------------------------
        # 2. 메인 좌우 분할 (QSplitter: 좌측 성적표 / 우측 단원별 수업자료)
        # -----------------------------------------------------------------
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        # === [좌측 패널] 학생 명렬표 연동 성적/수행평가 관리 ===
        self.left_group = HoverZoomCard(
            title="학생 명렬표 연동 성적 및 수행평가 관리",
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.left_group, role="hoverZoomCard", obj_name="GradeRosterGroup")

        self.btn_focus_left = QPushButton("전체화면")
        apply_qss_role(self.btn_focus_left, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_left.setCheckable(True)
        self.btn_focus_left.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_left.clicked.connect(lambda: self._toggle_card_focus("left"))
        self.left_group.add_header_widget(self.btn_focus_left)

        l_lay = self.left_group.body_layout()

        # 상단 툴바: 검색어 필터 + 명렬표 동기화 + 엑셀 내보내기
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.edit_grade_search = QLineEdit()
        self.edit_grade_search.setMinimumHeight(34)
        self.edit_grade_search.setPlaceholderText("학번, 이름, 비고/피드백 검색...")
        self.edit_grade_search.textChanged.connect(self._filter_grade_rows)

        self.btn_reload = QPushButton("명렬표 동기화")
        apply_qss_role(self.btn_reload, role="primary", obj_name="BtnReloadRosterGrades")
        self.btn_reload.setMinimumHeight(34)
        self.btn_reload.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reload.clicked.connect(self.refresh_student_preview)

        self.btn_export_grades = QPushButton("성적표 내보내기 (.csv/.xlsx)")
        apply_qss_role(self.btn_export_grades, role="secondary", obj_name="BtnExportGrades")
        self.btn_export_grades.setMinimumHeight(34)
        self.btn_export_grades.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export_grades.clicked.connect(self._on_export_gradebook)

        top_bar.addWidget(self.edit_grade_search, stretch=1)
        top_bar.addWidget(self.btn_reload)
        top_bar.addWidget(self.btn_export_grades)
        l_lay.addLayout(top_bar)

        # 성적 데이터 그리드 (QTableWidget)
        self.table_grades = QTableWidget(0, 5)
        self.table_grades.setAlternatingRowColors(True)
        self.table_grades.setMouseTracking(True)
        self.table_grades.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_grades.verticalHeader().setDefaultSectionSize(32)
        self.table_grades.verticalHeader().setVisible(False)
        self.table_grades.setHorizontalHeaderLabels(
            ["학번", "이름", "1차 지필", "수행평가", "비고/피드백"]
        )
        header = self.table_grades.horizontalHeader()
        header.setMinimumHeight(32)
        header.setDefaultSectionSize(32)
        header.setStyleSheet(
            "QHeaderView::section { background-color: #F8FAFC; color: #64748B; height: 32px; "
            "border: none; border-bottom: 1px solid #E2E8F0; border-right: 1px solid #F1F5F9; "
            "font-size: 11px; font-weight: 600; padding: 0 8px; }"
        )
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table_grades.itemChanged.connect(self._on_grade_item_changed)
        l_lay.addWidget(self.table_grades, stretch=1)

        footer_bar = QHBoxLayout()
        self.lbl_grade_summary = QLabel("셀을 더블클릭하여 지필/수행 점수 및 세특 비고를 즉시 편집할 수 있습니다.")
        apply_qss_role(self.lbl_grade_summary, role="mutedText")
        footer_bar.addWidget(self.lbl_grade_summary)
        footer_bar.addStretch()
        l_lay.addLayout(footer_bar)

        # === [우측 패널] 교과 수업 자료 버전 및 단원 디렉토리 뷰어 ===
        self.right_group = HoverZoomCard(
            title="교과 수업 자료 버전 및 단원 디렉토리 관리",
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.right_group, role="hoverZoomCard", obj_name="CourseMaterialGroup")

        self.btn_focus_right = QPushButton("전체화면")
        apply_qss_role(self.btn_focus_right, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_right.setCheckable(True)
        self.btn_focus_right.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_right.clicked.connect(lambda: self._toggle_card_focus("right"))
        self.right_group.add_header_widget(self.btn_focus_right)

        r_lay = self.right_group.body_layout()

        # 학기 & 단원 선택 바
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        self.combo_semester = QComboBox()
        self.combo_semester.setMinimumHeight(34)
        self.combo_semester.addItems(["2026학년도 2학기", "2026학년도 1학기", "2025학년도 2학기"])
        install_no_scroll_wheel_filter(self.combo_semester)

        self.combo_unit_filter = QComboBox()
        self.combo_unit_filter.setMinimumHeight(34)
        self.combo_unit_filter.addItems(
            ["전체 단원 보기", "I. 역학과 에너지", "II. 물질과 전자기장", "III. 파동과 정보 통신"]
        )
        install_no_scroll_wheel_filter(self.combo_unit_filter)
        self.combo_unit_filter.currentTextChanged.connect(self._on_unit_filter_changed)

        filter_row.addWidget(self.combo_semester, stretch=1)
        filter_row.addWidget(self.combo_unit_filter, stretch=1)
        r_lay.addLayout(filter_row)

        # 단원 계층형 디렉토리 트리 뷰어
        self.tree_materials = QTreeWidget()
        self.tree_materials.setColumnCount(3)
        self.tree_materials.setHeaderLabels(["단원 / 수업자료 파일명", "유형", "버전 및 상태"])
        self.tree_materials.setAlternatingRowColors(True)
        tree_header = self.tree_materials.header()
        tree_header.setMinimumHeight(32)
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        r_lay.addWidget(self.tree_materials, stretch=1)

        # 하단 액션 버튼 바 ("폴더 열기", "수업자료 업로드", "단원 압축 백업")
        action_bar = QHBoxLayout()
        action_bar.setSpacing(8)

        self.btn_open_folder = QPushButton("폴더 열기")
        apply_qss_role(self.btn_open_folder, role="secondary", obj_name="BtnOpenMaterialFolder")
        self.btn_open_folder.setMinimumHeight(34)
        self.btn_open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_folder.clicked.connect(self._on_open_material_folder)

        self.btn_upload_material = QPushButton("수업자료 업로드")
        apply_qss_role(self.btn_upload_material, role="primary", obj_name="BtnUploadMaterial")
        self.btn_upload_material.setMinimumHeight(34)
        self.btn_upload_material.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_upload_material.clicked.connect(self._on_upload_material)

        self.btn_backup_unit = QPushButton("단원 압축 백업")
        apply_qss_role(self.btn_backup_unit, role="secondary", obj_name="BtnBackupUnitZip")
        self.btn_backup_unit.setMinimumHeight(34)
        self.btn_backup_unit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_backup_unit.clicked.connect(self._on_backup_unit_zip)

        action_bar.addWidget(self.btn_open_folder, stretch=1)
        action_bar.addWidget(self.btn_upload_material, stretch=1)
        action_bar.addWidget(self.btn_backup_unit, stretch=1)
        r_lay.addLayout(action_bar)

        self.splitter.addWidget(self.left_group)
        self.splitter.addWidget(self.right_group)
        self.splitter.setSizes([640, 440])
        self.splitter.setStretchFactor(0, 6)
        self.splitter.setStretchFactor(1, 4)
        layout.addWidget(self.splitter, stretch=1)

        scroll.setWidget(container)
        outer_lay.addWidget(scroll)

    def _toggle_card_focus(self, target: str) -> None:
        if self._focused_card == target:
            self._focused_card = None
            self.left_group.set_zoom_enabled(True)
            self.right_group.set_zoom_enabled(True)
            self.left_group.show()
            self.right_group.show()
            self.btn_focus_left.setChecked(False)
            self.btn_focus_left.setText("전체화면")
            self.btn_focus_right.setChecked(False)
            self.btn_focus_right.setText("전체화면")
            self.splitter.setSizes([640, 440])
            return

        self._focused_card = target
        if target == "left":
            self.left_group.set_zoom_enabled(False)
            self.left_group.show()
            self.right_group.hide()
            self.btn_focus_left.setChecked(True)
            self.btn_focus_left.setText("원래 크기로")
            self.btn_focus_right.setChecked(False)
            self.btn_focus_right.setText("전체화면")
        else:
            self.right_group.set_zoom_enabled(False)
            self.left_group.hide()
            self.right_group.show()
            self.btn_focus_right.setChecked(True)
            self.btn_focus_right.setText("원래 크기로")
            self.btn_focus_left.setChecked(False)
            self.btn_focus_left.setText("전체화면")

    def refresh_student_preview(self) -> None:
        """DatabaseManager.get_students()에서 명렬표를 읽어 성적표 그리드와 상단 KPI 카드를 갱신합니다."""
        students = self.db.get_students()
        self._updating_table = True
        tabular_font = QFont("JetBrains Mono", 10)
        tabular_font.setStyleHint(QFont.StyleHint.Monospace)

        try:
            self.table_grades.setRowCount(0)
            for idx, s in enumerate(students):
                self.table_grades.insertRow(idx)
                sid_str = str(s["student_id"])
                cached = self._grade_cache.get(sid_str, {})

                m1_val = cached.get("m1", "-")
                perf_val = cached.get("perf", "-")
                memo_val = cached.get("memo", s.get("memo", "") or "")

                sid_it = QTableWidgetItem(sid_str)
                sid_it.setFont(tabular_font)
                sid_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                sid_it.setFlags(sid_it.flags() & ~Qt.ItemFlag.ItemIsEditable)

                name_it = QTableWidgetItem(s["name"])
                name_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                name_it.setFlags(name_it.flags() & ~Qt.ItemFlag.ItemIsEditable)

                m1_it = QTableWidgetItem(m1_val)
                m1_it.setFont(tabular_font)
                m1_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                p_it = QTableWidgetItem(perf_val)
                p_it.setFont(tabular_font)
                p_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                memo_it = QTableWidgetItem(memo_val)

                tip = (
                    f"<div style='padding:2px 4px;'><b>[{html.escape(sid_str)}] {html.escape(s['name'])}</b><br/>"
                    f"1차 지필: {html.escape(m1_val)} / 수행평가: {html.escape(perf_val)}<br/>"
                    f"비고: {html.escape(memo_val or '-')}</div>"
                )
                for col, it in enumerate((sid_it, name_it, m1_it, p_it, memo_it)):
                    it.setToolTip(tip)
                    self.table_grades.setItem(idx, col, it)
        finally:
            self._updating_table = False

        self._update_kpi_metrics()
        self._filter_grade_rows(self.edit_grade_search.text())

    def _on_grade_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table:
            return
        row = item.row()
        sid_item = self.table_grades.item(row, 0)
        if not sid_item:
            return
        sid = sid_item.text().strip()
        m1_txt = (self.table_grades.item(row, 2).text() if self.table_grades.item(row, 2) else "-").strip() or "-"
        perf_txt = (self.table_grades.item(row, 3).text() if self.table_grades.item(row, 3) else "-").strip() or "-"
        memo_txt = (self.table_grades.item(row, 4).text() if self.table_grades.item(row, 4) else "").strip()
        self._grade_cache[sid] = {"m1": m1_txt, "perf": perf_txt, "memo": memo_txt}
        self._update_kpi_metrics()

    def _update_kpi_metrics(self) -> None:
        total = self.table_grades.rowCount()
        graded = 0
        unentered_notes = 0

        for r in range(total):
            m1_it = self.table_grades.item(r, 2)
            p_it = self.table_grades.item(r, 3)
            memo_it = self.table_grades.item(r, 4)

            m1_txt = m1_it.text().strip() if m1_it else "-"
            p_txt = p_it.text().strip() if p_it else "-"
            memo_txt = memo_it.text().strip() if memo_it else ""

            if (m1_txt and m1_txt != "-") or (p_txt and p_txt != "-"):
                graded += 1
            if not memo_txt or memo_txt == "-":
                unentered_notes += 1

        self.lbl_kpi_registered.setText(str(total))
        self.lbl_kpi_registered_unit.setText("명 등록됨")
        self.bar_kpi_registered.setValue(100 if total > 0 else 0)

        self.lbl_kpi_graded.setText(str(graded))
        self.lbl_kpi_graded_unit.setText(f"/ {total}명 완료")
        graded_pct = int(round((graded / total) * 100)) if total > 0 else 0
        self.bar_kpi_graded.setValue(graded_pct)

        self.lbl_kpi_unentered.setText(str(unentered_notes))
        self.lbl_kpi_unentered_unit.setText("명 미입력")
        unentered_pct = int(round((unentered_notes / total) * 100)) if total > 0 else 0
        self.bar_kpi_unentered.setValue(unentered_pct)

    def _filter_grade_rows(self, keyword: str) -> None:
        kw = keyword.strip().lower()
        for r in range(self.table_grades.rowCount()):
            if not kw:
                self.table_grades.setRowHidden(r, False)
                continue
            row_text = " ".join(
                (self.table_grades.item(r, c).text() if self.table_grades.item(r, c) else "")
                for c in range(self.table_grades.columnCount())
            ).lower()
            self.table_grades.setRowHidden(r, kw not in row_text)

    def _on_export_gradebook(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "성적 및 수행평가 일람표 내보내기",
            f"성적일람표_{datetime.now().strftime('%Y%m%d')}.csv",
            "CSV 파일 (*.csv);;Excel 파일 (*.xlsx)",
        )
        if not file_path:
            return

        headers = ["학번", "이름", "1차 지필", "수행평가", "비고/피드백"]
        rows: List[List[str]] = []
        for r in range(self.table_grades.rowCount()):
            rows.append(
                [
                    self.table_grades.item(r, c).text() if self.table_grades.item(r, c) else ""
                    for c in range(5)
                ]
            )

        try:
            if file_path.lower().endswith(".xlsx"):
                import openpyxl

                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "성적일람표"
                ws.append(headers)
                for row_vals in rows:
                    ws.append(row_vals)
                wb.save(file_path)
            else:
                with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    writer.writerows(rows)
            QMessageBox.information(self, "내보내기 완료", f"성적 일람표가 저장되었습니다:\n{file_path}")
        except Exception as exc:
            QMessageBox.critical(self, "내보내기 오류", f"성적표 저장 중 오류가 발생했습니다:\n{exc}")

    # =========================================================================
    # 우측 패널: 단원별 수업자료 트리 및 폴더/업로드/백업 액션
    # =========================================================================
    def _ensure_unit_directories(self) -> Path:
        self.materials_base_dir.mkdir(parents=True, exist_ok=True)
        for unit in DEFAULT_UNIT_CURRICULUM:
            (self.materials_base_dir / unit["folder"]).mkdir(parents=True, exist_ok=True)
        return self.materials_base_dir

    def _populate_unit_tree(self, filter_title: str = "전체 단원 보기") -> None:
        self.tree_materials.clear()
        bold_font = QFont()
        bold_font.setBold(True)

        for unit in DEFAULT_UNIT_CURRICULUM:
            if filter_title != "전체 단원 보기" and filter_title not in unit["title"]:
                continue

            parent_item = QTreeWidgetItem(
                [f"{unit['unit_code']} · {unit['title']}", "FOLDER", unit["status"]]
            )
            parent_item.setData(0, Qt.ItemDataRole.UserRole, unit["folder"])
            parent_item.setFont(0, bold_font)
            parent_item.setForeground(0, QColor("#0F172A"))
            parent_item.setForeground(2, QColor("#004AC6"))
            self.tree_materials.addTopLevelItem(parent_item)

            for f_info in unit["files"]:
                child = QTreeWidgetItem([f_info["name"], f_info["type"], f_info["meta"]])
                child.setData(0, Qt.ItemDataRole.UserRole, unit["folder"])
                child.setForeground(1, QColor("#475569"))
                child.setForeground(2, QColor("#64748B"))
                parent_item.addChild(child)

            # 실제 디렉토리에 사용자가 업로드한 파일도 함께 표시
            unit_dir = self.materials_base_dir / unit["folder"]
            if unit_dir.exists():
                existing_names = {f["name"] for f in unit["files"]}
                for p in sorted(unit_dir.iterdir()):
                    if p.is_file() and p.name not in existing_names:
                        kb = max(1, p.stat().st_size // 1024)
                        ext_label = p.suffix.lstrip(".").upper() or "FILE"
                        child = QTreeWidgetItem(
                            [p.name, ext_label, f"{kb} KB · 사용자 업로드"]
                        )
                        child.setData(0, Qt.ItemDataRole.UserRole, unit["folder"])
                        parent_item.addChild(child)

            parent_item.setExpanded(True)

    def _on_unit_filter_changed(self, text: str) -> None:
        self._populate_unit_tree(text)

    def _selected_unit_folder_path(self) -> Path:
        base = self._ensure_unit_directories()
        curr = self.tree_materials.currentItem()
        if curr is not None:
            folder_name = curr.data(0, Qt.ItemDataRole.UserRole)
            if folder_name:
                target = base / str(folder_name)
                target.mkdir(parents=True, exist_ok=True)
                return target
        return base

    def _on_open_material_folder(self) -> None:
        target_dir = self._selected_unit_folder_path().resolve()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_dir)))

    def _on_upload_material(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "단원 수업자료 파일 업로드",
            "",
            "수업자료 파일 (*.pptx *.ppt *.docx *.doc *.pdf *.hwp *.hwpx *.xlsx *.zip);;모든 파일 (*)",
        )
        if not files:
            return

        target_dir = self._selected_unit_folder_path()
        copied = 0
        for src in files:
            src_path = Path(src)
            if src_path.exists() and src_path.is_file():
                shutil.copy2(str(src_path), str(target_dir / src_path.name))
                copied += 1

        self._populate_unit_tree(self.combo_unit_filter.currentText())
        QMessageBox.information(
            self,
            "수업자료 업로드 완료",
            f"선택한 단원 폴더({target_dir.name})에 {copied}개 자료를 등록했습니다.",
        )

    def _on_backup_unit_zip(self) -> None:
        base_dir = self._ensure_unit_directories()
        default_zip = f"수업자료_단원백업_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "단원 수업자료 압축 백업 (.zip)",
            default_zip,
            "ZIP 압축 파일 (*.zip)",
        )
        if not save_path:
            return

        try:
            with zipfile.ZipFile(save_path, "w", zipfile.ZIP_DEFLATED) as zf:
                file_count = 0
                for p in base_dir.rglob("*"):
                    if p.is_file():
                        zf.write(p, p.relative_to(base_dir))
                        file_count += 1
                # 샘플 메타 매니페스트 기록
                manifest_lines = [
                    f"EduFlow Course Materials Backup ({datetime.now().isoformat()})",
                    f"Semester: {self.combo_semester.currentText()}",
                    f"Total Local Files Archived: {file_count}",
                ]
                zf.writestr("BACKUP_MANIFEST.txt", "\n".join(manifest_lines))
            QMessageBox.information(
                self,
                "단원 압축 백업 완료",
                f"수업자료 압축 백업 파일이 생성되었습니다:\n{save_path}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "백업 실패", f"압축 백업 중 오류가 발생했습니다:\n{exc}")
