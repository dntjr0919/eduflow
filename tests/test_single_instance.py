import time
import unittest
import uuid

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMainWindow

from core.single_instance import SingleInstanceManager
from ui.main_window import MainWindow


class TestSingleInstanceAndRestore(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.app_id = f"EduFlow_Test_SingleInstance_{uuid.uuid4().hex}"
        self.managers = []

    def tearDown(self) -> None:
        for mgr in self.managers:
            mgr.close()
        self.app.processEvents()

    def _pump_events(self, duration_sec: float = 0.2) -> None:
        deadline = time.monotonic() + duration_sec
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

    def test_first_and_second_instance_signaling(self) -> None:
        first = SingleInstanceManager(app_id=self.app_id)
        self.managers.append(first)

        self.assertFalse(first.is_already_running())
        self.assertTrue(first.start_server())

        received = []
        first.show_window_requested.connect(lambda: received.append("SHOW"))

        second = SingleInstanceManager(app_id=self.app_id)
        self.managers.append(second)
        self.assertTrue(second.is_already_running())

        self._pump_events(0.25)
        self.assertEqual(received, ["SHOW"])

    def test_restore_from_tray_when_hidden_or_minimized(self) -> None:
        win = QMainWindow()
        win.resize(300, 200)
        win.show()
        self.app.processEvents()

        # 1. 트레이 숨김(hide) 상태에서 복원
        win.hide()
        self.app.processEvents()
        self.assertFalse(win.isVisible())

        MainWindow.restore_from_tray(win)
        self.app.processEvents()
        self.assertTrue(win.isVisible())
        self.assertFalse(bool(win.windowState() & Qt.WindowState.WindowMinimized))
        self.assertTrue(bool(win.windowState() & Qt.WindowState.WindowActive))

        # 2. 최소화(showMinimized) 상태에서 복원
        win.showMinimized()
        self.app.processEvents()
        self.assertTrue(win.isMinimized())

        MainWindow.restore_from_tray(win)
        self.app.processEvents()
        self.assertFalse(win.isMinimized())
        self.assertTrue(win.isVisible())
        self.assertTrue(bool(win.windowState() & Qt.WindowState.WindowActive))

        win.close()
        win.deleteLater()


if __name__ == "__main__":
    unittest.main()
