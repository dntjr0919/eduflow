"""
[탭 3] 📊 성적 및 수업 자료 관리 (ui/tabs/tab_grade_material.py)
- 모든 위젯에 setObjectName / setProperty("role", ...) 및 style().polish() 강제 적용
"""

from typing import Optional
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from core.base_worker import TaskRunner
from core.database import DatabaseManager


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
        self._init_ui()
        self.refresh_student_preview()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_group = QGroupBox("📊 학생 명렬표 연동 성적/수행평가 관리 (확장용 스켈레톤)")
        apply_qss_role(left_group, obj_name="GradeRosterGroup")
        l_lay = QVBoxLayout(left_group)
        l_lay.setContentsMargins(15, 15, 15, 15)

        top_bar = QHBoxLayout()
        btn_reload = QPushButton("🔄 환경설정 학생 명렬표 불러오기")
        apply_qss_role(btn_reload, role="primary", obj_name="BtnReloadRosterGrades")
        btn_reload.clicked.connect(self.refresh_student_preview)
        top_bar.addWidget(btn_reload)
        top_bar.addStretch()
        l_lay.addLayout(top_bar)

        self.table_grades = QTableWidget(0, 5)
        self.table_grades.setAlternatingRowColors(True)
        self.table_grades.setHorizontalHeaderLabels(["학번", "이름", "1차 지필", "수행평가", "비고/피드백"])
        header = self.table_grades.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        l_lay.addWidget(self.table_grades)

        right_group = QGroupBox("📚 교과 수업 자료 버전 및 폴더 관리 (확장용 스켈레톤)")
        apply_qss_role(right_group, obj_name="CourseMaterialGroup")
        r_lay = QVBoxLayout(right_group)
        r_lay.setContentsMargins(15, 15, 15, 15)
        desc = QLabel(
            "이 영역에는 학기별·단원별 수업 자료(PPT, 활동지, 평가문항) 버전 관리 및\n"
            "원클릭 폴더 트리 생성/백업 모듈이 추가됩니다."
        )
        apply_qss_role(desc, role="infoBanner", obj_name="CourseMaterialBanner")
        r_lay.addWidget(desc)
        r_lay.addStretch()

        splitter.addWidget(left_group)
        splitter.addWidget(right_group)
        splitter.setSizes([600, 450])
        layout.addWidget(splitter)

    def refresh_student_preview(self) -> None:
        students = self.db.get_students()
        self.table_grades.setRowCount(0)
        for idx, s in enumerate(students):
            self.table_grades.insertRow(idx)
            self.table_grades.setItem(idx, 0, QTableWidgetItem(str(s["student_id"])))
            self.table_grades.setItem(idx, 1, QTableWidgetItem(s["name"]))
            self.table_grades.setItem(idx, 2, QTableWidgetItem("-"))
            self.table_grades.setItem(idx, 3, QTableWidgetItem("-"))
            self.table_grades.setItem(idx, 4, QTableWidgetItem(s.get("memo", "")))
