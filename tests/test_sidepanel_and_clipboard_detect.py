"""미니 사이드패널 모드(항상 위에 포함) 및 수동 복붙 클립보드 자동 감지 단위 테스트."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from PyQt6.QtCore import QRect, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.base_worker import TaskRunner  # noqa: E402
from core.config_manager import ConfigManager  # noqa: E402
from core.database import DatabaseManager  # noqa: E402
from ui.components.ai_execution_dock import (  # noqa: E402
    AiExecutionDock,
    detect_hwpx_clipboard_payload,
)
from ui.main_window import MainWindow  # noqa: E402


class _DummyTaskRunner:
    def start_worker(self, *args, **kwargs):
        return None


class TestClipboardDetectionHelper(unittest.TestCase):
    def test_detects_valid_hwpx_json(self):
        raw = '```json\n{"operations": [{"op": "replace_text", "target_id": "p_0", "old_text": "a", "new_text": "b"}]}\n```'
        res = detect_hwpx_clipboard_payload(raw)
        self.assertIsNotNone(res)
        self.assertEqual(res["kind"], "json")
        self.assertEqual(res["ops_count"], 1)

    def test_detects_valid_code_mode_function(self):
        raw = (
            "```python\n"
            "def edit_hwpx(sec_root, helpers):\n"
            "    return {'modified_ids': []}\n"
            "```"
        )
        res = detect_hwpx_clipboard_payload(raw)
        self.assertIsNotNone(res)
        self.assertEqual(res["kind"], "code_mode")
        self.assertIn("def edit_hwpx", res["code"])

    def test_ignores_ordinary_text_and_non_hwpx_json(self):
        self.assertIsNone(detect_hwpx_clipboard_payload(""))
        self.assertIsNone(detect_hwpx_clipboard_payload("한글 문서에서 복사한 일반 문장입니다."))
        self.assertIsNone(detect_hwpx_clipboard_payload('{"students": [{"학번": "10101"}]}'))


class TestSidepanelAndClipboardDock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.db = DatabaseManager()
        self.cfg = ConfigManager()

    def tearDown(self):
        QApplication.clipboard().clear()
        self.tmpdir.cleanup()

    def test_sidepanel_mode_enter_pin_and_restore(self):
        with (
            patch.object(MainWindow, "_register_all_tabs", lambda self: None),
            patch.object(TaskRunner, "start_worker", lambda *a, **kw: None),
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
            patch.object(QMessageBox, "information"),
            patch.object(QMessageBox, "warning"),
        ):
            win = MainWindow(db=self.db, config=self.cfg, enable_startup_sync=False)
            try:
                initial_geom = QRect(100, 80, 1200, 760)
                win.setGeometry(initial_geom)
                win.show()
                QApplication.processEvents()

                win.ai_dock.register_target(
                    "hwpx_agent",
                    "4. HWPX AI 문서 에이전트",
                    context_getter=lambda: {
                        "target_id": "hwpx_agent",
                        "kind": "hwpx_agent",
                        "label": "4. HWPX AI 문서 에이전트",
                        "hwpx_path": str(self.tmp_path / "고사원안.hwpx"),
                    },
                )
                win.ai_dock.set_target("hwpx_agent", force=True)

                win.set_sidepanel_mode(True)
                QApplication.processEvents()

                self.assertTrue(win._sidepanel_mode)
                self.assertFalse(win.sidebar_frame.isVisible())
                self.assertFalse(win.workspace_frame.isVisible())
                self.assertTrue(win.ai_dock.isVisible())
                self.assertEqual(win.width(), MainWindow.SIDEPANEL_WIDTH)
                self.assertTrue(win.ai_dock.lbl_sidepanel_file_badge.isVisible())
                self.assertEqual(
                    win.ai_dock.lbl_sidepanel_file_badge.text(),
                    "편집 중: 고사원안.hwpx",
                )

                avail = win.screen().availableGeometry()
                self.assertEqual(
                    win.geometry().x(),
                    avail.x() + max(0, avail.width() - MainWindow.SIDEPANEL_WIDTH),
                )
                self.assertEqual(win.geometry().height(), avail.height())

                # Always-on-top pin toggle
                win.set_always_on_top(True)
                QApplication.processEvents()
                self.assertTrue(bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
                self.assertTrue(win.isVisible())

                # Exit sidepanel mode -> restores layout, geometry, and pre-sidepanel pin state
                win.set_sidepanel_mode(False)
                QApplication.processEvents()

                self.assertFalse(win._sidepanel_mode)
                self.assertTrue(win.sidebar_frame.isVisible())
                self.assertTrue(win.workspace_frame.isVisible())
                self.assertFalse(win.ai_dock.lbl_sidepanel_file_badge.isVisible())
                self.assertFalse(bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
                self.assertEqual(win.geometry(), initial_geom)
            finally:
                win._force_quit = True
                win.cleanup_on_quit()
                win.close()
                win.deleteLater()
                QApplication.processEvents()

    def test_clipboard_detection_on_window_activate_and_manual_apply(self):
        dock = AiExecutionDock(task_runner=_DummyTaskRunner(), db=self.db)
        try:
            dock.show()
            dock.set_mode_index(1)  # 수동 복붙 모드
            dock.register_target(
                "hwpx_agent",
                "4. HWPX AI 문서 에이전트",
                context_getter=lambda: {
                    "target_id": "hwpx_agent",
                    "kind": "hwpx_agent",
                    "label": "4. HWPX AI 문서 에이전트",
                    "hwpx_path": str(self.tmp_path / "test.hwpx"),
                },
            )
            dock.set_target("hwpx_agent", force=True)

            # 1. Ordinary text should be ignored
            QApplication.clipboard().setText("사용자가 한/글에서 복사한 일반 문장")
            dock.on_host_window_activated()
            self.assertFalse(dock.clipboard_detect_banner.isVisible())
            self.assertEqual(dock.txt_manual_response.toPlainText(), "")

            # 2. Valid HWPX JSON should auto-populate and show one-click banner
            json_resp = '{"operations": [{"op": "replace_text", "target_id": "p_0", "old_text": "구", "new_text": "신"}]}'
            QApplication.clipboard().setText(json_resp)
            dock.on_host_window_activated()
            self.assertTrue(dock.clipboard_detect_banner.isVisible())
            self.assertEqual(dock.txt_manual_response.toPlainText(), json_resp)

            # 3. Same clipboard hash on repeated activation should not re-trigger after dismiss
            dock._dismiss_clipboard_detect_banner()
            dock.txt_manual_response.clear()
            dock.on_host_window_activated()
            self.assertFalse(dock.clipboard_detect_banner.isVisible())

            # 4. Code mode (def edit_hwpx) + [클립보드 바로 적용] dispatches code_mode payload
            code_resp = "```python\ndef edit_hwpx(sec_root, helpers):\n    return {'modified_ids': ['p_0']}\n```"
            QApplication.clipboard().setText(code_resp)
            with patch.object(dock, "_dispatch_result") as mock_dispatch:
                dock._on_manual_clipboard_apply_clicked()
                mock_dispatch.assert_called_once()
                tid, payload = mock_dispatch.call_args[0]
                self.assertEqual(tid, "hwpx_agent")
                self.assertEqual(payload.get("exec_kind"), "code_mode")
                self.assertIn("def edit_hwpx", payload.get("code", ""))
        finally:
            dock.close()
            dock.deleteLater()
            QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
