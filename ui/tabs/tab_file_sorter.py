"""
[탭 1] 📁 스마트 파일 자동 분류기 (ui/tabs/tab_file_sorter.py)
- 기존 QGroupBox를 HoverZoomCard(1초 호버 시 Z-order 최상단 부상 + OutCubic 대각선 슬라이딩 확대/축소)로 교체
- 모든 입력 필드 최소 높이 34px 이상, verticalSpacing 12px 이상 적용
- '자동 분류 규칙 설정' 카드 및 '최근 파일 분류 내역' 카드에 [ ⛶ 전체화면 ] / [ 🗗 원래 크기로 ] 포커스 토글 버튼 제공
"""

import html
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.file_sorter_service import (
    BatchSortWorker,
    FileSorterService,
    RealtimeFolderWatchWorker,
)
from services.notifier import NotificationService
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


def build_floating_tooltip(title: str, lines: List[str]) -> str:
    """테이블/리스트 항목 위에 마우스를 올렸을 때 플로팅 팝업 카드 형태로 전체 내용을 보여주는 HTML 툴팁."""
    escaped_title = html.escape(title)
    body_html = "<br/>".join(lines)
    return (
        f"<div style='min-width: 260px; padding: 2px 4px;'>"
        f"<div style='color: #60a5fa; font-weight: 700; margin-bottom: 4px;'>{escaped_title}</div>"
        f"<div style='color: #f8fafc; line-height: 1.45;'>{body_html}</div>"
        f"</div>"
    )


class RuleEditDialog(QDialog):
    """분류 규칙 추가 및 수정을 위한 모달 다이얼로그."""

    def __init__(self, parent: Optional[QWidget] = None, rule_data: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="RuleEditModal")
        self.setWindowTitle("분류 규칙 설정" if rule_data else "새 분류 규칙 추가")
        self.setMinimumWidth(520)
        self.rule_data = rule_data or {}
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(6, 6, 6, 6)

        self.spin_priority = QSpinBox()
        self.spin_priority.setMinimumHeight(34)
        self.spin_priority.setRange(1, 999)
        self.spin_priority.setValue(int(self.rule_data.get("priority", 10)))

        self.edit_name = QLineEdit(str(self.rule_data.get("rule_name", "")))
        self.edit_name.setMinimumHeight(34)
        self.edit_name.setPlaceholderText("예: 공문 및 시행문 / 성적 평가자료")

        self.combo_match_type = QComboBox()
        self.combo_match_type.setMinimumHeight(34)
        self.combo_match_type.addItem("파일명 키워드 포함 (keyword)", "keyword")
        self.combo_match_type.addItem("파일 확장자 일치 (extension)", "extension")
        self.combo_match_type.addItem("키워드 + 확장자 모두 일치 (both)", "both")
        current_type = str(self.rule_data.get("match_type", "keyword"))
        idx = self.combo_match_type.findData(current_type)
        if idx >= 0:
            self.combo_match_type.setCurrentIndex(idx)

        self.edit_keywords = QLineEdit(str(self.rule_data.get("keywords", "")))
        self.edit_keywords.setMinimumHeight(34)
        self.edit_keywords.setPlaceholderText("쉼표(,)로 구분 (예: 공문,시행,협조,접수)")

        self.edit_extensions = QLineEdit(str(self.rule_data.get("extensions", "")))
        self.edit_extensions.setMinimumHeight(34)
        self.edit_extensions.setPlaceholderText("쉼표(,)로 구분 (예: .hwp,.hwpx,.pdf,.xlsx)")

        self.edit_folder = QLineEdit(str(self.rule_data.get("category_folder", "")))
        self.edit_folder.setMinimumHeight(34)
        self.edit_folder.setPlaceholderText("Target 폴더 아래 생성될 하위 폴더명 (예: 01_공문서_시행문)")

        self.chk_active = QCheckBox("이 규칙 활성화")
        self.chk_active.setChecked(bool(self.rule_data.get("is_active", 1)))

        form.addRow("우선순위 (1~999):", self.spin_priority)
        form.addRow("규칙 이름:", self.edit_name)
        form.addRow("매칭 조건 방식:", self.combo_match_type)
        form.addRow("파일명 키워드:", self.edit_keywords)
        form.addRow("파일 확장자:", self.edit_extensions)
        form.addRow("생성할 하위 폴더명:", self.edit_folder)
        form.addRow("활성 상태:", self.chk_active)

        layout.addLayout(form)

        guide_lbl = QLabel(
            "참고: 키워드 규칙의 우선순위 숫자를 확장자 규칙보다 작게(예: 1~5) 설정하면\n"
            "파일명에 '공문'이 포함된 문서가 일반 문서함 대신 '공문서' 폴더로 먼저 분류됩니다."
        )
        apply_qss_role(guide_lbl, role="infoBanner", obj_name="RuleDialogBanner")
        layout.addWidget(guide_lbl)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        name = self.edit_name.text().strip()
        folder = self.edit_folder.text().strip()
        m_type = self.combo_match_type.currentData()
        kw = self.edit_keywords.text().strip()
        ext = self.edit_extensions.text().strip()

        if not name:
            QMessageBox.warning(self, "입력 확인", "규칙 이름을 입력해 주세요.")
            return
        if not folder:
            QMessageBox.warning(self, "입력 확인", "이동할 하위 폴더명을 입력해 주세요.")
            return
        if m_type == "keyword" and not kw:
            QMessageBox.warning(self, "입력 확인", "키워드 매칭 방식에서는 최소 1개 이상의 키워드를 입력해야 합니다.")
            return
        if m_type == "extension" and not ext:
            QMessageBox.warning(self, "입력 확인", "확장자 매칭 방식에서는 최소 1개 이상의 확장자를 입력해야 합니다.")
            return
        if m_type == "both" and (not kw or not ext):
            QMessageBox.warning(self, "입력 확인", "'키워드 + 확장자' 방식에서는 두 항목을 모두 입력해야 합니다.")
            return

        self.accept()

    def get_values(self) -> Dict[str, Any]:
        return {
            "priority": self.spin_priority.value(),
            "rule_name": self.edit_name.text().strip(),
            "match_type": self.combo_match_type.currentData(),
            "keywords": self.edit_keywords.text().strip(),
            "extensions": self.edit_extensions.text().strip(),
            "category_folder": self.edit_folder.text().strip(),
            "is_active": self.chk_active.isChecked(),
        }


class FileSorterTab(QWidget):
    """[탭 1] 스마트 파일 자동 분류기 메인 탭 위젯."""

    realtime_sorted_signal = pyqtSignal(dict)

    WATCH_TASK_KEY = "realtime_folder_watcher"
    BATCH_TASK_KEY = "batch_folder_sorter"

    def __init__(self, task_runner: TaskRunner, parent: Optional[QWidget] = None):
        super().__init__(parent)
        apply_qss_role(self, obj_name="FileSorterTabPage")
        self.task_runner = task_runner
        self.config = ConfigManager()
        self.db = DatabaseManager()
        self.notifier = NotificationService()
        self.sorter_service = FileSorterService(self.db)
        self._is_watching = False
        self._focused_card: Optional[str] = None

        self.realtime_sorted_signal.connect(self._on_realtime_file_sorted)
        self._init_ui()
        self._load_config_to_ui()
        self.refresh_rules_table()
        self.refresh_history_table()

        if self.config.get("file_sorter", "auto_watch_on_startup", False):
            self.toggle_realtime_watch(force_state=True)

    def _init_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content_widget = QWidget()
        content_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding
        )
        main_layout = QVBoxLayout(content_widget)
        main_layout.setContentsMargins(14, 14, 24, 24)
        main_layout.setSpacing(14)

        self.top_group = HoverZoomCard(
            title="폴더 지정 및 스마트 분류 실행 옵션",
            parent=content_widget,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.top_group, role="hoverZoomCard", obj_name="SorterFolderGroup")
        top_layout = self.top_group.body_layout()

        folders_row = QHBoxLayout()
        folders_row.setSpacing(16)

        src_box = QVBoxLayout()
        src_header = QHBoxLayout()
        src_lbl = QLabel("<b>감시/원본 폴더 (Source Directories)</b>")
        btn_add_src = QPushButton("+ 폴더 추가")
        apply_qss_role(btn_add_src, role="secondary")
        btn_rem_src = QPushButton("- 선택 제거")
        apply_qss_role(btn_rem_src, role="outline")
        btn_open_src = QPushButton("폴더 열기")
        apply_qss_role(btn_open_src, role="outline")
        btn_add_src.clicked.connect(self._on_add_source_dir)
        btn_rem_src.clicked.connect(self._on_remove_source_dir)
        btn_open_src.clicked.connect(self._on_open_source_dir)

        src_header.addWidget(src_lbl)
        src_header.addStretch()
        src_header.addWidget(btn_add_src)
        src_header.addWidget(btn_rem_src)
        src_header.addWidget(btn_open_src)

        self.list_source_dirs = QListWidget()
        self.list_source_dirs.setMouseTracking(True)
        self.list_source_dirs.setMinimumHeight(76)
        src_box.addLayout(src_header)
        src_box.addWidget(self.list_source_dirs)
        folders_row.addLayout(src_box, stretch=3)

        target_box = QVBoxLayout()
        target_lbl = QLabel("<b>분류 기준 상위 폴더 (Target Base Directory)</b>")
        target_input_row = QHBoxLayout()
        self.edit_target_dir = QLineEdit()
        self.edit_target_dir.setMinimumHeight(32)
        self.edit_target_dir.setPlaceholderText("분류된 하위 폴더들이 생성될 기준 폴더 경로")
        self.edit_target_dir.editingFinished.connect(self._save_ui_to_config)
        btn_browse_target = QPushButton("찾아보기...")
        apply_qss_role(btn_browse_target, role="secondary")
        btn_browse_target.setMinimumHeight(32)
        btn_open_target = QPushButton("열기")
        apply_qss_role(btn_open_target, role="outline")
        btn_open_target.setMinimumHeight(32)
        btn_browse_target.clicked.connect(self._on_browse_target_dir)
        btn_open_target.clicked.connect(self._on_open_target_dir)
        target_input_row.addWidget(self.edit_target_dir, stretch=1)
        target_input_row.addWidget(btn_browse_target)
        target_input_row.addWidget(btn_open_target)

        opts_row = QHBoxLayout()
        opts_row.addWidget(QLabel("처리 방식:"))
        self.combo_action_mode = QComboBox()
        self.combo_action_mode.setMinimumHeight(32)
        self.combo_action_mode.addItem("파일 이동 (shutil.move)", "move")
        self.combo_action_mode.addItem("파일 복사 (원본 유지)", "copy")
        self.combo_action_mode.currentIndexChanged.connect(self._save_ui_to_config)
        opts_row.addWidget(self.combo_action_mode)

        opts_row.addWidget(QLabel("중복 파일명:"))
        self.combo_dup_policy = QComboBox()
        self.combo_dup_policy.setMinimumHeight(32)
        self.combo_dup_policy.addItem("번호 추가 (파일명_(1).ext)", "number")
        self.combo_dup_policy.addItem("날짜시간 추가 (파일명_YYYYMMDD.ext)", "date")
        self.combo_dup_policy.currentIndexChanged.connect(self._save_ui_to_config)
        opts_row.addWidget(self.combo_dup_policy)

        self.chk_unmatched = QCheckBox("미분류 파일도 '기타' 폴더로 정리")
        self.chk_unmatched.toggled.connect(self._save_ui_to_config)
        opts_row.addWidget(self.chk_unmatched)
        opts_row.addStretch()

        target_box.addWidget(target_lbl)
        target_box.addLayout(target_input_row)
        target_box.addLayout(opts_row)
        folders_row.addLayout(target_box, stretch=4)

        top_layout.addLayout(folders_row)

        action_bar = QHBoxLayout()
        action_bar.setSpacing(10)

        self.btn_run_once = QPushButton("즉시 1회 분류 실행")
        apply_qss_role(self.btn_run_once, role="primary", obj_name="BtnRunSortOnce")
        self.btn_run_once.setMinimumHeight(36)
        self.btn_run_once.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run_once.clicked.connect(self.run_batch_sort_once)

        self.btn_toggle_watch = QPushButton("실시간 자동 감시: OFF")
        self.btn_toggle_watch.setMinimumHeight(36)
        self.btn_toggle_watch.setCheckable(True)
        self.btn_toggle_watch.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_watch_button_style(False)
        self.btn_toggle_watch.clicked.connect(lambda: self.toggle_realtime_watch())

        self.lbl_watch_status = QLabel("대기 중 (수동 실행 또는 실시간 감시를 시작하세요)")
        apply_qss_role(self.lbl_watch_status, role="mutedText", obj_name="LblWatchStatus")

        action_bar.addWidget(self.btn_run_once)
        action_bar.addWidget(self.btn_toggle_watch)
        action_bar.addSpacing(8)
        action_bar.addWidget(self.lbl_watch_status)
        action_bar.addStretch()

        top_layout.addLayout(action_bar)
        main_layout.addWidget(self.top_group)

        # ---------------------------------------------------------------------
        # 카드 1: 자동 분류 규칙 설정
        # ---------------------------------------------------------------------
        self.rules_group = HoverZoomCard(
            title="자동 분류 규칙 설정 (우선순위 숫자가 작을수록 먼저 매칭됩니다)",
            parent=content_widget,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.rules_group, role="hoverZoomCard", obj_name="SorterRulesGroup")
        self.rules_group.setMinimumHeight(240)

        self.btn_focus_rules = QPushButton("전체화면")
        apply_qss_role(self.btn_focus_rules, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_rules.setCheckable(True)
        self.btn_focus_rules.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_rules.setToolTip("자동 분류 규칙 설정 카드를 전체화면으로 확대하거나 원래 크기로 복원합니다.")
        self.btn_focus_rules.clicked.connect(lambda: self._toggle_card_focus("rules"))
        self.rules_group.add_header_widget(self.btn_focus_rules)

        rules_layout = self.rules_group.body_layout()

        rules_btn_bar = QHBoxLayout()
        btn_add_rule = QPushButton("+ 새 규칙 추가")
        apply_qss_role(btn_add_rule, role="primary", obj_name="BtnAddSortingRule")
        btn_edit_rule = QPushButton("선택 규칙 수정")
        apply_qss_role(btn_edit_rule, role="secondary")
        btn_del_rule = QPushButton("선택 규칙 삭제")
        apply_qss_role(btn_del_rule, role="outline")
        btn_reset_rules = QPushButton("기본 규칙 복원")
        apply_qss_role(btn_reset_rules, role="outline")

        btn_add_rule.clicked.connect(self._on_add_rule)
        btn_edit_rule.clicked.connect(self._on_edit_rule)
        btn_del_rule.clicked.connect(self._on_delete_rule)
        btn_reset_rules.clicked.connect(self._on_reset_rules)

        rules_btn_bar.addWidget(btn_add_rule)
        rules_btn_bar.addWidget(btn_edit_rule)
        rules_btn_bar.addWidget(btn_del_rule)
        rules_btn_bar.addStretch()
        rules_btn_bar.addWidget(btn_reset_rules)
        rules_layout.addLayout(rules_btn_bar)

        self.table_rules = QTableWidget(0, 8)
        self.table_rules.setAlternatingRowColors(True)
        self.table_rules.setMouseTracking(True)
        self.table_rules.verticalHeader().setDefaultSectionSize(32)
        self.table_rules.setHorizontalHeaderLabels(
            ["ID", "우선순위", "활성", "규칙 이름", "매칭 방식", "키워드 (쉼표 구분)", "확장자 (쉼표 구분)", "생성/이동할 하위 폴더명"]
        )
        self.table_rules.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_rules.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_rules.doubleClicked.connect(self._on_edit_rule)
        r_header = self.table_rules.horizontalHeader()
        r_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        r_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        r_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        r_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        r_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        r_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        r_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        r_header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table_rules.hideColumn(0)

        rules_layout.addWidget(self.table_rules, stretch=1)
        main_layout.addWidget(self.rules_group, stretch=1)

        # ---------------------------------------------------------------------
        # 카드 2: 최근 파일 분류 내역 및 실행 취소
        # ---------------------------------------------------------------------
        self.history_group = HoverZoomCard(
            title="최근 파일 분류 내역 및 실행 취소 (원래 위치로 되돌리기)",
            parent=content_widget,
            zoom_ratio=1.0,
            hover_delay_ms=0,
        )
        apply_qss_role(self.history_group, role="hoverZoomCard", obj_name="SorterHistoryGroup")
        self.history_group.setMinimumHeight(220)

        self.btn_focus_history = QPushButton("전체화면")
        apply_qss_role(self.btn_focus_history, role="cardMax", obj_name="CardMaxButton")
        self.btn_focus_history.setCheckable(True)
        self.btn_focus_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus_history.setToolTip("최근 파일 분류 내역 카드를 전체화면으로 확대하거나 원래 크기로 복원합니다.")
        self.btn_focus_history.clicked.connect(lambda: self._toggle_card_focus("history"))
        self.history_group.add_header_widget(self.btn_focus_history)

        history_layout = self.history_group.body_layout()

        hist_btn_bar = QHBoxLayout()
        btn_undo_selected = QPushButton("선택 항목 실행 취소 (원래 폴더 복구)")
        apply_qss_role(btn_undo_selected, role="warningOutline", obj_name="BtnUndoSelected")
        btn_undo_batch = QPushButton("최근 실행 묶음(Batch) 전체 되돌리기")
        apply_qss_role(btn_undo_batch, role="secondary")
        btn_refresh_hist = QPushButton("새로고침")
        apply_qss_role(btn_refresh_hist, role="outline")
        btn_clear_hist = QPushButton("이력 비우기")
        apply_qss_role(btn_clear_hist, role="outline")

        btn_undo_selected.clicked.connect(self._on_undo_selected)
        btn_undo_batch.clicked.connect(self._on_undo_latest_batch)
        btn_refresh_hist.clicked.connect(self.refresh_history_table)
        btn_clear_hist.clicked.connect(self._on_clear_history)

        hist_btn_bar.addWidget(btn_undo_selected)
        hist_btn_bar.addWidget(btn_undo_batch)
        hist_btn_bar.addStretch()
        hist_btn_bar.addWidget(btn_refresh_hist)
        hist_btn_bar.addWidget(btn_clear_hist)
        history_layout.addLayout(hist_btn_bar)

        self.table_history = QTableWidget(0, 8)
        self.table_history.setAlternatingRowColors(True)
        self.table_history.setMouseTracking(True)
        self.table_history.verticalHeader().setDefaultSectionSize(32)
        self.table_history.setHorizontalHeaderLabels(
            ["ID", "작업 시각", "상태", "파일명", "분류 폴더", "적용 규칙", "원래 경로", "이동된 경로"]
        )
        self.table_history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        h_header = self.table_history.horizontalHeader()
        h_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        h_header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        h_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        h_header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table_history.hideColumn(0)

        history_layout.addWidget(self.table_history, stretch=1)
        main_layout.addWidget(self.history_group, stretch=1)

        scroll.setWidget(content_widget)
        outer_layout.addWidget(scroll)

    # =========================================================================
    # 섹션 카드 전체화면 포커스 토글 ([ ⛶ 전체화면 ] <-> [ 🗗 원래 크기로 ])
    # =========================================================================
    def _toggle_card_focus(self, target: str) -> None:
        if self._focused_card == target:
            self._focused_card = None
            self.rules_group.set_zoom_enabled(True)
            self.history_group.set_zoom_enabled(True)
            self.top_group.show()
            self.rules_group.show()
            self.history_group.show()
            self.btn_focus_rules.setChecked(False)
            self.btn_focus_rules.setText("전체화면")
            self.btn_focus_history.setChecked(False)
            self.btn_focus_history.setText("전체화면")
            return

        self._focused_card = target
        self.top_group.hide()
        if target == "rules":
            self.rules_group.set_zoom_enabled(False)
            self.rules_group.show()
            self.history_group.hide()
            self.btn_focus_rules.setChecked(True)
            self.btn_focus_rules.setText("원래 크기")
            self.btn_focus_history.setChecked(False)
            self.btn_focus_history.setText("전체화면")
        elif target == "history":
            self.history_group.set_zoom_enabled(False)
            self.rules_group.hide()
            self.history_group.show()
            self.btn_focus_history.setChecked(True)
            self.btn_focus_history.setText("원래 크기")
            self.btn_focus_rules.setChecked(False)
            self.btn_focus_rules.setText("전체화면")

    def _add_source_dir_item(self, path_str: str) -> None:
        item = QListWidgetItem(path_str)
        item.setToolTip(
            build_floating_tooltip(
                "감시/원본 폴더 전체 경로",
                [html.escape(path_str)],
            )
        )
        self.list_source_dirs.addItem(item)

    def _load_config_to_ui(self) -> None:
        sec = self.config.get_section("file_sorter")
        self.list_source_dirs.clear()
        for src in sec.get("source_dirs", []):
            self._add_source_dir_item(src)

        target_str = sec.get("target_base_dir", "")
        self.edit_target_dir.setText(target_str)
        self.edit_target_dir.setToolTip(target_str)

        mode_idx = self.combo_action_mode.findData(sec.get("action_mode", "move"))
        if mode_idx >= 0:
            self.combo_action_mode.setCurrentIndex(mode_idx)

        dup_idx = self.combo_dup_policy.findData(sec.get("duplicate_policy", "number"))
        if dup_idx >= 0:
            self.combo_dup_policy.setCurrentIndex(dup_idx)

        self.chk_unmatched.setChecked(bool(sec.get("include_unmatched", False)))

    def _save_ui_to_config(self) -> None:
        src_dirs = [
            self.list_source_dirs.item(i).text().strip()
            for i in range(self.list_source_dirs.count())
            if self.list_source_dirs.item(i).text().strip()
        ]
        target_str = self.edit_target_dir.text().strip()
        self.edit_target_dir.setToolTip(target_str)
        self.config.update_section(
            "file_sorter",
            {
                "source_dirs": src_dirs,
                "target_base_dir": target_str,
                "action_mode": self.combo_action_mode.currentData(),
                "duplicate_policy": self.combo_dup_policy.currentData(),
                "include_unmatched": self.chk_unmatched.isChecked(),
            },
            auto_save=True,
        )

    def _on_add_source_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "감시/원본 폴더 선택")
        if folder:
            norm = str(Path(folder).resolve())
            existing = [
                self.list_source_dirs.item(i).text()
                for i in range(self.list_source_dirs.count())
            ]
            if norm not in existing:
                self._add_source_dir_item(norm)
                self._save_ui_to_config()

    def _on_remove_source_dir(self) -> None:
        row = self.list_source_dirs.currentRow()
        if row >= 0:
            self.list_source_dirs.takeItem(row)
            self._save_ui_to_config()

    def _on_open_source_dir(self) -> None:
        item = self.list_source_dirs.currentItem()
        if item is None and self.list_source_dirs.count() > 0:
            item = self.list_source_dirs.item(0)
        if item:
            path_str = item.text().strip()
            if os.path.exists(path_str):
                os.startfile(path_str)
            else:
                QMessageBox.warning(self, "폴더 없음", f"경로가 존재하지 않습니다:\n{path_str}")

    def _on_browse_target_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "분류 기준 상위 폴더(Target Base Directory) 선택")
        if folder:
            self.edit_target_dir.setText(str(Path(folder).resolve()))
            self._save_ui_to_config()

    def _on_open_target_dir(self) -> None:
        target = self.edit_target_dir.text().strip()
        if not target:
            return
        Path(target).mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def refresh_rules_table(self) -> None:
        rules = self.db.get_sorting_rules(active_only=False)
        self.table_rules.setRowCount(0)

        type_labels = {
            "keyword": "키워드 포함",
            "extension": "확장자 일치",
            "both": "키워드+확장자",
        }

        for row_idx, r in enumerate(rules):
            self.table_rules.insertRow(row_idx)
            id_item = QTableWidgetItem(str(r["id"]))
            id_item.setData(Qt.ItemDataRole.UserRole, r)

            pri_item = QTableWidgetItem(str(r["priority"]))
            pri_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            active_str = "활성" if r["is_active"] else "비활성"
            act_item = QTableWidgetItem(active_str)
            act_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            name_item = QTableWidgetItem(r["rule_name"])
            mtype_str = type_labels.get(r["match_type"], r["match_type"])
            mtype_item = QTableWidgetItem(mtype_str)
            mtype_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            kw_item = QTableWidgetItem(r["keywords"] or "-")
            ext_item = QTableWidgetItem(r["extensions"] or "-")
            folder_item = QTableWidgetItem(r['category_folder'])

            tooltip_html = build_floating_tooltip(
                f"규칙: {r['rule_name']} (우선순위 {r['priority']})",
                [
                    f"<b>상태 / 매칭 방식:</b> {active_str} / {html.escape(mtype_str)}",
                    f"<b>파일명 키워드:</b> {html.escape(r['keywords'] or '-')}",
                    f"<b>파일 확장자:</b> {html.escape(r['extensions'] or '-')}",
                    f"<b>이동할 하위 폴더:</b> {html.escape(r['category_folder'])}",
                ],
            )

            row_items = [
                id_item,
                pri_item,
                act_item,
                name_item,
                mtype_item,
                kw_item,
                ext_item,
                folder_item,
            ]
            for col_idx, it in enumerate(row_items):
                it.setToolTip(tooltip_html)
                self.table_rules.setItem(row_idx, col_idx, it)

    def _get_selected_rule_data(self) -> Optional[Dict[str, Any]]:
        row = self.table_rules.currentRow()
        if row < 0:
            return None
        id_item = self.table_rules.item(row, 0)
        if not id_item:
            return None
        return id_item.data(Qt.ItemDataRole.UserRole)

    def _on_add_rule(self) -> None:
        dlg = RuleEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            self.db.add_sorting_rule(**vals)
            self.refresh_rules_table()

    def _on_edit_rule(self) -> None:
        rule_data = self._get_selected_rule_data()
        if not rule_data:
            QMessageBox.information(self, "안내", "수정할 규칙을 테이블에서 먼저 선택해 주세요.")
            return
        dlg = RuleEditDialog(self, rule_data=rule_data)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            self.db.update_sorting_rule(rule_id=int(rule_data["id"]), **vals)
            self.refresh_rules_table()

    def _on_delete_rule(self) -> None:
        rule_data = self._get_selected_rule_data()
        if not rule_data:
            QMessageBox.information(self, "안내", "삭제할 규칙을 선택해 주세요.")
            return
        ans = QMessageBox.question(
            self,
            "규칙 삭제 확인",
            f"'{rule_data['rule_name']}' 규칙을 삭제하시겠습니까?",
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.db.delete_sorting_rule(int(rule_data["id"]))
            self.refresh_rules_table()

    def _on_reset_rules(self) -> None:
        ans = QMessageBox.question(
            self,
            "기본 규칙 복원",
            "현재 규칙을 모두 지우고 교무/행정 표준 분류 규칙(11종)으로 초기화하시겠습니까?",
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.db.reset_default_sorting_rules()
            self.refresh_rules_table()

    def run_batch_sort_once(self) -> None:
        self._save_ui_to_config()
        sec = self.config.get_section("file_sorter")
        source_dirs = sec.get("source_dirs", [])
        target_dir = sec.get("target_base_dir", "").strip()

        if not source_dirs:
            QMessageBox.warning(self, "폴더 미지정", "감시/원본 폴더를 최소 1개 이상 등록해 주세요.")
            return
        if not target_dir:
            QMessageBox.warning(self, "폴더 미지정", "분류 기준 상위 폴더(Target Base Directory)를 지정해 주세요.")
            return

        worker = BatchSortWorker(
            sorter_service=self.sorter_service,
            source_dirs=source_dirs,
            target_base_dir=target_dir,
            action_mode=sec.get("action_mode", "move"),
            duplicate_policy=sec.get("duplicate_policy", "number"),
            include_unmatched=bool(sec.get("include_unmatched", False)),
            unmatched_folder_name=sec.get("unmatched_folder_name", "99_기타_미분류"),
            ignore_temp_files=bool(sec.get("ignore_temp_files", True)),
        )
        self.btn_run_once.setEnabled(False)
        self.task_runner.start_worker(
            worker=worker,
            task_key=self.BATCH_TASK_KEY,
            on_result=self._on_batch_sort_completed,
            on_finished=lambda: self.btn_run_once.setEnabled(True),
        )

    def _on_batch_sort_completed(self, result: Dict[str, Any]) -> None:
        self.refresh_history_table()
        processed = result.get("processed", 0)
        skipped = result.get("skipped", 0)
        if processed > 0:
            self.notifier.notify(
                "스마트 파일 분류 완료",
                f"총 {processed}개 파일이 자동 분류되었습니다. (미해당/건너뜀: {skipped}건)",
            )
        else:
            self.lbl_watch_status.setText(f"최근 1회 분류 결과: 이동된 파일 없음 (검사 건너뜀 {skipped}건)")

    def _update_watch_button_style(self, watching: bool) -> None:
        if watching:
            self.btn_toggle_watch.setText("실시간 자동 감시: ON (작동 중)")
            apply_qss_role(self.btn_toggle_watch, role="success", obj_name="BtnToggleRealtimeWatch")
        else:
            self.btn_toggle_watch.setText("실시간 자동 감시: OFF")
            apply_qss_role(self.btn_toggle_watch, role="slate", obj_name="BtnToggleRealtimeWatch")

    def toggle_realtime_watch(self, force_state: Optional[bool] = None) -> None:
        target_state = (not self._is_watching) if force_state is None else force_state

        if not target_state:
            self.task_runner.stop_task(self.WATCH_TASK_KEY)
            self._is_watching = False
            self.btn_toggle_watch.setChecked(False)
            self._update_watch_button_style(False)
            self.lbl_watch_status.setText("실시간 자동 감시가 중지되었습니다.")
            return

        self._save_ui_to_config()
        sec = self.config.get_section("file_sorter")
        source_dirs = sec.get("source_dirs", [])
        target_dir = sec.get("target_base_dir", "").strip()

        if not source_dirs or not target_dir:
            QMessageBox.warning(
                self, "설정 확인", "실시간 감시를 시작하려면 원본 폴더와 분류 기준 폴더를 모두 지정해야 합니다."
            )
            self.btn_toggle_watch.setChecked(False)
            return

        worker = RealtimeFolderWatchWorker(
            sorter_service=self.sorter_service,
            source_dirs=source_dirs,
            target_base_dir=target_dir,
            action_mode=sec.get("action_mode", "move"),
            duplicate_policy=sec.get("duplicate_policy", "number"),
            include_unmatched=bool(sec.get("include_unmatched", False)),
            unmatched_folder_name=sec.get("unmatched_folder_name", "99_기타_미분류"),
            ignore_temp_files=bool(sec.get("ignore_temp_files", True)),
            poll_interval_sec=float(sec.get("watch_interval_sec", 3)),
            on_file_sorted_signal=self.realtime_sorted_signal,
        )

        self._is_watching = True
        self.btn_toggle_watch.setChecked(True)
        self._update_watch_button_style(True)
        self.lbl_watch_status.setText(
            f"실시간 감시 중 ({len(source_dirs)}개 원본 폴더 -> {Path(target_dir).name})"
        )

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.WATCH_TASK_KEY,
            on_error=self._on_watch_error,
        )

    def _on_realtime_file_sorted(self, item_info: Dict[str, Any]) -> None:
        self.refresh_history_table()
        self.lbl_watch_status.setText(
            f"자동 분류됨: {item_info['file_name']} -> {item_info['category_folder']}"
        )

    def _on_watch_error(self, err_msg: str) -> None:
        self._is_watching = False
        self.btn_toggle_watch.setChecked(False)
        self._update_watch_button_style(False)
        self.lbl_watch_status.setText("실시간 감시 오류로 중단됨")

    def refresh_history_table(self) -> None:
        history = self.db.get_file_history(limit=200, include_undone=True)
        self.table_history.setRowCount(0)

        for row_idx, h in enumerate(history):
            self.table_history.insertRow(row_idx)
            is_undone = bool(h.get("is_undone", 0))

            id_item = QTableWidgetItem(str(h["id"]))
            id_item.setData(Qt.ItemDataRole.UserRole, h)

            time_item = QTableWidgetItem(h["created_at"])
            status_str = "복구됨(취소)" if is_undone else "분류완료"
            status_item = QTableWidgetItem(status_str)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            fname_item = QTableWidgetItem(h["file_name"])
            cat_item = QTableWidgetItem(h["category_folder"])
            rule_item = QTableWidgetItem(h["rule_name"])
            orig_item = QTableWidgetItem(h["original_path"])
            dest_item = QTableWidgetItem(h["target_path"])

            tooltip_html = build_floating_tooltip(
                h['file_name'],
                [
                    f"<b>작업 시각 / 상태:</b> {html.escape(h['created_at'])} ({status_str})",
                    f"<b>분류 폴더 / 규칙:</b> {html.escape(h['category_folder'])} ({html.escape(h['rule_name'])})",
                    f"<b>원래 경로:</b> {html.escape(h['original_path'])}",
                    f"<b>이동된 경로:</b> {html.escape(h['target_path'])}",
                ],
            )

            items = [
                id_item,
                time_item,
                status_item,
                fname_item,
                cat_item,
                rule_item,
                orig_item,
                dest_item,
            ]
            for col_idx, it in enumerate(items):
                it.setToolTip(tooltip_html)
                if is_undone:
                    it.setForeground(QBrush(QColor("#94a3b8")))
                self.table_history.setItem(row_idx, col_idx, it)

    def _on_undo_selected(self) -> None:
        selected_rows = {idx.row() for idx in self.table_history.selectedIndexes()}
        if not selected_rows:
            QMessageBox.information(self, "안내", "실행 취소(복구)할 내역을 테이블에서 선택해 주세요.")
            return

        restored_cnt = 0
        messages: List[str] = []
        for r in sorted(selected_rows):
            id_item = self.table_history.item(r, 0)
            if not id_item:
                continue
            h_data = id_item.data(Qt.ItemDataRole.UserRole)
            ok, msg = self.sorter_service.undo_history_item(h_data)
            messages.append(msg)
            if ok:
                restored_cnt += 1

        self.refresh_history_table()
        QMessageBox.information(
            self,
            "실행 취소 결과",
            f"선택한 {len(selected_rows)}건 중 {restored_cnt}건이 원래 위치로 복구되었습니다.\n\n"
            + "\n".join(messages[:10]),
        )

    def _on_undo_latest_batch(self) -> None:
        active_hist = self.db.get_file_history(limit=200, include_undone=False)
        if not active_hist:
            QMessageBox.information(self, "안내", "되돌릴 수 있는 최근 분류 작업 내역이 없습니다.")
            return

        latest_batch_id = active_hist[0]["batch_id"]
        batch_items = [h for h in active_hist if h["batch_id"] == latest_batch_id]

        ans = QMessageBox.question(
            self,
            "최근 일괄 작업 되돌리기",
            f"가장 최근 실행 묶음({latest_batch_id})의 파일 {len(batch_items)}개를 모두 원래 위치로 되돌리시겠습니까?",
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        restored = 0
        for h in batch_items:
            ok, _ = self.sorter_service.undo_history_item(h)
            if ok:
                restored += 1

        self.refresh_history_table()
        QMessageBox.information(
            self,
            "일괄 복구 완료",
            f"총 {len(batch_items)}건 중 {restored}건의 파일을 원래 폴더로 복구했습니다.",
        )

    def _on_clear_history(self) -> None:
        ans = QMessageBox.question(
            self,
            "이력 비우기",
            "분류 작업 내역 기록을 모두 삭제하시겠습니까? (실제 파일은 삭제되지 않습니다)",
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.db.clear_file_history()
            self.refresh_history_table()
