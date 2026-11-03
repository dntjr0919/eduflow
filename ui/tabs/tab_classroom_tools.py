"""
[탭 4] 🏫 학급 경영 도구함 (ui/tabs/tab_classroom_tools.py)
- 모든 위젯에 setObjectName / setProperty("role", ...) 및 style().polish() 강제 적용
"""

import random
from typing import Optional
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QTextEdit,
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


class ClassroomToolsTab(QWidget):
    """[탭 4] 학급 경영 도구함 모듈형 스켈레톤 탭 위젯."""

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="ClassroomToolsTabPage")
        self.task_runner = task_runner
        self.db = DatabaseManager()
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        header_lbl = QLabel(
            "<b>🏫 학급 경영 미니 툴 스테이션 (모듈형 카드 레이아웃)</b> — 새로운 학급 도구 위젯을 카드 단위로 계속 추가할 수 있습니다."
        )
        apply_qss_role(header_lbl, role="infoBanner", obj_name="ClassroomHeaderBanner")
        layout.addWidget(header_lbl)

        grid = QGridLayout()
        grid.setSpacing(14)

        card_attendance = QGroupBox("📋 1. 학급 출결 체크 & 증빙서류 관리 (확장 슬롯)")
        apply_qss_role(card_attendance, obj_name="CardAttendance")
        c1_lay = QVBoxLayout(card_attendance)
        c1_lay.setContentsMargins(15, 15, 15, 15)
        c1_lay.addWidget(
            QLabel("결석계, 교외체험학습 신청서/보고서 제출 현황 체크 및 자동 서류 생성 기능이 이곳에 연결됩니다.")
        )
        c1_lay.addStretch()

        card_cleaning = QGroupBox("🧹 2. 청소 당번 / 1인 1역 랜덤 배정기")
        apply_qss_role(card_cleaning, obj_name="CardCleaning")
        c2_lay = QVBoxLayout(card_cleaning)
        c2_lay.setContentsMargins(15, 15, 15, 15)
        btn_assign = QPushButton("🎲 등록된 학생 명렬표 기준 랜덤 순번 추첨")
        apply_qss_role(btn_assign, role="primary", obj_name="BtnRandomAssign")
        btn_assign.setMinimumHeight(38)
        btn_assign.clicked.connect(self._on_random_assign)
        self.txt_cleaning_result = QTextEdit()
        apply_qss_role(self.txt_cleaning_result, obj_name="WebAiInputBox")
        self.txt_cleaning_result.setPlaceholderText("명렬표 학생들을 무작위로 섞어 당번을 배정합니다...")
        c2_lay.addWidget(btn_assign)
        c2_lay.addWidget(self.txt_cleaning_result)

        card_seating = QGroupBox("🪑 3. 학급 자리 배치 및 모둠 편성기 (확장 슬롯)")
        apply_qss_role(card_seating, obj_name="CardSeating")
        c3_lay = QVBoxLayout(card_seating)
        c3_lay.setContentsMargins(15, 15, 15, 15)
        c3_lay.addWidget(QLabel("칠판 기준 좌석 그리드 배치 및 엑셀/이미지 출력 모듈이 이곳에 연결됩니다."))
        c3_lay.addStretch()

        card_counsel = QGroupBox("📝 4. 학생 관찰·상담 메모장 (확장 슬롯)")
        apply_qss_role(card_counsel, obj_name="CardCounsel")
        c4_lay = QVBoxLayout(card_counsel)
        c4_lay.setContentsMargins(15, 15, 15, 15)
        c4_lay.addWidget(QLabel("학생별 수시 관찰 기록 및 학기말 행동특성/종합의견 초안 생성 모듈 슬롯입니다."))
        c4_lay.addStretch()

        grid.addWidget(card_attendance, 0, 0)
        grid.addWidget(card_cleaning, 0, 1)
        grid.addWidget(card_seating, 1, 0)
        grid.addWidget(card_counsel, 1, 1)

        layout.addLayout(grid, stretch=1)

    def _on_random_assign(self) -> None:
        students = self.db.get_students()
        if not students:
            names = ["김민준(샘플)", "이서연(샘플)", "박지호(샘플)", "최수아(샘플)", "정하준(샘플)"]
        else:
            names = [f"{s['student_id']} {s['name']}" for s in students]
        random.shuffle(names)
        lines = [f"{idx}번 당번: {name}" for idx, name in enumerate(names, start=1)]
        self.txt_cleaning_result.setPlainText("\n".join(lines))
