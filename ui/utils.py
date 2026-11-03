"""
UI 공통 유틸리티 및 커스텀 위젯 모듈 (ui/utils.py)
- NoScrollWheelFilter: 포커스가 없을 때 마우스 휠로 QComboBox/QSpinBox 값이 오변경되는 것을 방지하고
  부모 스크롤 영역으로 휠 이벤트를 전달하는 이벤트 필터
- SearchableModelComboBox: QSortFilterProxyModel + QCompleter 기반으로 중간 단어(부분 일치) 실시간 필터링이
  완벽히 동작하는 커스텀 콤보박스
- COMBOBOX_STYLE: 드롭다운 화살표 및 팝업 리스트 뷰 QSS 스타일
"""

from typing import List, Optional
from PyQt6.QtCore import (
    QEvent,
    QObject,
    QSortFilterProxyModel,
    QStringListModel,
    Qt,
)
from PyQt6.QtWidgets import QComboBox, QCompleter, QListView, QSpinBox, QWidget


COMBOBOX_STYLE = """
QComboBox {
    min-height: 34px;
    height: 34px;
    font-size: 13px;
    padding: 2px 30px 2px 10px; /* 우측 화살표 공간 확보 */
    border: 1px solid #D0D5DD;
    border-radius: 6px;
    background-color: #FFFFFF;
    color: #101828;
}
QComboBox:focus {
    border: 1.5px solid #4F46E5;
}
/* 우측 드롭다운 버튼 영역 */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border-left: 1px solid #E4E7EC;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
    background-color: #F8FAFC;
}
QComboBox::drop-down:hover {
    background-color: #EEF2F6;
}
/* 화살표 아이콘 모양 (삼각형) */
QComboBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #475467;
    margin-right: 2px;
}
/* 드롭다운 펼쳐졌을 때 팝업 리스트 뷰 */
QComboBox QAbstractItemView {
    border: 1px solid #CBD5E1;
    border-radius: 6px;
    background-color: #FFFFFF;
    selection-background-color: #EEF2F6;
    selection-color: #1D2939;
    padding: 4px;
    outline: none;
    min-height: 120px;
}
QComboBox QAbstractItemView::item {
    min-height: 28px;
    padding: 4px 8px;
}
"""

COMPLETER_POPUP_STYLE = """
QAbstractItemView {
    border: 1px solid #CBD5E1;
    border-radius: 6px;
    background-color: #FFFFFF;
    selection-background-color: #EEF2F6;
    selection-color: #1D2939;
    padding: 4px;
    outline: none;
    font-size: 13px;
}
QAbstractItemView::item {
    min-height: 28px;
    padding: 4px 8px;
}
"""


class NoScrollWheelFilter(QObject):
    """
    포커스가 없을 때 마우스 휠로 값이 변경되는 것을 방지하고
    부모 스크롤 영역으로 이벤트를 전달하는 이벤트 필터
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            is_popup_open = (
                isinstance(obj, QComboBox)
                and obj.view() is not None
                and obj.view().isVisible()
            )
            # 포커스가 없고 드롭다운도 닫혀 있으면 휠 이벤트 무시 -> 상위 스크롤 영역이 스크롤됨
            if not obj.hasFocus() and not is_popup_open:
                event.ignore()
                return True
        return super().eventFilter(obj, event)


class SearchableModelComboBox(QComboBox):
    """
    중간 단어(부분 일치) 검색이 완벽하게 지원되는 커스텀 콤보박스
    예: 'flash' 입력 시 'gemini-1.5-flash', 'gemini-3.8-flash' 모두 팝업 노출
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setView(QListView())
        self.setStyleSheet(COMBOBOX_STYLE)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(12)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 1. 원본 데이터 모델 및 중간 필터 프록시 모델 구성
        self._source_model = QStringListModel(self)
        self._proxy_model = QSortFilterProxyModel(self)
        self._proxy_model.setSourceModel(self._source_model)
        self._proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy_model.setFilterKeyColumn(0)

        # 2. QCompleter 구성 및 프록시 모델 바인딩
        self._completer = QCompleter(self._proxy_model, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        if self._completer.popup() is not None:
            self._completer.popup().setStyleSheet(COMPLETER_POPUP_STYLE)
        self.setCompleter(self._completer)

        # 3. 텍스트 입력 시 프록시 정규식 실시간 업데이트
        if self.lineEdit() is not None:
            self.lineEdit().textEdited.connect(self._on_text_edited)

        # 4. 자동완성 팝업에서 항목 클릭 시 텍스트 반영
        self._completer.activated.connect(self._on_completer_activated)

        # 5. 마우스 휠 오조작 방지 필터 등록
        self._wheel_filter = NoScrollWheelFilter(self)
        self.installEventFilter(self._wheel_filter)
        if self.lineEdit() is not None:
            self.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.lineEdit().installEventFilter(self._wheel_filter)

    def _on_text_edited(self, text: str) -> None:
        # 입력된 텍스트를 기준으로 중간 포함 필터링 갱신
        self._proxy_model.setFilterFixedString(text)
        if text.strip():
            self._completer.complete()

    def _on_completer_activated(self, text: str) -> None:
        # 팝업에서 선택한 텍스트를 콤보박스에 적용하고 필터 초기화
        self.setCurrentText(text)
        self._proxy_model.setFilterFixedString("")

    def set_model_list(self, models: List[str], default_model: str = "") -> None:
        """새로운 모델 리스트를 등록하고 기본값 선택"""
        self.blockSignals(True)
        self.clear()

        items = list(models) if models else ([default_model] if default_model else [])

        # 콤보박스 자체 목록 및 자동완성 소스 모델 갱신
        self.addItems(items)
        self._source_model.setStringList(items)
        self._proxy_model.setFilterFixedString("")

        if default_model and default_model in items:
            self.setCurrentText(default_model)
        elif items:
            self.setCurrentText(items[0])

        self.blockSignals(False)


def install_no_scroll_wheel_filter(widget: QWidget) -> None:
    """QComboBox 또는 QSpinBox에 마우스 휠 오조작 방지 필터와 StrongFocus 정책을 적용합니다."""
    if isinstance(widget, (QComboBox, QSpinBox)):
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if not hasattr(widget, "_wheel_filter"):
            wheel_filter = NoScrollWheelFilter(widget)
            setattr(widget, "_wheel_filter", wheel_filter)
            widget.installEventFilter(wheel_filter)
        else:
            wheel_filter = getattr(widget, "_wheel_filter")
        if isinstance(widget, QComboBox) and widget.lineEdit() is not None:
            widget.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            widget.lineEdit().installEventFilter(wheel_filter)
