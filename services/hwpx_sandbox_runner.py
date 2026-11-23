"""
HWPX AI 에이전트용 하이브리드 샌드박스 Python(lxml) 실행기 (services/hwpx_sandbox_runner.py).
- AST 정적 검사로 import / 금지 함수 / 던더(__) 속성 접근 차단
- 제한된 빌트인과 검증된 hwpx_agent_service 헬퍼만 주입하여 exec 실행
- 타임아웃(무한 루프 방지) 및 사후 XML/ID/secPr 무결성 검증 후 실패 시 즉시 doc.undo() 롤백
"""

from __future__ import annotations

import ast
from copy import deepcopy
import re
import threading
import traceback
from typing import Any, Dict, List, Optional, Set, Tuple

from lxml import etree as ET

from services.hwpx_agent_service import (
    HC_NS,
    HH_NS,
    HP_NS,
    HS_NS,
    HwpxDocument,
    _collect_dom_from_roots,
    _collect_section_dom,
    _compute_section_content_width,
    _extract_paragraph_plain_text,
    _find_child,
    _flush_sections,
    _get_target_section_root,
    _local_tag,
    _next_section_object_id,
    _qname,
    _reassign_cloned_ids_and_strip,
    _serialize_xml,
    _set_paragraph_rich_text,
    _strip_linesegarray,
    _strip_sec_pr_and_ctrl,
    _wrap_table_in_paragraph,
)


class SandboxSecurityError(Exception):
    """샌드박스 정적 보안 검사 위반 시 발생하는 예외."""


FORBIDDEN_FUNCTIONS: Set[str] = {
    "open",
    "eval",
    "exec",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "dir",
    "breakpoint",
    "input",
}

ALLOWED_BUILTINS: Dict[str, Any] = {
    "len": len,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "sorted": sorted,
    "reversed": reversed,
    "any": any,
    "all": all,
    "isinstance": isinstance,
    "Exception": Exception,
    "ValueError": ValueError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "TypeError": TypeError,
}

_PYTHON_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n([\s\S]*?)```", re.IGNORECASE)


def extract_python_code_from_llm_response(raw_response: str) -> str:
    """LLM 응답에서 ```python ... ``` 코드블록을 추출합니다."""
    txt = str(raw_response or "").strip()
    m = _PYTHON_FENCE_RE.search(txt)
    if m:
        return m.group(1).strip()
    return txt


def validate_generated_code(code: str) -> ast.Module:
    """
    생성된 Python 코드를 AST로 정적 검사합니다.
    - import / from import 전면 차단
    - 금지 함수(open, eval, exec, compile, __import__, getattr, setattr, delattr, globals, locals 등) 차단
    - '__'로 시작하는 던더 속성/식별자 접근 차단
    - edit_hwpx(sec_root, helpers) 함수 정의 여부 확인
    """
    if not str(code or "").strip():
        raise SandboxSecurityError("실행할 Python 코드가 비어 있습니다.")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxSecurityError(f"Python 구문 오류(SyntaxError): {exc}") from exc

    has_edit_fn = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise SandboxSecurityError(
                "보안 정책 위반: import / from import 문은 사용할 수 없습니다."
            )
        if isinstance(node, ast.FunctionDef) and node.name == "edit_hwpx":
            has_edit_fn = True
        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_FUNCTIONS or node.id.startswith("__"):
                raise SandboxSecurityError(
                    f"보안 정책 위반: 금지된 식별자 또는 함수 '{node.id}' 사용"
                )
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise SandboxSecurityError(
                    f"보안 정책 위반: 던더(__) 속성 접근 '{node.attr}'은 허용되지 않습니다."
                )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_FUNCTIONS:
                raise SandboxSecurityError(
                    f"보안 정책 위반: 금지된 함수 호출 '{node.func.id}()'"
                )

    if not has_edit_fn:
        raise SandboxSecurityError(
            "필수 함수 누락: 'def edit_hwpx(sec_root, helpers):' 함수를 정의해야 합니다."
        )
    return tree


def _build_sandbox_helpers(
    doc: HwpxDocument,
    section_roots: Dict[str, Any],
    id_counter: List[int],
) -> Dict[str, Any]:
    """샌드박스 내부 edit_hwpx에 주입할 검증된 HWPX 유틸리티 딕셔너리."""
    return {
        "HP_NS": HP_NS,
        "HS_NS": HS_NS,
        "HC_NS": HC_NS,
        "HH_NS": HH_NS,
        "qname": _qname,
        "local_tag": _local_tag,
        "find_child": _find_child,
        "set_paragraph_rich_text": lambda p_elem, text, base_char_pr_id=None: _set_paragraph_rich_text(
            doc,
            p_elem,
            text,
            section_roots=section_roots,
            id_counter=id_counter,
            base_char_pr_id=base_char_pr_id,
        ),
        "extract_paragraph_plain_text": _extract_paragraph_plain_text,
        "next_object_id": lambda: _next_section_object_id(section_roots, id_counter),
        "strip_sec_pr_and_ctrl": _strip_sec_pr_and_ctrl,
        "strip_linesegarray": _strip_linesegarray,
        "wrap_table_in_paragraph": lambda tbl_elem, para_pr_id="0", char_pr_id="0": _wrap_table_in_paragraph(
            tbl_elem,
            section_roots,
            id_counter,
            para_pr_id=para_pr_id,
            char_pr_id=char_pr_id,
        ),
        "reassign_cloned_ids": lambda elem: _reassign_cloned_ids_and_strip(
            elem, section_roots, id_counter
        ),
        "compute_content_width": lambda default_width=42520: _compute_section_content_width(
            section_roots, default_width=default_width
        ),
        "collect_dom_ids": lambda: _collect_dom_from_roots(section_roots)[0],
    }


def _verify_section_integrity(section_roots: Dict[str, Any]) -> None:
    """
    편집 후 HWPX 섹션 무결성 검증:
    1) <hp:secPr>가 1개 이상 보존되었는지 확인
    2) 모든 <hp:tbl>, <hp:rect>, <hp:equation>의 id 속성에 중복이 없는지 확인
    3) ET.tostring 후 ET.fromstring 재파싱이 성공하는지 확인
    """
    sec_pr_count = 0
    seen_obj_ids: Dict[str, str] = {}

    for sec_name, root in section_roots.items():
        for node in root.iter():
            t = _local_tag(node)
            if t == "secPr":
                sec_pr_count += 1
            elif t in ("tbl", "rect", "equation"):
                obj_id = str(node.get("id", "")).strip()
                if not obj_id:
                    raise ValueError(
                        f"무결성 검증 실패 ({sec_name}): <hp:{t}> 요소에 id 속성이 누락되었습니다."
                    )
                if obj_id in seen_obj_ids:
                    raise ValueError(
                        f"무결성 검증 실패 ({sec_name}): 중복된 객체 id='{obj_id}' "
                        f"(<hp:{seen_obj_ids[obj_id]}> 및 <hp:{t}>)"
                    )
                seen_obj_ids[obj_id] = t

        xml_bytes = _serialize_xml(root)
        try:
            ET.fromstring(xml_bytes)
        except Exception as exc:
            raise ValueError(f"무결성 검증 실패 ({sec_name}): XML 재파싱 오류 - {exc}") from exc

    if sec_pr_count < 1:
        raise ValueError("무결성 검증 실패: <hp:secPr> 섹션 설정 요소가 1개 이상 보존되어야 합니다.")


def run_hwpx_edit_code(
    doc: HwpxDocument,
    code: str,
    timeout_sec: float = 5.0,
) -> Dict[str, Any]:
    """
    생성된 Python 코드를 샌드박스에서 실행해 HwpxDocument를 편집하고 무결성을 검증합니다.
    실패 시 doc.undo()로 즉시 롤백하고 에러 상세를 반환합니다.
    """
    try:
        tree = validate_generated_code(code)
    except SandboxSecurityError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "security",
        }

    doc.push_undo_snapshot()
    try:
        _, _, section_roots = _collect_section_dom(doc)
        sec_info = _get_target_section_root(doc, section_roots)
        if sec_info is None:
            doc.undo()
            return {
                "success": False,
                "error": "편집할 섹션(section0.xml)을 찾을 수 없습니다.",
                "error_type": "runtime",
            }
        sec_name, sec_root = sec_info
        id_counter = [0]
        helpers = _build_sandbox_helpers(doc, section_roots, id_counter)

        safe_globals: Dict[str, Any] = {
            "__builtins__": dict(ALLOWED_BUILTINS),
            "ET": ET,
            "deepcopy": deepcopy,
            "re": re,
            "helpers": helpers,
        }
        exec(compile(tree, "<hwpx_sandbox>", "exec"), safe_globals, safe_globals)
        edit_fn = safe_globals.get("edit_hwpx")
        if not callable(edit_fn):
            doc.undo()
            return {
                "success": False,
                "error": "edit_hwpx(sec_root, helpers) 함수를 찾을 수 없습니다.",
                "error_type": "runtime",
            }

        exec_error: List[str] = []

        def _runner() -> None:
            try:
                edit_fn(sec_root, helpers)
            except Exception:
                exec_error.append(traceback.format_exc(limit=5))

        worker_thread = threading.Thread(target=_runner, daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=max(0.1, float(timeout_sec)))

        if worker_thread.is_alive():
            doc.undo()
            return {
                "success": False,
                "error": f"실행 시간 초과 ({timeout_sec:.1f}초): 무한 루프가 감지되어 작업을 중단했습니다.",
                "error_type": "timeout",
            }

        if exec_error:
            doc.undo()
            return {
                "success": False,
                "error": exec_error[0].strip(),
                "error_type": "runtime",
            }

        _verify_section_integrity(section_roots)
        _flush_sections(doc, section_roots)
        return {
            "success": True,
            "section_name": sec_name,
            "error": "",
        }
    except Exception as exc:
        doc.undo()
        tb_summary = traceback.format_exc(limit=4).strip()
        return {
            "success": False,
            "error": f"{exc}\n{tb_summary}",
            "error_type": "integrity",
        }
