import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import openpyxl
from PyQt6.QtWidgets import QApplication

from services.hwpx_agent_service import (
    HP_NS,
    _local_tag,
    _normalize_hwp_equation_script,
    _parse_xml,
    _qname,
    apply_hwpx_operations,
    create_minimal_hwpx_document,
)
from ui.dialogs.extraction_review_dialog import ExtractionReviewDialog


class TestHwpxAgentFixes(unittest.TestCase):
    def test_multi_run_replace_preserves_non_target_char_pr(self) -> None:
        doc = create_minimal_hwpx_document()
        sec_name = doc.section_names()[0]
        root = _parse_xml(doc.entries[sec_name])

        p_elem = next(n for n in root.iter() if _local_tag(n) == "p")
        for ch in list(p_elem):
            if _local_tag(ch) == "run":
                p_elem.remove(ch)

        run_specs = [("10", "머리말 "), ("20", "H2"), ("30", "O 꼬리말")]
        for cid, txt in run_specs:
            r = ET.SubElement(p_elem, _qname(HP_NS, "run"), {"charPrIDRef": cid})
            t = ET.SubElement(r, _qname(HP_NS, "t"))
            t.text = txt

        from services.hwpx_agent_service import _serialize_xml

        doc.entries[sec_name] = _serialize_xml(root)

        touched = apply_hwpx_operations(
            doc,
            [
                {
                    "op": "replace_text",
                    "target_id": "p_0",
                    "old_text": "H2O",
                    "new_text": "물(H_{2}O)",
                }
            ],
        )
        self.assertEqual(touched, ["p_0"])

        updated_root = _parse_xml(doc.entries[sec_name])
        updated_p = next(n for n in updated_root.iter() if _local_tag(n) == "p")
        runs = [ch for ch in updated_p if _local_tag(ch) == "run"]

        # 첫 번째 run("머리말 ", charPrIDRef="10")과 마지막 run(" 꼬리말", charPrIDRef="30") 보존 확인
        self.assertEqual(runs[0].get("charPrIDRef"), "10")
        self.assertEqual(runs[0][0].text, "머리말 ")

        self.assertEqual(runs[-1].get("charPrIDRef"), "30")
        self.assertEqual(runs[-1][0].text, " 꼬리말")

        # 치환 시작 run("20") 스타일 기반 생성 확인
        self.assertEqual(runs[1].get("charPrIDRef"), "20")
        self.assertEqual(runs[1][0].text, "물(H")

    def test_equation_normalization_size_and_inline_attrs(self) -> None:
        # 1) LaTeX -> 한글 수식 정규화 검증
        self.assertEqual(
            _normalize_hwp_equation_script(r"\frac{H_{2}O}{CO_{2}}"),
            "{H_{2}O} over {CO_{2}}",
        )
        self.assertEqual(
            _normalize_hwp_equation_script(r"\sqrt{x+1} + \Delta H \rightleftharpoons \alpha + \beta + \gamma"),
            "sqrt {x+1} + Delta H <-> alpha + beta + gamma",
        )

        # 2) 기존 작은 높이의 템플릿 수식이 있어도 분수 수식은 height >= 2000, treatAsChar="1" 적용
        doc = create_minimal_hwpx_document()
        sec_name = doc.section_names()[0]

        # 먼저 일반 수식 하나를 넣어 템플릿 수식(height=1100)을 문서에 존재하게 만듦
        apply_hwpx_operations(
            doc,
            [
                {
                    "op": "set_paragraph_text",
                    "target_id": "p_0",
                    "new_text": "$$rm {CH}_{3}COOH(it {aq})$$",
                }
            ],
        )
        # 이어서 분수 수식 삽입 (템플릿 복제 경로 검증)
        apply_hwpx_operations(
            doc,
            [
                {
                    "op": "insert_paragraph_after",
                    "target_id": "p_0",
                    "text": r"$$\frac{H_{2}O}{CO_{2}}$$",
                }
            ],
        )

        root = _parse_xml(doc.entries[sec_name])
        eqs = [n for n in root.iter() if _local_tag(n) == "equation"]
        self.assertEqual(len(eqs), 2)

        # 첫 번째 수식: rm {CH}_{3}COOH(it {aq})
        sc0 = next(ch for ch in eqs[0] if _local_tag(ch) == "script")
        self.assertEqual(sc0.text, "rm {CH}_{3}COOH(it {aq})")
        pos0 = next(ch for ch in eqs[0] if _local_tag(ch) == "pos")
        self.assertEqual(pos0.get("treatAsChar"), "1")

        # 두 번째 수식: 분수
        eq1 = eqs[1]
        sc1 = next(ch for ch in eq1 if _local_tag(ch) == "script")
        self.assertIn("over", sc1.text or "")
        self.assertEqual(sc1.text, "{H_{2}O} over {CO_{2}}")

        sz1 = next(ch for ch in eq1 if _local_tag(ch) == "sz")
        self.assertGreaterEqual(int(sz1.get("height", "0")), 2000)

        baseline1 = int(eq1.get("baseLine", "0"))
        self.assertGreaterEqual(baseline1, 50)
        self.assertLessEqual(baseline1, 60)

        pos1 = next(ch for ch in eq1 if _local_tag(ch) == "pos")
        self.assertEqual(pos1.get("treatAsChar"), "1")
        self.assertEqual(pos1.get("vertRelTo"), "PARA")
        self.assertEqual(pos1.get("horzRelTo"), "PARA")
        self.assertEqual(pos1.get("flowWithText"), "1")

    def test_equation_ids_unique_across_batch_operations(self) -> None:
        doc = create_minimal_hwpx_document()
        apply_hwpx_operations(
            doc,
            [
                {"op": "set_paragraph_text", "target_id": "p_0", "new_text": "$$x=1$$"},
                {"op": "insert_paragraph_after", "target_id": "p_0", "text": "$$y=2$$"},
                {
                    "op": "insert_exam_question",
                    "target_id": "p_1",
                    "number": 1,
                    "question": r"$$\frac{a}{b}$$ 를 구하시오.",
                    "bogi": ["ㄱ. $$a=1$$", "ㄴ. $$b=2$$"],
                    "choices": ["① $$1$$", "② $$2$$"],
                },
            ],
        )
        sec_name = doc.section_names()[0]
        root = _parse_xml(doc.entries[sec_name])
        eq_ids = [n.get("id") for n in root.iter() if _local_tag(n) == "equation"]
        self.assertEqual(len(eq_ids), 7)
        self.assertEqual(len(set(eq_ids)), 7)


class TestExtractionReviewDialogSave(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def test_save_and_close_commits_uncommitted_editor_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            excel_path = Path(tmp) / "review_test.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "추출 결과"
            ws.append(["학번", "이름", "1번", "누락 항목", "원본파일명", "검토 상태"])
            ws.append(["10101", "홍길동", "원래값", "", "student1.pdf", "검토 필요"])
            wb.save(excel_path)

            items = [
                {
                    "student_id": "10101",
                    "name": "홍길동",
                    "file_name": "student1.pdf",
                    "file_path": "",
                    "field_name": "1번",
                    "extracted_value": "원래값",
                    "source": "검증",
                    "reason": "확인 필요",
                    "status": "pending",
                    "raw_text_preview": "수정된값",
                }
            ]

            dlg = ExtractionReviewDialog(excel_path=excel_path, review_items=items)
            dlg.txt_edit_value.setPlainText("  수정된값  ")
            dlg._on_save_and_close()

            self.assertTrue(dlg.has_saved_changes)
            self.assertEqual(dlg.items[0]["extracted_value"], "수정된값")
            self.assertEqual(dlg.items[0]["status"], "reviewed")

            wb_after = openpyxl.load_workbook(excel_path)
            ws_after = wb_after["추출 결과"]
            self.assertEqual(ws_after.cell(row=2, column=3).value, "수정된값")


if __name__ == "__main__":
    unittest.main()
