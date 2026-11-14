"""
1단계 과제 추출 중 '검토 필요'로 탐지된 의심 항목만 모아
원본 파일과 대조하고 수정·반영할 수 있는 교사용 검수 다이얼로그 (ui/dialogs/extraction_review_dialog.py).
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.multi_doc_extractor_service import (
    extract_raw_text_preview,
    load_review_items_from_excel,
    save_review_updates_to_excel,
)


class ExtractionReviewDialog(QDialog):
    """'검토 필요' 의심 항목을 원본과 대조하여 수정·확인 후 엑셀에 반영하는 검수 다이얼로그."""

    def __init__(
        self,
        excel_path: Path | str,
        review_items: Optional[List[Dict[str, Any]]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.excel_path = Path(excel_path)
        self.items: List[Dict[str, Any]] = (
            [dict(it) for it in review_items]
            if review_items is not None
            else load_review_items_from_excel(self.excel_path)
        )
        self._visible_indices: List[int] = []
        self._current_item_idx: Optional[int] = None
        self._preview_cache: Dict[str, str] = {}
        self.has_saved_changes: bool = False

        self.setWindowTitle(f"과제 추출 의심 항목 검수 센터 — {self.excel_path.name}")
        self.resize(1120, 720)
        self._build_ui()
        self._refresh_table()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # 상단 안내 바
        top_bar = QHBoxLayout()
        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet("font-weight: 700; font-size: 13px;")

        self.chk_only_pending = QCheckBox("미검토('검토 필요') 항목만 보기")
        self.chk_only_pending.setChecked(True)
        self.chk_only_pending.stateChanged.connect(self._refresh_table)

        top_bar.addWidget(self.lbl_summary, stretch=1)
        top_bar.addWidget(self.chk_only_pending)
        root.addLayout(top_bar)

        # 좌우 분할 (목록 vs 원본 대조/수정)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 좌측 패널: 의심 항목 목록
        left_w = QWidget()
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(0, 0, 4, 0)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["상태", "학번", "이름", "문항명", "의심 사유"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v_hdr = self.table.verticalHeader()
        if v_hdr is not None:
            v_hdr.setVisible(False)
        h_hdr = self.table.horizontalHeader()
        if h_hdr is not None:
            h_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            h_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            h_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            h_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            h_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.currentCellChanged.connect(self._on_row_selected)
        left_lay.addWidget(self.table)
        splitter.addWidget(left_w)

        # 우측 패널: 원본 대조 및 수정 에디터
        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(4, 0, 0, 0)
        right_lay.setSpacing(8)

        self.lbl_item_header = QLabel("좌측 목록에서 검수할 항목을 선택하세요.")
        self.lbl_item_header.setStyleSheet("font-weight: 700; font-size: 13px;")
        self.lbl_item_header.setWordWrap(True)
        right_lay.addWidget(self.lbl_item_header)

        self.lbl_reason_banner = QLabel("")
        self.lbl_reason_banner.setWordWrap(True)
        self.lbl_reason_banner.setStyleSheet(
            "background-color: #FEF3C7; color: #92400E; border: 1px solid #F59E0B; "
            "border-radius: 6px; padding: 8px; font-weight: 600;"
        )
        self.lbl_reason_banner.setVisible(False)
        right_lay.addWidget(self.lbl_reason_banner)

        # 원본 대조 영역 헤더
        raw_hdr = QHBoxLayout()
        lbl_raw = QLabel("원본 문서 텍스트 대조 (참고용)")
        lbl_raw.setStyleSheet("font-weight: 600;")
        self.btn_open_orig = QPushButton("원본 파일 열기")
        self.btn_open_orig.setMinimumHeight(28)
        self.btn_open_orig.clicked.connect(self._on_open_original_file)
        raw_hdr.addWidget(lbl_raw, stretch=1)
        raw_hdr.addWidget(self.btn_open_orig)
        right_lay.addLayout(raw_hdr)

        self.txt_raw_preview = QTextEdit()
        self.txt_raw_preview.setReadOnly(True)
        self.txt_raw_preview.setMinimumHeight(170)
        self.txt_raw_preview.setPlaceholderText("원본 문서의 추출 텍스트가 여기에 표시됩니다.")
        right_lay.addWidget(self.txt_raw_preview, stretch=1)

        # 수정 에디터
        lbl_edit = QLabel("추출된 값 수정 (엑셀 셀에 반영될 최종 내용)")
        lbl_edit.setStyleSheet("font-weight: 600;")
        right_lay.addWidget(lbl_edit)

        self.txt_edit_value = QTextEdit()
        self.txt_edit_value.setMinimumHeight(140)
        self.txt_edit_value.setPlaceholderText("원본과 대조 후 수정할 내용을 입력하세요.")
        right_lay.addWidget(self.txt_edit_value, stretch=1)

        # 항목별 액션 버튼
        item_btns = QHBoxLayout()
        self.btn_apply_next = QPushButton("수정 반영 및 확인 완료 (다음 ▶)")
        self.btn_apply_next.setMinimumHeight(34)
        self.btn_apply_next.setProperty("role", "primary")
        self.btn_apply_next.clicked.connect(lambda: self._commit_current_item(use_edited=True))

        self.btn_confirm_as_is = QPushButton("원본 그대로 확인 완료")
        self.btn_confirm_as_is.setMinimumHeight(34)
        self.btn_confirm_as_is.setProperty("role", "secondary")
        self.btn_confirm_as_is.clicked.connect(lambda: self._commit_current_item(use_edited=False))

        item_btns.addWidget(self.btn_apply_next, stretch=1)
        item_btns.addWidget(self.btn_confirm_as_is)
        right_lay.addLayout(item_btns)

        splitter.addWidget(right_w)
        splitter.setSizes([470, 620])
        root.addWidget(splitter, stretch=1)

        # 하단 저장/닫기 바
        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch(1)

        self.btn_save_excel = QPushButton("엑셀(.xlsx)에 일괄 저장 후 닫기")
        self.btn_save_excel.setMinimumHeight(36)
        self.btn_save_excel.setProperty("role", "success")
        self.btn_save_excel.clicked.connect(self._on_save_and_close)

        self.btn_close = QPushButton("닫기")
        self.btn_close.setMinimumHeight(36)
        self.btn_close.clicked.connect(self.reject)

        bottom_bar.addWidget(self.btn_save_excel)
        bottom_bar.addWidget(self.btn_close)
        root.addLayout(bottom_bar)

    def _update_summary_label(self) -> None:
        total = len(self.items)
        pending = sum(1 for it in self.items if it.get("status") != "reviewed")
        done = total - pending
        self.lbl_summary.setText(
            f"전체 의심 항목 {total}건 · 미검토 {pending}건 · 검토 완료 {done}건"
        )

    def _refresh_table(self) -> None:
        self._update_summary_label()
        only_pending = self.chk_only_pending.isChecked()
        self._visible_indices = [
            idx
            for idx, it in enumerate(self.items)
            if not only_pending or it.get("status") != "reviewed"
        ]

        self.table.blockSignals(True)
        self.table.setRowCount(len(self._visible_indices))
        for row_idx, item_idx in enumerate(self._visible_indices):
            it = self.items[item_idx]
            is_done = it.get("status") == "reviewed"
            st_text = "완료" if is_done else "검토 필요"
            row_items = [
                QTableWidgetItem(st_text),
                QTableWidgetItem(str(it.get("student_id", ""))),
                QTableWidgetItem(str(it.get("name", ""))),
                QTableWidgetItem(str(it.get("field_name", ""))),
                QTableWidgetItem(str(it.get("reason", ""))),
            ]
            row_items[0].setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_done:
                row_items[0].setForeground(QBrush(QColor("#15803D")))
            else:
                row_items[0].setForeground(QBrush(QColor("#B45309")))

            for col_idx, cell in enumerate(row_items):
                self.table.setItem(row_idx, col_idx, cell)
        self.table.blockSignals(False)

        if self._visible_indices:
            self.table.selectRow(0)
            self._load_item_detail(self._visible_indices[0])
        else:
            self._current_item_idx = None
            self.lbl_item_header.setText("표시할 검수 항목이 없습니다. (모든 항목 검토 완료)")
            self.lbl_reason_banner.setVisible(False)
            self.txt_raw_preview.clear()
            self.txt_edit_value.clear()
            self.btn_apply_next.setEnabled(False)
            self.btn_confirm_as_is.setEnabled(False)
            self.btn_open_orig.setEnabled(False)

    def _on_row_selected(self, row: int, _col: int, _prev_row: int, _prev_col: int) -> None:
        if 0 <= row < len(self._visible_indices):
            self._load_item_detail(self._visible_indices[row])

    def _load_item_detail(self, item_idx: int) -> None:
        self._current_item_idx = item_idx
        it = self.items[item_idx]

        self.btn_apply_next.setEnabled(True)
        self.btn_confirm_as_is.setEnabled(True)
        self.btn_open_orig.setEnabled(bool(it.get("file_path")))

        sid = it.get("student_id") or "학번미상"
        sname = it.get("name") or "이름미상"
        fname = it.get("file_name") or ""
        field_name = it.get("field_name") or ""
        self.lbl_item_header.setText(
            f"[{sid} {sname}] 문항: {field_name}  (파일: {fname})"
        )

        source = it.get("source") or "검증"
        reason = it.get("reason") or "확인 필요"
        self.lbl_reason_banner.setText(f"[{source}] {reason}")
        self.lbl_reason_banner.setVisible(True)

        fpath = str(it.get("file_path") or "")
        preview = str(it.get("raw_text_preview") or "").strip()
        if not preview and fpath:
            if fpath not in self._preview_cache:
                self._preview_cache[fpath] = extract_raw_text_preview(fpath)
            preview = self._preview_cache[fpath]
        self.txt_raw_preview.setPlainText(preview)

        self.txt_edit_value.setPlainText(str(it.get("extracted_value", "")))

    def _commit_current_item(self, use_edited: bool) -> None:
        if self._current_item_idx is None:
            return
        it = self.items[self._current_item_idx]
        if use_edited:
            it["extracted_value"] = self.txt_edit_value.toPlainText().strip()
        it["status"] = "reviewed"
        self._refresh_table()

    def _on_open_original_file(self) -> None:
        if self._current_item_idx is None:
            return
        fpath = str(self.items[self._current_item_idx].get("file_path") or "")
        if not fpath or not Path(fpath).exists():
            QMessageBox.warning(self, "파일 없음", f"원본 파일을 찾을 수 없습니다:\n{fpath}")
            return
        try:
            os.startfile(fpath)  # type: ignore[attr-defined]
        except Exception as exc:
            QMessageBox.warning(self, "열기 실패", f"원본 파일을 열 수 없습니다:\n{exc}")

    def _on_save_and_close(self) -> None:
        # 현재 편집 중인 텍스트가 있고 아직 커밋 전이라면 반영 여부 확인 없이 현재 선택 항목 값도 보존할 수 있도록 처리
        try:
            save_review_updates_to_excel(self.excel_path, self.items)
            self.has_saved_changes = True
            self.accept()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "저장 오류",
                f"엑셀 파일에 검수 결과를 저장하는 중 오류가 발생했습니다:\n{exc}\n\n"
                "(엑셀 파일이 다른 프로그램에서 열려 있다면 닫고 다시 시도해 주세요.)",
            )


# 별칭(Alias): OcrReviewDialog 이름으로도 직접 임포트 가능
OcrReviewDialog = ExtractionReviewDialog
