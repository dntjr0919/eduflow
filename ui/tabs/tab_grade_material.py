"""
[탭 3] 📊 성적 및 수업 자료 관리 (ui/tabs/tab_grade_material.py)
- HoverZoomCard 적용, QScrollArea 반응형 스크롤, 카드 최대화 토글 버튼 지원
"""

import html
from typing import Optional
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from core.base_worker import TaskRunner
from core.database import DatabaseManager
from ui.components.hover_zoom_card import HoverZoomCard


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


class GradeMaterialTab(QWidget):
    """[탭 3] 성적 및 수업 자료 관리 스켈레톤 탭 위젯."""

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="GradeMaterialTabPage")
        self.task_runner = task_runner
        self.db = DatabaseManager()
        self._focused_card: Optional[str] = None
        self._init_ui()
        self.refresh_student_preview()

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
        layout.setContentsMargins(14, 14, 24, 24)
        layout.setSpacing(12)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        self.left_group = HoverZoomCard(
            title="📊 학생 명렬표 연동 성적/수행평가 관리 (확장용 스켈레톤)",
            zoom_ratio=1.12,
            hover_delay_ms=1000,
        )
        apply_qss_role(self.left_group, role="hoverZoomCard", obj_name="GradeRosterGroup")

        self.btn_focus_left = QPushButton("⛶ 전체화면")
        apply_qss_role(self.btn_focus_left, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_left.setCheckable(True)
        self.btn_focus_left.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_left.clicked.connect(lambda: self._toggle_card_focus("left"))
        self.left_group.add_header_widget(self.btn_focus_left)

        l_lay = self.left_group.body_layout()

        top_bar = QHBoxLayout()
        btn_reload = QPushButton("🔄 환경설정 학생 명렬표 불러오기")
        apply_qss_role(btn_reload, role="primary", obj_name="BtnReloadRosterGrades")
        btn_reload.clicked.connect(self.refresh_student_preview)
        top_bar.addWidget(btn_reload)
        top_bar.addStretch()
        l_lay.addLayout(top_bar)

        self.table_grades = QTableWidget(0, 5)
        self.table_grades.setAlternatingRowColors(True)
        self.table_grades.setMouseTracking(True)
        self.table_grades.verticalHeader().setDefaultSectionSize(32)
        self.table_grades.setHorizontalHeaderLabels(["학번", "이름", "1차 지필", "수행평가", "비고/피드백"])
        header = self.table_grades.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        l_lay.addWidget(self.table_grades)

        self.right_group = HoverZoomCard(
            title="📚 교과 수업 자료 버전 및 폴더 관리 (확장용 스켈레톤)",
            zoom_ratio=1.12,
            hover_delay_ms=1000,
        )
        apply_qss_role(self.right_group, role="hoverZoomCard", obj_name="CourseMaterialGroup")

        self.btn_focus_right = QPushButton("⛶ 전체화면")
        apply_qss_role(self.btn_focus_right, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_right.setCheckable(True)
        self.btn_focus_right.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_right.clicked.connect(lambda: self._toggle_card_focus("right"))
        self.right_group.add_header_widget(self.btn_focus_right)

        r_lay = self.right_group.body_layout()
        desc = QLabel(
            "이 영역에는 학기별·단원별 수업 자료(PPT, 활동지, 평가문항) 버전 관리 및\n"
            "원클릭 폴더 트리 생성/백업 모듈이 추가됩니다."
        )
        apply_qss_role(desc, role="infoBanner", obj_name="CourseMaterialBanner")
        r_lay.addWidget(desc)
        r_lay.addStretch()

        self.splitter.addWidget(self.left_group)
        self.splitter.addWidget(self.right_group)
        self.splitter.setSizes([600, 450])
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
            self.btn_focus_left.setText("⛶ 전체화면")
            self.btn_focus_right.setChecked(False)
            self.btn_focus_right.setText("⛶ 전체화면")
            self.splitter.setSizes([600, 450])
            return

        self._focused_card = target
        if target == "left":
            self.left_group.set_zoom_enabled(False)
            self.left_group.show()
            self.right_group.hide()
            self.btn_focus_left.setChecked(True)
            self.btn_focus_left.setText("🗗 원래 크기로")
            self.btn_focus_right.setChecked(False)
            self.btn_focus_right.setText("⛶ 전체화면")
        else:
            self.right_group.set_zoom_enabled(False)
            self.left_group.hide()
            self.right_group.show()
            self.btn_focus_right.setChecked(True)
            self.btn_focus_right.setText("🗗 원래 크기로")
            self.btn_focus_left.setChecked(False)
            self.btn_focus_left.setText("⛶ 전체화면")

    def refresh_student_preview(self) -> None:
        students = self.db.get_students()
        self.table_grades.setRowCount(0)
        for idx, s in enumerate(students):
            self.table_grades.insertRow(idx)
            sid_it = QTableWidgetItem(str(s["student_id"]))
            name_it = QTableWidgetItem(s["name"])
            m1_it = QTableWidgetItem("-")
            p_it = QTableWidgetItem("-")
            memo_it = QTableWidgetItem(s.get("memo", ""))
            tip = (
                f"<div style='padding:2px 4px;'><b>[{html.escape(str(s['student_id']))}] {html.escape(s['name'])}</b><br/>"
                f"비고: {html.escape(s.get('memo', '') or '-')}</div>"
            )
            for col, it in enumerate((sid_it, name_it, m1_it, p_it, memo_it)):
                it.setToolTip(tip)
                self.table_grades.setItem(idx, col, it)
