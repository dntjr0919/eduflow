"""
[탭 3] 성적 및 수업 자료 관리 (ui/tabs/tab_grade_material.py)
- EduFlow Slate Workstation 디자인 시스템 적용 (Zero-Emoji, 정적 Instrument Card, 1px #E2E8F0 보더)
- 상단 KPI 요약 카드 (재적 인원, 채점 완료 인원, 미입력 특기사항 인원)
- 좌우 분할(QSplitter): 좌측 성적/수행평가 데이터 그리드 + 우측 단원별 수업자료 버전 트리 및 관리 도구
"""

import csv
import html
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import FunctionWorker, TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService
from ui.components.file_drop_helper import enable_file_drop
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


MATERIAL_STATUS_OPTIONS: List[str] = ["예정", "수업 중", "진도 완료", "보강 필요"]
PROGRESS_STATUS_OPTIONS: List[str] = ["예정", "진행중", "완료"]

AI_REFERENCE_EXTENSIONS: set[str] = {
    ".hwpx",
    ".hwp",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".xlsm",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
}

_IGNORED_MATERIAL_NAMES: set[str] = {"thumbs.db", "desktop.ini", ".ds_store"}


def _natural_sort_key(text: str) -> List[Any]:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", text or "")]


def _is_ignored_material_path(path: Path) -> bool:
    name = path.name
    if not name or name.startswith(".") or name.startswith("~$"):
        return True
    return name.lower() in _IGNORED_MATERIAL_NAMES


def format_material_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{max(0, size_bytes)} B"
    kb = size_bytes / 1024.0
    if kb < 1024.0:
        return f"{kb:.0f} KB" if kb >= 10 else f"{kb:.1f} KB"
    mb = kb / 1024.0
    return f"{mb:.1f} MB"


def extract_version_tag(filename: str) -> str:
    stem = Path(filename).stem
    m_ver = re.search(r"(?:^|[\s_\-\(])(v\d+(?:\.\d+)?)(?:$|[\s_\-\)])", stem, flags=re.IGNORECASE)
    if m_ver:
        return m_ver.group(1)
    for kw in ("최종본", "최종", "수정본", "개정판", "개정", "초안"):
        if kw in stem:
            return kw
    return ""


def build_next_version_file_path(src_file: Path) -> Path:
    parent = src_file.parent
    stem = src_file.stem
    suffix = src_file.suffix
    m = re.search(r"^(.*?)(?:([_\-\s])v(\d+))$", stem, flags=re.IGNORECASE)
    if m:
        base_stem = m.group(1)
        sep = m.group(2)
        next_num = int(m.group(3)) + 1
    else:
        base_stem = stem
        sep = "_"
        next_num = 2

    while True:
        candidate = parent / f"{base_stem}{sep}v{next_num}{suffix}"
        if not candidate.exists():
            return candidate
        next_num += 1


EVAL_COLUMN_PRESETS: List[tuple[str, List[str]]] = [
    ("2회 지필 + 수행 1개", ["1차 지필", "2차 지필", "수행평가"]),
    ("2회 지필 + 수행 2개", ["1차 지필", "2차 지필", "수행평가 1", "수행평가 2"]),
    ("1회 지필 + 수행 2개", ["지필평가", "수행평가 1", "수행평가 2"]),
    ("지필 없음 (수행 100%)", ["수행평가 1", "수행평가 2", "수행평가 3"]),
]


class EvalColumnsDialog(QDialog):
    """교과별 지필·수행 평가 항목(열) 편집 다이얼로그."""

    def __init__(
        self,
        subject_label: str,
        current_columns: List[Dict[str, str]],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("평가 항목 설정")
        self.setObjectName("RuleEditModal")
        self.setMinimumSize(500, 460)
        self._result_columns: List[Dict[str, str]] = [dict(c) for c in current_columns]
        self._build_ui(subject_label, current_columns)

    def _build_ui(self, subject_label: str, current_columns: List[Dict[str, str]]) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header_card = QFrame()
        apply_qss_role(header_card, obj_name="ModalHeaderCard")
        h_lay = QVBoxLayout(header_card)
        h_lay.setContentsMargins(14, 12, 14, 12)
        h_lay.setSpacing(4)

        lbl_title = QLabel("지필 및 수행평가 항목 설정")
        apply_qss_role(lbl_title, role="cardTitle")
        lbl_sub = QLabel(f"적용 대상: {subject_label}")
        apply_qss_role(lbl_sub, role="mutedText")
        h_lay.addWidget(lbl_title)
        h_lay.addWidget(lbl_sub)
        root.addWidget(header_card)

        # 빠른 프리셋 바
        preset_card = QFrame()
        apply_qss_role(preset_card, role="subCard")
        p_lay = QVBoxLayout(preset_card)
        p_lay.setContentsMargins(12, 10, 12, 10)
        p_lay.setSpacing(6)
        lbl_preset = QLabel("빠른 구성 프리셋 (클릭 시 아래 목록에 반영)")
        apply_qss_role(lbl_preset, role="fieldLabel")
        p_lay.addWidget(lbl_preset)

        preset_btn_row = QHBoxLayout()
        preset_btn_row.setSpacing(6)
        for preset_title, preset_names in EVAL_COLUMN_PRESETS:
            btn_p = QPushButton(preset_title)
            apply_qss_role(btn_p, role="tagChip")
            btn_p.setAutoDefault(False)
            btn_p.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_p.clicked.connect(
                lambda _checked=False, names=preset_names: self._apply_preset(names)
            )
            preset_btn_row.addWidget(btn_p)
        preset_btn_row.addStretch()
        p_lay.addLayout(preset_btn_row)
        root.addWidget(preset_card)

        # 중앙 항목 리스트 + 순서/편집 버튼
        mid_row = QHBoxLayout()
        mid_row.setSpacing(10)

        self.list_cols = QListWidget()
        self.list_cols.setAlternatingRowColors(True)
        self.list_cols.itemDoubleClicked.connect(lambda _it: self._on_rename_selected())
        mid_row.addWidget(self.list_cols, stretch=1)

        side_btns = QVBoxLayout()
        side_btns.setSpacing(6)
        self.btn_rename = QPushButton("이름 변경")
        self.btn_up = QPushButton("위로 이동")
        self.btn_down = QPushButton("아래로 이동")
        self.btn_delete = QPushButton("선택 삭제")
        apply_qss_role(self.btn_rename, role="secondary")
        apply_qss_role(self.btn_up, role="subtle")
        apply_qss_role(self.btn_down, role="subtle")
        apply_qss_role(self.btn_delete, role="danger")
        for b in (self.btn_rename, self.btn_up, self.btn_down, self.btn_delete):
            b.setAutoDefault(False)
            b.setMinimumWidth(92)
            b.setMinimumHeight(32)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            side_btns.addWidget(b)
        side_btns.addStretch()
        self.btn_rename.clicked.connect(self._on_rename_selected)
        self.btn_up.clicked.connect(lambda: self._move_selected(-1))
        self.btn_down.clicked.connect(lambda: self._move_selected(1))
        self.btn_delete.clicked.connect(self._on_delete_selected)
        mid_row.addLayout(side_btns)
        root.addLayout(mid_row, stretch=1)

        # 새 항목 추가 입력 바
        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.edit_new_name = QLineEdit()
        self.edit_new_name.setMinimumHeight(34)
        self.edit_new_name.setPlaceholderText("새 평가 항목 이름 (예: 2차 지필, 실험보고서, 포트폴리오)...")
        self.edit_new_name.returnPressed.connect(self._on_add_column)
        self.btn_add = QPushButton("항목 추가")
        apply_qss_role(self.btn_add, role="secondary")
        self.btn_add.setAutoDefault(False)
        self.btn_add.setMinimumHeight(34)
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.clicked.connect(self._on_add_column)
        add_row.addWidget(self.edit_new_name, stretch=1)
        add_row.addWidget(self.btn_add)
        root.addLayout(add_row)

        lbl_guide = QLabel("항목 이름을 더블클릭하거나 [이름 변경]으로 수정해도 이미 입력된 점수는 그대로 유지됩니다.")
        apply_qss_role(lbl_guide, role="mutedText")
        root.addWidget(lbl_guide)

        # 하단 확인/취소 바
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        bottom_row.addStretch()
        btn_cancel = QPushButton("취소")
        apply_qss_role(btn_cancel, role="subtle")
        btn_cancel.setAutoDefault(False)
        btn_cancel.setMinimumHeight(34)
        btn_cancel.clicked.connect(self.reject)

        btn_save = QPushButton("저장 및 적용")
        apply_qss_role(btn_save, role="primary")
        btn_save.setAutoDefault(False)
        btn_save.setMinimumHeight(34)
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(self._on_accept)

        bottom_row.addWidget(btn_cancel)
        bottom_row.addWidget(btn_save)
        root.addLayout(bottom_row)

        self._load_items(current_columns)

    def _load_items(self, cols: List[Dict[str, str]]) -> None:
        self.list_cols.clear()
        for c in cols:
            it = QListWidgetItem(str(c["name"]))
            it.setData(Qt.ItemDataRole.UserRole, str(c["id"]))
            self.list_cols.addItem(it)
        if self.list_cols.count() > 0:
            self.list_cols.setCurrentRow(0)

    def _current_items_as_dicts(self) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        for i in range(self.list_cols.count()):
            it = self.list_cols.item(i)
            if it is not None:
                items.append({"id": str(it.data(Qt.ItemDataRole.UserRole)), "name": it.text().strip()})
        return items

    def _apply_preset(self, names: List[str]) -> None:
        existing_by_name = {c["name"]: c["id"] for c in self._current_items_as_dicts()}
        used_ids = set()
        new_cols: List[Dict[str, str]] = []
        for name in names:
            cid = existing_by_name.get(name)
            if not cid or cid in used_ids:
                if name == "1차 지필" and "exam_1" not in used_ids:
                    cid = "exam_1"
                elif name == "2차 지필" and "exam_2" not in used_ids:
                    cid = "exam_2"
                elif name == "수행평가" and "perf_1" not in used_ids:
                    cid = "perf_1"
                else:
                    cid = f"col_{uuid.uuid4().hex[:8]}"
            used_ids.add(cid)
            new_cols.append({"id": cid, "name": name})
        self._load_items(new_cols)

    def _on_add_column(self) -> None:
        name = self.edit_new_name.text().strip()
        if not name:
            return
        existing_names = {c["name"] for c in self._current_items_as_dicts()}
        if name in existing_names:
            QMessageBox.warning(self, "중복 항목", f"'{name}' 항목이 이미 목록에 있습니다.")
            return
        it = QListWidgetItem(name)
        it.setData(Qt.ItemDataRole.UserRole, f"col_{uuid.uuid4().hex[:8]}")
        self.list_cols.addItem(it)
        self.list_cols.setCurrentItem(it)
        self.edit_new_name.clear()
        self.edit_new_name.setFocus()

    def _on_rename_selected(self) -> None:
        row = self.list_cols.currentRow()
        it = self.list_cols.item(row) if row >= 0 else None
        if it is None:
            return
        old_name = it.text().strip()
        new_name, ok = QInputDialog.getText(
            self, "평가 항목 이름 변경", "새 항목 이름을 입력하세요:", QLineEdit.EchoMode.Normal, old_name
        )
        if not ok:
            return
        clean = new_name.strip()
        if not clean or clean == old_name:
            return
        for i in range(self.list_cols.count()):
            if i != row and self.list_cols.item(i).text().strip() == clean:
                QMessageBox.warning(self, "중복 항목", f"'{clean}' 항목이 이미 존재합니다.")
                return
        it.setText(clean)

    def _move_selected(self, delta: int) -> None:
        row = self.list_cols.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.list_cols.count():
            return
        it = self.list_cols.takeItem(row)
        self.list_cols.insertItem(target, it)
        self.list_cols.setCurrentRow(target)

    def _on_delete_selected(self) -> None:
        if self.list_cols.count() <= 1:
            QMessageBox.warning(self, "삭제 불가", "최소 1개 이상의 평가 항목이 필요합니다.")
            return
        row = self.list_cols.currentRow()
        if row < 0:
            return
        self.list_cols.takeItem(row)

    def _on_accept(self) -> None:
        cols = self._current_items_as_dicts()
        if not cols:
            QMessageBox.warning(self, "항목 확인", "최소 1개 이상의 평가 항목을 추가해 주세요.")
            return
        self._result_columns = cols
        self.accept()

    def result_columns(self) -> List[Dict[str, str]]:
        return [dict(c) for c in self._result_columns]


class GradeMaterialTab(QWidget):
    """[탭 3] 성적 및 수업 자료 관리 워크스테이션 탭 위젯."""

    ai_reference_requested = pyqtSignal(str)

    ROLE_ABS_PATH = int(Qt.ItemDataRole.UserRole)
    ROLE_REL_PATH = int(Qt.ItemDataRole.UserRole) + 1
    ROLE_NODE_KIND = int(Qt.ItemDataRole.UserRole) + 2

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="GradeMaterialTabPage")
        self.task_runner = task_runner
        self.db = DatabaseManager()
        self._focused_card: Optional[str] = None
        self._updating_table: bool = False
        self._loading_material_meta: bool = False
        self._editing_progress_id: Optional[int] = None
        self._comcigan_rows: List[Dict[str, Any]] = []
        self._eval_columns: List[Dict[str, str]] = []
        self.materials_base_dir = Path("workspace") / "course_materials"
        self._current_material_root: Path = self.materials_base_dir.resolve()

        self._init_ui()
        self._populate_subject_combo()
        self.refresh_student_preview()
        self._refresh_progress_course_combo()
        self._refresh_progress_table()

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
        # 2. 상단 Pill 버튼 바 + 하단 단일 전체 폭 QStackedWidget
        # -----------------------------------------------------------------
        self.pill_bar = QFrame()
        pill_bar_lay = QHBoxLayout(self.pill_bar)
        pill_bar_lay.setContentsMargins(0, 0, 0, 0)
        pill_bar_lay.setSpacing(8)
        self.pill_btn_group = QButtonGroup(self)
        self.pill_btn_group.setExclusive(True)
        self.tool_pills: List[QPushButton] = []
        pill_titles = [
            "1. 학생 명렬표 연동 성적·수행평가 관리",
            "2. 교과 수업자료 폴더",
            "3. 학반별 진도 관리 (컴시간 연동)",
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

        # === [메뉴 1] 학생 명렬표 연동 성적/수행평가 관리 ===
        self.left_group = HoverZoomCard(
            title="1. 학생 명렬표 연동 성적 및 수행평가 관리",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.left_group, role="hoverZoomCard", obj_name="GradeRosterGroup")

        l_lay = self.left_group.body_layout()

        # [교과 연동 바] 교과 강좌 선택 콤보박스 및 교과 연동 배지
        subject_bar = QHBoxLayout()
        subject_bar.setSpacing(8)
        lbl_sub_title = QLabel("교과 강좌:")
        apply_qss_role(lbl_sub_title, role="fieldLabel")

        self.combo_subject_roster = QComboBox()
        self.combo_subject_roster.setMinimumHeight(34)
        self.combo_subject_roster.setMinimumWidth(220)
        self.combo_subject_roster.currentIndexChanged.connect(self._on_subject_roster_changed)

        self.lbl_subject_badge = QLabel("연동 명렬표: [교과 출석부] 등록된 교과 없음")
        apply_qss_role(self.lbl_subject_badge, role="badgeWarning")

        self.btn_config_columns = QPushButton("평가 항목 설정")
        apply_qss_role(self.btn_config_columns, role="secondary", obj_name="BtnConfigEvalColumns")
        self.btn_config_columns.setMinimumHeight(34)
        self.btn_config_columns.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_config_columns.setToolTip("과목별 1·2차 지필 및 수행평가 항목을 추가·수정·삭제합니다.")
        self.btn_config_columns.clicked.connect(self._on_open_eval_columns_dialog)

        subject_bar.addWidget(lbl_sub_title)
        subject_bar.addWidget(self.combo_subject_roster)
        subject_bar.addWidget(self.lbl_subject_badge)
        subject_bar.addStretch()
        subject_bar.addWidget(self.btn_config_columns)
        l_lay.addLayout(subject_bar)

        # 등록된 교과 명렬표가 없을 경우 안내 배너 노출
        self.banner_no_subject = QLabel(
            "등록된 교과 출석부가 없습니다. [환경설정] 탭의 [교과 출석부 명렬표]에서 나이스 교과출석부(.xlsx)를 먼저 등록해 주세요."
        )
        apply_qss_role(self.banner_no_subject, role="infoBanner")
        self.banner_no_subject.setVisible(False)
        l_lay.addWidget(self.banner_no_subject)

        # 상단 툴바: 검색어 필터 + 명렬표 동기화 + 엑셀 내보내기
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.edit_grade_search = QLineEdit()
        self.edit_grade_search.setMinimumHeight(34)
        self.edit_grade_search.setPlaceholderText("학번, 이름, 점수, 비고/피드백 검색...")
        self.edit_grade_search.textChanged.connect(self._filter_grade_rows)

        self.btn_reload = QPushButton("명렬표 동기화")
        apply_qss_role(self.btn_reload, role="primary", obj_name="BtnReloadRosterGrades")
        self.btn_reload.setMinimumHeight(34)
        self.btn_reload.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reload.clicked.connect(self._on_reload_clicked)

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
        self.table_grades = QTableWidget(0, 6)
        self.table_grades.setAlternatingRowColors(True)
        self.table_grades.setMouseTracking(True)
        self.table_grades.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_grades.verticalHeader().setDefaultSectionSize(32)
        self.table_grades.verticalHeader().setVisible(False)
        header = self.table_grades.horizontalHeader()
        header.setMinimumHeight(32)
        header.setMinimumSectionSize(72)
        header.setDefaultSectionSize(76)
        header.setStyleSheet(
            "QHeaderView::section { background-color: #F8FAFC; color: #64748B; height: 32px; "
            "border: none; border-bottom: 1px solid #E2E8F0; border-right: 1px solid #F1F5F9; "
            "font-size: 11px; font-weight: 600; padding: 0 8px; }"
        )
        header.sectionDoubleClicked.connect(self._on_header_double_clicked)
        self.table_grades.itemChanged.connect(self._on_grade_item_changed)
        l_lay.addWidget(self.table_grades, stretch=1)

        footer_bar = QHBoxLayout()
        self.lbl_grade_summary = QLabel(
            "셀을 더블클릭해 점수·비고를 편집하면 자동 저장되며, [평가 항목 설정] 또는 열 제목 더블클릭으로 지필·수행 항목을 수정할 수 있습니다."
        )
        apply_qss_role(self.lbl_grade_summary, role="mutedText")
        footer_bar.addWidget(self.lbl_grade_summary)
        footer_bar.addStretch()
        l_lay.addLayout(footer_bar)

        # === [메뉴 2] 교과 수업자료 폴더 ===
        self.right_group = HoverZoomCard(
            title="2. 교과 수업자료 폴더 (단원·버전·백업·AI 전달)",
            parent=container,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.right_group, role="hoverZoomCard", obj_name="CourseMaterialGroup")

        r_lay = self.right_group.body_layout()

        # 1) 과목별 연결 폴더 바
        self.folder_card = QFrame()
        apply_qss_role(self.folder_card, role="subCard")
        folder_lay = QHBoxLayout(self.folder_card)
        folder_lay.setContentsMargins(10, 8, 10, 8)
        folder_lay.setSpacing(8)

        self.lbl_linked_folder = QLabel("연결 폴더: 확인 중...")
        apply_qss_role(self.lbl_linked_folder, role="mutedText")
        self.lbl_linked_folder.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self.btn_link_folder = QPushButton("수업 폴더 연결")
        apply_qss_role(self.btn_link_folder, role="primary", obj_name="BtnLinkMaterialFolder")
        self.btn_link_folder.setMinimumHeight(32)
        self.btn_link_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_link_folder.setToolTip("내 PC에서 실제로 사용하는 과목별 수업 자료 폴더를 선택하거나 드래그앤 드롭하세요.")
        self.btn_link_folder.clicked.connect(self._on_select_material_folder)

        for drop_w in (self.folder_card, self.btn_link_folder):
            enable_file_drop(
                drop_w,
                allow_dirs=True,
                allow_files=False,
                multiple=False,
                on_files_dropped=lambda paths: self.link_subject_material_folder(paths[0]),
                context_label="과목 수업 자료 폴더",
            )

        self.btn_refresh_materials = QPushButton("새로고침")
        apply_qss_role(self.btn_refresh_materials, role="subtle", obj_name="BtnRefreshMaterials")
        self.btn_refresh_materials.setMinimumHeight(32)
        self.btn_refresh_materials.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_materials.setToolTip("연결된 로컬 폴더의 단원·파일 목록을 다시 읽어들입니다.")
        self.btn_refresh_materials.clicked.connect(lambda: self._refresh_material_panel_for_subject())

        folder_lay.addWidget(self.lbl_linked_folder, stretch=1)
        folder_lay.addWidget(self.btn_link_folder)
        folder_lay.addWidget(self.btn_refresh_materials)
        r_lay.addWidget(self.folder_card)

        # 2) 단원 폴더 필터 & 검색어 입력 바
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        self.combo_unit_filter = QComboBox()
        self.combo_unit_filter.setMinimumHeight(34)
        self.combo_unit_filter.addItem("전체 단원 보기")
        install_no_scroll_wheel_filter(self.combo_unit_filter)
        self.combo_unit_filter.currentTextChanged.connect(self._on_unit_filter_changed)

        self.edit_material_search = QLineEdit()
        self.edit_material_search.setMinimumHeight(34)
        self.edit_material_search.setPlaceholderText("단원명, 파일명, 버전, 진도 메모 검색...")
        self.edit_material_search.textChanged.connect(self._apply_material_tree_filter)

        filter_row.addWidget(self.combo_unit_filter, stretch=1)
        filter_row.addWidget(self.edit_material_search, stretch=1)
        r_lay.addLayout(filter_row)

        # 빈 폴더 안내 배너
        self.banner_empty_materials = QLabel(
            "연결된 폴더에 수업 자료가 없습니다. [수업 폴더 연결]로 기존 수업 자료 폴더를 지정하거나 [새 단원 폴더] · [자료 파일 추가]로 구성을 시작하세요."
        )
        apply_qss_role(self.banner_empty_materials, role="infoBanner")
        self.banner_empty_materials.setWordWrap(True)
        self.banner_empty_materials.setVisible(False)
        r_lay.addWidget(self.banner_empty_materials)

        # 3) 실제 로컬 디렉토리 계층 트리 뷰어 (더블클릭으로 파일 즉시 실행)
        self.tree_materials = QTreeWidget()
        self.tree_materials.setColumnCount(3)
        self.tree_materials.setHeaderLabels(["단원 / 수업자료 파일명", "유형·버전", "학반 진도 및 파일 정보"])
        self.tree_materials.setAlternatingRowColors(True)
        tree_header = self.tree_materials.header()
        tree_header.setMinimumHeight(32)
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_materials.itemDoubleClicked.connect(self._on_material_item_double_clicked)
        self.tree_materials.currentItemChanged.connect(self._on_material_selection_changed)
        r_lay.addWidget(self.tree_materials, stretch=1)

        # 4) 하단 실용 액션 버튼 바 (2행 3열 배치로 좁은 폭에서도 글자 잘림 방지)
        action_row_top = QHBoxLayout()
        action_row_top.setSpacing(8)

        self.btn_new_unit = QPushButton("새 단원 폴더")
        apply_qss_role(self.btn_new_unit, role="secondary", obj_name="BtnNewUnitFolder")
        self.btn_new_unit.setMinimumHeight(34)
        self.btn_new_unit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_unit.setToolTip("연결된 수업 폴더 안에 새 단원 하위 폴더를 생성합니다.")
        self.btn_new_unit.clicked.connect(self._on_create_unit_subfolder)

        self.btn_upload_material = QPushButton("자료 파일 추가")
        apply_qss_role(self.btn_upload_material, role="secondary", obj_name="BtnUploadMaterial")
        self.btn_upload_material.setMinimumHeight(34)
        self.btn_upload_material.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_upload_material.setToolTip("외부 수업 자료 파일을 현재 선택한 단원 폴더로 복사합니다 (드래그앤 드롭 가능).")
        self.btn_upload_material.clicked.connect(self._on_upload_material)

        material_drop_exts = [
            ".pptx", ".ppt", ".docx", ".doc", ".pdf", ".hwp", ".hwpx",
            ".xlsx", ".xls", ".csv", ".txt", ".md", ".zip", ".png", ".jpg", ".jpeg",
        ]
        for drop_w in (self.tree_materials, self.btn_upload_material):
            enable_file_drop(
                drop_w,
                allowed_exts=material_drop_exts,
                allow_dirs=True,
                allow_files=True,
                multiple=True,
                on_files_dropped=self._on_materials_dropped,
                context_label="수업 자료 파일",
            )

        self.btn_make_version = QPushButton("버전 사본 생성")
        apply_qss_role(self.btn_make_version, role="secondary", obj_name="BtnDuplicateMaterialVersion")
        self.btn_make_version.setMinimumHeight(34)
        self.btn_make_version.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_make_version.setToolTip("선택한 파일의 다음 버전 사본(_v2, _v3 등)을 같은 폴더에 생성합니다.")
        self.btn_make_version.clicked.connect(self._on_duplicate_material_version)

        action_row_top.addWidget(self.btn_new_unit, stretch=1)
        action_row_top.addWidget(self.btn_upload_material, stretch=1)
        action_row_top.addWidget(self.btn_make_version, stretch=1)
        r_lay.addLayout(action_row_top)

        action_row_bottom = QHBoxLayout()
        action_row_bottom.setSpacing(8)

        self.btn_open_folder = QPushButton("탐색기 열기")
        apply_qss_role(self.btn_open_folder, role="subtle", obj_name="BtnOpenMaterialFolder")
        self.btn_open_folder.setMinimumHeight(34)
        self.btn_open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_folder.setToolTip("선택한 단원 폴더(또는 파일이 들어 있는 위치)를 Windows 탐색기로 엽니다.")
        self.btn_open_folder.clicked.connect(self._on_open_material_folder)

        self.btn_backup_unit = QPushButton("단원 압축 백업")
        apply_qss_role(self.btn_backup_unit, role="subtle", obj_name="BtnBackupUnitZip")
        self.btn_backup_unit.setMinimumHeight(34)
        self.btn_backup_unit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_backup_unit.setToolTip("현재 연결된 수업 자료 폴더 전체를 ZIP 파일로 백업합니다.")
        self.btn_backup_unit.clicked.connect(self._on_backup_unit_zip)

        self.btn_send_to_ai = QPushButton("AI 패널로 전달")
        apply_qss_role(self.btn_send_to_ai, role="primary", obj_name="BtnSendMaterialToAi")
        self.btn_send_to_ai.setMinimumHeight(34)
        self.btn_send_to_ai.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send_to_ai.setToolTip(
            "선택한 문서를 HWPX AI 에이전트 참고자료로 등록하거나, 단원 폴더를 세특 생성 참고 경로로 보냅니다."
        )
        self.btn_send_to_ai.clicked.connect(self._on_send_material_to_ai)

        action_row_bottom.addWidget(self.btn_open_folder, stretch=1)
        action_row_bottom.addWidget(self.btn_backup_unit, stretch=1)
        action_row_bottom.addWidget(self.btn_send_to_ai, stretch=1)
        r_lay.addLayout(action_row_bottom)

        # === [메뉴 3] 학반별 진도 관리 (컴시간 연동) ===
        self.progress_group = self._build_lesson_progress_page(container)

        self.tool_stack.addWidget(self.left_group)
        self.tool_stack.addWidget(self.right_group)
        self.tool_stack.addWidget(self.progress_group)
        self.tool_stack.setCurrentIndex(0)
        layout.addWidget(self.tool_stack, stretch=1)

        scroll.setWidget(container)
        outer_lay.addWidget(scroll)

    def _toggle_card_focus(self, target: str) -> None:
        idx = 0 if target == "left" else 1
        if 0 <= idx < len(self.tool_pills):
            self.tool_pills[idx].setChecked(True)
        self.tool_stack.setCurrentIndex(idx)

    def _populate_subject_combo(self) -> None:
        """등록된 교과 강좌 목록을 콤보박스에 바인딩합니다."""
        groups = self.db.get_roster_groups(group_type="subject")
        current_id = self.combo_subject_roster.currentData()

        self.combo_subject_roster.blockSignals(True)
        self.combo_subject_roster.clear()
        for g in groups:
            extra = f" {g['extra_info']}".rstrip() if g.get("extra_info") else ""
            self.combo_subject_roster.addItem(f"{g['group_name']}{extra}", g["id"])
        self.combo_subject_roster.blockSignals(False)

        if groups:
            target_idx = 0
            if current_id is not None:
                found = self.combo_subject_roster.findData(current_id)
                if found >= 0:
                    target_idx = found
            self.combo_subject_roster.blockSignals(True)
            self.combo_subject_roster.setCurrentIndex(target_idx)
            self.combo_subject_roster.blockSignals(False)
            self.banner_no_subject.setVisible(False)
        else:
            self.lbl_subject_badge.setText("연동 명렬표: [교과 출석부] 등록된 교과 없음")
            apply_qss_role(self.lbl_subject_badge, role="badgeWarning")
            self.banner_no_subject.setVisible(True)

    def _on_subject_roster_changed(self, _idx: int = 0) -> None:
        """교과 콤보박스 변경 시 연동 배지를 업데이트하고 해당 교과 학생 목록으로 갱신합니다."""
        group_id = self.combo_subject_roster.currentData()
        if not group_id:
            self.lbl_subject_badge.setText("연동 명렬표: [교과 출석부] 등록된 교과 없음")
            apply_qss_role(self.lbl_subject_badge, role="badgeWarning")
            self.banner_no_subject.setVisible(True)
        else:
            subject_name = self.combo_subject_roster.currentText()
            self.lbl_subject_badge.setText(f"연동 명렬표: [교과 출석부] {subject_name}")
            apply_qss_role(self.lbl_subject_badge, role="badgeSuccess")
            self.banner_no_subject.setVisible(False)

        self.refresh_student_preview()

    def _on_reload_clicked(self) -> None:
        self._populate_subject_combo()
        self.refresh_student_preview()

    def _current_subject_key(self) -> str:
        """현재 선택된 교과 강좌의 고유 저장 키를 반환합니다 (미등록 시 '__default__')."""
        group_id = self.combo_subject_roster.currentData()
        if group_id:
            grp = self.db.get_roster_group(int(group_id))
            if grp:
                g_name = str(grp.get("group_name", "")).strip()
                g_extra = str(grp.get("extra_info", "")).strip()
                return f"{g_name}|{g_extra}" if g_name else f"group_{group_id}"
            return f"group_{group_id}"
        return "__default__"

    def _apply_eval_columns_to_table(self) -> None:
        """현재 과목의 평가 항목 설정을 읽어 테이블 헤더와 열 크기를 재구성합니다."""
        self._eval_columns = self.db.get_subject_eval_columns(self._current_subject_key())
        headers = ["학번", "이름"] + [c["name"] for c in self._eval_columns] + ["비고/피드백"]
        self.table_grades.setColumnCount(len(headers))
        self.table_grades.setHorizontalHeaderLabels(headers)

        header = self.table_grades.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for idx in range(len(self._eval_columns)):
            header.setSectionResizeMode(2 + idx, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)

    def _on_header_double_clicked(self, logical_index: int) -> None:
        last_col = self.table_grades.columnCount() - 1
        if 2 <= logical_index < last_col:
            self._on_open_eval_columns_dialog()

    def _on_open_eval_columns_dialog(self) -> None:
        subject_key = self._current_subject_key()
        if subject_key == "__default__":
            subject_label = "기본 평가 양식 (교과 출석부 등록 시 초기값으로 적용)"
        else:
            subject_label = self.combo_subject_roster.currentText().strip() or subject_key

        current_cols = self.db.get_subject_eval_columns(subject_key)
        dlg = EvalColumnsDialog(subject_label, current_cols, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.db.save_subject_eval_columns(subject_key, dlg.result_columns())
            self.refresh_student_preview()

    def _refresh_row_tooltip(self, row: int) -> None:
        sid_it = self.table_grades.item(row, 0)
        name_it = self.table_grades.item(row, 1)
        if not sid_it or not name_it:
            return
        sid_str = sid_it.text().strip()
        name_str = name_it.text().strip()
        seq_no = sid_it.data(Qt.ItemDataRole.UserRole) or (row + 1)
        score_parts: List[str] = []
        for idx, col_meta in enumerate(self._eval_columns):
            cell = self.table_grades.item(row, 2 + idx)
            val = cell.text().strip() if cell and cell.text().strip() else "-"
            score_parts.append(f"{html.escape(col_meta['name'])}: {html.escape(val)}")
        memo_col = 2 + len(self._eval_columns)
        memo_it = self.table_grades.item(row, memo_col)
        memo_val = memo_it.text().strip() if memo_it else ""
        tip = (
            f"<div style='padding:2px 4px;'><b>[연번 {seq_no}] [{html.escape(sid_str)}] {html.escape(name_str)}</b><br/>"
            f"{' / '.join(score_parts)}<br/>"
            f"비고: {html.escape(memo_val or '-')}</div>"
        )
        for c in range(self.table_grades.columnCount()):
            it = self.table_grades.item(row, c)
            if it is not None:
                it.setToolTip(tip)

    def refresh_student_preview(self) -> None:
        """선택된 교과 출석부에서 학생 명단과 저장된 성적·평가 항목을 읽어 그리드와 KPI 카드를 갱신합니다."""
        group_id = self.combo_subject_roster.currentData()
        if group_id:
            students = self.db.get_roster_students(group_id)
            subject_name = self.combo_subject_roster.currentText()
            self.lbl_subject_badge.setText(f"연동 명렬표: [교과 출석부] {subject_name}")
            apply_qss_role(self.lbl_subject_badge, role="badgeSuccess")
            self.banner_no_subject.setVisible(False)
        else:
            students = []
            self.lbl_subject_badge.setText("연동 명렬표: [교과 출석부] 등록된 교과 없음")
            apply_qss_role(self.lbl_subject_badge, role="badgeWarning")
            self.banner_no_subject.setVisible(True)

        subject_key = self._current_subject_key()
        grades_map = self.db.get_subject_grades_map(subject_key)

        self._updating_table = True
        tabular_font = QFont("JetBrains Mono", 10)
        tabular_font.setStyleHint(QFont.StyleHint.Monospace)

        try:
            self._apply_eval_columns_to_table()
            self.table_grades.setRowCount(0)
            for idx, s in enumerate(students):
                self.table_grades.insertRow(idx)
                sid_str = str(s["student_id"])
                seq_no = s.get("seq_no", idx + 1)
                saved_entry = grades_map.get(sid_str)
                scores_dict = saved_entry["scores"] if saved_entry else {}
                memo_val = (
                    saved_entry["memo"]
                    if saved_entry is not None
                    else str(s.get("memo", "") or "")
                )

                sid_it = QTableWidgetItem(sid_str)
                sid_it.setData(Qt.ItemDataRole.UserRole, seq_no)
                sid_it.setFont(tabular_font)
                sid_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                sid_it.setFlags(sid_it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table_grades.setItem(idx, 0, sid_it)

                name_it = QTableWidgetItem(s["name"])
                name_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                name_it.setFlags(name_it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table_grades.setItem(idx, 1, name_it)

                for c_offset, col_meta in enumerate(self._eval_columns):
                    raw_val = str(scores_dict.get(col_meta["id"], "-")).strip() or "-"
                    score_it = QTableWidgetItem(raw_val)
                    score_it.setFont(tabular_font)
                    score_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.table_grades.setItem(idx, 2 + c_offset, score_it)

                memo_it = QTableWidgetItem(memo_val)
                self.table_grades.setItem(idx, 2 + len(self._eval_columns), memo_it)
                self._refresh_row_tooltip(idx)
        finally:
            self._updating_table = False

        self._update_kpi_metrics()
        self._filter_grade_rows(self.edit_grade_search.text())
        self._refresh_material_panel_for_subject()

    def _on_grade_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table:
            return
        row = item.row()
        sid_item = self.table_grades.item(row, 0)
        if not sid_item:
            return
        sid = sid_item.text().strip()
        subject_key = self._current_subject_key()
        existing_map = self.db.get_subject_grades_map(subject_key)
        scores: Dict[str, str] = dict(existing_map.get(sid, {}).get("scores", {}))

        self._updating_table = True
        try:
            for idx, col_meta in enumerate(self._eval_columns):
                c_item = self.table_grades.item(row, 2 + idx)
                val = (c_item.text() if c_item else "-").strip() or "-"
                if c_item is not None and c_item.text() != val:
                    c_item.setText(val)
                scores[col_meta["id"]] = val

            memo_col = 2 + len(self._eval_columns)
            memo_it = self.table_grades.item(row, memo_col)
            memo_txt = (memo_it.text() if memo_it else "").strip()
            self._refresh_row_tooltip(row)
        finally:
            self._updating_table = False

        self.db.upsert_subject_student_grade(subject_key, sid, scores, memo_txt)
        self._update_kpi_metrics()

    def _update_kpi_metrics(self) -> None:
        total = self.table_grades.rowCount()
        graded = 0
        unentered_notes = 0
        eval_cnt = len(self._eval_columns)
        memo_col = 2 + eval_cnt

        for r in range(total):
            has_score = False
            for idx in range(eval_cnt):
                s_it = self.table_grades.item(r, 2 + idx)
                s_txt = s_it.text().strip() if s_it else "-"
                if s_txt and s_txt != "-":
                    has_score = True
                    break
            if has_score:
                graded += 1

            memo_it = self.table_grades.item(r, memo_col)
            memo_txt = memo_it.text().strip() if memo_it else ""
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
        subj_raw = self.combo_subject_roster.currentText().strip()
        safe_subj = re.sub(r'[\\/:*?"<>|]+', "_", subj_raw).strip("_") if subj_raw else "전체"
        default_file = f"성적일람표_{safe_subj}_{datetime.now().strftime('%Y%m%d')}.csv"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "성적 및 수행평가 일람표 내보내기",
            default_file,
            "CSV 파일 (*.csv);;Excel 파일 (*.xlsx)",
        )
        if not file_path:
            return

        col_count = self.table_grades.columnCount()
        headers = [
            (self.table_grades.horizontalHeaderItem(c).text() if self.table_grades.horizontalHeaderItem(c) else "")
            for c in range(col_count)
        ]
        rows: List[List[str]] = []
        for r in range(self.table_grades.rowCount()):
            rows.append(
                [
                    self.table_grades.item(r, c).text() if self.table_grades.item(r, c) else ""
                    for c in range(col_count)
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
    # 우측 패널: 과목별 로컬 수업자료 폴더 연동 · 학반별 진도 기록 · 버전/AI 액션
    # =========================================================================
    def _default_subject_material_dir(self) -> Path:
        group_id = self.combo_subject_roster.currentData()
        folder_label = "기본_수업자료"
        if group_id:
            grp = self.db.get_roster_group(int(group_id))
            if grp and str(grp.get("group_name", "")).strip():
                folder_label = str(grp["group_name"]).strip()
            elif self.combo_subject_roster.currentText().strip():
                folder_label = self.combo_subject_roster.currentText().strip()
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", folder_label).strip(" _") or "기본_수업자료"
        target = (self.materials_base_dir / safe_name).resolve()
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _resolve_material_root(self) -> tuple[Path, str]:
        """(실제 사용할 폴더 Path, 상태 태그 'custom' | 'missing' | 'default')를 반환합니다."""
        saved = self.db.get_subject_material_folder(self._current_subject_key())
        if saved:
            cand = Path(saved)
            try:
                if cand.exists() and cand.is_dir():
                    return cand.resolve(), "custom"
            except OSError:
                return self._default_subject_material_dir(), "missing"
            return self._default_subject_material_dir(), "missing"
        return self._default_subject_material_dir(), "default"

    @staticmethod
    def _shorten_path_for_label(path_obj: Path, max_chars: int = 40) -> str:
        full = str(path_obj)
        if len(full) <= max_chars:
            return full
        parts = path_obj.parts
        if len(parts) >= 3:
            tail = os.path.join(parts[-2], parts[-1])
            return f"{parts[0]}...\\{tail}"
        return "..." + full[-(max_chars - 3) :]

    @staticmethod
    def _status_color(status_text: str) -> QColor:
        if status_text == "진도 완료":
            return QColor("#059669")
        if status_text == "수업 중":
            return QColor("#004AC6")
        if status_text == "보강 필요":
            return QColor("#BA1A1A")
        return QColor("#64748B")

    def _refresh_material_panel_for_subject(self, preserve_rel_path: str = "") -> None:
        prev_rel = preserve_rel_path
        if not prev_rel:
            curr_it = self.tree_materials.currentItem()
            if curr_it is not None:
                prev_rel = str(curr_it.data(0, self.ROLE_REL_PATH) or "")

        root_dir, mode = self._resolve_material_root()
        self._current_material_root = root_dir
        short_p = self._shorten_path_for_label(root_dir)
        if mode == "custom":
            self.lbl_linked_folder.setText(f"연결 폴더: {short_p}")
            self.lbl_linked_folder.setToolTip(f"연결된 내 PC 수업 폴더:\n{root_dir}")
        elif mode == "missing":
            saved_raw = self.db.get_subject_material_folder(self._current_subject_key())
            self.lbl_linked_folder.setText(f"경로 재확인 필요 (임시 기본 폴더): {short_p}")
            self.lbl_linked_folder.setToolTip(
                f"이전에 지정한 폴더를 찾을 수 없어 기본 폴더를 표시 중입니다.\n지정 경로: {saved_raw}\n현재 경로: {root_dir}"
            )
        else:
            self.lbl_linked_folder.setText(f"기본 폴더: {short_p}")
            self.lbl_linked_folder.setToolTip(
                f"아직 외부 수업 폴더를 지정하지 않아 과목 기본 폴더를 사용 중입니다.\n경로: {root_dir}"
            )

        unit_dirs, root_files = self._scan_material_root(root_dir)
        prev_filter = self.combo_unit_filter.currentText()
        filter_items = ["전체 단원 보기"] + [d.name for d in unit_dirs]
        if root_files:
            filter_items.append("[루트 자료]")

        self.combo_unit_filter.blockSignals(True)
        self.combo_unit_filter.clear()
        self.combo_unit_filter.addItems(filter_items)
        if prev_filter in filter_items:
            self.combo_unit_filter.setCurrentText(prev_filter)
        else:
            self.combo_unit_filter.setCurrentIndex(0)
        self.combo_unit_filter.blockSignals(False)

        self._populate_unit_tree(
            filter_title=self.combo_unit_filter.currentText(),
            preserve_rel_path=prev_rel,
        )

    @staticmethod
    def _scan_material_root(root_dir: Path) -> tuple[List[Path], List[Path]]:
        unit_dirs: List[Path] = []
        root_files: List[Path] = []
        if not root_dir.exists():
            return unit_dirs, root_files
        try:
            entries = list(root_dir.iterdir())
        except OSError:
            return unit_dirs, root_files

        for entry in entries:
            if _is_ignored_material_path(entry):
                continue
            if entry.is_dir():
                unit_dirs.append(entry)
            elif entry.is_file():
                root_files.append(entry)

        unit_dirs.sort(key=lambda p: _natural_sort_key(p.name))
        root_files.sort(key=lambda p: _natural_sort_key(p.name))
        return unit_dirs, root_files

    @staticmethod
    def _scan_files_in_unit(unit_dir: Path) -> List[Path]:
        found: List[Path] = []
        try:
            for p in unit_dir.rglob("*"):
                if not p.is_file():
                    continue
                rel_parts = p.relative_to(unit_dir).parts
                if any(
                    (not part or part.startswith(".") or part.startswith("~$") or part.lower() in _IGNORED_MATERIAL_NAMES)
                    for part in rel_parts
                ):
                    continue
                found.append(p)
        except OSError:
            return []
        found.sort(key=lambda p: _natural_sort_key(p.relative_to(unit_dir).as_posix()))
        return found

    def _build_file_tree_item(
        self,
        file_path: Path,
        root_dir: Path,
        display_name: str,
        progress_map: Dict[str, Dict[str, str]],
    ) -> QTreeWidgetItem:
        rel_key = file_path.relative_to(root_dir).as_posix()
        ext_upper = file_path.suffix.lstrip(".").upper() or "FILE"
        ver_tag = extract_version_tag(file_path.name)
        type_col = f"{ext_upper} · {ver_tag}" if ver_tag else ext_upper

        try:
            st = file_path.stat()
            size_str = format_material_file_size(st.st_size)
            mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%m.%d %H:%M")
        except OSError:
            size_str = "-"
            mtime_str = "-"

        meta_entry = progress_map.get(rel_key, {})
        status_val = meta_entry.get("status", "예정")
        memo_val = meta_entry.get("memo", "")

        if memo_val:
            info_col = f"[{status_val}] {memo_val} ({size_str})"
        elif status_val != "예정":
            info_col = f"[{status_val}] {size_str} · {mtime_str}"
        else:
            info_col = f"{size_str} · {mtime_str}"

        item = QTreeWidgetItem([display_name, type_col, info_col])
        item.setData(0, self.ROLE_ABS_PATH, str(file_path.resolve()))
        item.setData(0, self.ROLE_REL_PATH, rel_key)
        item.setData(0, self.ROLE_NODE_KIND, "file")
        item.setForeground(1, QColor("#475569"))
        item.setForeground(
            2,
            self._status_color(status_val) if (status_val != "예정" or memo_val) else QColor("#64748B"),
        )
        tip_lines = [
            f"<b>{html.escape(file_path.name)}</b>",
            f"경로: {html.escape(str(file_path))}",
            f"크기·수정일: {html.escape(size_str)} · {html.escape(mtime_str)}",
            f"학반 진도: {html.escape(status_val)} / 메모: {html.escape(memo_val or '-')}",
            "더블클릭하면 연결 프로그램으로 즉시 엽니다.",
        ]
        for col in range(3):
            item.setToolTip(col, "<div style='padding:2px 4px;'>" + "<br/>".join(tip_lines) + "</div>")
        return item

    def _populate_unit_tree(
        self,
        filter_title: str = "전체 단원 보기",
        preserve_rel_path: str = "",
    ) -> None:
        root_dir = self._current_material_root
        unit_dirs, root_files = self._scan_material_root(root_dir)
        progress_map = self.db.get_subject_material_progress_map(self._current_subject_key())

        has_any_content = bool(unit_dirs or root_files)
        self.banner_empty_materials.setVisible(not has_any_content)

        self.tree_materials.blockSignals(True)
        self.tree_materials.clear()
        bold_font = QFont()
        bold_font.setBold(True)
        item_to_select: Optional[QTreeWidgetItem] = None

        try:
            for u_dir in unit_dirs:
                if filter_title not in ("전체 단원 보기", "") and filter_title != u_dir.name:
                    continue
                rel_unit = u_dir.relative_to(root_dir).as_posix()
                files_in_unit = self._scan_files_in_unit(u_dir)
                u_meta = progress_map.get(rel_unit, {})
                u_status = u_meta.get("status", "예정")
                u_memo = u_meta.get("memo", "")

                if u_memo:
                    status_summary = f"[{u_status}] {u_memo} ({len(files_in_unit)}개)"
                else:
                    status_summary = f"{u_status} ({len(files_in_unit)}개 파일)"

                parent_item = QTreeWidgetItem([u_dir.name, "단원 폴더", status_summary])
                parent_item.setData(0, self.ROLE_ABS_PATH, str(u_dir.resolve()))
                parent_item.setData(0, self.ROLE_REL_PATH, rel_unit)
                parent_item.setData(0, self.ROLE_NODE_KIND, "dir")
                parent_item.setFont(0, bold_font)
                parent_item.setForeground(0, QColor("#0F172A"))
                parent_item.setForeground(1, QColor("#475569"))
                parent_item.setForeground(2, self._status_color(u_status))
                u_tip = (
                    f"<div style='padding:2px 4px;'><b>[단원 폴더] {html.escape(u_dir.name)}</b><br/>"
                    f"경로: {html.escape(str(u_dir))}<br/>"
                    f"포함 파일: {len(files_in_unit)}개<br/>"
                    f"학반 진도: {html.escape(u_status)} / 메모: {html.escape(u_memo or '-')}</div>"
                )
                for col in range(3):
                    parent_item.setToolTip(col, u_tip)
                self.tree_materials.addTopLevelItem(parent_item)
                if preserve_rel_path and rel_unit == preserve_rel_path:
                    item_to_select = parent_item

                for f_path in files_in_unit:
                    disp = f_path.relative_to(u_dir).as_posix()
                    child = self._build_file_tree_item(f_path, root_dir, disp, progress_map)
                    parent_item.addChild(child)
                    if preserve_rel_path and child.data(0, self.ROLE_REL_PATH) == preserve_rel_path:
                        item_to_select = child

                parent_item.setExpanded(True)

            if root_files and filter_title in ("전체 단원 보기", "", "[루트 자료]"):
                root_rel = "__root__"
                r_meta = progress_map.get(root_rel, {})
                r_status = r_meta.get("status", "예정")
                r_memo = r_meta.get("memo", "")
                r_summary = (
                    f"[{r_status}] {r_memo} ({len(root_files)}개)"
                    if r_memo
                    else f"{r_status} ({len(root_files)}개 파일)"
                )
                root_group_item = QTreeWidgetItem(["[공통 · 루트 자료]", "폴더", r_summary])
                root_group_item.setData(0, self.ROLE_ABS_PATH, str(root_dir.resolve()))
                root_group_item.setData(0, self.ROLE_REL_PATH, root_rel)
                root_group_item.setData(0, self.ROLE_NODE_KIND, "root_group")
                root_group_item.setFont(0, bold_font)
                root_group_item.setForeground(0, QColor("#0F172A"))
                root_group_item.setForeground(2, self._status_color(r_status))
                self.tree_materials.addTopLevelItem(root_group_item)
                if preserve_rel_path == root_rel:
                    item_to_select = root_group_item

                for rf in root_files:
                    child = self._build_file_tree_item(rf, root_dir, rf.name, progress_map)
                    root_group_item.addChild(child)
                    if preserve_rel_path and child.data(0, self.ROLE_REL_PATH) == preserve_rel_path:
                        item_to_select = child

                root_group_item.setExpanded(True)
        finally:
            self.tree_materials.blockSignals(False)

        self._apply_material_tree_filter(self.edit_material_search.text())

        if item_to_select is not None:
            self.tree_materials.setCurrentItem(item_to_select)
        elif self.tree_materials.topLevelItemCount() > 0:
            self.tree_materials.setCurrentItem(self.tree_materials.topLevelItem(0))
        else:
            self._on_material_selection_changed(None, None)

    def _on_unit_filter_changed(self, text: str) -> None:
        self._populate_unit_tree(filter_title=text)

    def _apply_material_tree_filter(self, keyword: str = "") -> None:
        kw = (keyword or "").strip().lower()
        for i in range(self.tree_materials.topLevelItemCount()):
            top_it = self.tree_materials.topLevelItem(i)
            if top_it is None:
                continue
            if not kw:
                top_it.setHidden(False)
                for j in range(top_it.childCount()):
                    ch = top_it.child(j)
                    if ch is not None:
                        ch.setHidden(False)
                continue

            parent_text = " ".join(top_it.text(c) for c in range(3)).lower()
            parent_match = kw in parent_text
            any_child_match = False
            for j in range(top_it.childCount()):
                ch = top_it.child(j)
                if ch is None:
                    continue
                child_text = " ".join(ch.text(c) for c in range(3)).lower()
                matched = parent_match or (kw in child_text)
                ch.setHidden(not matched)
                if kw in child_text:
                    any_child_match = True

            visible = parent_match or any_child_match
            top_it.setHidden(not visible)
            if visible and any_child_match:
                top_it.setExpanded(True)

    def _build_lesson_progress_page(self, parent: QWidget) -> HoverZoomCard:
        """[메뉴 3] 학반별 진도 관리 (컴시간 시간표 연동 + 수업_진도 양방향 동기화) 화면을 생성합니다."""
        card = HoverZoomCard(
            title="3. 학반별 진도 관리 (컴시간 시간표 연동)",
            parent=parent,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(card, role="hoverZoomCard", obj_name="LessonProgressGroup")
        p_lay = card.body_layout()

        # 1) 컴시간 시간표 연동 및 강좌·학반 선택 바
        top_bar = QFrame()
        apply_qss_role(top_bar, role="subCard")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(10, 8, 10, 8)
        tb_lay.setSpacing(8)

        lbl_course = QLabel("강좌 · 학반:")
        apply_qss_role(lbl_course, role="fieldLabel")

        self.combo_progress_course = QComboBox()
        self.combo_progress_course.setMinimumHeight(32)
        self.combo_progress_course.setMinimumWidth(240)
        install_no_scroll_wheel_filter(self.combo_progress_course)
        self.combo_progress_course.currentIndexChanged.connect(self._on_progress_course_changed)

        self.btn_load_comcigan_schedule = QPushButton("컴시간 시간표 불러오기")
        apply_qss_role(self.btn_load_comcigan_schedule, role="secondary", obj_name="BtnLoadComciganSchedule")
        self.btn_load_comcigan_schedule.setMinimumHeight(32)
        self.btn_load_comcigan_schedule.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_load_comcigan_schedule.setToolTip(
            "컴시간 시간표(오늘/이번 주 수업 및 담당 학반 목록)를 불러와 강좌·학반 선택 목록에 반영합니다."
        )
        self.btn_load_comcigan_schedule.clicked.connect(self.load_comcigan_timetable_courses)

        self.lbl_comcigan_status = QLabel("등록된 교과 명렬표 기준 (컴시간 불러오기 가능)")
        apply_qss_role(self.lbl_comcigan_status, role="mutedText")

        self.btn_sync_mobile_progress = QPushButton("모바일·어드민 진도 동기화")
        apply_qss_role(self.btn_sync_mobile_progress, role="primary", obj_name="BtnSyncMobileProgress")
        self.btn_sync_mobile_progress.setMinimumHeight(32)
        self.btn_sync_mobile_progress.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sync_mobile_progress.setToolTip(
            "데스크톱 진도 기록과 모바일(Firestore 수업_진도)을 양방향 동기화(업로드/불러오기)합니다."
        )
        self.btn_sync_mobile_progress.clicked.connect(self._on_sync_mobile_progress)

        tb_lay.addWidget(lbl_course)
        tb_lay.addWidget(self.combo_progress_course)
        tb_lay.addWidget(self.btn_load_comcigan_schedule)
        tb_lay.addWidget(self.lbl_comcigan_status, stretch=1)
        tb_lay.addWidget(self.btn_sync_mobile_progress)
        p_lay.addWidget(top_bar)

        # 2) 이번 주 / 오늘 컴시간 수업 슬롯 빠른 선택 리스트
        self.list_comcigan_slots = QListWidget()
        self.list_comcigan_slots.setMaximumHeight(74)
        self.list_comcigan_slots.setFlow(QListWidget.Flow.LeftToRight)
        self.list_comcigan_slots.setWrapping(True)
        self.list_comcigan_slots.setSpacing(4)
        self.list_comcigan_slots.setToolTip("컴시간 시간표의 수업 슬롯을 클릭하면 해당 과목·학반·교시가 자동 선택됩니다.")
        self.list_comcigan_slots.itemClicked.connect(self._on_comcigan_slot_clicked)
        p_lay.addWidget(self.list_comcigan_slots)

        # 3) 진도 입력 / 수정 폼
        form_card = QFrame()
        apply_qss_role(form_card, role="subCard")
        f_lay = QVBoxLayout(form_card)
        f_lay.setContentsMargins(10, 8, 10, 8)
        f_lay.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(8)

        self.edit_prog_date = QLineEdit(datetime.now().strftime("%Y-%m-%d"))
        self.edit_prog_date.setMinimumHeight(32)
        self.edit_prog_date.setMaximumWidth(120)
        self.edit_prog_date.setPlaceholderText("YYYY-MM-DD")

        self.spin_prog_period = QSpinBox()
        self.spin_prog_period.setMinimumHeight(32)
        self.spin_prog_period.setRange(0, 10)
        self.spin_prog_period.setSpecialValueText("교시 미지정")
        self.spin_prog_period.setSuffix("교시")
        self.spin_prog_period.setValue(1)

        self.edit_prog_unit = QLineEdit()
        self.edit_prog_unit.setMinimumHeight(32)
        self.edit_prog_unit.setPlaceholderText("단원명 (예: II. 물질과 규칙성)")

        self.edit_prog_lesson_no = QLineEdit()
        self.edit_prog_lesson_no.setMinimumHeight(32)
        self.edit_prog_lesson_no.setMaximumWidth(110)
        self.edit_prog_lesson_no.setPlaceholderText("차시 (예: 3차시)")

        self.combo_prog_status = QComboBox()
        self.combo_prog_status.setMinimumHeight(32)
        self.combo_prog_status.addItems(PROGRESS_STATUS_OPTIONS)
        install_no_scroll_wheel_filter(self.combo_prog_status)

        row1.addWidget(QLabel("수업일:"))
        row1.addWidget(self.edit_prog_date)
        row1.addWidget(QLabel("교시:"))
        row1.addWidget(self.spin_prog_period)
        row1.addWidget(QLabel("단원:"))
        row1.addWidget(self.edit_prog_unit, stretch=1)
        row1.addWidget(QLabel("차시:"))
        row1.addWidget(self.edit_prog_lesson_no)
        row1.addWidget(QLabel("상태:"))
        row1.addWidget(self.combo_prog_status)
        f_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self.edit_prog_content = QLineEdit()
        self.edit_prog_content.setMinimumHeight(32)
        self.edit_prog_content.setPlaceholderText("수업 내용 (예: 원소의 주기성 탐구 활동 완료)")

        self.edit_prog_next_plan = QLineEdit()
        self.edit_prog_next_plan.setMinimumHeight(32)
        self.edit_prog_next_plan.setPlaceholderText("다음 시간 계획 (예: 이온 결합 형성 모형 실습)")

        self.edit_prog_memo = QLineEdit()
        self.edit_prog_memo.setMinimumHeight(32)
        self.edit_prog_memo.setPlaceholderText("비고 / 학반 특이사항 메모")

        row2.addWidget(QLabel("수업 내용:"))
        row2.addWidget(self.edit_prog_content, stretch=2)
        row2.addWidget(QLabel("다음 계획:"))
        row2.addWidget(self.edit_prog_next_plan, stretch=2)
        row2.addWidget(QLabel("메모:"))
        row2.addWidget(self.edit_prog_memo, stretch=1)
        f_lay.addLayout(row2)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_prog_new = QPushButton("새로 입력")
        apply_qss_role(self.btn_prog_new, role="subtle", obj_name="BtnNewLessonProgress")
        self.btn_prog_new.setMinimumHeight(32)
        self.btn_prog_new.clicked.connect(self._on_new_lesson_progress)

        self.btn_prog_save = QPushButton("진도 기록 저장")
        apply_qss_role(self.btn_prog_save, role="primary", obj_name="BtnSaveLessonProgress")
        self.btn_prog_save.setMinimumHeight(32)
        self.btn_prog_save.clicked.connect(self._on_save_lesson_progress)

        self.btn_prog_delete = QPushButton("선택 삭제")
        apply_qss_role(self.btn_prog_delete, role="secondary", obj_name="BtnDeleteLessonProgress")
        self.btn_prog_delete.setMinimumHeight(32)
        self.btn_prog_delete.clicked.connect(self._on_delete_lesson_progress)

        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_prog_new)
        btn_row.addWidget(self.btn_prog_delete)
        btn_row.addWidget(self.btn_prog_save)
        f_lay.addLayout(btn_row)
        p_lay.addWidget(form_card)

        # 4) 학반별 진도 이력 테이블
        self.table_progress = QTableWidget(0, 8)
        self.table_progress.setAlternatingRowColors(True)
        self.table_progress.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_progress.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_progress.verticalHeader().setDefaultSectionSize(30)
        self.table_progress.verticalHeader().setVisible(False)
        self.table_progress.setHorizontalHeaderLabels(
            ["날짜", "교시", "강좌 · 학반", "단원", "차시", "수업 내용", "다음 계획", "상태"]
        )
        p_hdr = self.table_progress.horizontalHeader()
        p_hdr.setMinimumHeight(32)
        p_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        p_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        p_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        p_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        p_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        p_hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        p_hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        p_hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        self.table_progress.cellClicked.connect(self._on_progress_row_clicked)
        p_lay.addWidget(self.table_progress, stretch=1)

        return card

    def _on_material_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        self._loading_material_meta = True
        try:
            if current is None:
                return
        finally:
            self._loading_material_meta = False

    def _on_save_material_progress(self) -> None:
        return

    def _get_selected_progress_course_meta(self) -> Dict[str, str]:
        if getattr(self, "combo_progress_course", None) is not None:
            data = self.combo_progress_course.currentData()
            if isinstance(data, dict) and (data.get("subject") or data.get("roster_key")):
                return {
                    "roster_key": str(data.get("roster_key") or "").strip(),
                    "subject": str(data.get("subject") or "").strip(),
                    "class_label": str(data.get("class_label") or "").strip(),
                }
        return self._current_subject_meta()

    def _refresh_progress_course_combo(
        self,
        comcigan_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """등록된 교과 명렬표와 컴시간 시간표에서 강좌·학반 목록을 구성합니다."""
        if getattr(self, "combo_progress_course", None) is None:
            return
        if comcigan_rows is not None:
            self._comcigan_rows = list(comcigan_rows)

        prev_data = self.combo_progress_course.currentData()
        entries: List[Tuple[str, Dict[str, str]]] = []
        seen_keys = set()

        # 1) 컴시간 시간표에서 추출된 강좌·학반 우선 추가
        for row in self._comcigan_rows:
            if not isinstance(row, dict):
                continue
            subj = str(row.get("subject") or "").strip()
            if not subj:
                continue
            g = row.get("grade")
            c = row.get("class_no")
            class_label = str(row.get("class_label") or row.get("target_class") or "").strip()
            if not class_label and g and c:
                class_label = f"{g}-{c}"
            label = f"{subj} {class_label}".strip()
            rk = f"comcigan_{subj}_{class_label}".strip("_")
            dedup = (subj, class_label)
            if dedup not in seen_keys:
                seen_keys.add(dedup)
                entries.append((label, {"roster_key": rk, "subject": subj, "class_label": class_label}))

        # 2) 등록된 교과 명렬표 추가
        for grp in self.db.get_roster_groups(group_type="subject"):
            subj = str(grp.get("group_name") or "").strip()
            class_label = str(grp.get("extra_info") or "").strip()
            rk = f"subject_{grp['id']}"
            label = f"{subj} {class_label}".strip()
            dedup = (subj, class_label)
            if dedup not in seen_keys:
                seen_keys.add(dedup)
                entries.append((label, {"roster_key": rk, "subject": subj, "class_label": class_label}))

        self.combo_progress_course.blockSignals(True)
        self.combo_progress_course.clear()
        self.combo_progress_course.addItem("전체 강좌 · 학반 보기", {"roster_key": "", "subject": "", "class_label": ""})
        for label, meta in entries:
            self.combo_progress_course.addItem(label, meta)
        if isinstance(prev_data, dict):
            for i in range(self.combo_progress_course.count()):
                d = self.combo_progress_course.itemData(i)
                if isinstance(d, dict) and d.get("subject") == prev_data.get("subject") and d.get("class_label") == prev_data.get("class_label"):
                    self.combo_progress_course.setCurrentIndex(i)
                    break
        elif self.combo_progress_course.count() > 1:
            self.combo_progress_course.setCurrentIndex(1)
        self.combo_progress_course.blockSignals(False)

        self._populate_comcigan_slots_list()

    def _populate_comcigan_slots_list(self) -> None:
        if getattr(self, "list_comcigan_slots", None) is None:
            return
        self.list_comcigan_slots.clear()
        day_names = ["월", "화", "수", "목", "금", "토", "일"]
        today_day = day_names[datetime.now().weekday()]
        day_order = {d: i for i, d in enumerate(day_names)}
        sorted_rows = sorted(
            [r for r in self._comcigan_rows if isinstance(r, dict) and str(r.get("subject") or "").strip()],
            key=lambda r: (
                0 if str(r.get("day") or "").strip() == today_day else 1,
                day_order.get(str(r.get("day") or "").strip(), 9),
                int(r.get("period") or 99),
            ),
        )
        for r in sorted_rows:
            day_str = str(r.get("day") or "").strip()
            period_val = r.get("period") or ""
            subj = str(r.get("subject") or "").strip()
            g = r.get("grade")
            c = r.get("class_no")
            class_label = str(r.get("class_label") or r.get("target_class") or "").strip()
            if not class_label and g and c:
                class_label = f"{g}-{c}"
            prefix = "[오늘] " if day_str == today_day else ""
            text = f"{prefix}{day_str} {period_val}교시 · {subj} {class_label}".strip()
            item = QListWidgetItem(text)
            item.setData(
                Qt.ItemDataRole.UserRole,
                {
                    "day": day_str,
                    "period": period_val,
                    "subject": subj,
                    "class_label": class_label,
                },
            )
            self.list_comcigan_slots.addItem(item)

    def _on_comcigan_slot_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        subj = str(data.get("subject") or "").strip()
        clbl = str(data.get("class_label") or "").strip()
        for i in range(self.combo_progress_course.count()):
            d = self.combo_progress_course.itemData(i)
            if isinstance(d, dict) and d.get("subject") == subj and d.get("class_label") == clbl:
                self.combo_progress_course.setCurrentIndex(i)
                break
        if data.get("period"):
            try:
                self.spin_prog_period.setValue(int(data["period"]))
            except Exception:
                pass

    def _on_progress_course_changed(self, _idx: int = 0) -> None:
        self._refresh_progress_table()

    def load_comcigan_timetable_courses(self) -> None:
        """Firestore `시간표_기본`에서 컴시간 교사 시간표를 불러와 강좌·학반 목록을 갱신합니다."""
        cfg = ConfigManager()
        tp = cfg.get_section("teacher_profile") or {}
        api = cfg.get_section("api_and_integrations") or {}
        teacher_name = str(tp.get("name") or api.get("comcigan_teacher_name") or "").strip()
        school_code = str(tp.get("school_code") or tp.get("neis_school_code") or "").strip()

        self.btn_load_comcigan_schedule.setEnabled(False)
        self.btn_load_comcigan_schedule.setText("불러오는 중...")

        def _work(_worker: Any) -> List[Dict[str, Any]]:
            svc = FirestoreSyncService(db=self.db, config=cfg)
            return svc.fetch_timetable_base(teacher_name=teacher_name, school_code=school_code)

        def _on_result(rows: Any) -> None:
            row_list = list(rows or [])
            self._refresh_progress_course_combo(comcigan_rows=row_list)
            self._refresh_progress_table()
            self.lbl_comcigan_status.setText(f"컴시간 연동 완료 ({len(row_list)}개 교시)")

        def _on_error(err: str) -> None:
            QMessageBox.warning(
                self,
                "컴시간 시간표 조회 실패",
                f"시간표를 불러오지 못했습니다:\n{str(err or '').splitlines()[0]}",
            )

        def _on_finished() -> None:
            self.btn_load_comcigan_schedule.setEnabled(True)
            self.btn_load_comcigan_schedule.setText("컴시간 시간표 불러오기")

        worker = FunctionWorker("컴시간 시간표 불러오기", _work)
        self.task_runner.start_worker(
            worker,
            task_key="grade_material_load_comcigan",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=_on_finished,
        )

    def _on_new_lesson_progress(self) -> None:
        self._editing_progress_id = None
        self.edit_prog_date.setText(datetime.now().strftime("%Y-%m-%d"))
        self.edit_prog_unit.clear()
        self.edit_prog_lesson_no.clear()
        self.edit_prog_content.clear()
        self.edit_prog_next_plan.clear()
        self.edit_prog_memo.clear()
        self.combo_prog_status.setCurrentIndex(0)

    def _on_save_lesson_progress(self) -> None:
        meta = self._get_selected_progress_course_meta()
        subject = meta.get("subject") or ""
        class_label = meta.get("class_label") or ""
        roster_key = meta.get("roster_key") or ""
        if not subject:
            QMessageBox.warning(self, "진도 저장", "먼저 강좌·학반을 선택하거나 등록해 주세요.")
            return

        period_val = self.spin_prog_period.value()
        period = period_val if period_val > 0 else None
        log_id, _ = self.db.upsert_lesson_progress_log(
            log_id=self._editing_progress_id,
            roster_key=roster_key,
            subject=subject,
            class_label=class_label,
            date=self.edit_prog_date.text().strip() or datetime.now().strftime("%Y-%m-%d"),
            period=period,
            unit_title=self.edit_prog_unit.text().strip(),
            lesson_no=self.edit_prog_lesson_no.text().strip(),
            content=self.edit_prog_content.text().strip(),
            next_plan=self.edit_prog_next_plan.text().strip(),
            status=self.combo_prog_status.currentText().strip() or "예정",
            memo=self.edit_prog_memo.text().strip(),
        )
        self._editing_progress_id = log_id
        self._refresh_progress_table()

    def _on_delete_lesson_progress(self) -> None:
        row = self.table_progress.currentRow()
        if row < 0:
            return
        it = self.table_progress.item(row, 0)
        log_id = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        if not log_id:
            return
        self.db.delete_lesson_progress_log(int(log_id))
        if self._editing_progress_id == int(log_id):
            self._on_new_lesson_progress()
        self._refresh_progress_table()

    def _on_progress_row_clicked(self, row: int, _col: int) -> None:
        it = self.table_progress.item(row, 0)
        rec = it.data(Qt.ItemDataRole.UserRole + 1) if it is not None else None
        if not isinstance(rec, dict):
            return
        self._editing_progress_id = int(rec.get("id") or 0) or None
        self.edit_prog_date.setText(str(rec.get("date") or ""))
        self.spin_prog_period.setValue(int(rec["period"]) if rec.get("period") else 0)
        self.edit_prog_unit.setText(str(rec.get("unit_title") or ""))
        self.edit_prog_lesson_no.setText(str(rec.get("lesson_no") or ""))
        self.edit_prog_content.setText(str(rec.get("content") or ""))
        self.edit_prog_next_plan.setText(str(rec.get("next_plan") or ""))
        self.edit_prog_memo.setText(str(rec.get("memo") or ""))
        st_idx = self.combo_prog_status.findText(str(rec.get("status") or "예정"))
        self.combo_prog_status.setCurrentIndex(st_idx if st_idx >= 0 else 0)

    def _refresh_progress_table(self) -> None:
        if getattr(self, "table_progress", None) is None:
            return
        meta = self._get_selected_progress_course_meta()
        logs = self.db.get_lesson_progress_logs(
            roster_key=meta.get("roster_key", ""),
            subject=meta.get("subject", ""),
            class_label=meta.get("class_label", ""),
        )
        self.table_progress.setRowCount(0)
        for idx, rec in enumerate(logs):
            self.table_progress.insertRow(idx)
            course_disp = f"{rec.get('subject', '')} {rec.get('class_label', '')}".strip()
            period_disp = f"{rec['period']}교시" if rec.get("period") else "-"
            items = [
                QTableWidgetItem(str(rec.get("date") or "")),
                QTableWidgetItem(period_disp),
                QTableWidgetItem(course_disp),
                QTableWidgetItem(str(rec.get("unit_title") or "")),
                QTableWidgetItem(str(rec.get("lesson_no") or "")),
                QTableWidgetItem(str(rec.get("content") or "")),
                QTableWidgetItem(str(rec.get("next_plan") or "")),
                QTableWidgetItem(str(rec.get("status") or "예정")),
            ]
            items[0].setData(Qt.ItemDataRole.UserRole, int(rec["id"]))
            items[0].setData(Qt.ItemDataRole.UserRole + 1, rec)
            for c_idx, it in enumerate(items):
                if c_idx in (0, 1, 4, 7):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table_progress.setItem(idx, c_idx, it)

    def _current_subject_meta(self) -> Dict[str, str]:
        subject_key = self._current_subject_key()
        subject_name = self.combo_subject_roster.currentText().strip()
        class_label = ""
        group_id = self.combo_subject_roster.currentData()
        if group_id:
            grp = self.db.get_roster_group(int(group_id))
            if grp:
                subject_name = str(grp.get("group_name") or subject_name).strip()
                class_label = str(grp.get("extra_info") or "").strip()
        return {
            "roster_key": subject_key,
            "subject": subject_name or subject_key,
            "class_label": class_label,
        }

    def _build_lesson_progress_items_for_sync(
        self,
        config: Optional[ConfigManager] = None,
    ) -> List[Dict[str, Any]]:
        """로컬 진도 관리 기록(`lesson_progress_logs`)을 `수업_진도` 업로드 페이로드로 변환합니다."""
        cfg = config or ConfigManager()
        tp = cfg.get_section("teacher_profile") or {}
        school_code = str(tp.get("school_code") or tp.get("neis_school_code") or "").strip()
        teacher_id = str(tp.get("teacher_id") or "").strip()
        meta = self._get_selected_progress_course_meta()
        logs = self.db.get_lesson_progress_logs(
            roster_key=meta.get("roster_key", ""),
            subject=meta.get("subject", ""),
            class_label=meta.get("class_label", ""),
        )
        items: List[Dict[str, Any]] = []
        for rec in logs:
            item: Dict[str, Any] = {
                "school_code": school_code,
                "teacher_id": teacher_id,
                "date": str(rec.get("date") or "").strip(),
                "period": rec.get("period"),
                "subject": str(rec.get("subject") or meta.get("subject") or "").strip(),
                "class_label": str(rec.get("class_label") or meta.get("class_label") or "").strip(),
                "roster_key": str(rec.get("roster_key") or meta.get("roster_key") or "").strip(),
                "unit_title": str(rec.get("unit_title") or "").strip(),
                "lesson_no": str(rec.get("lesson_no") or "").strip(),
                "content": str(rec.get("content") or "").strip(),
                "next_plan": str(rec.get("next_plan") or "").strip(),
                "memo": str(rec.get("memo") or "").strip(),
                "status": str(rec.get("status") or "예정").strip(),
                "updated_at": str(rec.get("updated_at") or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if rec.get("remote_id"):
                item["remote_id"] = str(rec["remote_id"]).strip()
            items.append(item)
        return items

    def _apply_remote_lesson_progress(self, remote_items: List[Dict[str, Any]]) -> int:
        """모바일(`수업_진도`)에서 불러온 진도 기록을 로컬 `lesson_progress_logs`에 반영합니다."""
        meta = self._get_selected_progress_course_meta()
        filter_rk = meta.get("roster_key", "")
        filter_subj = meta.get("subject", "")
        filter_clbl = meta.get("class_label", "")

        local_by_rid = {
            str(r.get("remote_id") or "").strip(): r
            for r in self.db.get_lesson_progress_logs()
            if str(r.get("remote_id") or "").strip()
        }
        applied = 0

        for item in remote_items or []:
            if not isinstance(item, dict):
                continue
            r_key = str(item.get("roster_key") or "").strip()
            r_subj = str(item.get("subject") or "").strip()
            r_clbl = str(item.get("class_label") or "").strip()
            if filter_subj and r_subj and r_subj != filter_subj:
                continue
            if filter_clbl and r_clbl and r_clbl != filter_clbl:
                continue

            rid = str(item.get("remote_id") or "").strip()
            remote_updated = str(item.get("updated_at") or "").strip()
            local_rec = local_by_rid.get(rid) if rid else None
            if local_rec and local_rec.get("updated_at") and remote_updated and remote_updated < str(local_rec["updated_at"]):
                continue

            raw_status = str(item.get("status") or "").strip()
            if raw_status in PROGRESS_STATUS_OPTIONS:
                status_val = raw_status
            elif raw_status == "진도 완료":
                status_val = "완료"
            elif raw_status == "수업 중":
                status_val = "진행중"
            elif str(item.get("content") or "").strip():
                status_val = "완료"
            else:
                status_val = "예정"

            self.db.upsert_lesson_progress_log(
                remote_id=rid,
                roster_key=r_key or filter_rk,
                subject=r_subj or filter_subj,
                class_label=r_clbl or filter_clbl,
                date=str(item.get("date") or "").strip(),
                period=item.get("period"),
                unit_title=str(item.get("unit_title") or "").strip(),
                lesson_no=str(item.get("lesson_no") or "").strip(),
                content=str(item.get("content") or "").strip(),
                next_plan=str(item.get("next_plan") or "").strip(),
                status=status_val,
                memo=str(item.get("memo") or "").strip(),
                updated_at=remote_updated,
            )
            applied += 1

        return applied

    def _on_sync_mobile_progress(self) -> None:
        """[모바일·어드민 진도 동기화] 버튼 핸들러."""
        cfg = ConfigManager()
        upload_items = self._build_lesson_progress_items_for_sync(config=cfg)
        meta = self._get_selected_progress_course_meta()

        self.btn_sync_mobile_progress.setEnabled(False)
        self.btn_sync_mobile_progress.setText("진도 동기화 중...")

        def _work(_worker: Any) -> Dict[str, Any]:
            svc = FirestoreSyncService(db=self.db, config=cfg)
            uploaded = 0
            upload_ok = True
            if upload_items:
                upload_ok = svc.sync_lesson_progress_batch(upload_items)
                if upload_ok:
                    uploaded = len(upload_items)
            remote_items = svc.fetch_lesson_progress(
                roster_key=meta.get("roster_key", ""),
                subject=meta.get("subject", ""),
            )
            return {
                "upload_ok": upload_ok,
                "uploaded": uploaded,
                "remote_items": remote_items,
            }

        def _on_result(res: Any) -> None:
            data = res if isinstance(res, dict) else {}
            remote_items = data.get("remote_items") or []
            pulled = self._apply_remote_lesson_progress(remote_items)
            self._refresh_progress_table()
            uploaded = int(data.get("uploaded") or 0)
            if not data.get("upload_ok", True):
                QMessageBox.warning(
                    self,
                    "모바일 진도 동기화 부분 실패",
                    f"로컬 진도 업로드에 실패했습니다.\n모바일 진도 불러오기: {pulled}건 반영",
                )
            else:
                QMessageBox.information(
                    self,
                    "모바일 진도 동기화 완료",
                    f"업로드: {uploaded}건 · 모바일 진도 반영: {pulled}건\n"
                    f"({meta.get('subject') or '전체'} 진도가 모바일 수업_진도와 동기화되었습니다.)",
                )

        def _on_error(err: str) -> None:
            QMessageBox.critical(
                self,
                "모바일 진도 동기화 오류",
                f"진도 동기화 중 오류가 발생했습니다:\n{str(err or '').splitlines()[0]}",
            )

        def _on_finished() -> None:
            self.btn_sync_mobile_progress.setEnabled(True)
            self.btn_sync_mobile_progress.setText("모바일·어드민 진도 동기화")

        worker = FunctionWorker("모바일 수업 진도 동기화", _work)
        self.task_runner.start_worker(
            worker,
            task_key="grade_material_sync_mobile_progress",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=_on_finished,
        )

    def link_subject_material_folder(self, chosen: str) -> None:
        if not chosen:
            return
        chosen_path = str(Path(chosen).resolve())
        self.db.save_subject_material_folder(self._current_subject_key(), chosen_path)
        self.combo_unit_filter.blockSignals(True)
        self.combo_unit_filter.setCurrentIndex(0)
        self.combo_unit_filter.blockSignals(False)
        self._refresh_material_panel_for_subject()

    def _on_select_material_folder(self) -> None:
        start_dir = str(self._current_material_root) if self._current_material_root.exists() else ""
        chosen = QFileDialog.getExistingDirectory(
            self,
            "과목 수업 자료 로컬 폴더 선택",
            start_dir,
        )
        if chosen:
            self.link_subject_material_folder(chosen)

    def _on_material_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        kind = str(item.data(0, self.ROLE_NODE_KIND) or "")
        if kind != "file":
            return
        abs_str = str(item.data(0, self.ROLE_ABS_PATH) or "").strip()
        if not abs_str:
            return
        target_file = Path(abs_str)
        if not target_file.is_file():
            QMessageBox.warning(self, "파일 확인", f"파일을 찾을 수 없습니다:\n{target_file}")
            self._refresh_material_panel_for_subject()
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(target_file))  # type: ignore[attr-defined]
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_file)))
        except Exception as exc:
            QMessageBox.warning(self, "열기 오류", f"파일 실행 중 오류가 발생했습니다:\n{exc}")

    def _selected_target_directory(self) -> Path:
        root_dir = self._current_material_root
        root_dir.mkdir(parents=True, exist_ok=True)
        curr = self.tree_materials.currentItem()
        if curr is not None:
            kind = str(curr.data(0, self.ROLE_NODE_KIND) or "")
            abs_str = str(curr.data(0, self.ROLE_ABS_PATH) or "").strip()
            if abs_str:
                p = Path(abs_str)
                if kind == "dir" and p.is_dir():
                    return p
                if kind == "file" and p.parent.is_dir():
                    return p.parent
        filter_txt = self.combo_unit_filter.currentText().strip()
        if filter_txt and filter_txt not in ("전체 단원 보기", "[루트 자료]"):
            cand = root_dir / filter_txt
            if cand.is_dir():
                return cand
        return root_dir

    def _on_create_unit_subfolder(self) -> None:
        root_dir = self._current_material_root
        root_dir.mkdir(parents=True, exist_ok=True)
        folder_name, ok = QInputDialog.getText(
            self,
            "새 단원 폴더 만들기",
            "생성할 단원 폴더 이름을 입력하세요 (예: 01_화학의_첫걸음):",
        )
        if not ok:
            return
        clean = re.sub(r'[\\/:*?"<>|]+', "_", folder_name.strip()).strip()
        if not clean:
            return
        new_dir = root_dir / clean
        if new_dir.exists():
            QMessageBox.information(self, "폴더 존재", f"이미 '{clean}' 폴더가 있습니다.")
            return
        try:
            new_dir.mkdir(parents=True, exist_ok=False)
            rel_key = new_dir.relative_to(root_dir).as_posix()
            self._refresh_material_panel_for_subject(preserve_rel_path=rel_key)
        except Exception as exc:
            QMessageBox.critical(self, "폴더 생성 실패", f"단원 폴더 생성 중 오류가 발생했습니다:\n{exc}")

    def _on_open_material_folder(self) -> None:
        target_dir = self._selected_target_directory().resolve()
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(target_dir))  # type: ignore[attr-defined]
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_dir)))
        except Exception:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_dir)))

    def _on_materials_dropped(self, paths: List[str]) -> None:
        files = [p for p in paths if Path(p).is_file()]
        dirs = [p for p in paths if Path(p).is_dir()]
        if dirs and not files:
            self.link_subject_material_folder(dirs[0])
            return
        if files:
            self.upload_material_files(files)

    def upload_material_files(self, files: List[str]) -> int:
        if not files:
            return 0

        target_dir = self._selected_target_directory()
        target_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        last_rel = ""
        for src_raw in files:
            src_path = Path(src_raw)
            if not src_path.is_file():
                continue
            dst_path = target_dir / src_path.name
            if dst_path.exists() and dst_path.resolve() == src_path.resolve():
                last_rel = dst_path.relative_to(self._current_material_root).as_posix()
                continue
            if dst_path.exists():
                dst_path = build_next_version_file_path(dst_path)
            shutil.copy2(str(src_path), str(dst_path))
            last_rel = dst_path.relative_to(self._current_material_root).as_posix()
            copied += 1

        self._refresh_material_panel_for_subject(preserve_rel_path=last_rel)
        if copied > 0:
            QMessageBox.information(
                self,
                "자료 추가 완료",
                f"'{target_dir.name}' 폴더에 {copied}개 자료를 추가했습니다.",
            )
        return copied

    def _on_upload_material(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "단원 수업자료 파일 추가",
            "",
            "수업자료 파일 (*.pptx *.ppt *.docx *.doc *.pdf *.hwp *.hwpx *.xlsx *.xls *.csv *.txt *.md *.zip);;모든 파일 (*)",
        )
        if files:
            self.upload_material_files(files)

    def _on_duplicate_material_version(self) -> None:
        curr = self.tree_materials.currentItem()
        if curr is None or str(curr.data(0, self.ROLE_NODE_KIND) or "") != "file":
            QMessageBox.information(
                self,
                "파일 선택 안내",
                "버전 사본을 만들 수업 자료 파일을 트리에서 먼저 선택해 주세요.",
            )
            return
        src_path = Path(str(curr.data(0, self.ROLE_ABS_PATH) or ""))
        if not src_path.is_file():
            QMessageBox.warning(self, "파일 없음", f"원본 파일을 찾을 수 없습니다:\n{src_path}")
            return
        dst_path = build_next_version_file_path(src_path)
        try:
            shutil.copy2(str(src_path), str(dst_path))
            new_rel = dst_path.relative_to(self._current_material_root).as_posix()
            self._refresh_material_panel_for_subject(preserve_rel_path=new_rel)
            QMessageBox.information(
                self,
                "버전 사본 생성 완료",
                f"새 버전 파일이 생성되었습니다:\n{dst_path.name}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "사본 생성 실패", f"버전 사본 생성 중 오류가 발생했습니다:\n{exc}")

    def _on_send_material_to_ai(self) -> None:
        curr = self.tree_materials.currentItem()
        if curr is None:
            if self._current_material_root.is_dir():
                self.ai_reference_requested.emit(str(self._current_material_root))
            return

        kind = str(curr.data(0, self.ROLE_NODE_KIND) or "")
        abs_str = str(curr.data(0, self.ROLE_ABS_PATH) or "").strip()
        if not abs_str:
            return
        target_p = Path(abs_str)
        if kind in ("dir", "root_group") and target_p.is_dir():
            self.ai_reference_requested.emit(str(target_p))
            return

        if kind == "file" and target_p.is_file():
            if target_p.suffix.lower() not in AI_REFERENCE_EXTENSIONS:
                QMessageBox.information(
                    self,
                    "지원 형식 안내",
                    "AI 참고 문서로 전달 가능한 파일 형식은 .hwpx, .hwp, .docx, .pptx, .pdf, .xlsx, .xls, .csv, .txt, .md 입니다.\n"
                    "단원 폴더 전체를 세특 참고 경로로 보내려면 상위 단원 폴더를 선택해 주세요.",
                )
                return
            self.ai_reference_requested.emit(str(target_p))

    def _on_backup_unit_zip(self) -> None:
        base_dir = self._current_material_root
        if not base_dir.exists():
            QMessageBox.information(self, "백업 안내", "백업할 수업 자료 폴더가 없습니다.")
            return
        subj_raw = self.combo_subject_roster.currentText().strip() or base_dir.name
        safe_subj = re.sub(r'[\\/:*?"<>|]+', "_", subj_raw).strip("_") or "수업자료"
        default_zip = f"수업자료백업_{safe_subj}_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "단원 수업자료 압축 백업 (.zip)",
            default_zip,
            "ZIP 압축 파일 (*.zip)",
        )
        if not save_path:
            return

        save_resolved = Path(save_path).resolve()
        try:
            with zipfile.ZipFile(save_resolved, "w", zipfile.ZIP_DEFLATED) as zf:
                file_count = 0
                for p in base_dir.rglob("*"):
                    if not p.is_file():
                        continue
                    if p.resolve() == save_resolved:
                        continue
                    if _is_ignored_material_path(p):
                        continue
                    zf.write(p, p.relative_to(base_dir))
                    file_count += 1
                manifest_lines = [
                    f"EduFlow Course Materials Backup ({datetime.now().isoformat()})",
                    f"Subject: {subj_raw}",
                    f"Source Folder: {base_dir}",
                    f"Total Local Files Archived: {file_count}",
                ]
                zf.writestr("BACKUP_MANIFEST.txt", "\n".join(manifest_lines))
            QMessageBox.information(
                self,
                "단원 압축 백업 완료",
                f"총 {file_count}개 파일이 압축 백업되었습니다:\n{save_path}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "백업 실패", f"압축 백업 중 오류가 발생했습니다:\n{exc}")
