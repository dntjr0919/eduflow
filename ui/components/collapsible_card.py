"""
클릭형 아코디언(접이식) 카드 컴포넌트 (ui/components/collapsible_card.py).
- 마우스 호버로 크기가 변하는 동작은 일체 없으며, 헤더를 "클릭"했을 때만 내용 영역이 펼쳐지거나 접힙니다.
- 콘텐츠 영역의 maximumHeight를 QPropertyAnimation(220ms, InOutQuad)으로 0 <-> 실제 필요 높이까지 슬라이딩합니다.
- 펼침 애니메이션이 끝나면 maximumHeight 제한을 해제하여, 창 크기 변경/텍스트 줄바꿈 등으로
  내용 높이가 늘어나도 입력칸이 잘리거나 찌그러지지 않도록 합니다.
- AccordionGroup으로 여러 카드를 묶으면 단일 모드(한 번에 하나만 열림) / 다중 모드를 제어할 수 있습니다.
"""

from typing import List, Optional

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

QWIDGETSIZE_MAX = 16777215

ACCORDION_STYLE = """
QFrame#AccordionHeader {
    background-color: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 8px;
}
QFrame#AccordionHeader:hover {
    background-color: #F1F5F9;
    border-color: #CBD5E1;
}
QFrame#AccordionHeader[expanded="true"] {
    background-color: #EEF2F6;
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 0px;
}
QPushButton#AccordionToggle {
    text-align: left;
    font-size: 14px;
    font-weight: 600;
    color: #1D2939;
    background: transparent;
    border: none;
    padding: 12px 16px;
}
QFrame#AccordionContent {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-top: none;
    border-bottom-left-radius: 8px;
    border-bottom-right-radius: 8px;
}
"""


class AccordionCard(QWidget):
    """클릭 시 부드럽게 내용이 펼쳐지고 접히는 아코디언 카드 위젯."""

    toggled = pyqtSignal(bool)
    animation_finished = pyqtSignal(bool)

    def __init__(
        self,
        title: str,
        parent: Optional[QWidget] = None,
        is_expanded: bool = False,
        duration_ms: int = 220,
    ):
        super().__init__(parent)
        self._title_text = title
        self.setStyleSheet(ACCORDION_STYLE)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 8)
        main_layout.setSpacing(0)

        # 1. 헤더 바 (토글 버튼 + 우측 보조 위젯 영역)
        self.header = QFrame(self)
        self.header.setObjectName("AccordionHeader")
        self.header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.header.setAttribute(Qt.WidgetAttribute.WA_Hover, True)  # QSS :hover 배경색 전용
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(0, 0, 10, 0)
        self.header_layout.setSpacing(8)

        self.toggle_btn = QPushButton(self.header)
        self.toggle_btn.setObjectName("AccordionToggle")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(is_expanded)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle_btn.clicked.connect(self.toggle_state)
        self.header_layout.addWidget(self.toggle_btn, stretch=1)
        main_layout.addWidget(self.header)

        # 2. 내용 컨테이너 (애니메이션 대상)
        self.content_area = QFrame(self)
        self.content_area.setObjectName("AccordionContent")
        self.content_area.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.content_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(8, 8, 8, 12)
        self.content_layout.setSpacing(12)
        main_layout.addWidget(self.content_area)

        # 3. 슬라이딩 접기/펼치기 애니메이션
        self.anim = QPropertyAnimation(self.content_area, b"maximumHeight", self)
        self.anim.setDuration(duration_ms)
        self.anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.anim.finished.connect(self._on_animation_finished)

        # 초기 상태 반영 (애니메이션 없이 즉시)
        self._apply_static_state(is_expanded)
        self._update_header()

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    def title(self) -> str:
        return self._title_text

    def setTitle(self, title: str) -> None:
        self._title_text = title
        self._update_header()

    def is_expanded(self) -> bool:
        return self.toggle_btn.isChecked()

    def add_layout(self, layout: QLayout) -> None:
        self.content_layout.addLayout(layout)

    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        self.content_layout.addWidget(widget, stretch)

    def body_layout(self) -> QVBoxLayout:
        """카드 본문(콘텐츠 영역)의 QVBoxLayout을 반환합니다."""
        return self.content_layout

    def add_header_widget(self, widget: QWidget) -> None:
        """헤더 바 우측에 보조 버튼(예: 전체화면) 등을 배치합니다."""
        self.header_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_expanded(self, expanded: bool, animated: bool = True) -> None:
        if expanded == self.is_expanded() and not self.anim.state() == QPropertyAnimation.State.Running:
            return
        self.toggle_btn.setChecked(expanded)
        if animated:
            self.toggle_state()
        else:
            self.anim.stop()
            self._apply_static_state(expanded)
            self._update_header()
            self.toggled.emit(expanded)

    def expand(self, animated: bool = True) -> None:
        self.set_expanded(True, animated)

    def collapse(self, animated: bool = True) -> None:
        self.set_expanded(False, animated)

    # ------------------------------------------------------------------
    # 내부 동작
    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        # 헤더 빈 영역 클릭 시에도 토글 (보조 버튼 클릭은 해당 버튼이 먼저 소비)
        if event.button() == Qt.MouseButton.LeftButton and self.header.geometry().contains(
            event.position().toPoint()
        ):
            self.toggle_btn.toggle()
            self.toggle_state()
            event.accept()
            return
        super().mousePressEvent(event)

    def _update_header(self) -> None:
        expanded = self.toggle_btn.isChecked()
        arrow = "▼" if expanded else "▶"
        self.toggle_btn.setText(f"{arrow}  {self._title_text}")
        self.header.setProperty("expanded", "true" if expanded else "false")
        st = self.header.style()
        if st is not None:
            st.unpolish(self.header)
            st.polish(self.header)

    def _content_target_height(self) -> int:
        layout = self.content_area.layout()
        width = self.content_area.width() or self.width()
        if layout is not None and layout.hasHeightForWidth() and width > 0:
            return max(layout.totalHeightForWidth(width), self.content_area.sizeHint().height())
        return self.content_area.sizeHint().height()

    def _apply_static_state(self, expanded: bool) -> None:
        if expanded:
            self.content_area.setVisible(True)
            self.content_area.setMaximumHeight(QWIDGETSIZE_MAX)
        else:
            self.content_area.setMaximumHeight(0)
            self.content_area.setVisible(False)

    @pyqtSlot()
    def toggle_state(self) -> None:
        checked = self.toggle_btn.isChecked()
        self._update_header()

        current_height = self.content_area.height() if self.content_area.isVisible() else 0
        self.anim.stop()
        if checked:
            self.content_area.setMaximumHeight(current_height)
            self.content_area.setVisible(True)
            self.anim.setStartValue(current_height)
            self.anim.setEndValue(self._content_target_height())
        else:
            self.anim.setStartValue(current_height)
            self.anim.setEndValue(0)
        self.anim.start()
        self.toggled.emit(checked)

    def _on_animation_finished(self) -> None:
        expanded = self.toggle_btn.isChecked()
        # 펼침 완료 후 높이 제한 해제 / 접힘 완료 후 숨김
        self._apply_static_state(expanded)
        self.animation_finished.emit(expanded)


class AccordionGroup:
    """
    여러 AccordionCard를 묶어 단일 모드(exclusive=True: 한 번에 하나만 펼침)
    또는 다중 모드(exclusive=False)로 제어하는 헬퍼.
    """

    def __init__(self, exclusive: bool = True):
        self.exclusive = exclusive
        self._cards: List[AccordionCard] = []

    def add_card(self, card: AccordionCard) -> AccordionCard:
        self._cards.append(card)
        card.toggled.connect(lambda expanded, c=card: self._on_card_toggled(c, expanded))
        return card

    def cards(self) -> List[AccordionCard]:
        return list(self._cards)

    def set_exclusive(self, exclusive: bool) -> None:
        self.exclusive = exclusive
        if exclusive:
            opened = [c for c in self._cards if c.is_expanded()]
            for card in opened[1:]:
                card.collapse()

    def expand_all(self) -> None:
        if not self.exclusive:
            for card in self._cards:
                card.expand()

    def collapse_all(self) -> None:
        for card in self._cards:
            card.collapse()

    def _on_card_toggled(self, card: AccordionCard, expanded: bool) -> None:
        if not (self.exclusive and expanded):
            return
        for other in self._cards:
            if other is not card and other.is_expanded():
                other.collapse()
