"""
UI 공통 유틸리티 및 커스텀 위젯 모듈 (ui/utils.py)
- NoScrollWheelFilter: 포커스가 없을 때 마우스 휠로 QComboBox/QSpinBox 값이 오변경되는 것을 방지하고
  부모 스크롤 영역으로 휠 이벤트를 전달하는 이벤트 필터
- GlobalWheelGuard / install_global_wheel_guard: 위 필터를 QApplication 전역에 설치하여
  모든 콤보박스/스핀박스에 StrongFocus 정책과 휠 가드를 자동 적용
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
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QCompleter,
    QListView,
    QSpinBox,
    QWidget,
)


COMBOBOX_STYLE = """
QComboBox {
    min-height: 34px;
    height: 34px;
    font-size: 13px;
    padding: 2px 30px 2px 10px; /* 우측 화살표 공간 확보 */
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    background-color: #FFFFFF;
    color: #0F172A;
}
QComboBox:focus {
    border: 1.5px solid #004AC6;
}
/* 우측 드롭다운 버튼 영역 */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border-left: 1px solid #E2E8F0;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
    background-color: #F8F9FF;
}
QComboBox::drop-down:hover {
    background-color: #EEF3FD;
}
/* 화살표 아이콘 모양 (삼각형) */
QComboBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #475569;
    margin-right: 2px;
}
/* 드롭다운 펼쳐졌을 때 팝업 리스트 뷰 */
QComboBox QAbstractItemView {
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    background-color: #FFFFFF;
    selection-background-color: #EEF3FD;
    selection-color: #004AC6;
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
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    background-color: #FFFFFF;
    selection-background-color: #EEF3FD;
    selection-color: #004AC6;
    padding: 4px;
    outline: none;
    font-size: 13px;
}
QAbstractItemView::item {
    min-height: 28px;
    padding: 4px 8px;
}
"""


_WHEEL_GUARDED_TYPES = (QComboBox, QAbstractSpinBox)


def _find_guarded_owner(obj: QObject) -> Optional[QWidget]:
    """
    휠 이벤트를 받은 객체(또는 그 상위 위젯 중)에서 보호 대상인
    QComboBox / QAbstractSpinBox(QSpinBox, QDoubleSpinBox, QDateEdit 등)를 찾습니다.
    (콤보박스/스핀박스 내부의 QLineEdit 자식이 휠 이벤트를 먼저 받는 경우 대응)
    """
    if not isinstance(obj, QWidget):
        return None
    w: Optional[QWidget] = obj
    depth = 0
    while w is not None and depth < 3:
        if isinstance(w, _WHEEL_GUARDED_TYPES):
            return w
        w = w.parentWidget()
        depth += 1
    return None


def _is_guarded_widget_active(owner: QWidget) -> bool:
    """사용자가 명시적으로 활성화(클릭/Tab 포커스)했거나 드롭다운이 열린 상태인지 판정합니다."""
    if isinstance(owner, QComboBox):
        view = owner.view()
        if view is not None and view.isVisible():
            return True
    if owner.hasFocus():
        return True
    focus_w = QApplication.focusWidget()
    return focus_w is not None and owner.isAncestorOf(focus_w)


def _enforce_strong_focus(widget: QWidget) -> None:
    """마우스 휠만으로 포커스를 가져가지 못하도록 StrongFocus(클릭/Tab 전용)로 고정합니다."""
    if widget.focusPolicy() != Qt.FocusPolicy.StrongFocus:
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    line_edit = getattr(widget, "lineEdit", lambda: None)()
    if isinstance(line_edit, QWidget) and line_edit.focusPolicy() != Qt.FocusPolicy.StrongFocus:
        line_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


class NoScrollWheelFilter(QObject):
    """
    QComboBox / QSpinBox 마우스 휠 오조작 방지 이벤트 필터.

    - 위젯이 클릭으로 포커스를 받았거나 드롭다운이 펼쳐진 상태가 아니면 휠 이벤트를 무시(ignore)합니다.
    - 필터가 True를 반환하되 이벤트를 ignore 상태로 두면, Qt가 휠 이벤트를 부모 위젯으로 전파하므로
      상위 QScrollArea가 정상적으로 스크롤됩니다.
    - 개별 위젯에 설치해도 되고, QApplication 전체에 설치해도 동작합니다(GlobalWheelGuard 참조).
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            owner = _find_guarded_owner(obj)
            if owner is not None and not _is_guarded_widget_active(owner):
                event.ignore()
                return True
        return super().eventFilter(obj, event)


class GlobalWheelGuard(NoScrollWheelFilter):
    """
    애플리케이션 전역 휠 가드.
    QApplication에 한 번만 설치하면 이후 생성되는 모든 QComboBox / QAbstractSpinBox에 대해
    1) 위젯 polish 시점에 FocusPolicy를 StrongFocus로 강제하고
    2) 비활성 상태의 휠 이벤트를 부모 스크롤 영역으로 넘깁니다.
    (Qt 기본값인 WheelFocus는 휠 이벤트 전달 직전에 포커스를 먼저 부여하므로 반드시 교체해야 합니다.)
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        etype = event.type()
        if etype in (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.Enter):
            owner = _find_guarded_owner(obj)
            if owner is not None:
                _enforce_strong_focus(owner)
            return False
        if etype == QEvent.Type.Wheel:
            return super().eventFilter(obj, event)
        return False


def install_global_wheel_guard(app: QApplication) -> GlobalWheelGuard:
    """QApplication 전역에 휠 오조작 방지 가드를 설치하고, 이미 생성된 위젯에도 StrongFocus를 적용합니다."""
    existing = getattr(app, "_eduflow_wheel_guard", None)
    if isinstance(existing, GlobalWheelGuard):
        return existing
    guard = GlobalWheelGuard(app)
    app.installEventFilter(guard)
    setattr(app, "_eduflow_wheel_guard", guard)
    for widget in app.allWidgets():
        if isinstance(widget, _WHEEL_GUARDED_TYPES):
            _enforce_strong_focus(widget)
    return guard


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
    """
    QComboBox 또는 QSpinBox(QAbstractSpinBox 계열)에 마우스 휠 오조작 방지 필터와 StrongFocus 정책을 적용합니다.
    (main.py에서 install_global_wheel_guard()로 전역 가드를 설치하므로 하위 호환용으로 유지됩니다.)
    """
    if isinstance(widget, _WHEEL_GUARDED_TYPES):
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
