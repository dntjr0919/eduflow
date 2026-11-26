"""
가정통신문 PDF 양식 좌표 매핑 에디터 다이얼로그.
- PDF 및 HWP/HWPX(convert_hwp_to_pdf 변환) 문서를 pypdfium2로 페이지 이미지 렌더링
- 좌측 팔레트: 1번부터 순번([1], [2], ...) 부여 및 배치 완료(✓) 구분 표시
- 우측 캔버스: #1, #2(체크 마커는 1✓) 번호 배지 표시 + 마우스 오버 시 전체 항목명 툴팁
- 클릭 즉시 배치 & 다음 미배치 항목 자동 이동 + 페이지 맞춤/75%/100%/125% 줌
- [AI/텍스트 자동 위치 추천]: 질문 문장 주변 근접 선택지 탐색 및 라벨 겹침 회피
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pypdfium2 as pdfium
from PyQt6.QtCore import QEvent, QPoint, QRectF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QHelpEvent,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import FunctionWorker
from services.llm_client import MultiLlmClient
from services.notice_parser_service import convert_hwp_to_pdf_with_reason
from services.notice_pdf_fill_service import (
    build_pdf_vision_mapping_prompt,
    parse_pdf_vision_mapping_response,
    render_pdf_pages_to_png_files,
    request_pdf_vision_mapping_api,
)

SYSTEM_PALETTE_FIELDS: List[Dict[str, Any]] = [
    {"key": "grade", "label": "학년", "kind": "text", "w_ratio": 0.045, "h_ratio": 0.026},
    {"key": "class_no", "label": "반", "kind": "text", "w_ratio": 0.045, "h_ratio": 0.026},
    {"key": "number", "label": "번호", "kind": "text", "w_ratio": 0.045, "h_ratio": 0.026},
    {"key": "name", "label": "이름", "kind": "text", "w_ratio": 0.11, "h_ratio": 0.028},
    {"key": "student_signature", "label": "학생 서명", "kind": "signature", "w_ratio": 0.13, "h_ratio": 0.050},
    {"key": "parent_signature", "label": "학부모 서명", "kind": "signature", "w_ratio": 0.13, "h_ratio": 0.050},
]

_KIND_COLORS: Dict[str, Tuple[QColor, QColor]] = {
    "text": (QColor(37, 99, 235, 65), QColor(29, 78, 216, 235)),
    "option": (QColor(16, 185, 129, 75), QColor(5, 150, 105, 240)),
    "signature": (QColor(168, 85, 247, 65), QColor(126, 34, 206, 235)),
}


def build_palette_entries(reply_schema: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """시스템 기본 필드와 reply_schema 항목을 1-based 순번이 부여된 팔레트 목록으로 변환합니다."""
    raw_entries: List[Dict[str, Any]] = []
    for sys_item in SYSTEM_PALETTE_FIELDS:
        raw_entries.append(
            {
                "palette_id": f"sys:{sys_item['key']}",
                "key": sys_item["key"],
                "label": sys_item["label"],
                "kind": sys_item["kind"],
                "option": "",
                "w_ratio": sys_item["w_ratio"],
                "h_ratio": sys_item["h_ratio"],
                "base_display": f"[기본] {sys_item['label']}",
            }
        )

    for idx, item in enumerate(reply_schema or [], start=1):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or f"field_{idx}").strip()
        label = str(item.get("label") or key).strip()
        ftype = str(item.get("type") or "text").strip().lower()
        options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]

        if ftype in ("radio", "checkbox") and options:
            for opt in options:
                raw_entries.append(
                    {
                        "palette_id": f"opt:{key}:{opt}",
                        "key": key,
                        "label": label,
                        "kind": "option",
                        "option": opt,
                        "w_ratio": 0.032,
                        "h_ratio": 0.026,
                        "base_display": f"[✓] {label} → {opt}",
                    }
                )
        else:
            h_r = 0.07 if ftype == "textarea" else 0.030
            w_r = 0.34 if ftype == "textarea" else 0.20
            raw_entries.append(
                {
                    "palette_id": f"txt:{key}",
                    "key": key,
                    "label": label,
                    "kind": "text",
                    "option": "",
                    "w_ratio": w_r,
                    "h_ratio": h_r,
                    "base_display": f"[입력] {label}",
                }
            )

    for num, entry in enumerate(raw_entries, start=1):
        entry["num"] = num
        entry["display"] = f"[{num}] {entry['base_display']}"
    return raw_entries


class _PdfMappingCanvas(QWidget):
    """PDF 페이지 이미지 위에 번호 배지 박스를 표시하고 클릭 배치·이동·리사이즈·삭제하는 캔버스."""

    selection_changed = pyqtSignal(str)
    map_changed = pyqtSignal()
    empty_area_clicked = pyqtSignal(float, float)  # x_ratio, y_ratio

    _HANDLE_SIZE = 10

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._pixmap: Optional[QPixmap] = None
        self._current_page: int = 1
        self._field_map: List[Dict[str, Any]] = []
        self._palette_num_by_pid: Dict[str, int] = {}
        self._selected_id: str = ""
        self._drag_mode: str = ""  # "move" | "resize"
        self._drag_start_pos: QPoint = QPoint()
        self._drag_orig_rect: Tuple[float, float, float, float] = (0.0, 0.0, 0.1, 0.04)

    def set_palette_index_map(self, mapping: Dict[str, int]) -> None:
        self._palette_num_by_pid = dict(mapping)
        self.update()

    def set_page_pixmap(self, pixmap: QPixmap, page_num: int) -> None:
        self._pixmap = pixmap
        self._current_page = int(page_num)
        self.setFixedSize(pixmap.size())
        self.update()

    def set_field_map(self, field_map: List[Dict[str, Any]]) -> None:
        self._field_map = field_map
        self.update()

    def selected_id(self) -> str:
        return self._selected_id

    def select_id(self, box_id: str) -> None:
        self._selected_id = box_id
        self.selection_changed.emit(box_id)
        self.update()

    def item_center_px(self, item: Dict[str, Any]) -> QPoint:
        r = self._item_rect_px(item)
        return QPoint(int(r.center().x()), int(r.center().y()))

    def _current_page_items(self) -> List[Dict[str, Any]]:
        return [item for item in self._field_map if int(item.get("page", 1)) == self._current_page]

    def _item_rect_px(self, item: Dict[str, Any]) -> QRectF:
        w = max(1.0, float(self.width()))
        h = max(1.0, float(self.height()))
        return QRectF(
            float(item.get("x_ratio", 0.0)) * w,
            float(item.get("y_ratio", 0.0)) * h,
            max(16.0, float(item.get("w_ratio", 0.1)) * w),
            max(16.0, float(item.get("h_ratio", 0.04)) * h),
        )

    def _resize_handle_rect(self, r: QRectF) -> QRectF:
        hs = float(self._HANDLE_SIZE)
        return QRectF(r.right() - hs, r.bottom() - hs, hs, hs)

    def _badge_text(self, item: Dict[str, Any]) -> str:
        num = item.get("num") or self._palette_num_by_pid.get(str(item.get("palette_id") or ""), 0)
        kind = str(item.get("kind") or "text")
        if num:
            return f"{num}✓" if kind == "option" else f"#{num}"
        return "✓" if kind == "option" else str(item.get("key") or "")

    def _tooltip_text(self, item: Dict[str, Any]) -> str:
        num = item.get("num") or self._palette_num_by_pid.get(str(item.get("palette_id") or ""), 0)
        prefix = f"[{num}] " if num else ""
        label = str(item.get("label") or item.get("key") or "")
        if item.get("kind") == "option" and item.get("option"):
            return f"{prefix}{label} → {item.get('option')}"
        return f"{prefix}{label}"

    def delete_selected(self) -> bool:
        if not self._selected_id:
            return False
        before = len(self._field_map)
        self._field_map[:] = [x for x in self._field_map if x.get("id") != self._selected_id]
        if len(self._field_map) != before:
            self._selected_id = ""
            self.selection_changed.emit("")
            self.map_changed.emit()
            self.update()
            return True
        return False

    def event(self, ev: QEvent) -> bool:
        if ev.type() == QEvent.Type.ToolTip and isinstance(ev, QHelpEvent):
            pos = ev.pos().toPointF()
            for item in reversed(self._current_page_items()):
                if self._item_rect_px(item).contains(pos):
                    QToolTip.showText(ev.globalPos(), self._tooltip_text(item), self)
                    return True
            QToolTip.hideText()
        return super().event(ev)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._pixmap is not None:
            painter.drawPixmap(0, 0, self._pixmap)

        font = QFont("Malgun Gothic", 9, QFont.Weight.Bold)
        painter.setFont(font)

        for item in self._current_page_items():
            rect = self._item_rect_px(item)
            kind = str(item.get("kind") or "text")
            fill_col, border_col = _KIND_COLORS.get(kind, _KIND_COLORS["text"])
            is_sel = item.get("id") == self._selected_id

            if is_sel:
                fill_col = QColor(245, 158, 11, 95)
                border_col = QColor(217, 119, 6, 255)

            painter.fillRect(rect, fill_col)
            pen = QPen(border_col, 2.8 if is_sel else 1.6, Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            painter.drawRect(rect)

            badge = self._badge_text(item)
            painter.setPen(QColor(255, 255, 255))
            badge_w = min(rect.width(), max(22.0, len(badge) * 8.5 + 8.0))
            badge_h = min(rect.height(), 18.0)
            badge_rect = QRectF(rect.left(), rect.top(), badge_w, badge_h)
            painter.fillRect(badge_rect, border_col)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)

            if is_sel:
                hr = self._resize_handle_rect(rect)
                painter.fillRect(hr, border_col)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if event.button() == Qt.MouseButton.RightButton:
            for item in reversed(self._current_page_items()):
                if self._item_rect_px(item).contains(pos):
                    self.select_id(str(item.get("id") or ""))
                    menu = QMenu(self)
                    act_del = QAction("이 항목 삭제", self)
                    act_del.triggered.connect(self.delete_selected)
                    menu.addAction(act_del)
                    menu.exec(event.globalPosition().toPoint())
                    return
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        for item in reversed(self._current_page_items()):
            r = self._item_rect_px(item)
            if self._resize_handle_rect(r).contains(pos):
                self.select_id(str(item.get("id") or ""))
                self._drag_mode = "resize"
                self._drag_start_pos = event.pos()
                self._drag_orig_rect = (
                    float(item["x_ratio"]),
                    float(item["y_ratio"]),
                    float(item["w_ratio"]),
                    float(item["h_ratio"]),
                )
                return
            if r.contains(pos):
                self.select_id(str(item.get("id") or ""))
                self._drag_mode = "move"
                self._drag_start_pos = event.pos()
                self._drag_orig_rect = (
                    float(item["x_ratio"]),
                    float(item["y_ratio"]),
                    float(item["w_ratio"]),
                    float(item["h_ratio"]),
                )
                return

        self.select_id("")
        self._drag_mode = ""
        xr = max(0.0, min(1.0, pos.x() / max(1.0, float(self.width()))))
        yr = max(0.0, min(1.0, pos.y() / max(1.0, float(self.height()))))
        self.empty_area_clicked.emit(xr, yr)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._drag_mode or not self._selected_id:
            return
        item = next((x for x in self._field_map if x.get("id") == self._selected_id), None)
        if item is None:
            return

        dx = (event.pos().x() - self._drag_start_pos.x()) / max(1.0, float(self.width()))
        dy = (event.pos().y() - self._drag_start_pos.y()) / max(1.0, float(self.height()))
        ox, oy, ow, oh = self._drag_orig_rect

        if self._drag_mode == "move":
            nx = max(0.0, min(1.0 - ow, ox + dx))
            ny = max(0.0, min(1.0 - oh, oy + dy))
            item["x_ratio"] = round(nx, 4)
            item["y_ratio"] = round(ny, 4)
        elif self._drag_mode == "resize":
            nw = max(0.02, min(1.0 - ox, ow + dx))
            nh = max(0.018, min(1.0 - oy, oh + dy))
            item["w_ratio"] = round(nw, 4)
            item["h_ratio"] = round(nh, 4)
        self.update()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        if self._drag_mode:
            self._drag_mode = ""
            self.map_changed.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.delete_selected():
                return
        super().keyPressEvent(event)


class NoticePdfMapperDialog(QDialog):
    """가정통신문 PDF 양식 좌표 매핑 에디터 다이얼로그."""

    def __init__(
        self,
        *,
        pdf_path: str,
        reply_schema: List[Dict[str, Any]],
        initial_field_map: Optional[List[Dict[str, Any]]] = None,
        task_runner: Optional[Any] = None,
        ai_dock: Optional[Any] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("가정통신문 PDF 양식 좌표 매핑 에디터")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setSizeGripEnabled(True)
        self.resize(1240, 840)

        self._task_runner = task_runner
        self._ai_dock = ai_dock
        self._temp_conv_dir: Optional[Path] = None
        self._vision_temp_dir: Optional[Path] = None
        self._resolved_pdf_path = self._resolve_pdf_source(pdf_path)
        self._doc = pdfium.PdfDocument(str(self._resolved_pdf_path))
        self._page_count = max(1, len(self._doc))
        self._current_page = 1

        self._reply_schema: List[Dict[str, Any]] = copy.deepcopy(reply_schema or [])
        self._palette_entries = build_palette_entries(self._reply_schema)
        self._palette_num_by_pid = {
            str(e["palette_id"]): int(e["num"]) for e in self._palette_entries
        }
        self._field_map: List[Dict[str, Any]] = copy.deepcopy(initial_field_map or [])
        for box in self._field_map:
            pid = str(box.get("palette_id") or "")
            if pid in self._palette_num_by_pid:
                box["num"] = self._palette_num_by_pid[pid]

        self._build_ui()
        self._refresh_palette_items()
        self._render_current_page()
        self._select_next_unplaced_palette_item()
        self._refresh_status()

    def _resolve_pdf_source(self, src_path: str) -> Path:
        p = Path(str(src_path or "").strip())
        if not p.is_file():
            raise FileNotFoundError(f"양식 파일을 찾을 수 없습니다: {p}")
        if p.suffix.lower() in (".hwp", ".hwpx"):
            converted, reason = convert_hwp_to_pdf_with_reason(p)
            if converted is None or not converted.is_file():
                detail = f"\n\n(실패 사유: {reason})" if reason else ""
                raise RuntimeError(
                    "HWP/HWPX 파일을 PDF로 변환하지 못했습니다. 한컴오피스(한글) 설치 상태를 확인하거나 PDF 파일로 변환 후 선택해 주세요."
                    + detail
                )
            self._temp_conv_dir = converted.parent
            return converted
        if p.suffix.lower() != ".pdf":
            raise ValueError("PDF, HWP, HWPX 파일만 양식 좌표 매핑을 지원합니다.")
        return p

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top_bar = QHBoxLayout()
        self.btn_prev_page = QPushButton("◀ 이전 페이지")
        self.btn_prev_page.clicked.connect(lambda: self._go_page(self._current_page - 1))
        self.lbl_page_info = QLabel("1 / 1 페이지")
        self.lbl_page_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_page_info.setMinimumWidth(100)
        self.btn_next_page = QPushButton("다음 페이지 ▶")
        self.btn_next_page.clicked.connect(lambda: self._go_page(self._current_page + 1))

        self.combo_zoom = QComboBox()
        self.combo_zoom.addItem("페이지 맞춤", "fit")
        self.combo_zoom.addItem("75%", 0.75)
        self.combo_zoom.addItem("100%", 1.0)
        self.combo_zoom.addItem("125%", 1.25)
        self.combo_zoom.currentIndexChanged.connect(lambda _: self._render_current_page())

        self.btn_auto_suggest = QPushButton("텍스트 위치 추천")
        self.btn_auto_suggest.setToolTip("PDF 내 텍스트 좌표를 로컬에서 빠르게 검색해 자동 배치합니다.")
        self.btn_auto_suggest.clicked.connect(self._on_auto_suggest_positions)

        self.btn_ai_vision_map = QPushButton("AI 비전 자동 매핑")
        self.btn_ai_vision_map.setStyleSheet(
            "QPushButton { background: #2563EB; color: white; font-weight: bold; padding: 6px 14px; border-radius: 6px; }"
        )
        self.btn_ai_vision_map.setToolTip("PDF 페이지 이미지를 AI 비전으로 분석해 실제 빈 기입칸·체크박스 좌표를 자동 매핑합니다.")
        self.btn_ai_vision_map.clicked.connect(self._on_ai_vision_mapping)

        self.btn_paste_ai_json = QPushButton("AI 응답 JSON 붙여넣기 반영")
        self.btn_paste_ai_json.setStyleSheet(
            "QPushButton { background: #0EA5E9; color: white; font-weight: bold; padding: 6px 12px; border-radius: 6px; }"
        )
        self.btn_paste_ai_json.setToolTip("수동 복붙 모드에서 웹 AI가 출력한 JSON 좌표 배열을 붙여넣어 캔버스에 반영합니다.")
        self.btn_paste_ai_json.clicked.connect(self._on_paste_ai_json)

        self.btn_delete_box = QPushButton("선택 삭제")
        self.btn_delete_box.clicked.connect(self._on_delete_selected)

        self.btn_clear_boxes = QPushButton("전체 초기화")
        self.btn_clear_boxes.clicked.connect(self._on_clear_all)

        self.btn_toggle_fullscreen = QPushButton("전체화면")
        self.btn_toggle_fullscreen.setCheckable(True)
        self.btn_toggle_fullscreen.setToolTip("창을 최대화(전체화면)하거나 원래 크기로 되돌립니다.")
        self.btn_toggle_fullscreen.toggled.connect(self._on_toggle_fullscreen)

        top_bar.addWidget(self.btn_prev_page)
        top_bar.addWidget(self.lbl_page_info)
        top_bar.addWidget(self.btn_next_page)
        top_bar.addSpacing(8)
        top_bar.addWidget(QLabel("화면 크기:"))
        top_bar.addWidget(self.combo_zoom)
        top_bar.addSpacing(10)
        top_bar.addWidget(self.btn_auto_suggest)
        top_bar.addWidget(self.btn_ai_vision_map)
        top_bar.addWidget(self.btn_paste_ai_json)
        top_bar.addWidget(self.btn_delete_box)
        top_bar.addWidget(self.btn_clear_boxes)
        top_bar.addStretch()
        top_bar.addWidget(self.btn_toggle_fullscreen)
        root.addLayout(top_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QFrame()
        left_lay = QVBoxLayout(left_panel)
        left_lay.setContentsMargins(6, 6, 6, 6)
        left_lay.setSpacing(6)
        pal_header = QHBoxLayout()
        lbl_pal = QLabel("항목 팔레트 (선택 후 PDF 클릭)")
        lbl_pal.setStyleSheet("font-weight: bold; color: #1E293B;")
        self.btn_add_schema_field = QPushButton("+ 항목 추가")
        self.btn_add_schema_field.setStyleSheet(
            "QPushButton { background: #10B981; color: white; font-weight: bold; padding: 4px 10px; border-radius: 5px; }"
        )
        self.btn_add_schema_field.setToolTip("모달 안에서 새 회신 항목(reply_schema)을 즉시 추가합니다.")
        self.btn_add_schema_field.clicked.connect(self._on_add_schema_field)
        self.btn_remove_schema_field = QPushButton("- 삭제")
        self.btn_remove_schema_field.setToolTip("선택한 사용자 회신 항목을 삭제합니다.")
        self.btn_remove_schema_field.clicked.connect(self._on_remove_schema_field)
        pal_header.addWidget(lbl_pal, stretch=1)
        pal_header.addWidget(self.btn_add_schema_field)
        pal_header.addWidget(self.btn_remove_schema_field)
        left_lay.addLayout(pal_header)

        self.list_palette = QListWidget()
        self.list_palette.itemClicked.connect(self._on_palette_item_clicked)
        self.list_palette.itemDoubleClicked.connect(self._on_palette_item_activated)
        left_lay.addWidget(self.list_palette, stretch=1)

        self.btn_add_selected_item = QPushButton("선택 항목 현재 페이지에 배치")
        self.btn_add_selected_item.clicked.connect(self._on_add_selected_palette_item)
        left_lay.addWidget(self.btn_add_selected_item)

        self.canvas = _PdfMappingCanvas()
        self.canvas.set_palette_index_map(self._palette_num_by_pid)
        self.canvas.map_changed.connect(self._on_canvas_map_changed)
        self.canvas.empty_area_clicked.connect(self._on_canvas_empty_clicked)
        self.canvas.set_field_map(self._field_map)
        self._rebuild_palette_from_schema()

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area.setWidget(self.canvas)

        splitter.addWidget(left_panel)
        splitter.addWidget(self.scroll_area)
        splitter.setSizes([310, 860])
        root.addWidget(splitter, stretch=1)

        bottom = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #475569; font-size: 12px;")
        self.btn_save = QPushButton("저장 및 닫기")
        self.btn_save.setStyleSheet(
            "QPushButton { background: #059669; color: white; font-weight: bold; padding: 7px 18px; border-radius: 6px; }"
        )
        self.btn_save.clicked.connect(self.accept)
        self.btn_cancel = QPushButton("취소")
        self.btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(self.lbl_status, stretch=1)
        bottom.addWidget(self.btn_cancel)
        bottom.addWidget(self.btn_save)
        root.addLayout(bottom)

    def _on_toggle_fullscreen(self, checked: bool) -> None:
        if checked:
            self.showMaximized()
            self.btn_toggle_fullscreen.setText("원래 크기")
        else:
            self.showNormal()
            self.btn_toggle_fullscreen.setText("전체화면")
        if hasattr(self, "combo_zoom") and self.combo_zoom.currentData() == "fit":
            QTimer.singleShot(0, self._render_current_page)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "combo_zoom") and self.combo_zoom.currentData() == "fit":
            self._render_current_page()

    def _compute_render_scale(self, page: Any) -> float:
        zoom_val = self.combo_zoom.currentData()
        if zoom_val == "fit":
            vp = self.scroll_area.viewport().size()
            vw = max(320, vp.width() - 28)
            vh = max(320, vp.height() - 28)
            pw = max(1.0, float(page.get_width() or 595.0))
            ph = max(1.0, float(page.get_height() or 842.0))
            return max(0.45, min(2.2, min(vw / pw, vh / ph)))
        return float(zoom_val or 1.0) * 1.35

    def _go_page(self, page_num: int) -> None:
        clamped = max(1, min(self._page_count, int(page_num)))
        if clamped == self._current_page:
            return
        self._current_page = clamped
        self._render_current_page()
        self._refresh_status()

    def _render_current_page(self) -> None:
        page = self._doc[self._current_page - 1]
        scale = self._compute_render_scale(page)
        pil_img = page.render(scale=scale).to_pil().convert("RGBA")
        raw = pil_img.tobytes("raw", "RGBA")
        qimg = QImage(raw, pil_img.width, pil_img.height, QImage.Format.Format_RGBA8888)
        pixmap = QPixmap.fromImage(qimg)
        self.canvas.set_page_pixmap(pixmap, self._current_page)
        self.lbl_page_info.setText(f"{self._current_page} / {self._page_count} 페이지")
        self.btn_prev_page.setEnabled(self._current_page > 1)
        self.btn_next_page.setEnabled(self._current_page < self._page_count)

    def _rebuild_palette_from_schema(self, select_key: str = "") -> None:
        self._palette_entries = build_palette_entries(self._reply_schema)
        self._palette_num_by_pid = {
            str(e["palette_id"]): int(e["num"]) for e in self._palette_entries
        }
        for box in self._field_map:
            pid = str(box.get("palette_id") or "")
            if pid in self._palette_num_by_pid:
                box["num"] = self._palette_num_by_pid[pid]
        if hasattr(self, "canvas"):
            self.canvas.set_palette_index_map(self._palette_num_by_pid)
            self.canvas.update()

        self.list_palette.clear()
        select_row = -1
        for row_idx, entry in enumerate(self._palette_entries):
            it = QListWidgetItem(entry["display"])
            it.setData(Qt.ItemDataRole.UserRole, entry)
            self.list_palette.addItem(it)
            if select_key and entry.get("key") == select_key and select_row < 0:
                select_row = row_idx
        self._refresh_palette_items()
        if select_row >= 0:
            self.list_palette.setCurrentRow(select_row)
            self.list_palette.scrollToItem(self.list_palette.item(select_row))

    def add_schema_field(
        self,
        *,
        label: str,
        field_type: str = "text",
        options: Optional[List[str]] = None,
        required: bool = True,
    ) -> Dict[str, Any]:
        """모달 내에서 reply_schema 항목을 추가하고 팔레트를 즉시 갱신·선택합니다."""
        clean_label = str(label or "").strip()
        if not clean_label:
            raise ValueError("항목명을 입력해 주세요.")
        existing_keys = {str(item.get("key") or "").strip() for item in self._reply_schema}
        idx = len(self._reply_schema) + 1
        key = f"field_{idx}"
        while key in existing_keys:
            idx += 1
            key = f"field_{idx}"

        norm_type = str(field_type or "text").strip().lower()
        if norm_type not in ("text", "textarea", "radio", "checkbox"):
            norm_type = "text"
        clean_opts = [str(o).strip() for o in (options or []) if str(o).strip()]

        new_item: Dict[str, Any] = {
            "key": key,
            "label": clean_label,
            "type": norm_type,
            "options": clean_opts if norm_type in ("radio", "checkbox") else [],
            "required": bool(required),
        }
        self._reply_schema.append(new_item)
        self._rebuild_palette_from_schema(select_key=key)
        self._refresh_status()
        return new_item

    def _on_add_schema_field(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("새 회신 항목 추가")
        dlg.resize(400, 220)
        lay = QVBoxLayout(dlg)
        form = QFormLayout()

        edit_label = QLineEdit(dlg)
        edit_label.setPlaceholderText("예: 참가 동의 여부, 알레르기 유무, 비상연락처")

        combo_type = QComboBox(dlg)
        combo_type.addItem("단답형 입력", "text")
        combo_type.addItem("서술형 입력", "textarea")
        combo_type.addItem("객관식 (단일 선택)", "radio")
        combo_type.addItem("체크박스 (다중 선택)", "checkbox")

        edit_options = QLineEdit(dlg)
        edit_options.setPlaceholderText("예: 동의, 미동의 (객관식/체크박스일 때 쉼표 구분)")
        chk_required = QCheckBox("필수 입력", dlg)
        chk_required.setChecked(True)

        form.addRow("항목명:", edit_label)
        form.addRow("유형:", combo_type)
        form.addRow("선택지:", edit_options)
        form.addRow("", chk_required)
        lay.addLayout(form)

        btn_row = QHBoxLayout()
        btn_ok = QPushButton("추가", dlg)
        btn_cancel = QPushButton("취소", dlg)
        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        lay.addLayout(btn_row)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        label = edit_label.text().strip()
        if not label:
            QMessageBox.warning(self, "입력 확인", "항목명을 입력해 주세요.")
            return
        ftype = str(combo_type.currentData() or "text")
        opts = [o.strip() for o in re.split(r"[,/|]", edit_options.text()) if o.strip()]
        if ftype in ("radio", "checkbox") and not opts:
            QMessageBox.warning(self, "선택지 확인", "객관식/체크박스는 선택지를 쉼표로 구분해 입력해 주세요.")
            return

        self.add_schema_field(
            label=label,
            field_type=ftype,
            options=opts,
            required=chk_required.isChecked(),
        )

    def _on_remove_schema_field(self) -> None:
        cur = self.list_palette.currentItem()
        if cur is None:
            return
        entry = cur.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return
        if str(entry.get("palette_id") or "").startswith("sys:"):
            QMessageBox.information(self, "삭제 불가", "시스템 기본 필드는 삭제할 수 없습니다.")
            return
        key = str(entry.get("key") or "")
        self._reply_schema[:] = [item for item in self._reply_schema if str(item.get("key") or "") != key]
        self._field_map[:] = [box for box in self._field_map if str(box.get("key") or "") != key]
        self.canvas.select_id("")
        self._rebuild_palette_from_schema()
        self._refresh_status()

    def _placed_palette_ids(self) -> set[str]:
        return {str(x.get("palette_id") or "") for x in self._field_map if x.get("palette_id")}

    def _refresh_palette_items(self) -> None:
        placed = self._placed_palette_ids()
        for row in range(self.list_palette.count()):
            it = self.list_palette.item(row)
            entry = it.data(Qt.ItemDataRole.UserRole)
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("palette_id") or "")
            num = int(entry.get("num") or (row + 1))
            base = str(entry.get("base_display") or "")
            if pid in placed:
                it.setText(f"[{num}] ✓ {base}")
                it.setForeground(QColor("#059669"))
            else:
                it.setText(f"[{num}] {base}")
                it.setForeground(QColor("#0F172A"))

    def _select_next_unplaced_palette_item(self, start_row: int = 0) -> None:
        placed = self._placed_palette_ids()
        total = self.list_palette.count()
        if total == 0:
            return
        for offset in range(total):
            row = (start_row + offset) % total
            it = self.list_palette.item(row)
            entry = it.data(Qt.ItemDataRole.UserRole)
            if isinstance(entry, dict) and str(entry.get("palette_id") or "") not in placed:
                self.list_palette.setCurrentRow(row)
                return

    def _on_canvas_map_changed(self) -> None:
        self._refresh_palette_items()
        self._refresh_status()

    def _add_box_from_entry(
        self,
        entry: Dict[str, Any],
        *,
        page: Optional[int] = None,
        x_ratio: Optional[float] = None,
        y_ratio: Optional[float] = None,
        center_on_click: bool = False,
    ) -> Dict[str, Any]:
        w_r = float(entry.get("w_ratio", 0.12))
        h_r = float(entry.get("h_ratio", 0.032))
        if x_ratio is None or y_ratio is None:
            x_ratio = 0.18
            y_ratio = 0.22
        elif center_on_click:
            x_ratio = float(x_ratio) - (w_r / 2.0)
            y_ratio = float(y_ratio) - (h_r / 2.0)

        x_r = max(0.0, min(1.0 - w_r, float(x_ratio)))
        y_r = max(0.0, min(1.0 - h_r, float(y_ratio)))

        box = {
            "id": uuid.uuid4().hex[:10],
            "num": int(entry.get("num") or 0),
            "palette_id": entry.get("palette_id", ""),
            "key": entry["key"],
            "label": entry["label"],
            "kind": entry["kind"],
            "option": entry.get("option", ""),
            "page": int(page or self._current_page),
            "x_ratio": round(x_r, 4),
            "y_ratio": round(y_r, 4),
            "w_ratio": round(w_r, 4),
            "h_ratio": round(h_r, 4),
        }
        self._field_map.append(box)
        self.canvas.select_id(box["id"])
        self._refresh_palette_items()
        self._refresh_status()
        return box

    def _on_palette_item_clicked(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return
        pid = str(entry.get("palette_id") or "")
        existing = next((b for b in self._field_map if str(b.get("palette_id") or "") == pid), None)
        if existing is not None:
            pg = int(existing.get("page", 1))
            if pg != self._current_page:
                self._go_page(pg)
            self.canvas.select_id(str(existing.get("id") or ""))
            center = self.canvas.item_center_px(existing)
            self.scroll_area.ensureVisible(center.x(), center.y(), 90, 90)

    def _on_palette_item_activated(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(entry, dict):
            self._add_box_from_entry(entry)
            self._select_next_unplaced_palette_item(self.list_palette.currentRow() + 1)

    def _on_add_selected_palette_item(self) -> None:
        cur = self.list_palette.currentItem()
        if cur is None:
            QMessageBox.information(self, "항목 선택", "좌측 팔레트에서 배치할 항목을 먼저 선택하세요.")
            return
        self._on_palette_item_activated(cur)

    def _on_canvas_empty_clicked(self, xr: float, yr: float) -> None:
        cur = self.list_palette.currentItem()
        if cur is None:
            return
        entry = cur.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return
        pid = str(entry.get("palette_id") or "")
        if pid in self._placed_palette_ids():
            return
        self._add_box_from_entry(entry, x_ratio=xr, y_ratio=yr, center_on_click=True)
        self._select_next_unplaced_palette_item(self.list_palette.currentRow() + 1)

    def _on_delete_selected(self) -> None:
        if not self.canvas.delete_selected():
            QMessageBox.information(self, "삭제 안내", "캔버스에서 삭제할 박스를 먼저 클릭하세요.")

    def _on_clear_all(self) -> None:
        if not self._field_map:
            return
        ans = QMessageBox.question(self, "초기화 확인", "배치된 모든 좌표를 삭제하시겠습니까?")
        if ans == QMessageBox.StandardButton.Yes:
            self._field_map.clear()
            self.canvas.select_id("")
            self._refresh_palette_items()
            self._select_next_unplaced_palette_item(0)
            self._refresh_status()

    def _on_auto_suggest_positions(self) -> None:
        """PDF 각 페이지에서 질문·선택지 주변 좌표를 탐색해 미배치 항목을 자동 배치합니다."""
        existing_pids = self._placed_palette_ids()
        placed_count = 0

        for entry in self._palette_entries:
            pid = str(entry.get("palette_id") or "")
            if pid in existing_pids:
                continue

            pos = self._suggest_position_for_entry(entry)
            if pos is None:
                continue

            pg_num, xr, yr = pos
            self._add_box_from_entry(entry, page=pg_num, x_ratio=xr, y_ratio=yr)
            existing_pids.add(pid)
            placed_count += 1

        self._refresh_palette_items()
        self._select_next_unplaced_palette_item(0)
        self._refresh_status()
        QMessageBox.information(
            self,
            "자동 위치 추천 완료",
            f"PDF 텍스트 검색으로 {placed_count}개 항목을 자동 배치했습니다.\n"
            "번호 배지를 확인하고 누락된 항목은 좌측 선택 후 캔버스를 클릭해 배치해 주세요.",
        )

    def _suggest_position_for_entry(
        self, entry: Dict[str, Any]
    ) -> Optional[Tuple[int, float, float]]:
        kind = str(entry.get("kind") or "text")
        key = str(entry.get("key") or "")
        w_r = float(entry.get("w_ratio", 0.12))

        if kind == "option":
            opt = str(entry.get("option") or "").strip()
            q_kws = self._question_keywords_for_label(str(entry.get("label") or ""))
            q_match = self._find_first_keyword_in_pdf(q_kws)
            if q_match is not None:
                pg_num, q_l, q_b, q_r, q_t, pw, ph = q_match
                opt_match = self._find_nearest_keyword_on_page(
                    pg_num - 1,
                    [opt, f"□ {opt}", f"☐ {opt}"],
                    anchor_rect=(q_l, q_b, q_r, q_t),
                )
                if opt_match is not None:
                    left, _b, _r, top, pw, ph = opt_match
                    xr = max(0.0, (left / pw) - 0.022)
                    yr = max(0.0, (ph - top) / ph - 0.004)
                    return (pg_num, xr, yr)

            fallback = self._find_first_keyword_in_pdf([opt])
            if fallback is None:
                return None
            pg_num, left, _b, _r, top, pw, ph = fallback
            return (pg_num, max(0.0, (left / pw) - 0.022), max(0.0, (ph - top) / ph - 0.004))

        kws = self._candidate_keywords_for_entry(entry)
        match = self._find_first_keyword_in_pdf(kws)
        if match is None:
            return None
        pg_num, left, _b, right, top, pw, ph = match
        yr = max(0.0, (ph - top) / ph - 0.004)
        # 학년/반/번호는 라벨 앞 빈칸('___학년 ___반 ___번') 우선, 부족하면 오른쪽 배치
        if key in ("grade", "class_no", "number"):
            left_candidate = (left / pw) - w_r - 0.006
            xr = left_candidate if left_candidate >= 0.03 else min(0.88, (right / pw) + 0.012)
        else:
            xr = min(0.85, (right / pw) + 0.014)
        return (pg_num, xr, yr)

    @staticmethod
    def _question_keywords_for_label(label: str) -> List[str]:
        clean = re.sub(r"\([^)]*\)", "", str(label or "")).strip()
        clean = re.sub(r"^\d+[.)]\s*", "", clean).strip()
        kws: List[str] = []
        if clean:
            kws.append(clean)
        if len(clean) >= 8:
            kws.append(clean[:10])
        if len(clean) >= 5:
            kws.append(clean[:6])
        return [k for k in kws if k]

    @staticmethod
    def _candidate_keywords_for_entry(entry: Dict[str, Any]) -> List[str]:
        key = str(entry.get("key") or "")
        label = str(entry.get("label") or "").strip()
        if key == "student_signature":
            return ["학생 성명", "학생 서명", "학생명", "학생"]
        if key == "parent_signature":
            return ["학부모 성명", "보호자 성명", "학부모 서명", "보호자 서명", "보호자", "학부모"]
        if key == "name":
            return ["성명", "이름"]
        short = re.sub(r"\([^)]*\)", "", label).strip()
        kws = [label]
        if short and short != label:
            kws.append(short)
        if len(short) >= 4:
            kws.append(short[:8])
        return [k for k in kws if k]

    def _find_nearest_keyword_on_page(
        self,
        pg_idx: int,
        keywords: List[str],
        *,
        anchor_rect: Tuple[float, float, float, float],
    ) -> Optional[Tuple[float, float, float, float, float, float]]:
        page = self._doc[pg_idx]
        pw = float(page.get_width() or 595.0)
        ph = float(page.get_height() or 842.0)
        tp = page.get_textpage()
        q_l, q_b, q_r, q_t = anchor_rect
        q_cy = (q_b + q_t) / 2.0

        best: Optional[Tuple[float, Tuple[float, float, float, float, float, float]]] = None
        for kw in keywords:
            if not kw:
                continue
            try:
                searcher = tp.search(kw, match_case=False, match_whole_word=False)
                while True:
                    found = searcher.get_next()
                    if not found:
                        break
                    cnt = tp.count_rects(found[0], found[1])
                    if cnt <= 0:
                        continue
                    left, bottom, right, top = tp.get_rect(0)
                    cy = (bottom + top) / 2.0
                    # PDF Y좌표는 위가 크므로 아래쪽 행은 q_cy - cy >= 0
                    dy_down = q_cy - cy
                    if dy_down < -0.025 * ph or dy_down > 0.16 * ph:
                        continue
                    dx = max(0.0, left - q_r) if left >= q_r else abs(left - q_l) * 0.5
                    score = abs(dy_down) * 2.5 + dx * 0.4
                    cand = (left, bottom, right, top, pw, ph)
                    if best is None or score < best[0]:
                        best = (score, cand)
            except Exception:
                continue
        return best[1] if best is not None else None

    def _find_first_keyword_in_pdf(
        self, keywords: List[str]
    ) -> Optional[Tuple[int, float, float, float, float, float, float]]:
        for pg_idx in range(self._page_count):
            page = self._doc[pg_idx]
            pw = float(page.get_width() or 595.0)
            ph = float(page.get_height() or 842.0)
            tp = page.get_textpage()
            for kw in keywords:
                if not kw:
                    continue
                try:
                    searcher = tp.search(kw, match_case=False, match_whole_word=False)
                    found = searcher.get_next()
                except Exception:
                    found = None
                if not found:
                    continue
                cnt = tp.count_rects(found[0], found[1])
                if cnt > 0:
                    left, bottom, right, top = tp.get_rect(0)
                    return (pg_idx + 1, left, bottom, right, top, pw, ph)
        return None

    def _refresh_status(self) -> None:
        total = len(self._palette_entries)
        placed = len(self._placed_palette_ids())
        self.lbl_status.setText(
            f"배치 현황: {placed}/{total}개 항목 | 좌측 선택 후 PDF 클릭: 즉시 배치 · 드래그: 이동/크기조절 · 우클릭/Delete: 삭제"
        )

    def _apply_vision_mapped_boxes(self, boxes: List[Dict[str, Any]]) -> int:
        if not boxes:
            return 0
        incoming_pids = {str(b.get("palette_id") or "") for b in boxes if b.get("palette_id")}
        self._field_map[:] = [
            existing for existing in self._field_map
            if str(existing.get("palette_id") or "") not in incoming_pids
        ]
        self._field_map.extend(boxes)
        self.canvas.set_field_map(self._field_map)
        self.canvas.select_id(str(boxes[-1].get("id") or ""))
        self._refresh_palette_items()
        self._select_next_unplaced_palette_item(0)
        self._refresh_status()
        return len(boxes)

    def _prepare_vision_page_images(self) -> List[Path]:
        if self._vision_temp_dir is None or not self._vision_temp_dir.exists():
            self._vision_temp_dir = Path(tempfile.mkdtemp(prefix="eduflow_pdf_vision_"))
        return render_pdf_pages_to_png_files(
            self._resolved_pdf_path,
            self._vision_temp_dir,
            scale=1.8,
        )

    def _on_ai_vision_mapping(self) -> None:
        try:
            image_paths = self._prepare_vision_page_images()
        except Exception as exc:
            QMessageBox.warning(self, "페이지 이미지 변환 오류", str(exc))
            return

        client = MultiLlmClient()
        if self._ai_dock is not None and hasattr(self._ai_dock, "current_mode_index"):
            mode_idx = int(self._ai_dock.current_mode_index())
        else:
            mode_idx = 1 if client.is_manual_mode_active() else 0

        if mode_idx == 2 and self._ai_dock is not None and hasattr(self._ai_dock, "run_playwright_single_prompt"):
            prompt_text = build_pdf_vision_mapping_prompt(
                self._palette_entries,
                page_count=self._page_count,
            )
            win = self.parent().window() if self.parent() is not None else None
            if win is not None and hasattr(win, "toggle_ai_dock") and not getattr(win, "_ai_dock_expanded", True):
                win.toggle_ai_dock(True)

            self.btn_ai_vision_map.setEnabled(False)
            self.btn_ai_vision_map.setText("Playwright 실행 중...")
            self.lbl_status.setText(
                "Playwright 자동 매핑 실행 중 — 우측 AI 실행 패널에서 로그인/진행 상태를 확인하세요."
            )

            def _on_pw_done(payload: Dict[str, Any]) -> None:
                self.btn_ai_vision_map.setEnabled(True)
                self.btn_ai_vision_map.setText("AI 비전 자동 매핑")
                raw_resp = (
                    payload.get("raw_response")
                    or payload.get("formatted_text")
                    or payload.get("parsed_json")
                    or ""
                )
                if isinstance(raw_resp, (dict, list)):
                    raw_resp = json.dumps(raw_resp, ensure_ascii=False)
                try:
                    cnt = self.apply_vision_ai_response(str(raw_resp))
                except Exception as exc:
                    self._refresh_status()
                    QMessageBox.warning(self, "Playwright 응답 파싱 오류", str(exc))
                    return
                QMessageBox.information(
                    self,
                    "Playwright AI 비전 매핑 완료",
                    f"Playwright 자동 응답에서 {cnt}개 항목 좌표를 캔버스에 반영했습니다.",
                )

            def _on_pw_err(err: Any) -> None:
                self.btn_ai_vision_map.setEnabled(True)
                self.btn_ai_vision_map.setText("AI 비전 자동 매핑")
                self._refresh_status()
                QMessageBox.warning(self, "Playwright 매핑 오류", str(err or "").splitlines()[0])

            started = self._ai_dock.run_playwright_single_prompt(
                prompt_text=prompt_text,
                attachment_paths=[str(p) for p in image_paths],
                task_label="PDF 양식 AI 비전 매핑",
                on_result=_on_pw_done,
                on_error=_on_pw_err,
            )
            if not started:
                self.btn_ai_vision_map.setEnabled(True)
                self.btn_ai_vision_map.setText("AI 비전 자동 매핑")
                self._refresh_status()
            return

        if mode_idx != 0 or client.is_manual_mode_active():
            prompt_text = build_pdf_vision_mapping_prompt(
                self._palette_entries,
                page_count=self._page_count,
            )
            QApplication.clipboard().setText(prompt_text)

            is_offscreen = os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen"
            if not is_offscreen:
                if image_paths and sys.platform == "win32":
                    try:
                        subprocess.Popen(["explorer", "/select,", str(image_paths[0].resolve())])
                    except Exception:
                        pass
                web_url = "https://gemini.google.com/app"
                if self._ai_dock is not None and hasattr(self._ai_dock, "_resolve_target_web_url"):
                    try:
                        provider_key = str(
                            self._ai_dock.combo_web_provider.currentData()
                            or getattr(self._ai_dock, "_manual_provider", "gemini")
                            or "gemini"
                        )
                        custom_edit = getattr(self._ai_dock, "edit_chat_custom_url", None)
                        resolved = self._ai_dock._resolve_target_web_url(provider_key, custom_edit)
                        if resolved:
                            web_url = resolved
                    except Exception:
                        pass
                try:
                    webbrowser.open(web_url)
                except Exception:
                    pass

            self.lbl_status.setText(
                "수동 복붙 대기: 프롬프트가 클립보드에 복사되고 PNG 폴더와 웹 AI 창을 열었습니다. 응답 복사 후 [AI 응답 JSON 붙여넣기 반영]을 누르세요."
            )
            QMessageBox.information(
                self,
                "AI 비전 수동 복붙 안내",
                f"1) 프롬프트가 클립보드에 복사되었습니다.\n"
                f"2) 열린 탐색기의 PNG 이미지({len(image_paths)}장)를 웹 AI 창에 드래그해 첨부하고 Ctrl+V로 질문하세요.\n"
                f"3) AI가 출력한 JSON 배열을 복사한 뒤 상단 [AI 응답 JSON 붙여넣기 반영] 버튼을 눌러 주세요.",
            )
            return

        # API 모드 비동기 실행
        self.btn_ai_vision_map.setEnabled(False)
        self.btn_ai_vision_map.setText("AI 비전 분석 중...")
        self.lbl_status.setText("AI 비전 모델이 PDF 페이지의 기입칸·체크박스 위치를 분석 중입니다...")
        entries_snap = copy.deepcopy(self._palette_entries)

        def _work(_worker: Any = None) -> List[Dict[str, Any]]:
            return request_pdf_vision_mapping_api(
                image_paths,
                entries_snap,
                llm_client=client,
            )

        def _on_done(boxes: Any) -> None:
            self.btn_ai_vision_map.setEnabled(True)
            self.btn_ai_vision_map.setText("AI 비전 자동 매핑")
            cnt = self._apply_vision_mapped_boxes(list(boxes or []))
            QMessageBox.information(
                self,
                "AI 비전 자동 매핑 완료",
                f"AI 비전 분석으로 {cnt}개 항목 좌표를 캔버스에 반영했습니다.\n"
                "필요 시 드래그로 미세 조정한 뒤 [저장 및 닫기]를 눌러 주세요.",
            )

        def _on_err(err: Any) -> None:
            self.btn_ai_vision_map.setEnabled(True)
            self.btn_ai_vision_map.setText("AI 비전 자동 매핑")
            self._refresh_status()
            QMessageBox.warning(self, "AI 비전 매핑 오류", str(err or "").splitlines()[0])

        if self._task_runner is not None:
            worker = FunctionWorker("PDF 양식 AI 비전 자동 매핑", _work)
            self._task_runner.start_worker(
                worker,
                task_key="notice_pdf_vision_mapping",
                on_result=_on_done,
                on_error=_on_err,
            )
        else:
            try:
                _on_done(_work())
            except Exception as exc:
                _on_err(exc)

    def apply_vision_ai_response(self, raw_response: str) -> int:
        """AI 비전 응답 문자열(JSON)을 파싱해 캔버스에 즉시 반영하고 반영 건수를 반환합니다."""
        boxes = parse_pdf_vision_mapping_response(
            raw_response,
            self._palette_entries,
            page_count=self._page_count,
            default_page=self._current_page,
        )
        return self._apply_vision_mapped_boxes(boxes)

    def _on_paste_ai_json(self) -> None:
        clip = (QApplication.clipboard().text() or "").strip()
        raw_text, ok = QInputDialog.getMultiLineText(
            self,
            "AI 응답 JSON 붙여넣기",
            "웹 AI가 출력한 좌표 JSON 배열([ ... ])을 붙여넣어 주세요:",
            clip if ("x_ratio" in clip or clip.startswith("[") or clip.startswith("```")) else "",
        )
        if not ok or not raw_text.strip():
            return
        try:
            boxes = parse_pdf_vision_mapping_response(
                raw_text,
                self._palette_entries,
                page_count=self._page_count,
                default_page=self._current_page,
            )
        except Exception as exc:
            QMessageBox.warning(self, "JSON 파싱 오류", f"좌표 JSON을 해석하지 못했습니다:\n{exc}")
            return

        if not boxes:
            QMessageBox.information(self, "반영할 좌표 없음", "현재 팔레트 번호와 일치하는 유효한 좌표가 없습니다.")
            return
        cnt = self._apply_vision_mapped_boxes(boxes)
        QMessageBox.information(
            self,
            "좌표 반영 완료",
            f"AI 응답에서 {cnt}개 항목 좌표를 캔버스에 반영했습니다.",
        )

    def get_field_map(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._field_map)

    def get_reply_schema(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._reply_schema)

    def closeEvent(self, event: Any) -> None:
        try:
            self._doc.close()
        except Exception:
            pass
        if self._temp_conv_dir is not None:
            shutil.rmtree(self._temp_conv_dir, ignore_errors=True)
            self._temp_conv_dir = None
        if self._vision_temp_dir is not None:
            shutil.rmtree(self._vision_temp_dir, ignore_errors=True)
            self._vision_temp_dir = None
        super().closeEvent(event)
