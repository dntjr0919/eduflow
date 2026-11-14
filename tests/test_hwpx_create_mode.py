"""
새 HWPX 양식 생성 기능 (방식 1: 프롬프트 단독 / 방식 2: 참고 문서 파싱 + 분할 이어붙이기)
단위·통합 테스트 (tests/test_hwpx_create_mode.py).
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.hwpx_agent_service import (
    HwpxDocument,
    apply_hwpx_operations,
    apply_single_hwpx_operation,
    build_hwpx_create_prompt,
    create_minimal_hwpx_document,
    extract_hwpx_dom_map,
    extract_hwpx_reference_chunks,
    render_hwpx_to_html,
)
from utils.path_helper import get_resource_path


def _write_minimal_docx(dst: Path, paragraphs: list[str], table_rows: list[list[str]]) -> Path:
    """외부 python-docx 없이 표준 OpenXML .docx 테스트 파일을 생성합니다."""
    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    p_xml = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    tr_xml = ""
    for row in table_rows:
        tcs = "".join(
            f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row
        )
        tr_xml += f"<w:tr>{tcs}</w:tr>"
    tbl_xml = f"<w:tbl>{tr_xml}</w:tbl>" if tr_xml else ""
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{w_ns}">'
        f"<w:body>{p_xml}{tbl_xml}</w:body>"
        "</w:document>"
    ).encode("utf-8")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ).encode("utf-8")
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    ).encode("utf-8")

    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", doc_xml)
    return dst


class TestHwpxCreateOpsAndZipIntegrity(unittest.TestCase):
    """요구사항 1: append_paragraph, append_table, insert_table_after, page_break & OCF 무결성 검증."""

    def test_append_paragraph_table_page_break_save_reload_and_html(self) -> None:
        doc = create_minimal_hwpx_document(title="새 양식", empty=True)
        self.assertEqual(extract_hwpx_dom_map(doc)["block_count"], 0)

        ops = [
            {
                "op": "append_paragraph",
                "text": "2026학년도 과학 탐구 보고서 양식",
                "bold": True,
                "font_size_pt": 16.0,
                "align": "CENTER",
            },
            {
                "op": "append_paragraph",
                "text": "반응식: H_{2}O + $$x^2 + y^2 = r^2$$ 및 Fe^{3+} 이온 확인",
                "bold": False,
                "font_size_pt": 10.5,
                "align": "RIGHT",
            },
            {
                "op": "append_table",
                "headers": ["항목", "세부 내용"],
                "rows": [
                    ["실험 목표", "중화 적정 곡선과 H_{3}O^{+} 농도 계산"],
                    ["핵심 수식", "$$pH = -log [H_{3}O^{+}]$$"],
                ],
                "col_widths_ratio": [1, 3],
            },
            {"op": "page_break"},
            {
                "op": "insert_table_after",
                "target_id": "p_0",
                "headers": ["학년/반", "이름"],
                "rows": [["2학년 3반", "홍길동"]],
                "col_widths_ratio": [1, 2],
            },
        ]
        touched = apply_hwpx_operations(doc, ops)
        self.assertGreaterEqual(len(touched), 4)
        self.assertTrue({"p_0", "p_1", "p_2", "tbl_0"}.issubset(set(touched)))

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "created_form.hwpx"
            doc.save(out_path)

            # OCF ZIP 규격 확인: mimetype이 첫 번째 엔트리 + ZIP_STORED 무압축 + 필수 OWPML 파일 포함
            with zipfile.ZipFile(out_path, "r") as zf:
                infos = zf.infolist()
                self.assertGreater(len(infos), 0)
                self.assertEqual(infos[0].filename, "mimetype")
                self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
                self.assertEqual(zf.read("mimetype"), b"application/hwp+zip")

                names = set(zf.namelist())
                for req_entry in (
                    "version.xml",
                    "settings.xml",
                    "META-INF/container.xml",
                    "META-INF/manifest.xml",
                    "Contents/content.hpf",
                    "Contents/header.xml",
                    "Contents/section0.xml",
                ):
                    self.assertIn(req_entry, names)

                header_raw = zf.read("Contents/header.xml").decode("utf-8")
                self.assertNotIn("_eduflow_", header_raw)
                header_root = ET.fromstring(header_raw.encode("utf-8"))
                bf_nodes = [el for el in header_root.iter() if el.tag.endswith("}borderFill")]
                self.assertGreaterEqual(len(bf_nodes), 2)

                # 표준 XML 및 <hp:secPr>, <hp:tbl>/<hp:tc> 필수 속성·자식 순서 검사
                sec_root = ET.fromstring(zf.read("Contents/section0.xml"))
                top_ps = [ch for ch in sec_root if ch.tag.endswith("}p")]
                self.assertGreaterEqual(len(top_ps), 1)
                self.assertFalse(any(ch.tag.endswith("}tbl") for ch in sec_root))
                self.assertTrue(any(el.tag.endswith("}secPr") for el in top_ps[0].iter()))
                self.assertTrue(any(el.tag.endswith("}ctrl") for el in top_ps[0].iter()))

                tbl_elems = [el for el in sec_root.iter() if el.tag.endswith("}tbl")]
                self.assertEqual(len(tbl_elems), 2)
                main_tbl = tbl_elems[1]  # append_table (p_0 뒤 insert_table_after가 앞)
                self.assertEqual(main_tbl.get("rowCnt"), "3")
                self.assertEqual(main_tbl.get("colCnt"), "2")
                for tbl_attr in (
                    "id",
                    "zOrder",
                    "numberingType",
                    "textWrap",
                    "textFlow",
                    "lock",
                    "dropcapstyle",
                    "pageBreak",
                    "repeatHeader",
                    "cellSpacing",
                    "borderFillIDRef",
                ):
                    self.assertIsNotNone(main_tbl.get(tbl_attr), msg=f"missing tbl attr {tbl_attr}")

                # 사용된 borderFillIDRef가 header.xml의 4면 SOLID borderFill을 가리키는지 검증
                used_bf_id = main_tbl.get("borderFillIDRef")
                matched_bf = next((bf for bf in bf_nodes if bf.get("id") == used_bf_id), None)
                self.assertIsNotNone(matched_bf)
                for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
                    side_el = next((c for c in matched_bf if c.tag.endswith("}" + side)), None)
                    self.assertIsNotNone(side_el)
                    self.assertEqual(side_el.get("type"), "SOLID")

                tbl_child_tags = [c.tag.split("}")[-1] for c in main_tbl]
                self.assertEqual(tbl_child_tags[:4], ["sz", "pos", "outMargin", "inMargin"])
                pos_el = next(c for c in main_tbl if c.tag.endswith("}pos"))
                self.assertEqual(pos_el.get("treatAsChar"), "1")

                first_tr = next(ch for ch in main_tbl if ch.tag.endswith("}tr"))
                tcs = [ch for ch in first_tr if ch.tag.endswith("}tc")]
                self.assertEqual(len(tcs), 2)
                tc_tags = [c.tag.split("}")[-1] for c in tcs[0]]
                self.assertEqual(tc_tags, ["subList", "cellAddr", "cellSpan", "cellSz", "cellMargin"])
                for tc_attr in ("name", "header", "hasMargin", "protect", "editable", "dirty", "borderFillIDRef"):
                    self.assertIsNotNone(tcs[0].get(tc_attr), msg=f"missing tc attr {tc_attr}")
                w0 = int(next(c for c in tcs[0] if c.tag.endswith("}cellSz")).get("width"))
                w1 = int(next(c for c in tcs[1] if c.tag.endswith("}cellSz")).get("width"))
                self.assertAlmostEqual(w1 / max(1, w0), 3.0, delta=0.1)

                pb_ps = [
                    el
                    for el in sec_root.iter()
                    if el.tag.endswith("}p") and el.get("pageBreak") == "1"
                ]
                self.assertEqual(len(pb_ps), 1)

            reloaded = HwpxDocument.load(out_path)
            dom_map = extract_hwpx_dom_map(reloaded)
            # p_0(제목), tbl_0(p_0 뒤 삽입), p_1(반응식), tbl_1(본문표), p_2(page_break)
            self.assertEqual(dom_map["block_count"], 5)
            self.assertEqual(dom_map["blocks"][0]["text"], "2026학년도 과학 탐구 보고서 양식")
            self.assertEqual(dom_map["blocks"][1]["type"], "table")
            self.assertTrue(dom_map["blocks"][4].get("page_break"))

            rendered_html = render_hwpx_to_html(reloaded, highlighted_ids={"p_0", "tbl_1"})
            self.assertIn("font-weight:bold;", rendered_html)
            self.assertIn("text-align:center;", rendered_html)
            self.assertIn("text-align:right;", rendered_html)
            self.assertIn("font-size:16pt;", rendered_html)
            self.assertIn("[수식: x^2 + y^2 = r^2]", rendered_html)
            self.assertIn("<sub>2</sub>", rendered_html)
            self.assertIn("<sup>3+</sup>", rendered_html)
            self.assertIn("페이지 나누기", rendered_html)
            self.assertIn("중화 적정 곡선", rendered_html)

    def test_blank_template_resource_and_prompt1_form_structure(self) -> None:
        tmpl_path = Path(get_resource_path("assets/blank_template.hwpx"))
        self.assertTrue(tmpl_path.is_file())
        build_exe_src = (ROOT_DIR / "build_exe.py").read_text(encoding="utf-8")
        self.assertIn("assets/blank_template.hwpx;assets", build_exe_src)

        # Prompt-1 시나리오: 제목 + 학번/이름 표 + 수식 포함 새 문서 생성 및 저장 구조 검증
        doc = create_minimal_hwpx_document(title="화학 수행평가", empty=True)
        ops = [
            {
                "op": "append_paragraph",
                "text": "2026학년도 화학 탐구 보고서",
                "bold": True,
                "font_size_pt": 16.0,
                "align": "CENTER",
            },
            {
                "op": "append_table",
                "headers": ["학번", "이름"],
                "rows": [["20301", "김철수"]],
                "col_widths_ratio": [1, 1],
            },
            {
                "op": "append_paragraph",
                "text": "중화 반응식: H_{3}O^{+} + OH^{-} -> 2H_{2}O, $$pH = -log[H_{3}O^{+}]$$",
            },
        ]
        apply_hwpx_operations(doc, ops)
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "prompt1_form.hwpx"
            doc.save(out_path)
            with zipfile.ZipFile(out_path, "r") as zf:
                infos = zf.infolist()
                self.assertEqual(infos[0].filename, "mimetype")
                self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
                for req_entry in (
                    "META-INF/container.xml",
                    "META-INF/manifest.xml",
                    "Contents/content.hpf",
                    "version.xml",
                    "settings.xml",
                    "Contents/header.xml",
                    "Contents/section0.xml",
                ):
                    self.assertIn(req_entry, zf.namelist())
                ET.fromstring(zf.read("Contents/content.hpf"))
                ET.fromstring(zf.read("META-INF/container.xml"))
                ET.fromstring(zf.read("Contents/header.xml"))
                sec_root = ET.fromstring(zf.read("Contents/section0.xml"))
                self.assertTrue(any(el.tag.endswith("}secPr") for el in sec_root.iter()))
                self.assertEqual(len([el for el in sec_root.iter() if el.tag.endswith("}tbl")]), 1)

            reloaded = HwpxDocument.load(out_path)
            rendered = render_hwpx_to_html(reloaded)
            self.assertIn("2026학년도 화학 탐구 보고서", rendered)
            self.assertIn("20301", rendered)
            self.assertIn("[수식: pH = -log[H_{3}O^{+}]]", rendered)

    def test_insert_exam_question_on_empty_document(self) -> None:
        doc = create_minimal_hwpx_document(title="", empty=True)
        touched = apply_single_hwpx_operation(
            doc,
            {
                "op": "insert_exam_question",
                "number": 1,
                "score": "4점",
                "question": "다음 중 물 분자의 화학식으로 옳은 것은?",
                "bogi": ["ㄱ. H_{2}O이다."],
                "choices": ["① H_{2}O", "② CO_{2}"],
                "answer": "①",
                "explanation": "물은 수소 2개와 산소 1개로 이루어집니다.",
            },
        )
        self.assertTrue(touched)
        dom = extract_hwpx_dom_map(doc)
        self.assertGreaterEqual(dom["block_count"], 4)
        self.assertEqual(len(doc.exam_answers), 1)
        sec_root = ET.fromstring(doc.entries["Contents/section0.xml"])
        self.assertTrue(any(el.tag.endswith("}secPr") for el in sec_root.iter()))
        self.assertFalse(any(ch.tag.endswith("}tbl") for ch in sec_root))


class TestHwpxReferenceParserAndChunking(unittest.TestCase):
    """요구사항 2: extract_hwpx_reference_chunks 다중 포맷 파싱 및 분할 검증."""

    def test_parse_hwpx_docx_xlsx_txt_and_chunk_split(self) -> None:
        import openpyxl  # type: ignore

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # 1) .hwpx 참고 파일
            ref_hwpx_doc = create_minimal_hwpx_document(title="기존 학사 일정 안내")
            apply_hwpx_operations(
                ref_hwpx_doc,
                [
                    {
                        "op": "append_table",
                        "headers": ["월", "행사명"],
                        "rows": [["3월", "입학식 및 학부모 총회"]],
                    }
                ],
            )
            hwpx_path = tmp / "ref.hwpx"
            ref_hwpx_doc.save(hwpx_path)

            # 2) .docx 참고 파일
            docx_path = _write_minimal_docx(
                tmp / "ref.docx",
                paragraphs=["DOCX 참고 문단: 수행평가 기준 안내"],
                table_rows=[["영역", "배점"], ["실험 보고서", "40점"]],
            )

            # 3) .xlsx 참고 파일
            xlsx_path = tmp / "ref.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "평가기준"
            ws.append(["문항번호", "성취기준", "배점"])
            ws.append(["1번", "[10통과01-01] 빅뱅 우주론", "5점"])
            wb.save(xlsx_path)
            wb.close()

            # 4) .txt 참고 파일 (CP949 + UTF-8 혼합 시나리오)
            txt_cp949 = tmp / "ref_cp949.txt"
            txt_cp949.write_bytes("안내 사항: CP949 인코딩 문서 본문입니다.".encode("cp949"))

            chunks_single = extract_hwpx_reference_chunks(
                [hwpx_path, docx_path, xlsx_path, txt_cp949],
                max_chars_per_chunk=20000,
            )
            self.assertEqual(len(chunks_single), 1)
            merged = chunks_single[0]["content"]
            self.assertEqual(chunks_single[0]["part_index"], 1)
            self.assertEqual(chunks_single[0]["total_parts"], 1)
            self.assertIn("기존 학사 일정 안내", merged)
            self.assertIn("입학식 및 학부모 총회", merged)
            self.assertIn("DOCX 참고 문단: 수행평가 기준 안내", merged)
            self.assertIn("실험 보고서 | 40점", merged)
            self.assertIn("[시트: 평가기준]", merged)
            self.assertIn("[10통과01-01] 빅뱅 우주론 | 5점", merged)
            self.assertIn("CP949 인코딩 문서 본문입니다.", merged)

            # 긴 텍스트 입력 시 지정된 max_chars_per_chunk 기준으로 Part 1/N ~ N/N 분할 검증
            long_txt = tmp / "long_notes.txt"
            paragraphs = [
                f"[섹션 {i:02d}] 교직 실무 양식 참고 데이터 본문 항목 번호 {i}번 상세 설명 텍스트입니다."
                for i in range(1, 25)
            ]
            long_txt.write_text("\n\n".join(paragraphs), encoding="utf-8")

            split_chunks = extract_hwpx_reference_chunks([long_txt], max_chars_per_chunk=280)
            self.assertGreaterEqual(len(split_chunks), 3)
            total_n = len(split_chunks)
            for idx, item in enumerate(split_chunks, start=1):
                self.assertEqual(item["part_index"], idx)
                self.assertEqual(item["total_parts"], total_n)
                self.assertLessEqual(len(item["content"]), 280)

            rejoined = "\n\n".join(c["content"] for c in split_chunks)
            for i in range(1, 25):
                self.assertIn(f"[섹션 {i:02d}]", rejoined)


class TestHwpxSequentialCreateAndUiIntegration(unittest.TestCase):
    """요구사항 3: Part 1 + Part 2 순차 프롬프트·누적 적용 및 DocOcrTab/AiExecutionDock 연동 검증."""

    def test_sequential_part1_and_part2_accumulation(self) -> None:
        doc = create_minimal_hwpx_document(title="새 양식", empty=True)

        # Part 1 프롬프트 확인
        sys1, usr1 = build_hwpx_create_prompt(
            user_instruction="성적 평가 계획서 양식을 생성하세요.",
            reference_chunk="[Part 1 참고] 과목: 통합과학, 평가 항목 표 생성",
            part_index=1,
            total_parts=2,
            current_dom_map=extract_hwpx_dom_map(doc),
        )
        self.assertIn("append_paragraph", sys1)
        self.assertIn("append_table", sys1)
        self.assertIn("Part 1 / 2", usr1)

        part1_ops = [
            {
                "op": "append_paragraph",
                "text": "2026학년도 1학기 통합과학 평가 계획서",
                "bold": True,
                "font_size_pt": 15.0,
                "align": "CENTER",
            },
            {
                "op": "append_table",
                "headers": ["평가 영역", "반영 비율", "비고"],
                "rows": [["지필평가", "60%", "중간/기말 각 30%"]],
                "col_widths_ratio": [2, 1, 3],
            },
        ]
        apply_hwpx_operations(doc, part1_ops)

        dom_after_p1 = extract_hwpx_dom_map(doc)
        self.assertEqual(dom_after_p1["block_count"], 2)

        # Part 2 프롬프트에 Part 1 결과 DOM ID 전달 확인
        sys2, usr2 = build_hwpx_create_prompt(
            user_instruction="성적 평가 계획서 양식을 생성하세요.",
            reference_chunk="[Part 2 참고] 수행평가 40% 행 추가 및 하단 안내 문단 작성",
            part_index=2,
            total_parts=2,
            current_dom_map=dom_after_p1,
        )
        self.assertIn("Part 2/2", usr2)
        self.assertIn("tbl_0", usr2)
        self.assertIn("덮어쓰거나 삭제", usr2)

        part2_ops = [
            {
                "op": "add_table_row",
                "target_id": "tbl_0",
                "after_row": 1,
                "cells": ["수행평가", "40%", "실험 보고서 및 발표"],
            },
            {
                "op": "append_paragraph",
                "text": "※ 위 평가 기준은 교과협의회를 거쳐 확정되었습니다.",
                "bold": False,
                "font_size_pt": 10.0,
                "align": "LEFT",
            },
        ]
        apply_hwpx_operations(doc, part2_ops)

        final_dom = extract_hwpx_dom_map(doc)
        self.assertEqual(final_dom["block_count"], 3)
        self.assertEqual(final_dom["blocks"][0]["text"], "2026학년도 1학기 통합과학 평가 계획서")
        tbl_rows = final_dom["blocks"][1]["rows"]
        self.assertEqual(len(tbl_rows), 3)
        self.assertEqual(tbl_rows[1][0]["text"], "지필평가")
        self.assertEqual(tbl_rows[2][0]["text"], "수행평가")
        self.assertEqual(final_dom["blocks"][2]["text"], "※ 위 평가 기준은 교과협의회를 거쳐 확정되었습니다.")

    def test_tab_and_dock_create_mode_and_multipart_manual_flow(self) -> None:
        from PyQt6.QtWidgets import QApplication
        from core.base_worker import TaskRunner
        from ui.components.ai_execution_dock import AiExecutionDock
        from ui.tabs.tab_doc_ocr import DocOcrTab

        app = QApplication.instance() or QApplication([])
        _ = app

        runner = TaskRunner()
        tab = DocOcrTab(task_runner=runner)
        dock = AiExecutionDock(task_runner=runner)
        dock.register_target(
            target_id="hwpx_agent",
            label="4. HWPX AI 문서 에이전트",
            context_getter=lambda: tab.get_ai_context("hwpx_agent"),
            on_result=tab.apply_ai_dock_result,
        )
        tab.ai_context_changed.connect(dock.sync_from_context)

        # 1) [새 HWPX 양식 만들기] 버튼 클릭 -> 빈 .hwpx 전용 생성 모드 진입 확인
        tab.btn_hwpx_create_new.click()
        ctx = tab.get_ai_context("hwpx_agent")
        self.assertTrue(ctx["is_create_mode"])
        self.assertIsNone(tab._hwpx_original_hwp_path)
        self.assertEqual(ctx["dom_map"]["block_count"], 0)
        self.assertEqual(ctx["reference_files"], [])

        with tempfile.TemporaryDirectory() as tmpdir:
            ref_path = Path(tmpdir) / "long_ref.txt"
            lines = [
                f"[항목 {i}] 새 양식 생성을 위한 분할 테스트 참고 문단 내용입니다."
                for i in range(1, 26)
            ]
            ref_path.write_text("\n\n".join(lines), encoding="utf-8")

            # 참고 파일 첨부 -> 다중 파트 자동 계산 확인
            with patch("services.hwpx_agent_service.extract_hwpx_reference_chunks") as mock_extract:
                # ai_execution_dock이 임포트한 extract_hwpx_reference_chunks도 테스트 가능하도록 작은 청크 size 대신 직접 패치 또는 8000자 이상 파일 생성
                mock_extract.return_value = [
                    {"part_index": 1, "total_parts": 2, "content": "Part 1 참고 본문"},
                    {"part_index": 2, "total_parts": 2, "content": "Part 2 참고 본문"},
                ]
                with patch(
                    "ui.components.ai_execution_dock.extract_hwpx_reference_chunks",
                    side_effect=mock_extract,
                ):
                    tab._hwpx_reference_files = [str(ref_path)]
                    tab._update_hwpx_ref_summary()
                    tab.ai_context_changed.emit(tab.get_ai_context("hwpx_agent"))

                    dock.set_target("hwpx_agent", force=True)
                    dock.txt_hwpx_instruction.setPlainText("학사 보고서 양식을 만들어 주세요.")

                    self.assertFalse(dock.manual_batch_card.isHidden())
                    self.assertEqual(dock.combo_manual_batch.count(), 2)

                    p1_prompt = dock._build_current_manual_prompt()
                    self.assertIn("Part 1 / 2", p1_prompt)
                    self.assertIn("Part 1 참고 본문", p1_prompt)

                    # Part 1 응답 적용 후 자동으로 Part 2 프롬프트가 클립보드에 복사되는지 검증
                    dock.txt_manual_response.setPlainText(
                        '```json\n{"summary": "파트1 생성", "operations": ['
                        '{"op": "append_paragraph", "text": "제목1", "bold": true},'
                        '{"op": "append_table", "headers": ["항목", "값"], "rows": [["A", "1"]]}'
                        ']}\n```'
                    )
                    dock._on_manual_apply_and_next()

                    self.assertEqual(dock.combo_manual_batch.currentIndex(), 1)
                    clip_text = QApplication.clipboard().text()
                    self.assertIn("Part 2/2", clip_text)
                    self.assertIn("Part 2 참고 본문", clip_text)
                    self.assertIn("tbl_0", clip_text)

                    # Part 2 응답 적용 -> 최종 누적 검증
                    dock.txt_manual_response.setPlainText(
                        '```json\n{"summary": "파트2 추가", "operations": ['
                        '{"op": "append_paragraph", "text": "맺음말 문단"}'
                        ']}\n```'
                    )
                    dock._on_manual_apply_and_next()

                    final_dom = extract_hwpx_dom_map(tab._hwpx_doc)
                    self.assertEqual(final_dom["block_count"], 3)
                    self.assertEqual(final_dom["blocks"][0]["text"], "제목1")
                    self.assertEqual(final_dom["blocks"][1]["type"], "table")
                    self.assertEqual(final_dom["blocks"][2]["text"], "맺음말 문단")

    def test_api_worker_multipart_and_playwright_no_double_apply(self) -> None:
        from PyQt6.QtWidgets import QApplication
        from core.base_worker import TaskRunner
        from services.web_gemini_auto_worker import WebGeminiAutoWorker
        from ui.components.ai_execution_dock import (
            AiExecutionDock,
            _DockApiHwpxAgentWorker,
        )
        from ui.tabs.tab_doc_ocr import DocOcrTab

        app = QApplication.instance() or QApplication([])
        _ = app

        runner = TaskRunner()
        tab = DocOcrTab(task_runner=runner)
        dock = AiExecutionDock(task_runner=runner)
        dock.register_target(
            target_id="hwpx_agent",
            label="4. HWPX AI 문서 에이전트",
            context_getter=lambda: tab.get_ai_context("hwpx_agent"),
            on_result=tab.apply_ai_dock_result,
        )
        tab.btn_hwpx_create_new.click()
        dock.set_target("hwpx_agent", force=True)

        # 1) API 다중 파트 워커가 직전 DOM을 Part 2에 전달하고 part_completed를 순차 발생시키는지 검증
        seen_prompts: list[str] = []
        responses = [
            '```json\n{"summary": "파트1", "operations": ['
            '{"op": "append_paragraph", "text": "Part1 제목", "bold": true},'
            '{"op": "append_table", "headers": ["A", "B"], "rows": [["1", "2"]]}'
            ']}\n```',
            '```json\n{"summary": "파트2", "operations": ['
            '{"op": "add_table_row", "target_id": "tbl_0", "after_row": 1, "cells": ["3", "4"]},'
            '{"op": "append_paragraph", "text": "Part2 결론"}'
            ']}\n```',
        ]

        class _FakeClient:
            def generate(self, prompt: str, system_prompt: str = "") -> str:
                seen_prompts.append(f"{system_prompt}\n{prompt}")
                return responses[len(seen_prompts) - 1]

        emitted_parts: list[dict] = []
        with patch("ui.components.ai_execution_dock.MultiLlmClient", _FakeClient):
            worker = _DockApiHwpxAgentWorker(
                dom_map=extract_hwpx_dom_map(tab._hwpx_doc),
                instruction="새 2부작 보고서 작성",
                is_create_mode=True,
                ref_chunks=[
                    {"part_index": 1, "total_parts": 2, "content": "참고 내용 1부"},
                    {"part_index": 2, "total_parts": 2, "content": "참고 내용 2부"},
                ],
            )
            worker.part_completed.connect(lambda p: (emitted_parts.append(p), dock._on_api_hwpx_part_completed("hwpx_agent", p)))
            final_res = worker.execute()

        self.assertTrue(final_res["already_applied_parts"])
        self.assertEqual(len(emitted_parts), 2)
        self.assertEqual(len(seen_prompts), 2)
        self.assertIn("Part 1 / 2", seen_prompts[0])
        self.assertIn("Part 2/2", seen_prompts[1])
        self.assertIn("tbl_0", seen_prompts[1])

        api_dom = extract_hwpx_dom_map(tab._hwpx_doc)
        self.assertEqual(api_dom["block_count"], 3)
        self.assertEqual(api_dom["blocks"][0]["text"], "Part1 제목")
        self.assertEqual(len(api_dom["blocks"][1]["rows"]), 3)
        self.assertEqual(api_dom["blocks"][2]["text"], "Part2 결론")

        # 2) WebGeminiAutoWorker multi-single 배치 생성 + AiExecutionDock 중복 적용 방지 검증
        pw_worker = WebGeminiAutoWorker(
            excel_path=None,
            single_prompts=["P1", "P2"],
            prompt_builder=lambda idx, total, _prev: f"Built {idx}/{total}",
            require_login=False,
        )
        pw_batches = pw_worker._build_execution_batches()
        self.assertEqual(len(pw_batches), 2)
        self.assertTrue(all(b.get("is_single_prompt") for b in pw_batches))

        tab.btn_hwpx_create_new.click()
        dock._pw_single_batches_applied = False
        dock._on_pw_batch_completed(
            "hwpx_agent",
            {
                "is_single_prompt": True,
                "immediate": True,
                "batch_index": 1,
                "total_batches": 2,
                "parsed_json": {"summary": "PW1", "operations": [{"op": "append_paragraph", "text": "PW 문단"}]},
            },
        )
        # finished_result가 동일 parsed_json을 다시 들고 와도 중복 적용되지 않아야 함
        dock._on_pw_finished_result(
            "hwpx_agent",
            {
                "missing_playwright": False,
                "parsed_json": {"summary": "PW1", "operations": [{"op": "append_paragraph", "text": "PW 문단"}]},
            },
        )
        pw_dom = extract_hwpx_dom_map(tab._hwpx_doc)
        self.assertEqual(pw_dom["block_count"], 1)
        self.assertEqual(pw_dom["blocks"][0]["text"], "PW 문단")


if __name__ == "__main__":
    unittest.main()
