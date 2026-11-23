"""HWPX AI 문서 에이전트 [다시 불러오기] 및 QFileSystemWatcher 외부 수정 감지 테스트."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from services.hwpx_agent_service import (  # noqa: E402
    apply_single_hwpx_operation,
    create_minimal_hwpx_document,
)
from ui.tabs.tab_doc_ocr import DocOcrTab  # noqa: E402


class _DummyTaskRunner:
    def start_worker(self, *args, **kwargs):
        return None


class TestHwpxReloadAndWatcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.tab = DocOcrTab(task_runner=_DummyTaskRunner())

    def tearDown(self):
        self.tab._hwpx_watch_debounce.stop()
        self.tab.deleteLater()
        self.tmpdir.cleanup()

    def _create_sample_hwpx(self, name: str = "sample.hwpx", text: str = "초기 본문") -> Path:
        doc = create_minimal_hwpx_document(title="테스트", empty=True)
        apply_single_hwpx_operation(doc, {"op": "append_paragraph", "text": text})
        out_path = self.tmp_path / name
        doc.save(out_path)
        return out_path

    def _modify_hwpx_on_disk(self, path: Path, new_text: str) -> None:
        doc = create_minimal_hwpx_document(title="테스트", empty=True)
        apply_single_hwpx_operation(doc, {"op": "append_paragraph", "text": new_text})
        doc.save(path)
        # Ensure st_mtime_ns strictly advances even on coarse filesystem clocks
        stat_res = path.stat()
        os.utime(path, ns=(stat_res.st_atime_ns, stat_res.st_mtime_ns + 1_000_000_000))

    def test_reload_button_state_and_manual_reload_clean(self):
        self.assertFalse(self.tab.btn_hwpx_reload.isEnabled())

        hwpx_path = self._create_sample_hwpx(text="원본 문단")
        self.tab._load_hwpx_document_into_agent(hwpx_path, "로드 완료")

        self.assertTrue(self.tab.btn_hwpx_reload.isEnabled())
        self.assertEqual(self.tab._hwpx_watched_path, hwpx_path)
        self.assertIn(str(hwpx_path), self.tab._hwpx_file_watcher.files())
        self.assertIsNotNone(self.tab._hwpx_last_disk_mtime_ns)

        # Modify file on disk externally
        self._modify_hwpx_on_disk(hwpx_path, "외부 수정 문단")

        emitted_contexts = []
        self.tab.ai_context_changed.connect(emitted_contexts.append)

        self.tab._on_hwpx_reload_from_disk()

        self.assertIn("외부 수정 문단", self.tab.browser_hwpx_preview.toPlainText())
        self.assertFalse(self.tab._hwpx_dirty)
        self.assertEqual(len(emitted_contexts), 1)
        self.assertIn("다시 불러오기 완료", self.tab.lbl_hwpx_summary_badge.text())

        # New document mode disables reload button and clears watcher
        self.tab._on_hwpx_create_new(emit_sync=False)
        self.assertFalse(self.tab.btn_hwpx_reload.isEnabled())
        self.assertEqual(self.tab._hwpx_file_watcher.files(), [])

    def test_manual_reload_dirty_prompts_and_respects_choice(self):
        hwpx_path = self._create_sample_hwpx(text="원본 문단")
        self.tab._load_hwpx_document_into_agent(hwpx_path, "로드 완료")
        self._modify_hwpx_on_disk(hwpx_path, "디스크 최신 문단")
        self.tab._hwpx_dirty = True

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ) as mock_q:
            self.tab._on_hwpx_reload_from_disk()
            mock_q.assert_called_once()
        self.assertNotIn("디스크 최신 문단", self.tab.browser_hwpx_preview.toPlainText())
        self.assertTrue(self.tab._hwpx_dirty)

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ) as mock_q:
            self.tab._on_hwpx_reload_from_disk()
            mock_q.assert_called_once()
        self.assertIn("디스크 최신 문단", self.tab.browser_hwpx_preview.toPlainText())
        self.assertFalse(self.tab._hwpx_dirty)

    def test_manual_reload_hwp_origin_calls_open_hwpx_or_hwp_path(self):
        hwpx_path = self._create_sample_hwpx(text="임시 변환 본문")
        hwp_origin = self.tmp_path / "origin.hwp"
        hwp_origin.write_bytes(b"dummy-hwp")

        self.tab._load_hwpx_document_into_agent(
            hwpx_path, "HWP 변환 완료", original_hwp_path=hwp_origin
        )
        self.assertEqual(self.tab._hwpx_watched_path, hwp_origin)
        self.assertIn(str(hwp_origin), self.tab._hwpx_file_watcher.files())

        with patch.object(self.tab, "open_hwpx_or_hwp_path") as mock_open:
            self.tab._on_hwpx_reload_from_disk()
            mock_open.assert_called_once_with(str(hwp_origin))

    def test_watcher_auto_reloads_clean_hwpx_after_debounce(self):
        hwpx_path = self._create_sample_hwpx(text="초기 내용")
        self.tab._load_hwpx_document_into_agent(hwpx_path, "로드 완료")

        self._modify_hwpx_on_disk(hwpx_path, "한글에서 자동 저장된 내용")
        # Simulate atomic replace dropping the path from QFileSystemWatcher
        self.tab._hwpx_file_watcher.removePaths(self.tab._hwpx_file_watcher.files())

        self.tab._on_hwpx_watch_debounce_timeout()

        self.assertIn("한글에서 자동 저장된 내용", self.tab.browser_hwpx_preview.toPlainText())
        self.assertEqual(
            self.tab.lbl_hwpx_summary_badge.text(),
            "외부(한글)에서 수정된 내용이 자동 반영되었습니다.",
        )
        self.assertIn(str(hwpx_path), self.tab._hwpx_file_watcher.files())

    def test_watcher_ignores_internal_save_mtime(self):
        hwpx_path = self._create_sample_hwpx(text="초기 내용")
        self.tab._load_hwpx_document_into_agent(hwpx_path, "로드 완료")

        with patch.object(QMessageBox, "information"):
            self.tab._on_hwpx_save()

        with patch.object(self.tab, "_reload_hwpx_silently") as mock_reload:
            self.tab._on_hwpx_watch_debounce_timeout()
            mock_reload.assert_not_called()

    def test_watcher_prompts_when_dirty_and_prevents_duplicate_popup(self):
        hwpx_path = self._create_sample_hwpx(text="초기 내용")
        self.tab._load_hwpx_document_into_agent(hwpx_path, "로드 완료")
        self.tab._hwpx_dirty = True
        self._modify_hwpx_on_disk(hwpx_path, "외부 변경 내용")

        # If prompt is already open, no second popup should appear
        self.tab._hwpx_reload_prompt_open = True
        with patch.object(QMessageBox, "question") as mock_q:
            self.tab._on_hwpx_watch_debounce_timeout()
            mock_q.assert_not_called()
        self.tab._hwpx_reload_prompt_open = False

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ) as mock_q:
            self.tab._on_hwpx_watch_debounce_timeout()
            mock_q.assert_called_once()
        self.assertIn("외부 변경 내용", self.tab.browser_hwpx_preview.toPlainText())
        self.assertFalse(self.tab._hwpx_dirty)

    def test_reload_preserves_scroll_position_while_new_load_resets_to_top(self):
        doc = create_minimal_hwpx_document(title="긴 문서", empty=True)
        for i in range(60):
            apply_single_hwpx_operation(
                doc, {"op": "append_paragraph", "text": f"테스트 문단 {i:02d} - 스크롤 유지 검증"}
            )
        hwpx_path = self.tmp_path / "long_doc.hwpx"
        doc.save(hwpx_path)

        self.tab.resize(900, 600)
        self.tab.browser_hwpx_preview.setFixedHeight(220)
        self.tab.show()
        self.tab._load_hwpx_document_into_agent(hwpx_path, "긴 문서 로드")
        QApplication.processEvents()

        vsb = self.tab.browser_hwpx_preview.verticalScrollBar()
        self.assertGreater(vsb.maximum(), 0)
        target_scroll = max(1, vsb.maximum() // 2)
        vsb.setValue(target_scroll)
        QApplication.processEvents()
        self.assertEqual(vsb.value(), target_scroll)

        # Update file on disk and silently reload -> scroll should remain at target_scroll
        doc2 = create_minimal_hwpx_document(title="긴 문서", empty=True)
        for i in range(60):
            apply_single_hwpx_operation(
                doc2, {"op": "append_paragraph", "text": f"수정 문단 {i:02d} - 스크롤 유지 검증"}
            )
        doc2.save(hwpx_path)

        self.assertTrue(self.tab._reload_hwpx_silently())
        QApplication.processEvents()
        self.assertGreater(vsb.value(), 0)
        self.assertEqual(vsb.value(), target_scroll)

        # Loading a document fresh resets scroll to top (0)
        self.tab._load_hwpx_document_into_agent(hwpx_path, "새로 열기")
        QApplication.processEvents()
        self.assertEqual(vsb.value(), 0)


if __name__ == "__main__":
    unittest.main()
