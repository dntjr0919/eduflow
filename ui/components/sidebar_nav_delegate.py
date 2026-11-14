"""
좌측 사이드바 내비게이션(QListWidget#SidebarNavList) 전용 아이콘/텍스트 렌더러.
- PyQt6.QtSvg.QSvgRenderer를 사용해 로컬 Lucide 단색 선형 SVG 아이콘을 DPI 배율에 관계없이 선명하게 렌더링
- 접힌 상태: 중앙 정렬 20x20 아이콘만 표시 (텍스트 미표시)
- 펼친 상태: 고정 너비 아이콘 영역 + 12px 간격 + 단일 행 한국어 메뉴명 표시
- 상태 우선순위: Selected(Hover 포함) > Hover > Default, 키보드 포커스 링 별도 표시
"""

import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from utils.path_helper import get_resource_path


class SidebarNavDelegate(QStyledItemDelegate):
    """사이드바 메뉴 항목의 벡터 아이콘·텍스트·상태 스타일을 일관되게 그리는 델리게이트."""

    ICON_NAME_ROLE = Qt.ItemDataRole.UserRole + 1
    UNREAD_ROLE = Qt.ItemDataRole.UserRole + 2

    ICON_SIZE = 20
    ICON_BOX_WIDTH = 20
    ICON_TEXT_GAP = 10
    EXPANDED_LEFT_PAD = 12
    EXPANDED_RIGHT_PAD = 8
    ITEM_HEIGHT = 40
    CORNER_RADIUS = 8.0
    UNREAD_DOT_SIZE = 8

    COLOR_DEFAULT_FG = "#94A3B8"
    COLOR_HOVER_BG = "#1E293B"
    COLOR_HOVER_FG = "#E2E8F0"
    COLOR_SELECTED_BG = "#0B4DCB"
    COLOR_SELECTED_FG = "#FFFFFF"
    COLOR_FOCUS_RING = "#38BDF8"
    COLOR_UNREAD_DOT = "#EF4444"

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._collapsed = False
        self._svg_text_cache: Dict[str, str] = {}
        self._renderer_cache: Dict[Tuple[str, str], QSvgRenderer] = {}

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    @staticmethod
    def _resolve_icon_path(icon_name: str) -> Optional[Path]:
        candidates = [
            Path(get_resource_path(os.path.join("ui", "assets", "icons", f"{icon_name}.svg"))),
            Path(__file__).resolve().parent.parent / "assets" / "icons" / f"{icon_name}.svg",
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def _get_renderer(self, icon_name: str, color_hex: str) -> Optional[QSvgRenderer]:
        if not icon_name:
            return None
        cache_key = (icon_name, color_hex)
        cached = self._renderer_cache.get(cache_key)
        if cached is not None:
            return cached

        svg_template = self._svg_text_cache.get(icon_name)
        if svg_template is None:
            icon_path = self._resolve_icon_path(icon_name)
            if icon_path is None:
                return None
            svg_template = icon_path.read_text(encoding="utf-8")
            self._svg_text_cache[icon_name] = svg_template

        colored_svg = svg_template.replace("currentColor", color_hex)
        renderer = QSvgRenderer(QByteArray(colored_svg.encode("utf-8")), self)
        if not renderer.isValid():
            return None
        self._renderer_cache[cache_key] = renderer
        return renderer

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        base = super().sizeHint(option, index)
        return QSize(base.width(), self.ITEM_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(option.rect)
        # QListWidget::setSpacing(4) 사용 시 아이템 간 세로 간격은 이미 확보됨
        bg_rect = rect.adjusted(0.0, 0.0, 0.0, 0.0)

        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        has_focus = bool(option.state & QStyle.StateFlag.State_HasFocus)

        # 상태 우선순위: Selected(Hover 포함) > Hover > Default
        if is_selected:
            bg_color: Optional[QColor] = QColor(self.COLOR_SELECTED_BG)
            fg_hex = self.COLOR_SELECTED_FG
        elif is_hovered:
            bg_color = QColor(self.COLOR_HOVER_BG)
            fg_hex = self.COLOR_HOVER_FG
        else:
            bg_color = None
            fg_hex = self.COLOR_DEFAULT_FG

        if bg_color is not None:
            bg_path = QPainterPath()
            bg_path.addRoundedRect(bg_rect, self.CORNER_RADIUS, self.CORNER_RADIUS)
            painter.fillPath(bg_path, bg_color)

        # 키보드 포커스 링 (아이콘·활성 배경을 가리지 않도록 외곽선에 얇게 표시)
        if has_focus:
            focus_rect = bg_rect.adjusted(1.5, 1.5, -1.5, -1.5)
            focus_pen = QPen(QColor(self.COLOR_FOCUS_RING), 1.5)
            painter.setPen(focus_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                focus_rect,
                max(self.CORNER_RADIUS - 1.5, 2.0),
                max(self.CORNER_RADIUS - 1.5, 2.0),
            )

        icon_name = str(index.data(self.ICON_NAME_ROLE) or "")
        has_unread = bool(index.data(self.UNREAD_ROLE))
        renderer = self._get_renderer(icon_name, fg_hex)

        if self._collapsed:
            icon_x = bg_rect.x() + (bg_rect.width() - self.ICON_SIZE) / 2.0
            icon_y = bg_rect.y() + (bg_rect.height() - self.ICON_SIZE) / 2.0
            if renderer is not None:
                renderer.render(
                    painter,
                    QRectF(icon_x, icon_y, float(self.ICON_SIZE), float(self.ICON_SIZE)),
                )
            if has_unread:
                dot_rect = QRectF(
                    icon_x + self.ICON_SIZE - 4.0,
                    icon_y - 3.0,
                    float(self.UNREAD_DOT_SIZE),
                    float(self.UNREAD_DOT_SIZE),
                )
                painter.setPen(QPen(QColor("#0F172A"), 1.5))
                painter.setBrush(QColor(self.COLOR_UNREAD_DOT))
                painter.drawEllipse(dot_rect)
        else:
            icon_x = bg_rect.x() + self.EXPANDED_LEFT_PAD + (self.ICON_BOX_WIDTH - self.ICON_SIZE) / 2.0
            icon_y = bg_rect.y() + (bg_rect.height() - self.ICON_SIZE) / 2.0
            if renderer is not None:
                renderer.render(
                    painter,
                    QRectF(icon_x, icon_y, float(self.ICON_SIZE), float(self.ICON_SIZE)),
                )

            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            if text:
                text_left = (
                    bg_rect.x()
                    + self.EXPANDED_LEFT_PAD
                    + self.ICON_BOX_WIDTH
                    + self.ICON_TEXT_GAP
                )
                right_reserve = 18.0 if has_unread else 0.0
                text_right = bg_rect.right() - self.EXPANDED_RIGHT_PAD - right_reserve
                if text_right > text_left:
                    text_rect = QRectF(
                        text_left,
                        bg_rect.y(),
                        text_right - text_left,
                        bg_rect.height(),
                    )
                    font = QFont(option.font)
                    font.setPixelSize(13)
                    font.setWeight(
                        QFont.Weight.DemiBold if is_selected else QFont.Weight.Medium
                    )
                    painter.setFont(font)
                    painter.setPen(QColor(fg_hex))
                    painter.save()
                    painter.setClipRect(text_rect)
                    painter.drawText(
                        text_rect,
                        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine),
                        text,
                    )
                    painter.restore()

            if has_unread:
                dot_rect = QRectF(
                    bg_rect.right() - 16.0,
                    bg_rect.center().y() - (self.UNREAD_DOT_SIZE / 2.0),
                    float(self.UNREAD_DOT_SIZE),
                    float(self.UNREAD_DOT_SIZE),
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(self.COLOR_UNREAD_DOT))
                painter.drawEllipse(dot_rect)

        painter.restore()
