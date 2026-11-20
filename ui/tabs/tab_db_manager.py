"""
[통합 DB 관리] Cloud Firestore 컬렉션 스프레드시트 관리 탭 (ui/tabs/tab_db_manager.py).
- 로컬 SQLite 증분(델타) 캐시 기반으로 탭 열기/컬렉션 전환 시 Firestore 읽기 0건으로 즉시 표시
- [변경분 새로고침]: updated_at > last_sync_at - 2분 (Notifications는 created_at) 단일 필드 쿼리 병합
- [전체 다시 받기]: 컬렉션 전체 재수신 후 로컬 캐시 교체
- 읽기 전용 시스템/대용량 필드 수정 잠금 + Base64 요약 표시 + 변경 셀/행 하이라이트 + 일괄 저장/추가/삭제/CSV 내보내기
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QBrush, QColor
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
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import FunctionWorker, TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.firestore_sync_service import (
    BOOL_FIELDS,
    DB_MANAGER_COLLECTIONS,
    INT_FIELDS,
    FirestoreSyncService,
)
from ui.utils import install_no_scroll_wheel_filter, load_svg_icon


READONLY_FIELDS: Set[str] = {
    "remote_id",
    "password_hash",
    "face_descriptor",
    "photo_data_url",
    "updated_at",
    "created_at",
}

LARGE_DATA_FIELDS: Set[str] = {
    "face_descriptor",
    "photo_data_url",
}

PRIORITY_COLUMNS: List[str] = [
    "remote_id",
    "title",
    "name",
    "student_id",
    "student_name",
    "grade",
    "class_no",
    "date",
    "start_date",
    "end_date",
    "category",
    "status",
]

TAIL_COLUMNS: List[str] = ["updated_at", "created_at"]


def _apply_role(widget: QWidget, role: str = "", obj_name: str = "") -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    if obj_name:
        widget.setObjectName(obj_name)
    if role:
        widget.setProperty("role", role)
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


def is_readonly_field(field_name: str) -> bool:
    name = str(field_name or "").strip()
    if name in READONLY_FIELDS:
        return True
    if name.endswith("_data_url"):
        return True
    return False


def format_cell_display(field_name: str, value: Any) -> Tuple[str, bool]:
    """셀 표시 문자열과 대용량 요약 여부를 반환합니다."""
    if value is None:
        return "", False
    name = str(field_name or "").strip()
    if isinstance(value, (dict, list)):
        dumped = json.dumps(value, ensure_ascii=False)
        if name == "face_descriptor" or len(dumped) > 300:
            return f"[임베딩/대용량 데이터 ({len(dumped)}자)]", True
        return dumped, False
    if isinstance(value, bool):
        return ("true" if value else "false"), False
    if isinstance(value, str):
        if value.startswith("data:image/") or (
            name in ("photo_data_url",) or name.endswith("_data_url")
        ) and len(value) > 40:
            return "[이미지 데이터]", True
        if name in LARGE_DATA_FIELDS and len(value) > 40:
            return f"[대용량 데이터 ({len(value)}자)]", True
        if len(value) > 500:
            return f"[대용량 텍스트 ({len(value)}자)]", True
        return value, False
    return str(value), False


def parse_edited_cell_value(
    raw_text: str, original_value: Any, field_name: str
) -> Any:
    """편집된 셀 문자열을 원래 필드 타입에 맞게 변환합니다."""
    text = str(raw_text if raw_text is not None else "").strip()
    name = str(field_name or "").strip()

    if name in BOOL_FIELDS or isinstance(original_value, bool):
        low = text.lower()
        if low in ("true", "1", "yes", "y", "예", "on"):
            return True
        if low in ("false", "0", "no", "n", "아니오", "off", ""):
            return False
    if name in INT_FIELDS or (isinstance(original_value, int) and not isinstance(original_value, bool)):
        if text == "":
            return None
        try:
            return int(float(text))
        except (ValueError, TypeError):
            return text
    if isinstance(original_value, (dict, list)):
        if not text:
            return [] if isinstance(original_value, list) else {}
        try:
            parsed = json.loads(text)
            return parsed
        except Exception:
            return text
    return text


class DbManagerTab(QWidget):
    """좌측 사이드바 [통합 DB 관리] 스프레드시트 탭."""

    TASK_SYNC_KEY = "db_manager_firestore_sync"

    def __init__(
        self,
        task_runner: TaskRunner,
        parent: Optional[QWidget] = None,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
        sync_service: Optional[FirestoreSyncService] = None,
    ):
        super().__init__(parent)
        _apply_role(self, obj_name="DbManagerTabPage")
        self.task_runner = task_runner
        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()
        self.sync_service = sync_service or FirestoreSyncService(
            db=self.db, config=self.config
        )

        self._current_collection: str = DB_MANAGER_COLLECTIONS[0]
        self._columns: List[str] = []
        self._row_records: List[Dict[str, Any]] = []
        self._dirty_rows: Set[int] = set()
        self._dirty_cells: Set[Tuple[int, int]] = set()
        self._new_row_ids: Set[str] = set()

        self._init_ui()
        self.load_from_local_cache()

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 1. 상단 컬렉션 선택 + 검색 + 상태 배지 카드
        top_card = QFrame(self)
        _apply_role(top_card, role="instrumentCard", obj_name="DbManagerTopCard")
        top_lay = QHBoxLayout(top_card)
        top_lay.setContentsMargins(14, 10, 14, 10)
        top_lay.setSpacing(10)

        lbl_col = QLabel("컬렉션:")
        _apply_role(lbl_col, role="fieldLabel")

        self.combo_collection = QComboBox()
        self.combo_collection.setMinimumWidth(200)
        self.combo_collection.setMinimumHeight(34)
        for col_name in DB_MANAGER_COLLECTIONS:
            self.combo_collection.addItem(col_name, col_name)
        install_no_scroll_wheel_filter(self.combo_collection)
        self.combo_collection.currentIndexChanged.connect(self._on_collection_changed)

        self.edit_search = QLineEdit()
        self.edit_search.setMinimumHeight(34)
        self.edit_search.setPlaceholderText("표 내용 실시간 검색 (문서 ID, 이름, 날짜, 내용 등)...")
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.textChanged.connect(self._apply_search_filter)

        self.lbl_row_count = QLabel("0건")
        _apply_role(self.lbl_row_count, role="badgePrimary", obj_name="DbManagerRowCountBadge")

        self.lbl_last_sync = QLabel("마지막 동기화: 로컬 캐시 없음")
        _apply_role(self.lbl_last_sync, role="badgeNeutral", obj_name="DbManagerLastSyncBadge")

        top_lay.addWidget(lbl_col)
        top_lay.addWidget(self.combo_collection)
        top_lay.addWidget(self.edit_search, stretch=1)
        top_lay.addWidget(self.lbl_row_count)
        top_lay.addWidget(self.lbl_last_sync)
        root.addWidget(top_card)

        # 2. 액션 버튼 툴바
        action_bar = QHBoxLayout()
        action_bar.setSpacing(8)

        self.btn_delta_refresh = QPushButton("변경분 새로고침")
        _apply_role(self.btn_delta_refresh, role="primary", obj_name="BtnDbDeltaRefresh")
        self.btn_delta_refresh.setIcon(load_svg_icon("refresh-cw", "#FFFFFF", 14))
        self.btn_delta_refresh.setIconSize(QSize(14, 14))
        self.btn_delta_refresh.setMinimumHeight(34)
        self.btn_delta_refresh.setToolTip(
            "마지막 동기화 시각 이후 변경·추가된 문서만 조회해 로컬 캐시에 병합합니다 (읽기 비용 최소화)."
        )
        self.btn_delta_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delta_refresh.clicked.connect(self._on_delta_refresh_clicked)

        self.btn_full_refresh = QPushButton("전체 다시 받기")
        _apply_role(self.btn_full_refresh, role="secondary", obj_name="BtnDbFullRefresh")
        self.btn_full_refresh.setIcon(load_svg_icon("rotate-cw", "#334155", 14))
        self.btn_full_refresh.setIconSize(QSize(14, 14))
        self.btn_full_refresh.setMinimumHeight(34)
        self.btn_full_refresh.setToolTip(
            "외부에서 삭제된 문서를 반영하기 위해 선택된 컬렉션 전체를 다시 받아 로컬 캐시를 교체합니다."
        )
        self.btn_full_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_full_refresh.clicked.connect(self._on_full_refresh_clicked)

        self.btn_add_row = QPushButton("+ 행 추가")
        _apply_role(self.btn_add_row, role="secondary", obj_name="BtnDbAddRow")
        self.btn_add_row.setMinimumHeight(34)
        self.btn_add_row.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_row.clicked.connect(self._on_add_row_clicked)

        self.btn_delete_rows = QPushButton("선택 행 삭제")
        _apply_role(self.btn_delete_rows, role="danger", obj_name="BtnDbDeleteRows")
        self.btn_delete_rows.setMinimumHeight(34)
        self.btn_delete_rows.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete_rows.clicked.connect(self._on_delete_rows_clicked)

        self.btn_save_changes = QPushButton("변경사항 일괄 저장")
        _apply_role(self.btn_save_changes, role="slate", obj_name="BtnDbSaveChanges")
        self.btn_save_changes.setMinimumHeight(34)
        self.btn_save_changes.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save_changes.setEnabled(False)
        self.btn_save_changes.clicked.connect(self._on_save_changes_clicked)

        self.btn_export_csv = QPushButton("CSV 내보내기")
        _apply_role(self.btn_export_csv, role="outline", obj_name="BtnDbExportCsv")
        self.btn_export_csv.setIcon(load_svg_icon("file-spreadsheet", "#059669", 14))
        self.btn_export_csv.setIconSize(QSize(14, 14))
        self.btn_export_csv.setMinimumHeight(34)
        self.btn_export_csv.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export_csv.clicked.connect(self._on_export_csv_clicked)

        action_bar.addWidget(self.btn_delta_refresh)
        action_bar.addWidget(self.btn_full_refresh)
        action_bar.addSpacing(8)
        action_bar.addWidget(self.btn_add_row)
        action_bar.addWidget(self.btn_delete_rows)
        action_bar.addStretch()
        action_bar.addWidget(self.btn_save_changes)
        action_bar.addWidget(self.btn_export_csv)
        root.addLayout(action_bar)

        # 3. 안내 배너 (읽기 전용 / 변경사항 안내)
        self.lbl_hint = QLabel(
            "💡 탭 전환 시 로컬 캐시만 즉시 표시됩니다(Firestore 읽기 0건). "
            "회색 셀(remote_id, password_hash, face_descriptor, photo_data_url, updated_at, created_at)은 읽기 전용이며, "
            "수정한 셀은 노란색으로 표시된 뒤 [변경사항 일괄 저장] 시 반영됩니다."
        )
        _apply_role(self.lbl_hint, role="infoBanner")
        self.lbl_hint.setWordWrap(True)
        root.addWidget(self.lbl_hint)

        # 4. 스프레드시트 테이블
        self.table = QTableWidget(0, 0, self)
        _apply_role(self.table, obj_name="DbManagerSpreadsheetTable")
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.itemChanged.connect(self._on_table_item_changed)
        root.addWidget(self.table, stretch=1)

    # =========================================================================
    # 로컬 캐시 로드 & 테이블 렌더링 (Firestore 읽기 0건)
    # =========================================================================
    def _on_collection_changed(self) -> None:
        new_col = str(self.combo_collection.currentData() or DB_MANAGER_COLLECTIONS[0])
        if self._dirty_rows:
            ans = QMessageBox.question(
                self,
                "저장되지 않은 변경사항",
                f"'{self._current_collection}'에 저장되지 않은 변경사항이 {len(self._dirty_rows)}행 있습니다.\n"
                "저장하지 않고 다른 컬렉션으로 이동하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                prev_idx = self.combo_collection.findData(self._current_collection)
                if prev_idx >= 0:
                    self.combo_collection.blockSignals(True)
                    self.combo_collection.setCurrentIndex(prev_idx)
                    self.combo_collection.blockSignals(False)
                return

        self._current_collection = new_col
        self.load_from_local_cache()

    def load_from_local_cache(self, collection_name: Optional[str] = None) -> None:
        col = str(collection_name or self._current_collection or DB_MANAGER_COLLECTIONS[0])
        self._current_collection = col
        cache = self.db.get_collection_cache(col)
        rows = list(cache.get("rows") or [])
        last_sync_at = str(cache.get("last_sync_at") or "").strip()

        self._dirty_rows.clear()
        self._dirty_cells.clear()
        self._new_row_ids.clear()
        self._row_records = [dict(r) for r in rows if isinstance(r, dict)]
        self._columns = self._derive_columns(self._row_records)

        self._render_table()
        self._update_status_labels(last_sync_at=last_sync_at)
        self._update_save_button_state()

    @staticmethod
    def _derive_columns(rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return ["remote_id", "title", "updated_at", "created_at"]
        all_keys: Set[str] = set()
        for r in rows:
            all_keys.update(str(k) for k in r.keys())
        ordered: List[str] = []
        for col in PRIORITY_COLUMNS:
            if col in all_keys and col not in ordered:
                ordered.append(col)
        middle = sorted(k for k in all_keys if k not in ordered and k not in TAIL_COLUMNS)
        ordered.extend(middle)
        for col in TAIL_COLUMNS:
            if col in all_keys and col not in ordered:
                ordered.append(col)
        return ordered

    def _render_table(self) -> None:
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            self.table.setColumnCount(len(self._columns))
            self.table.setHorizontalHeaderLabels(self._columns)

            ro_bg = QBrush(QColor("#F1F5F9"))
            ro_fg = QBrush(QColor("#64748B"))

            for row_idx, rec in enumerate(self._row_records):
                self.table.insertRow(row_idx)
                for col_idx, col_name in enumerate(self._columns):
                    raw_val = rec.get(col_name, "")
                    disp, is_summarized = format_cell_display(col_name, raw_val)
                    item = QTableWidgetItem(disp)
                    item.setData(Qt.ItemDataRole.UserRole, raw_val)
                    item.setData(Qt.ItemDataRole.UserRole + 1, is_summarized)

                    if is_readonly_field(col_name):
                        item.setFlags(
                            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                        )
                        item.setBackground(ro_bg)
                        item.setForeground(ro_fg)
                    else:
                        item.setFlags(
                            Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsSelectable
                            | Qt.ItemFlag.ItemIsEditable
                        )
                    self.table.setItem(row_idx, col_idx, item)
        finally:
            self.table.blockSignals(False)

        self._apply_search_filter()

    def _apply_search_filter(self) -> None:
        query = self.edit_search.text().strip().lower()
        visible = 0
        total = self.table.rowCount()
        for row_idx in range(total):
            if not query:
                self.table.setRowHidden(row_idx, False)
                visible += 1
                continue
            matched = False
            for col_idx in range(self.table.columnCount()):
                item = self.table.item(row_idx, col_idx)
                if item is not None and query in item.text().lower():
                    matched = True
                    break
            self.table.setRowHidden(row_idx, not matched)
            if matched:
                visible += 1

        if query:
            self.lbl_row_count.setText(f"필터 {visible} / {total}건")
        else:
            self.lbl_row_count.setText(f"총 {total}건")

    def _update_status_labels(self, last_sync_at: str = "") -> None:
        if last_sync_at:
            self.lbl_last_sync.setText(f"마지막 동기화: {last_sync_at}")
            _apply_role(self.lbl_last_sync, role="badgeSuccess", obj_name="DbManagerLastSyncBadge")
        else:
            self.lbl_last_sync.setText("마지막 동기화: 로컬 캐시 없음")
            _apply_role(self.lbl_last_sync, role="badgeNeutral", obj_name="DbManagerLastSyncBadge")

    def _update_save_button_state(self) -> None:
        cnt = len(self._dirty_rows)
        self.btn_save_changes.setEnabled(cnt > 0)
        self.btn_save_changes.setText(
            f"변경사항 일괄 저장 ({cnt}건)" if cnt > 0 else "변경사항 일괄 저장"
        )

    # =========================================================================
    # 셀 편집 & 행 추가/삭제
    # =========================================================================
    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        row_idx = item.row()
        col_idx = item.column()
        if not (0 <= row_idx < len(self._row_records)):
            return
        if not (0 <= col_idx < len(self._columns)):
            return
        col_name = self._columns[col_idx]
        if is_readonly_field(col_name):
            return

        orig_val = item.data(Qt.ItemDataRole.UserRole)
        new_val = parse_edited_cell_value(item.text(), orig_val, col_name)
        self._row_records[row_idx][col_name] = new_val
        item.setData(Qt.ItemDataRole.UserRole, new_val)

        self._dirty_rows.add(row_idx)
        self._dirty_cells.add((row_idx, col_idx))

        dirty_bg = QBrush(QColor("#FEF3C7"))
        item.setBackground(dirty_bg)
        self._update_save_button_state()

    def _on_add_row_clicked(self) -> None:
        if not self._columns:
            self._columns = ["remote_id", "title", "updated_at", "created_at"]
        new_id = f"manual_{uuid.uuid4().hex[:12]}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_rec: Dict[str, Any] = {col: "" for col in self._columns}
        new_rec["remote_id"] = new_id
        new_rec["updated_at"] = now_str
        new_rec["created_at"] = now_str

        self._row_records.append(new_rec)
        row_idx = self.table.rowCount()
        self._new_row_ids.add(new_id)
        self._dirty_rows.add(row_idx)

        self.table.blockSignals(True)
        try:
            if self.table.columnCount() != len(self._columns):
                self.table.setColumnCount(len(self._columns))
                self.table.setHorizontalHeaderLabels(self._columns)
            self.table.insertRow(row_idx)
            ro_bg = QBrush(QColor("#F1F5F9"))
            ro_fg = QBrush(QColor("#64748B"))
            new_bg = QBrush(QColor("#FEF3C7"))
            for col_idx, col_name in enumerate(self._columns):
                val = new_rec.get(col_name, "")
                item = QTableWidgetItem(str(val))
                item.setData(Qt.ItemDataRole.UserRole, val)
                item.setData(Qt.ItemDataRole.UserRole + 1, False)
                if is_readonly_field(col_name):
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    item.setBackground(ro_bg)
                    item.setForeground(ro_fg)
                else:
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsEditable
                    )
                    item.setBackground(new_bg)
                self.table.setItem(row_idx, col_idx, item)
        finally:
            self.table.blockSignals(False)

        self._apply_search_filter()
        self._update_save_button_state()
        self.table.scrollToBottom()
        first_editable = next(
            (i for i, c in enumerate(self._columns) if not is_readonly_field(c)),
            0,
        )
        self.table.setCurrentCell(row_idx, first_editable)

    def _on_delete_rows_clicked(self) -> None:
        selected_rows = sorted(
            {idx.row() for idx in self.table.selectedIndexes() if not self.table.isRowHidden(idx.row())},
            reverse=True,
        )
        if not selected_rows:
            QMessageBox.information(self, "행 선택", "삭제할 행을 먼저 선택해 주세요.")
            return

        ans = QMessageBox.question(
            self,
            "선택 행 삭제 확인",
            f"선택하신 {len(selected_rows)}개 행을 Firestore 및 로컬 캐시에서 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        remote_ids_to_delete: List[str] = []
        local_only_rows: List[int] = []
        for r in selected_rows:
            rec = self._row_records[r]
            rid = str(rec.get("remote_id") or "").strip()
            if rid and rid not in self._new_row_ids:
                remote_ids_to_delete.append(rid)
            else:
                local_only_rows.append(r)

        if not remote_ids_to_delete:
            self._remove_rows_from_table(local_only_rows)
            return

        col = self._current_collection
        self._set_busy(True)

        def _do_delete(_worker: FunctionWorker) -> Dict[str, Any]:
            return self.sync_service.delete_collection_rows(col, remote_ids_to_delete)

        worker = FunctionWorker(f"Firestore 삭제 ({col})", _do_delete)

        def _on_done(res: Dict[str, Any]) -> None:
            self._set_busy(False)
            if not res.get("ok"):
                QMessageBox.warning(
                    self,
                    "삭제 실패",
                    f"Firestore 문서 삭제에 실패했습니다:\n{res.get('error', '알 수 없는 오류')}",
                )
                return
            self.load_from_local_cache(col)
            QMessageBox.information(
                self,
                "삭제 완료",
                f"{res.get('deleted_count', len(remote_ids_to_delete))}건이 삭제되었습니다.",
            )

        def _on_err(err: str) -> None:
            self._set_busy(False)
            QMessageBox.warning(self, "삭제 오류", f"삭제 중 오류가 발생했습니다:\n{err}")

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_SYNC_KEY,
            on_result=_on_done,
            on_error=_on_err,
        )

    def _remove_rows_from_table(self, row_indices: List[int]) -> None:
        self.table.blockSignals(True)
        try:
            for r in sorted(row_indices, reverse=True):
                if 0 <= r < len(self._row_records):
                    self._row_records.pop(r)
                    self.table.removeRow(r)
                    self._dirty_rows.discard(r)
        finally:
            self.table.blockSignals(False)
        self._dirty_rows = {
            r - sum(1 for d in row_indices if d < r) for r in self._dirty_rows
        }
        self._apply_search_filter()
        self._update_save_button_state()

    # =========================================================================
    # Firestore 증분(델타) 새로고침 / 전체 재동기화 / 변경사항 일괄 저장
    # =========================================================================
    def _set_busy(self, busy: bool) -> None:
        self.combo_collection.setEnabled(not busy)
        self.btn_delta_refresh.setEnabled(not busy)
        self.btn_full_refresh.setEnabled(not busy)
        self.btn_add_row.setEnabled(not busy)
        self.btn_delete_rows.setEnabled(not busy)
        self.btn_save_changes.setEnabled((not busy) and len(self._dirty_rows) > 0)

    def _on_delta_refresh_clicked(self) -> None:
        col = self._current_collection
        self._set_busy(True)

        def _run(_worker: FunctionWorker) -> Dict[str, Any]:
            return self.sync_service.sync_collection_delta(col)

        worker = FunctionWorker(f"변경분 새로고침 ({col})", _run)

        def _on_done(res: Dict[str, Any]) -> None:
            self._set_busy(False)
            if not res.get("ok"):
                QMessageBox.warning(
                    self,
                    "새로고침 실패",
                    f"Firestore 변경분 조회에 실패했습니다:\n{res.get('error', '알 수 없는 오류')}",
                )
                return
            self.load_from_local_cache(col)

        def _on_err(err: str) -> None:
            self._set_busy(False)
            QMessageBox.warning(self, "새로고침 오류", f"새로고침 중 오류가 발생했습니다:\n{err}")

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_SYNC_KEY,
            on_result=_on_done,
            on_error=_on_err,
        )

    def _on_full_refresh_clicked(self) -> None:
        col = self._current_collection
        self._set_busy(True)

        def _run(_worker: FunctionWorker) -> Dict[str, Any]:
            return self.sync_service.sync_collection_full(col)

        worker = FunctionWorker(f"전체 다시 받기 ({col})", _run)

        def _on_done(res: Dict[str, Any]) -> None:
            self._set_busy(False)
            if not res.get("ok"):
                QMessageBox.warning(
                    self,
                    "전체 받기 실패",
                    f"Firestore 전체 조회에 실패했습니다:\n{res.get('error', '알 수 없는 오류')}",
                )
                return
            self.load_from_local_cache(col)

        def _on_err(err: str) -> None:
            self._set_busy(False)
            QMessageBox.warning(self, "전체 받기 오류", f"전체 받기 중 오류가 발생했습니다:\n{err}")

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_SYNC_KEY,
            on_result=_on_done,
            on_error=_on_err,
        )

    def _on_save_changes_clicked(self) -> None:
        if not self._dirty_rows:
            return
        col = self._current_collection
        dirty_records = [
            dict(self._row_records[r])
            for r in sorted(self._dirty_rows)
            if 0 <= r < len(self._row_records)
        ]
        if not dirty_records:
            return

        self._set_busy(True)

        def _run(_worker: FunctionWorker) -> Dict[str, Any]:
            return self.sync_service.upsert_collection_rows(col, dirty_records)

        worker = FunctionWorker(f"변경사항 일괄 저장 ({col})", _run)

        def _on_done(res: Dict[str, Any]) -> None:
            self._set_busy(False)
            if not res.get("ok"):
                QMessageBox.warning(
                    self,
                    "일괄 저장 실패",
                    f"Firestore 저장에 실패했습니다 (Direct Admin 권한 및 네트워크를 확인하세요):\n"
                    f"{res.get('error', '알 수 없는 오류')}",
                )
                return
            self.load_from_local_cache(col)
            QMessageBox.information(
                self,
                "일괄 저장 완료",
                f"수정/추가된 {res.get('saved_count', len(dirty_records))}건이 Firestore 및 로컬 캐시에 저장되었습니다.",
            )

        def _on_err(err: str) -> None:
            self._set_busy(False)
            QMessageBox.warning(self, "일괄 저장 오류", f"저장 중 오류가 발생했습니다:\n{err}")

        self.task_runner.start_worker(
            worker=worker,
            task_key=self.TASK_SYNC_KEY,
            on_result=_on_done,
            on_error=_on_err,
        )

    # =========================================================================
    # CSV 내보내기 (UTF-8 BOM)
    # =========================================================================
    def _on_export_csv_clicked(self) -> None:
        if not self._columns or not self._row_records:
            QMessageBox.information(self, "내보내기 안내", "내보낼 데이터가 없습니다.")
            return
        col = self._current_collection
        default_name = f"eduflow_{col}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "CSV 내보내기 (UTF-8 BOM)",
            default_name,
            "CSV (*.csv)",
        )
        if not save_path:
            return
        count = self.export_visible_to_csv(save_path)
        QMessageBox.information(
            self,
            "CSV 내보내기 완료",
            f"총 {count}건의 데이터를 '{Path(save_path).name}'에 저장했습니다.",
        )

    def export_visible_to_csv(self, output_path: str) -> int:
        written = 0
        with open(output_path, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(self._columns)
            for row_idx in range(self.table.rowCount()):
                if self.table.isRowHidden(row_idx):
                    continue
                row_vals: List[str] = []
                for col_idx in range(self.table.columnCount()):
                    item = self.table.item(row_idx, col_idx)
                    row_vals.append(item.text() if item is not None else "")
                writer.writerow(row_vals)
                written += 1
        return written
