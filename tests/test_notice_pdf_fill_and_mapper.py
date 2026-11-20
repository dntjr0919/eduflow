from __future__ import annotations

import base64
import io
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PyQt6.QtCore import Qt
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from services.notice_pdf_fill_service import (
    batch_export_notice_pdfs,
    build_pdf_vision_mapping_prompt,
    decode_signature_data_url,
    fill_notice_pdf_for_reply,
    normalized_rect_to_pdf_points,
    parse_pdf_vision_mapping_response,
    render_pdf_pages_to_png_files,
)


def _make_sample_pdf(path: Path, pages: int = 2) -> None:
    c = canvas.Canvas(str(path), pagesize=(595.0, 842.0))
    for i in range(1, pages + 1):
        c.drawString(72, 760, f"Sample Notice Page {i}")
        c.drawString(72, 680, "Consent:")
        c.drawString(180, 680, "Agree")
        c.drawString(260, 680, "Disagree")
        c.drawString(72, 600, "Signature:")
        c.showPage()
    c.save()


def _make_sample_signature_data_url() -> str:
    img = Image.new("RGBA", (120, 40), (0, 0, 0, 0))
    for x in range(10, 110):
        img.putpixel((x, 20), (15, 23, 42, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


class TestNoticePdfFillService(unittest.TestCase):
    def test_normalized_rect_to_pdf_points(self) -> None:
        x, y, w, h = normalized_rect_to_pdf_points(0.1, 0.2, 0.3, 0.1, 500.0, 1000.0)
        self.assertAlmostEqual(x, 50.0)
        self.assertAlmostEqual(w, 150.0)
        self.assertAlmostEqual(h, 100.0)
        # top-left y=0.2, h=0.1 -> bottom is 0.3 from top -> 0.7 from bottom = 700.0
        self.assertAlmostEqual(y, 700.0)

    def test_decode_signature_data_url(self) -> None:
        data_url = _make_sample_signature_data_url()
        raw = decode_signature_data_url(data_url)
        self.assertIsNotNone(raw)
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertIsNone(decode_signature_data_url("https://drive.google.com/file/d/123"))
        self.assertIsNone(decode_signature_data_url(""))

    def test_fill_and_batch_export_individual_and_merged(self) -> None:
        sig_url = _make_sample_signature_data_url()
        field_map = [
            {"id": "f1", "key": "name", "label": "이름", "kind": "text", "page": 1, "x_ratio": 0.2, "y_ratio": 0.15, "w_ratio": 0.2, "h_ratio": 0.04},
            {"id": "f2", "key": "consent", "label": "동의", "kind": "option", "option": "Agree", "page": 1, "x_ratio": 0.28, "y_ratio": 0.18, "w_ratio": 0.04, "h_ratio": 0.03},
            {"id": "f3", "key": "consent", "label": "미동의", "kind": "option", "option": "Disagree", "page": 1, "x_ratio": 0.42, "y_ratio": 0.18, "w_ratio": 0.04, "h_ratio": 0.03},
            {"id": "f4", "key": "parent_signature", "label": "학부모 서명", "kind": "signature", "page": 1, "x_ratio": 0.3, "y_ratio": 0.27, "w_ratio": 0.18, "h_ratio": 0.06},
            {"id": "f5", "key": "reason", "label": "비고", "kind": "text", "page": 2, "x_ratio": 0.2, "y_ratio": 0.3, "w_ratio": 0.4, "h_ratio": 0.06},
        ]
        replies = [
            {
                "student_id": "10101",
                "student_name": "Kim_Minjun",
                "answers": {"consent": "Agree", "reason": "Participate"},
                "parent_signature_data_url": sig_url,
            },
            {
                "student_id": "10102",
                "student_name": "Lee_Seoyeon",
                "answers": {"consent": "Disagree", "reason": "Absent"},
                "signature_data_url": sig_url,
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            src_pdf = Path(td) / "notice.pdf"
            _make_sample_pdf(src_pdf, pages=2)

            single_out = Path(td) / "single.pdf"
            fill_notice_pdf_for_reply(src_pdf, field_map, replies[0], single_out)
            self.assertTrue(single_out.is_file())
            r_single = PdfReader(str(single_out))
            self.assertEqual(len(r_single.pages), 2)
            p1_txt = r_single.pages[0].extract_text() or ""
            p2_txt = r_single.pages[1].extract_text() or ""
            self.assertIn("Kim_Minjun", p1_txt)
            self.assertIn("Participate", p2_txt)

            indiv_dir = Path(td) / "indiv"
            res_indiv = batch_export_notice_pdfs(src_pdf, field_map, replies, indiv_dir, mode="individual")
            self.assertEqual(res_indiv["count"], 2)
            self.assertTrue((indiv_dir / "10101_Kim_Minjun.pdf").is_file())
            self.assertTrue((indiv_dir / "10102_Lee_Seoyeon.pdf").is_file())

            merged_pdf = Path(td) / "merged.pdf"
            res_merged = batch_export_notice_pdfs(src_pdf, field_map, replies, merged_pdf, mode="merged")
            self.assertEqual(res_merged["count"], 2)
            self.assertEqual(res_merged["total_pages"], 4)
            self.assertEqual(len(PdfReader(str(merged_pdf)).pages), 4)


class TestNoticePdfMapperAndReset(unittest.TestCase):
    def test_mapper_dialog_and_attachment_url_reset(self) -> None:
        from PyQt6.QtWidgets import QApplication
        from core.base_worker import TaskRunner
        from ui.dialogs.notice_pdf_mapper_dialog import NoticePdfMapperDialog, build_palette_entries
        from ui.tabs.tab_classroom_tools import ClassroomToolsTab

        app = QApplication.instance() or QApplication([])
        _ = app

        entries = build_palette_entries(
            [
                {"key": "consent", "label": "Consent", "type": "radio", "options": ["Agree", "Disagree"]},
                {"key": "memo", "label": "Memo", "type": "text", "options": []},
            ]
        )
        displays = [e["display"] for e in entries]
        self.assertTrue(any(d.endswith("[✓] Consent → Agree") and d.startswith("[7]") for d in displays))
        self.assertTrue(any(d.endswith("[✓] Consent → Disagree") and d.startswith("[8]") for d in displays))
        self.assertTrue(any(d.endswith("[입력] Memo") and d.startswith("[9]") for d in displays))
        self.assertIn("[6] [기본] 학부모 서명", displays)

        with tempfile.TemporaryDirectory() as td:
            src_pdf = Path(td) / "form.pdf"
            c = canvas.Canvas(str(src_pdf), pagesize=(595.0, 842.0))
            c.drawString(72, 720, "Question One:")
            c.drawString(260, 720, "Agree")
            c.drawString(340, 720, "Disagree")
            c.drawString(72, 620, "Question Two:")
            c.drawString(260, 620, "Agree")
            c.drawString(340, 620, "Disagree")
            c.save()

            dlg = NoticePdfMapperDialog(
                pdf_path=str(src_pdf),
                reply_schema=[
                    {"key": "q1", "label": "Question One", "type": "radio", "options": ["Agree", "Disagree"]},
                    {"key": "q2", "label": "Question Two", "type": "radio", "options": ["Agree", "Disagree"]},
                ],
            )
            self.assertEqual(dlg.combo_zoom.count(), 4)
            self.assertEqual(dlg.combo_zoom.currentText(), "페이지 맞춤")
            self.assertTrue(bool(dlg.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint))
            self.assertEqual(dlg.btn_toggle_fullscreen.text(), "전체화면")
            dlg.btn_toggle_fullscreen.setChecked(True)
            self.assertEqual(dlg.btn_toggle_fullscreen.text(), "원래 크기")
            dlg.btn_toggle_fullscreen.setChecked(False)
            self.assertEqual(dlg.btn_toggle_fullscreen.text(), "전체화면")

            # Click-to-place & auto-advance
            dlg.list_palette.setCurrentRow(0)
            dlg._on_canvas_empty_clicked(0.25, 0.25)
            self.assertEqual(len(dlg.get_field_map()), 1)
            self.assertIn("✓", dlg.list_palette.item(0).text())
            self.assertEqual(dlg.list_palette.currentRow(), 1)
            self.assertEqual(dlg.canvas._badge_text(dlg.get_field_map()[0]), "#1")

            # Proximity search: q2's Agree should be below q1's Agree (higher y_ratio)
            q1_entry = next(e for e in dlg._palette_entries if e["key"] == "q1" and e["option"] == "Agree")
            q2_entry = next(e for e in dlg._palette_entries if e["key"] == "q2" and e["option"] == "Agree")
            pos1 = dlg._suggest_position_for_entry(q1_entry)
            pos2 = dlg._suggest_position_for_entry(q2_entry)
            self.assertIsNotNone(pos1)
            self.assertIsNotNone(pos2)
            self.assertGreater(pos2[2], pos1[2] + 0.05)
            dlg.close()

            tab = ClassroomToolsTab(task_runner=TaskRunner())
            tab.edit_notice_attachment_url.setText("https://drive.google.com/old-url")
            tab._set_notice_pdf_file(str(src_pdf), force_ai=False)
            self.assertEqual(tab.edit_notice_attachment_url.text(), "")

    def test_vision_mapping_prompt_parse_and_canvas_apply(self) -> None:
        from PyQt6.QtWidgets import QApplication
        from ui.dialogs.notice_pdf_mapper_dialog import NoticePdfMapperDialog, build_palette_entries

        app = QApplication.instance() or QApplication([])
        _ = app

        entries = build_palette_entries(
            [{"key": "consent", "label": "Consent", "type": "radio", "options": ["Agree", "Disagree"]}]
        )
        prompt = build_pdf_vision_mapping_prompt(entries, page_count=2, target_page=1)
        self.assertIn("[1] 텍스트 입력칸: 학년", prompt)
        self.assertIn("[7] 체크박스(✓): Consent → 선택지 'Agree'", prompt)
        self.assertIn("빈 기입칸", prompt)

        raw_ai = """```json
[
  {"index": 1, "page": 1, "x_ratio": 0.12, "y_ratio": 0.18, "w_ratio": 0.05, "h_ratio": 0.03},
  {"index": 7, "page": 1, "x_ratio": 0.44, "y_ratio": 0.35, "w_ratio": 0.03, "h_ratio": 0.025}
]
```"""
        parsed = parse_pdf_vision_mapping_response(raw_ai, entries, page_count=2)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["key"], "grade")
        self.assertEqual(parsed[1]["kind"], "option")
        self.assertEqual(parsed[1]["option"], "Agree")

        with tempfile.TemporaryDirectory() as td:
            src_pdf = Path(td) / "vision.pdf"
            _make_sample_pdf(src_pdf, pages=2)
            pngs = render_pdf_pages_to_png_files(src_pdf, Path(td) / "pngs", scale=1.0)
            self.assertEqual(len(pngs), 2)
            self.assertTrue(pngs[0].is_file())

            dlg = NoticePdfMapperDialog(
                pdf_path=str(src_pdf),
                reply_schema=[{"key": "consent", "label": "Consent", "type": "radio", "options": ["Agree", "Disagree"]}],
            )
            cnt = dlg._apply_vision_mapped_boxes(parsed)
            self.assertEqual(cnt, 2)
            self.assertEqual(len(dlg.get_field_map()), 2)
            self.assertIn("✓", dlg.list_palette.item(0).text())
            self.assertIn("✓", dlg.list_palette.item(6).text())
            dlg.close()

    def test_modal_add_schema_field_and_sync_to_tab(self) -> None:
        from PyQt6.QtWidgets import QApplication
        from core.base_worker import TaskRunner
        from ui.dialogs.notice_pdf_mapper_dialog import NoticePdfMapperDialog
        from ui.tabs.tab_classroom_tools import ClassroomToolsTab

        app = QApplication.instance() or QApplication([])
        _ = app

        with tempfile.TemporaryDirectory() as td:
            src_pdf = Path(td) / "sync.pdf"
            _make_sample_pdf(src_pdf, pages=1)

            dlg = NoticePdfMapperDialog(pdf_path=str(src_pdf), reply_schema=[])
            self.assertEqual(dlg.list_palette.count(), 6)

            added = dlg.add_schema_field(
                label="비상연락처",
                field_type="text",
                options=[],
                required=True,
            )
            self.assertEqual(added["label"], "비상연락처")
            self.assertEqual(dlg.list_palette.count(), 7)
            self.assertEqual(dlg.list_palette.currentRow(), 6)
            self.assertIn("[7]", dlg.list_palette.item(6).text())
            self.assertIn("비상연락처", dlg.list_palette.item(6).text())

            # 선택된 새 항목을 캔버스 클릭으로 즉시 배치
            dlg._on_canvas_empty_clicked(0.5, 0.5)
            self.assertEqual(len(dlg.get_field_map()), 1)
            self.assertEqual(dlg.get_field_map()[0]["label"], "비상연락처")

            updated_schema = dlg.get_reply_schema()
            self.assertEqual(len(updated_schema), 1)
            self.assertEqual(updated_schema[0]["label"], "비상연락처")
            dlg.close()

            tab = ClassroomToolsTab(task_runner=TaskRunner())
            tab._populate_reply_schema_table(updated_schema)
            collected = tab._collect_reply_schema_from_table()
            self.assertEqual(len(collected), 1)
            self.assertEqual(collected[0]["label"], "비상연락처")
            self.assertEqual(collected[0]["type"], "text")

    def test_playwright_vision_mapping_dispatch_and_apply(self) -> None:
        from unittest.mock import MagicMock, patch
        from PyQt6.QtWidgets import QApplication
        from ui.dialogs.notice_pdf_mapper_dialog import NoticePdfMapperDialog

        app = QApplication.instance() or QApplication([])
        _ = app

        mock_dock = MagicMock()
        mock_dock.current_mode_index.return_value = 2

        def _fake_pw_run(**kwargs):
            self.assertTrue(len(kwargs["attachment_paths"]) == 1)
            self.assertTrue(Path(kwargs["attachment_paths"][0]).name == "page_1.png")
            self.assertIn("[1] 텍스트 입력칸: 학년", kwargs["prompt_text"])
            kwargs["on_result"](
                {
                    "raw_response": '[{"index": 1, "page": 1, "x_ratio": 0.15, "y_ratio": 0.22, "w_ratio": 0.05, "h_ratio": 0.03}]'
                }
            )
            return True

        mock_dock.run_playwright_single_prompt.side_effect = _fake_pw_run

        with tempfile.TemporaryDirectory() as td:
            src_pdf = Path(td) / "pw.pdf"
            _make_sample_pdf(src_pdf, pages=1)
            dlg = NoticePdfMapperDialog(
                pdf_path=str(src_pdf),
                reply_schema=[],
                ai_dock=mock_dock,
            )
            with patch("ui.dialogs.notice_pdf_mapper_dialog.QMessageBox.information"):
                dlg._on_ai_vision_mapping()
            self.assertEqual(mock_dock.run_playwright_single_prompt.call_count, 1)
            self.assertEqual(len(dlg.get_field_map()), 1)
            self.assertEqual(dlg.get_field_map()[0]["key"], "grade")
            dlg.close()


if __name__ == "__main__":
    unittest.main()
