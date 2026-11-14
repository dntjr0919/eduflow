"""
애플리케이션 진입점 (main.py).
확장형 교무/업무 자동화 데스크톱 워크스테이션 (PyQt6 기반)
- ui/styles/theme.qss 모던 웹 AI 스타일시트 강제 로드 및 적용
- Pretendard / Segoe UI / Apple SD Gothic Neo 모던 폰트 우선 렌더링
"""

import os
import sys
import traceback
from pathlib import Path
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication, QMessageBox

from ui.main_window import MainWindow
from ui.utils import create_eduflow_app_icon, install_global_wheel_guard
from utils.path_helper import get_resource_path


def install_exception_hook() -> None:
    """예기치 않은 예외 발생 시 프로그램이 조용히 죽지 않도록 로그 및 알림창을 표시합니다."""

    def _excepthook(exc_type, exc_value, exc_tb):
        tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        if QApplication.instance() is not None:
            QMessageBox.critical(
                None,
                "예기치 않은 오류 발생",
                f"오류가 발생했습니다:\n{exc_value}\n\n상세 내역:\n{tb_str[:800]}",
            )

    sys.excepthook = _excepthook


def select_modern_font() -> QFont:
    """Pretendard, Segoe UI, Apple SD Gothic Neo 순으로 시스템 최적 모던 폰트를 선택합니다."""
    preferred_families = [
        "Pretendard",
        "Pretendard Variable",
        "Segoe UI Variable Display",
        "Segoe UI",
        "Apple SD Gothic Neo",
    ]
    installed = set(QFontDatabase.families())
    for fam in preferred_families:
        if fam in installed:
            font = QFont(fam, 10)
            font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
            return font

    fallback = QFont("Segoe UI", 10)
    fallback.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return fallback


def load_theme_stylesheet(app: QApplication) -> str:
    """ui/styles/theme.qss 스타일시트 파일을 읽어 애플리케이션 전역에 적용합니다."""
    candidates = [
        Path(get_resource_path(os.path.join("ui", "styles", "theme.qss"))),
        Path(__file__).resolve().parent / "ui" / "styles" / "theme.qss",
    ]
    for qss_path in candidates:
        if qss_path.exists():
            try:
                qss_text = qss_path.read_text(encoding="utf-8")
                app.setStyleSheet(qss_text)
                return qss_text
            except Exception as exc:
                print(f"[Main] QSS 테마 로드 오류 ({qss_path}): {exc}")
    return ""


def apply_light_palette(app: QApplication) -> None:
    """Windows 다크모드 영향으로 QDateEdit/QTimeEdit 등이 검게 렌더링되지 않도록 라이트 팔레트를 고정 적용합니다."""
    palette = QPalette()
    active_colors = {
        QPalette.ColorRole.Window: QColor("#F8F9FF"),
        QPalette.ColorRole.WindowText: QColor("#0F172A"),
        QPalette.ColorRole.Base: QColor("#FFFFFF"),
        QPalette.ColorRole.AlternateBase: QColor("#F8FAFC"),
        QPalette.ColorRole.ToolTipBase: QColor("#0F172A"),
        QPalette.ColorRole.ToolTipText: QColor("#FFFFFF"),
        QPalette.ColorRole.Text: QColor("#0F172A"),
        QPalette.ColorRole.Button: QColor("#FFFFFF"),
        QPalette.ColorRole.ButtonText: QColor("#0F172A"),
        QPalette.ColorRole.BrightText: QColor("#FFFFFF"),
        QPalette.ColorRole.Highlight: QColor("#2563EB"),
        QPalette.ColorRole.HighlightedText: QColor("#FFFFFF"),
        QPalette.ColorRole.PlaceholderText: QColor("#94A3B8"),
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        for role, color in active_colors.items():
            palette.setColor(group, role, color)

    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#94A3B8"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#94A3B8"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#94A3B8"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor("#F1F5F9"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor("#F1F5F9"))
    app.setPalette(palette)


def main() -> int:
    install_exception_hook()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_light_palette(app)
    app_icon = create_eduflow_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    app.setQuitOnLastWindowClosed(False)  # 시스템 트레이 상주를 위해 창이 닫혀도 앱 유지
    app.setFont(select_modern_font())

    # 모든 QComboBox / QSpinBox 마우스 휠 오조작 방지 (StrongFocus 강제 + 비활성 휠 이벤트 부모 전달)
    install_global_wheel_guard(app)

    qss_text = load_theme_stylesheet(app)

    window = MainWindow()
    if qss_text:
        window.setStyleSheet(qss_text)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
