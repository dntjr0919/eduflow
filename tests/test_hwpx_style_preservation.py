import json
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.hwpx_agent_service import (  # noqa: E402
    ET,
    HH_NS,
    HP_NS,
    _compact_dom_map_for_prompt,
    _ensure_script_char_pr,
    _ensure_styled_char_pr,
    _ensure_styled_para_pr,
    _find_child,
    _local_tag,
    _parse_xml,
    _qname,
    _serialize_xml,
    apply_hwpx_operations,
    build_hwpx_agent_prompt,
    build_hwpx_code_mode_prompt,
    create_minimal_hwpx_document,
    execute_hwpx_hybrid_edit,
    extract_hwpx_dom_map,
    should_use_hwpx_code_mode,
)


def _add_font_to_header(doc, font_id: str, face_name: str) -> None:
    header_name = next(n for n in doc.entries if n.lower() == "contents/header.xml")
    root = _parse_xml(doc.entries[header_name])
    for ff in root.iter():
        if _local_tag(ff) == "fontface":
            ET.SubElement(
                ff,
                _qname(HH_NS, "font"),
                {"id": font_id, "face": face_name, "type": "TTF", "isEmbedded": "0"},
            )
            ff.set("fontCnt", str(len([c for c in ff if _local_tag(c) == "font"])))
    doc.entries[header_name] = _serialize_xml(root)


def _add_char_pr(
    doc,
    cid: str,
    *,
    height: str = "1000",
    text_color: str = "#000000",
    font_ref_id: str = "0",
    bold: bool = False,
    italic: bool = False,
    ratio: str = "100",
    spacing: str = "0",
) -> None:
    header_name = next(n for n in doc.entries if n.lower() == "contents/header.xml")
    root = _parse_xml(doc.entries[header_name])
    char_props = next(n for n in root.iter() if _local_tag(n) == "charProperties")
    cp = ET.SubElement(
        char_props,
        _qname(HH_NS, "charPr"),
        {
            "id": cid,
            "height": height,
            "textColor": text_color,
            "shadeColor": "none",
            "useFontSpace": "0",
            "useKerning": "0",
            "symMark": "NONE",
            "borderFillIDRef": "1",
        },
    )
    ET.SubElement(
        cp,
        _qname(HH_NS, "fontRef"),
        {
            "hangul": font_ref_id,
            "latin": font_ref_id,
            "hanja": font_ref_id,
            "japanese": font_ref_id,
            "other": font_ref_id,
            "symbol": font_ref_id,
            "user": font_ref_id,
        },
    )
    ET.SubElement(
        cp,
        _qname(HH_NS, "ratio"),
        {k: ratio for k in ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user")},
    )
    ET.SubElement(
        cp,
        _qname(HH_NS, "spacing"),
        {k: spacing for k in ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user")},
    )
    if italic:
        ET.SubElement(cp, _qname(HH_NS, "italic"))
    if bold:
        ET.SubElement(cp, _qname(HH_NS, "bold"))
    char_props.set("itemCnt", str(len([c for c in char_props if _local_tag(c) == "charPr"])))
    doc.entries[header_name] = _serialize_xml(root)


def _add_para_pr(
    doc,
    pid: str,
    *,
    align: str = "JUSTIFY",
    line_spacing_val: str = "160",
    tab_pr_id: str = "0",
) -> None:
    header_name = next(n for n in doc.entries if n.lower() == "contents/header.xml")
    root = _parse_xml(doc.entries[header_name])
    para_props = next(n for n in root.iter() if _local_tag(n) == "paraProperties")
    pr = ET.SubElement(
        para_props,
        _qname(HH_NS, "paraPr"),
        {"id": pid, "tabPrIDRef": tab_pr_id, "condense": "0"},
    )
    ET.SubElement(pr, _qname(HH_NS, "align"), {"horizontal": align, "vertical": "BASELINE"})
    ET.SubElement(
        pr,
        _qname(HH_NS, "breakSetting"),
        {"keepWithNext": "0", "pageBreakBefore": "0", "lineWrap": "BREAK"},
    )
    ET.SubElement(pr, _qname(HH_NS, "lineSpacing"), {"type": "PERCENT", "value": line_spacing_val, "unit": "HWPUNIT"})
    para_props.set("itemCnt", str(len([c for c in para_props if _local_tag(c) == "paraPr"])))
    doc.entries[header_name] = _serialize_xml(root)


class TestHwpxStylePreservation(unittest.TestCase):
    def test_ensure_styled_and_script_char_pr_never_mix_fonts_or_ratio(self) -> None:
        doc = create_minimal_hwpx_document()
        _add_font_to_header(doc, "10", "맑은 고딕")
        # id=101: 함초롬바탕(fontRef=1), 10pt, 검정, bold=True
        _add_char_pr(doc, "101", height="1000", text_color="#000000", font_ref_id="1", bold=True)
        # id=102: 맑은 고딕(fontRef=10), 10pt, 검정, bold=False
        _add_char_pr(doc, "102", height="1000", text_color="#000000", font_ref_id="10", bold=False)
        # id=103: 함초롬바탕(fontRef=1), 10pt, 검정, bold=True, ratio=90
        _add_char_pr(doc, "103", height="1000", text_color="#000000", font_ref_id="1", bold=True, ratio="90")

        # 맑은 고딕(id=102)을 bold=True로 요청 시 함초롬바탕(id=101)을 재사용하면 안 됨
        new_malgun_bold = _ensure_styled_char_pr(doc, "102", bold=True)
        self.assertNotIn(new_malgun_bold, ("101", "102", "103"))

        header_name = next(n for n in doc.entries if n.lower() == "contents/header.xml")
        root = _parse_xml(doc.entries[header_name])
        char_prs = {
            str(n.get("id")): n
            for n in root.iter()
            if _local_tag(n) == "charPr"
        }
        new_font_ref = _find_child(char_prs[new_malgun_bold], "fontRef")
        self.assertIsNotNone(new_font_ref)
        self.assertEqual(new_font_ref.get("hangul"), "10")

        # 첨자 역시 함초롬바탕 vs 맑은 고딕을 구분해야 함
        sub_batang = _ensure_script_char_pr(doc, "101", "subscript")
        sub_malgun = _ensure_script_char_pr(doc, "102", "subscript")
        self.assertNotEqual(sub_batang, sub_malgun)

        root2 = _parse_xml(doc.entries[header_name])
        char_prs2 = {str(n.get("id")): n for n in root2.iter() if _local_tag(n) == "charPr"}
        self.assertEqual(_find_child(char_prs2[sub_batang], "fontRef").get("hangul"), "1")
        self.assertEqual(_find_child(char_prs2[sub_malgun], "fontRef").get("hangul"), "10")

    def test_ensure_styled_para_pr_respects_line_spacing_and_tab_pr(self) -> None:
        doc = create_minimal_hwpx_document()
        # id=101: CENTER, lineSpacing=130%
        _add_para_pr(doc, "101", align="CENTER", line_spacing_val="130")
        # id=102: LEFT, lineSpacing=200%
        _add_para_pr(doc, "102", align="LEFT", line_spacing_val="200")

        # id=102(lineSpacing=200%)를 CENTER로 바꾸면 id=101(130%)을 재사용하지 않고 200%를 복제해야 함
        new_pid = _ensure_styled_para_pr(doc, "102", align="CENTER")
        self.assertNotIn(new_pid, ("0", "101", "102"))

        header_name = next(n for n in doc.entries if n.lower() == "contents/header.xml")
        root = _parse_xml(doc.entries[header_name])
        para_prs = {str(n.get("id")): n for n in root.iter() if _local_tag(n) == "paraPr"}
        ls_el = _find_child(para_prs[new_pid], "lineSpacing")
        self.assertIsNotNone(ls_el)
        self.assertEqual(ls_el.get("value"), "200")

    def test_dom_map_includes_referenced_styles_and_runs_and_compact_preserves_styles(self) -> None:
        doc = create_minimal_hwpx_document()
        _add_font_to_header(doc, "2", "맑은 고딕")
        _add_char_pr(doc, "5", height="1200", text_color="#1D4ED8", font_ref_id="2", bold=True)
        _add_char_pr(doc, "99", height="1500", text_color="#FF0000", font_ref_id="2")  # 미사용 스타일
        _add_para_pr(doc, "3", align="CENTER", line_spacing_val="180")

        sec_name = doc.section_names()[0]
        root = _parse_xml(doc.entries[sec_name])
        p_elem = next(n for n in root.iter() if _local_tag(n) == "p")
        p_elem.set("paraPrIDRef", "3")
        for ch in list(p_elem):
            if _local_tag(ch) == "run" and not any(_local_tag(s) in ("secPr", "ctrl") for s in ch):
                p_elem.remove(ch)

        r1 = ET.SubElement(p_elem, _qname(HP_NS, "run"), {"charPrIDRef": "5"})
        ET.SubElement(r1, _qname(HP_NS, "t")).text = "중요: "
        r2 = ET.SubElement(p_elem, _qname(HP_NS, "run"), {"charPrIDRef": "0"})
        ET.SubElement(r2, _qname(HP_NS, "t")).text = "안내사항"
        doc.entries[sec_name] = _serialize_xml(root)

        dom_map = extract_hwpx_dom_map(doc)
        self.assertIn("styles", dom_map)
        self.assertIn("5", dom_map["styles"]["charPr"])
        self.assertIn("0", dom_map["styles"]["charPr"])
        self.assertNotIn("99", dom_map["styles"]["charPr"])  # 미참조 스타일 제외 확인
        self.assertEqual(dom_map["styles"]["charPr"]["5"]["font"], "맑은 고딕")
        self.assertEqual(dom_map["styles"]["charPr"]["5"]["size_pt"], 12.0)
        self.assertEqual(dom_map["styles"]["charPr"]["5"]["color"], "#1D4ED8")
        self.assertTrue(dom_map["styles"]["charPr"]["5"]["bold"])
        self.assertIn("3", dom_map["styles"]["paraPr"])
        self.assertEqual(dom_map["styles"]["paraPr"]["3"]["line_spacing"], "180%")

        p0 = dom_map["blocks"][0]
        self.assertEqual(p0["paraPr"], "3")
        self.assertEqual(
            p0["runs"],
            [{"t": "중요: ", "charPr": "5"}, {"t": "안내사항", "charPr": "0"}],
        )

        # _compact_dom_map_for_prompt가 max_chars 축약 시에도 styles 보존 확인
        compacted = json.loads(_compact_dom_map_for_prompt(dom_map, max_chars=450))
        self.assertIn("styles", compacted)
        self.assertIn("5", compacted["styles"]["charPr"])

    def test_replace_text_set_paragraph_runs_update_cell_and_inferred_defaults(self) -> None:
        doc = create_minimal_hwpx_document()
        _add_font_to_header(doc, "2", "맑은 고딕")
        _add_char_pr(doc, "5", height="1100", text_color="#DC2626", font_ref_id="2", bold=True)
        _add_char_pr(doc, "7", height="1000", text_color="#000000", font_ref_id="2", bold=False)
        _add_para_pr(doc, "3", align="LEFT", line_spacing_val="180")
        _add_para_pr(doc, "4", align="CENTER", line_spacing_val="150")

        sec_name = doc.section_names()[0]
        root = _parse_xml(doc.entries[sec_name])
        p0 = next(n for n in root.iter() if _local_tag(n) == "p")
        p0.set("paraPrIDRef", "3")
        for ch in list(p0):
            if _local_tag(ch) == "run" and not any(_local_tag(s) in ("secPr", "ctrl") for s in ch):
                p0.remove(ch)
        r1 = ET.SubElement(p0, _qname(HP_NS, "run"), {"charPrIDRef": "5"})
        ET.SubElement(r1, _qname(HP_NS, "t")).text = "제목 "
        r2 = ET.SubElement(p0, _qname(HP_NS, "run"), {"charPrIDRef": "7"})
        ET.SubElement(r2, _qname(HP_NS, "t")).text = "2026년 안내"
        doc.entries[sec_name] = _serialize_xml(root)

        # 1) replace_text: 부분 치환 시 앞쪽 볼드/빨간 run(5)과 뒤쪽 run(7) 모두 보존
        apply_hwpx_operations(
            doc,
            [{"op": "replace_text", "target_id": "p_0", "old_text": "2026년", "new_text": "2027년"}],
        )
        root_after = _parse_xml(doc.entries[sec_name])
        p0_after = next(n for n in root_after.iter() if _local_tag(n) == "p")
        non_ctrl_runs = [
            ch for ch in p0_after
            if _local_tag(ch) == "run" and not any(_local_tag(s) in ("secPr", "ctrl") for s in ch)
        ]
        self.assertEqual(non_ctrl_runs[0].get("charPrIDRef"), "5")
        self.assertEqual(non_ctrl_runs[1].get("charPrIDRef"), "7")
        self.assertEqual(p0_after.get("paraPrIDRef"), "3")

        # 2) set_paragraph_runs: paraPr 생략 시 기존 paraPrIDRef("3") 및 secPr 보존
        apply_hwpx_operations(
            doc,
            [
                {
                    "op": "set_paragraph_runs",
                    "target_id": "p_0",
                    "runs": [{"t": "새 제목 ", "charPr": "5"}, {"t": "새 본문", "charPr": "7"}],
                }
            ],
        )
        root_runs = _parse_xml(doc.entries[sec_name])
        p0_runs = next(n for n in root_runs.iter() if _local_tag(n) == "p")
        self.assertEqual(p0_runs.get("paraPrIDRef"), "3")
        has_sec_pr = any(_local_tag(n) == "secPr" for n in p0_runs.iter())
        self.assertTrue(has_sec_pr)
        plain_runs = [
            ch for ch in p0_runs
            if _local_tag(ch) == "run" and not any(_local_tag(s) in ("secPr", "ctrl") for s in ch)
        ]
        self.assertEqual([(r.get("charPrIDRef"), r[0].text) for r in plain_runs], [("5", "새 제목 "), ("7", "새 본문")])

        # 3) append_paragraph / append_table: 문서 최빈 charPr/paraPr 추론 확인
        apply_hwpx_operations(
            doc,
            [
                {"op": "append_paragraph", "text": "추가 문단"},
                {"op": "append_table", "rows": [["1행1열\n1행1열 둘째줄"]]},
            ],
        )
        dom_mid = extract_hwpx_dom_map(doc)
        self.assertEqual(dom_mid["blocks"][1]["paraPr"], "3")
        self.assertIn(dom_mid["blocks"][1]["charPr"], ("5", "7"))

        # 4) 표 셀에 다중 문단(1줄: paraPr=4, charPr=5 / 2줄: paraPr=3, charPr=7) 구성 후 update_cell
        sec_root = _parse_xml(doc.entries[sec_name])
        tc = next(n for n in sec_root.iter() if _local_tag(n) == "tc")
        sub_list = _find_child(tc, "subList")
        for old_p in [c for c in list(sub_list) if _local_tag(c) == "p"]:
            sub_list.remove(old_p)
        p_line1 = ET.SubElement(sub_list, _qname(HP_NS, "p"), {"id": "9001", "paraPrIDRef": "4"})
        ET.SubElement(ET.SubElement(p_line1, _qname(HP_NS, "run"), {"charPrIDRef": "5"}), _qname(HP_NS, "t")).text = "첫째"
        p_line2 = ET.SubElement(sub_list, _qname(HP_NS, "p"), {"id": "9002", "paraPrIDRef": "3"})
        ET.SubElement(ET.SubElement(p_line2, _qname(HP_NS, "run"), {"charPrIDRef": "7"}), _qname(HP_NS, "t")).text = "둘째"
        doc.entries[sec_name] = _serialize_xml(sec_root)

        apply_hwpx_operations(
            doc,
            [{"op": "update_cell", "target_id": "tbl_0_r0_c0", "new_text": "수정1\n수정2\n수정3"}],
        )
        sec_root2 = _parse_xml(doc.entries[sec_name])
        tc2 = next(n for n in sec_root2.iter() if _local_tag(n) == "tc")
        ps2 = [c for c in _find_child(tc2, "subList") if _local_tag(c) == "p"]
        self.assertEqual([p.get("paraPrIDRef") for p in ps2], ["4", "3", "3"])
        self.assertEqual(
            [next(c for c in p if _local_tag(c) == "run").get("charPrIDRef") for p in ps2],
            ["5", "7", "7"],
        )

    def test_prompt_mentions_styles_runs_and_replace_text_priority(self) -> None:
        doc = create_minimal_hwpx_document()
        dom_map = extract_hwpx_dom_map(doc)
        sys_prompt, _ = build_hwpx_agent_prompt(dom_map, "문서 수정")
        self.assertIn("set_paragraph_runs", sys_prompt)
        self.assertIn("reorder_blocks", sys_prompt)
        self.assertIn("styles.charPr", sys_prompt)
        self.assertIn("replace_text", sys_prompt)

    def test_large_exam_dom_map_no_middle_truncation_and_compaction_order(self) -> None:
        doc = create_minimal_hwpx_document()
        sec_name = doc.section_names()[0]
        root = _parse_xml(doc.entries[sec_name])

        # 320개 문단(빈 문단 + 다중 run 문항 문단) 생성
        for i in range(1, 321):
            p = ET.SubElement(
                root,
                _qname(HP_NS, "p"),
                {"id": str(10000 + i), "paraPrIDRef": "0", "styleIDRef": "0"},
            )
            if i % 5 == 0:
                ET.SubElement(ET.SubElement(p, _qname(HP_NS, "run"), {"charPrIDRef": "0"}), _qname(HP_NS, "t")).text = ""
            else:
                r1 = ET.SubElement(p, _qname(HP_NS, "run"), {"charPrIDRef": "0"})
                ET.SubElement(r1, _qname(HP_NS, "t")).text = f"문항 {i}. "
                r2 = ET.SubElement(p, _qname(HP_NS, "run"), {"charPrIDRef": "0"})
                ET.SubElement(r2, _qname(HP_NS, "t")).text = f"다음 설명 중 옳은 것은? (배점 {i % 5 + 1}점)"
        doc.entries[sec_name] = _serialize_xml(root)

        dom_map = extract_hwpx_dom_map(doc)
        self.assertGreaterEqual(dom_map["block_count"], 320)

        compacted_default = json.loads(_compact_dom_map_for_prompt(dom_map))
        self.assertFalse(compacted_default.get("truncated", False))
        all_texts = [b.get("text", "") for b in compacted_default["blocks"]]
        self.assertTrue(any("문항 319." in t for t in all_texts))
        self.assertFalse(any(b.get("id") == "_omitted" for b in compacted_default["blocks"]))

        # max_chars를 타이트하게 주었을 때 runs가 먼저 제거되고 문항 블록은 유지되는지 확인
        tight_json = _compact_dom_map_for_prompt(dom_map, max_chars=38000)
        tight = json.loads(tight_json)
        self.assertFalse(tight.get("truncated", False))
        self.assertTrue(any("문항 319." in b.get("text", "") for b in tight["blocks"]))

    def test_code_mode_routing_and_reorder_blocks_preserves_secpr_pic_equation(self) -> None:
        for inst in ("문항을 역순으로 배치해 주세요", "문항 순서 변경", "재배치", "문항 1~5번 이동", "셔플"):
            self.assertTrue(should_use_hwpx_code_mode(inst), f"Expected code_mode for: {inst}")

        doc = create_minimal_hwpx_document()
        code_sys, _ = build_hwpx_code_mode_prompt(extract_hwpx_dom_map(doc), "문항을 역순으로 배치")
        self.assertIn("sec_root.remove", code_sys)
        self.assertIn("secPr", code_sys)

        sec_name = doc.section_names()[0]
        root = _parse_xml(doc.entries[sec_name])
        # p0(secPr 포함)를 첫 번째 문항으로 두고, 그림(<hp:pic>)과 수식(<hp:equation>)이 포함된 문항 2개 구성
        p0 = next(n for n in root.iter() if _local_tag(n) == "p")
        r_q1 = ET.SubElement(p0, _qname(HP_NS, "run"), {"charPrIDRef": "0"})
        ET.SubElement(r_q1, _qname(HP_NS, "t")).text = "1. 첫 번째 문항"
        r_pic = ET.SubElement(p0, _qname(HP_NS, "run"), {"charPrIDRef": "0"})
        ET.SubElement(r_pic, _qname(HP_NS, "pic"), {"id": "777"})

        p1 = ET.SubElement(root, _qname(HP_NS, "p"), {"id": "20001", "paraPrIDRef": "0"})
        r_q2 = ET.SubElement(p1, _qname(HP_NS, "run"), {"charPrIDRef": "0"})
        ET.SubElement(r_q2, _qname(HP_NS, "t")).text = "2. 두 번째 문항 "
        r_eq = ET.SubElement(p1, _qname(HP_NS, "run"), {"charPrIDRef": "0"})
        eq = ET.SubElement(r_eq, _qname(HP_NS, "equation"), {"id": "888"})
        ET.SubElement(eq, _qname(HP_NS, "script")).text = "x^2 + y^2"
        doc.entries[sec_name] = _serialize_xml(root)

        # 1) reorder_blocks 연산자: p_1과 p_0 순서 교체 시 p_0의 secPr은 index 0에 보호되어야 함
        touched = apply_hwpx_operations(
            doc,
            [{"op": "reorder_blocks", "groups": [["p_1"], ["p_0"]]}],
        )
        self.assertEqual(touched, ["p_1", "p_0"])
        dom_reordered = extract_hwpx_dom_map(doc)
        self.assertIn("2. 두 번째 문항", dom_reordered["blocks"][0]["text"])
        self.assertIn("1. 첫 번째 문항", dom_reordered["blocks"][1]["text"])

        root_reordered = _parse_xml(doc.entries[sec_name])
        ps_reordered = [ch for ch in root_reordered if _local_tag(ch) == "p"]
        # index 0 문단에 secPr이 보존되고, 이동된 1번 문단에는 pic이 그대로 보존되어야 함
        self.assertTrue(any(_local_tag(n) == "secPr" for n in ps_reordered[0].iter()))
        self.assertTrue(any(_local_tag(n) == "pic" for n in ps_reordered[1].iter()))
        self.assertTrue(any(_local_tag(n) == "equation" for n in ps_reordered[0].iter()))

        # 2) execute_hwpx_hybrid_edit가 "역순" 지시 시 code_mode로 라우팅되는지 확인
        def _fake_llm(_usr: str, _sys: str) -> str:
            return (
                "```python\n"
                "def edit_hwpx(sec_root, helpers):\n"
                "    ps = [ch for ch in list(sec_root) if helpers['local_tag'](ch) == 'p']\n"
                "    if len(ps) >= 2:\n"
                "        p0, p1 = ps[0], ps[1]\n"
                "        sec_root.remove(p1)\n"
                "        sec_root.insert(0, p1)\n"
                "```"
            )

        # 첫 문단에 secPr이 있을 때 직접 code_mode로 p0<->p1을 바꾸면 secPr 검증이 동작하므로,
        # 고정 헤더 p0(secPr) 뒤에 두 문항을 둔 표준 구조로 code_mode 검증
        doc2 = create_minimal_hwpx_document()
        s2 = doc2.section_names()[0]
        r2 = _parse_xml(doc2.entries[s2])
        pa = ET.SubElement(r2, _qname(HP_NS, "p"), {"id": "30001", "paraPrIDRef": "0"})
        ET.SubElement(ET.SubElement(pa, _qname(HP_NS, "run"), {"charPrIDRef": "0"}), _qname(HP_NS, "t")).text = "1번"
        pb = ET.SubElement(r2, _qname(HP_NS, "p"), {"id": "30002", "paraPrIDRef": "0"})
        ET.SubElement(ET.SubElement(pb, _qname(HP_NS, "run"), {"charPrIDRef": "0"}), _qname(HP_NS, "t")).text = "2번"
        doc2.entries[s2] = _serialize_xml(r2)

        def _fake_llm_reverse(_usr: str, _sys: str) -> str:
            return (
                "```python\n"
                "def edit_hwpx(sec_root, helpers):\n"
                "    ps = [ch for ch in list(sec_root) if helpers['local_tag'](ch) == 'p']\n"
                "    q_ps = ps[1:]\n"
                "    for p in q_ps:\n"
                "        sec_root.remove(p)\n"
                "    for p in reversed(q_ps):\n"
                "        sec_root.append(p)\n"
                "```"
            )

        res = execute_hwpx_hybrid_edit(doc2, "문항을 역순으로 배치해 주세요", _fake_llm_reverse)
        self.assertEqual(res["mode"], "code_mode")
        self.assertTrue(res["success"])
        dom2 = extract_hwpx_dom_map(doc2)
        self.assertEqual([b["text"] for b in dom2["blocks"][-2:]], ["2번", "1번"])


if __name__ == "__main__":
    unittest.main()
