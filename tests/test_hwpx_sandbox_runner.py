import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lxml import etree as ET

from services.hwpx_agent_service import (
    HwpxDocument,
    apply_hwpx_operations,
    build_hwpx_code_mode_prompt,
    create_minimal_hwpx_document,
    execute_hwpx_hybrid_edit,
    extract_hwpx_dom_map,
    render_hwpx_to_html,
)
from services.hwpx_sandbox_runner import (
    SandboxSecurityError,
    run_hwpx_edit_code,
    validate_generated_code,
)


class TestHwpxSandboxRunner(unittest.TestCase):
    def test_block_forbidden_import_and_open(self) -> None:
        doc = create_minimal_hwpx_document(title="보안 테스트", empty=False)

        bad_import = (
            "import os\n"
            "def edit_hwpx(sec_root, helpers):\n"
            "    pass\n"
        )
        with self.assertRaises(SandboxSecurityError):
            validate_generated_code(bad_import)
        res_imp = run_hwpx_edit_code(doc, bad_import)
        self.assertFalse(res_imp["success"])
        self.assertEqual(res_imp["error_type"], "security")

        bad_open = (
            "def edit_hwpx(sec_root, helpers):\n"
            "    f = open('secret.txt', 'w')\n"
        )
        with self.assertRaises(SandboxSecurityError):
            validate_generated_code(bad_open)
        res_open = run_hwpx_edit_code(doc, bad_open)
        self.assertFalse(res_open["success"])
        self.assertEqual(res_open["error_type"], "security")

        bad_dunder = (
            "def edit_hwpx(sec_root, helpers):\n"
            "    x = sec_root.__class__\n"
        )
        with self.assertRaises(SandboxSecurityError):
            validate_generated_code(bad_dunder)

    def test_rollback_on_secpr_deletion(self) -> None:
        doc = create_minimal_hwpx_document(title="secPr 보호 테스트", empty=False)
        before_xml = doc.entries["Contents/section0.xml"]

        malicious_or_buggy_code = (
            "def edit_hwpx(sec_root, helpers):\n"
            "    for child in list(sec_root):\n"
            "        sec_root.remove(child)\n"
        )
        res = run_hwpx_edit_code(doc, malicious_or_buggy_code)
        self.assertFalse(res["success"])
        self.assertEqual(res["error_type"], "integrity")
        self.assertIn("secPr", res["error"])
        # doc.undo()로 원본 section0.xml이 그대로 복원되었는지 확인
        self.assertEqual(doc.entries["Contents/section0.xml"], before_xml)

    def test_clone_bogi_table_into_two_questions_without_duplicate_ids(self) -> None:
        doc = create_minimal_hwpx_document(title="2단 시험지 테스트", empty=True)
        # <보기> 표를 포함한 1번 문항 생성
        apply_hwpx_operations(
            doc,
            [
                {
                    "op": "insert_exam_question",
                    "target_id": "END",
                    "number": 1,
                    "score": "3점",
                    "question": "다음 <보기>의 설명 중 옳은 것은?",
                    "bogi": ["ㄱ. 물은 극성 분자이다."],
                    "choices": ["① ㄱ", "② ㄴ"],
                }
            ],
        )

        clone_code = (
            "def edit_hwpx(sec_root, helpers):\n"
            "    id_map = helpers['collect_dom_ids']()\n"
            "    _, _, tbl0 = id_map['tbl_0']\n"
            "    new_p2 = deepcopy(id_map['p_0'][2])\n"
            "    helpers['reassign_cloned_ids'](new_p2)\n"
            "    helpers['set_paragraph_rich_text'](new_p2, '2. **__옳지 않은__** 설명은? [3점]')\n"
            "    sec_root.append(new_p2)\n"
            "    new_tbl = deepcopy(tbl0)\n"
            "    helpers['reassign_cloned_ids'](new_tbl)\n"
            "    ps = [n for n in new_tbl.iter() if helpers['local_tag'](n) == 'p']\n"
            "    if len(ps) >= 2:\n"
            "        helpers['set_paragraph_rich_text'](ps[1], 'ㄱ. 이산화탄소는 무극성 분자이다.')\n"
            "    sec_root.append(helpers['wrap_table_in_paragraph'](new_tbl))\n"
        )
        res = run_hwpx_edit_code(doc, clone_code)
        self.assertTrue(res["success"], msg=res.get("error"))

        # 표가 2개이고 모든 tbl id가 고유한지 검증
        root = ET.fromstring(doc.entries["Contents/section0.xml"])
        tbl_ids = [
            node.get("id")
            for node in root.iter()
            if node.tag.split("}")[-1] == "tbl"
        ]
        self.assertEqual(len(tbl_ids), 2)
        self.assertEqual(len(set(tbl_ids)), 2)

        # OCF 저장 및 HTML 미리보기 호환성 확인
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "cloned_bogi.hwpx"
            saved = doc.save(out_path)
            self.assertTrue(saved.exists())
            reloaded = HwpxDocument.load(saved)
            html_out = render_hwpx_to_html(reloaded)
            self.assertIn("이산화탄소는 무극성 분자이다", html_out)

    def test_hybrid_prompt_and_self_correction_loop(self) -> None:
        doc = create_minimal_hwpx_document(title="Self-Correction", empty=False)
        dom_map = extract_hwpx_dom_map(doc)
        sys_p, usr_p = build_hwpx_code_mode_prompt(dom_map, "기존 서식 복제해 문항 2개 구성")
        self.assertIn("def edit_hwpx(sec_root, helpers):", sys_p)
        self.assertIn("deepcopy", sys_p)

        calls = []

        def fake_llm(u_prompt: str, s_prompt: str) -> str:
            calls.append(u_prompt)
            if len(calls) == 1:
                # 1차 시도: 금지된 import 포함 -> 보안 오류 유발
                return "```python\nimport os\ndef edit_hwpx(sec_root, helpers):\n    pass\n```"
            # 2차 시도: 정상 코드로 자가 수정
            return (
                "```python\n"
                "def edit_hwpx(sec_root, helpers):\n"
                "    id_map = helpers['collect_dom_ids']()\n"
                "    p0 = id_map['p_0'][2]\n"
                "    p_new = deepcopy(p0)\n"
                "    helpers['reassign_cloned_ids'](p_new)\n"
                "    helpers['set_paragraph_rich_text'](p_new, '2. 복제된 문항')\n"
                "    sec_root.append(p_new)\n"
                "```"
            )

        res = execute_hwpx_hybrid_edit(
            doc,
            "기존 서식 복제해 문항 2개 구성",
            fake_llm,
            dom_map=dom_map,
            max_retries=2,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["mode"], "code_mode")
        self.assertEqual(res["attempts"], 2)
        self.assertIn("이전 코드 실행 오류", calls[1])
        self.assertIn("2. 복제된 문항", res["html_preview"])


if __name__ == "__main__":
    unittest.main()
