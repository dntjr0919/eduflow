"""
정적 카드 컴포넌트 (ui/components/hover_zoom_card.py).
- QFrame을 상속받아 섹션 타이틀 헤더(옵션)와 본문 레이아웃을 제공하며 기존 QGroupBox를 대체합니다.
- [개편] 마우스 호버 시 화면이 의도치 않게 커져 시야/작업 흐름을 방해하던 문제로
  호버 감지(enterEvent/leaveEvent), 지연 타이머, 지오메트리 확대 애니메이션을 전면 폐지했습니다.
  이제 이 카드는 마우스를 올려도 확대/팝업되지 않는 순수 정적 카드입니다.
- 기존 탭 코드와의 호환을 위해 클래스명, 생성자 인자(zoom_ratio, hover_delay_ms),
  set_zoom_enabled() 등의 API는 유지하되 아무 동작도 하지 않습니다.
"""

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class HoverZoomCard(QFrame):
    """타이틀 헤더 + 본문 레이아웃을 제공하는 정적 카드 위젯 (호버 확대 기능 없음)."""

    def __init__(
        self,
        title: str = "",
        parent: Optional[QWidget] = None,
        zoom_ratio: float = 1.0,  # 하위 호환용 (미사용)
        hover_delay_ms: int = 0,  # 하위 호환용 (미사용)
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("role", "hoverZoomCard")
        self.is_zoomed: bool = False

        # 내부 기본 레이아웃 및 선택적 타이틀 헤더
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(18, 16, 18, 16)
        self._root_layout.setSpacing(12)

        self.header_layout = QHBoxLayout()
        self.header_layout.setContentsMargins(0, 0, 0, 4)
        self.header_layout.setSpacing(8)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("HoverCardTitle")
        self.title_label.setVisible(bool(title))

        self.header_layout.addWidget(self.title_label)
        self.header_layout.addStretch()
        self._root_layout.addLayout(self.header_layout)

        # 입체감 표현을 위한 고정 Drop Shadow 효과
        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(14)
        self.shadow.setColor(QColor(15, 23, 42, 38))
        self.shadow.setOffset(0, 4)
        self.setGraphicsEffect(self.shadow)

    def setTitle(self, title: str) -> None:
        self.title_label.setText(title)
        self.title_label.setVisible(bool(title))

    def title(self) -> str:
        return self.title_label.text()

    def body_layout(self) -> QVBoxLayout:
        """카드 본문 위젯/레이아웃을 추가할 수 있는 루트 QVBoxLayout을 반환합니다."""
        return self._root_layout

    def add_header_widget(self, widget: QWidget) -> None:
        """카드 우측 상단(타이틀 옆)에 포커스 버튼 등 보조 위젯을 배치합니다."""
        self.header_layout.addWidget(widget)

    def set_zoom_enabled(self, enabled: bool) -> None:
        """하위 호환용 no-op (호버 확대 기능은 폐지되었습니다)."""
        return
