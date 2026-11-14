"""
HWPX AI 문서 에이전트 XML 분석·패치 엔진 (services/hwpx_agent_service.py).
- lxml.etree 우선 사용 (미설치 시 xml.etree.ElementTree + HWPX 네임스페이스 접두사 명시 등록 폴백)
- OCF ZIP 규격 준수: 'mimetype' 파일은 반드시 첫 번째 엔트리 + ZIP_STORED(무압축), 나머지는 ZIP_DEFLATED
- 경량 DOM 맵 추출(extract_hwpx_dom_map) 및 8종 + insert_exam_question JSON Patch 연산자 지원
- 아래첨자(_{...}), 위첨자(^{...}), 수식($$...$$) 자동 변환 및 실행 취소(Undo) 스냅샷 스택 지원
"""

import html
import io
import json
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from lxml import etree as ET  # type: ignore

    HAS_LXML = True
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore

    HAS_LXML = False

HWPX_NAMESPACES: Dict[str, str] = {
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "ha": "http://www.hancom.co.kr/hwpml/2011/app",
    "hp10": "http://www.hancom.co.kr/hwpml/2016/paragraph",
    "opf": "http://www.idpf.org/2007/opf/",
}

if not HAS_LXML:
    for _prefix, _uri in HWPX_NAMESPACES.items():
        ET.register_namespace(_prefix, _uri)

HP_NS = HWPX_NAMESPACES["hp"]
HS_NS = HWPX_NAMESPACES["hs"]
HC_NS = HWPX_NAMESPACES["hc"]
HH_NS = HWPX_NAMESPACES["hh"]

_RICH_TOKEN_RE = re.compile(r"(\$\$[\s\S]+?\$\$|_\{[^{}]+\}|\^\{[^{}]+\})")


def _qname(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _local_tag(elem: Any) -> str:
    tag = str(getattr(elem, "tag", ""))
    return tag.split("}")[-1] if "}" in tag else tag


def _parse_xml(xml_bytes: bytes) -> Any:
    if HAS_LXML:
        parser = ET.XMLParser(remove_blank_text=False, resolve_entities=False)
        return ET.fromstring(xml_bytes, parser=parser)
    return ET.fromstring(xml_bytes)


def _serialize_xml(root: Any) -> bytes:
    if HAS_LXML:
        return ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _natural_section_key(name: str) -> int:
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else 0


# =============================================================================
# 1. HWPX 문서 컨테이너 & Undo 스냅샷 관리
# =============================================================================
class HwpxDocument:
    """메모리상에서 HWPX ZIP 엔트리를 관리하며 OCF 무결성 저장 및 Undo 스냅샷을 지원합니다."""

    def __init__(self, entries: Dict[str, bytes], source_path: Optional[Path] = None):
        self.entries: Dict[str, bytes] = dict(entries)
        self.source_path: Optional[Path] = Path(source_path) if source_path else None
        self.undo_stack: List[Dict[str, bytes]] = []
        self.exam_answers: List[Dict[str, Any]] = []

    @classmethod
    def load(cls, hwpx_path: Path | str) -> "HwpxDocument":
        p = Path(hwpx_path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"HWPX 파일을 찾을 수 없습니다: {p}")
        entries: Dict[str, bytes] = {}
        with zipfile.ZipFile(p, "r") as zf:
            for info in zf.infolist():
                entries[info.filename] = zf.read(info.filename)
        if "mimetype" not in entries:
            entries["mimetype"] = b"application/hwp+zip"
        return cls(entries=entries, source_path=p)

    def push_undo_snapshot(self) -> None:
        self.undo_stack.append(dict(self.entries))

    def can_undo(self) -> bool:
        return len(self.undo_stack) > 0

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        self.entries = self.undo_stack.pop()
        return True

    def section_names(self) -> List[str]:
        return sorted(
            [
                n
                for n in self.entries.keys()
                if re.match(r"^Contents/section\d+\.xml$", n, re.IGNORECASE)
            ],
            key=_natural_section_key,
        )

    def save(self, out_path: Optional[Path | str] = None) -> Path:
        target = Path(out_path) if out_path else self.source_path
        if target is None:
            raise ValueError("저장할 파일 경로가 지정되지 않았습니다.")
        target.parent.mkdir(parents=True, exist_ok=True)

        mimetype_bytes = self.entries.get("mimetype", b"application/hwp+zip")
        with zipfile.ZipFile(target, "w") as zf:
            # OCF 규격: mimetype을 반드시 첫 번째 엔트리 + ZIP_STORED(무압축)로 기록
            mime_info = zipfile.ZipInfo("mimetype")
            mime_info.compress_type = zipfile.ZIP_STORED
            zf.writestr(mime_info, mimetype_bytes, compress_type=zipfile.ZIP_STORED)

            for name, data in self.entries.items():
                if name == "mimetype":
                    continue
                zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)

        self.source_path = target
        return target


# =============================================================================
# 2. header.xml 스타일(charPr / paraPr) 동적 복제 유틸
# =============================================================================
def _next_numeric_id(elements: List[Any], attr_name: str = "id") -> str:
    max_id = 0
    for el in elements:
        val = str(el.get(attr_name, "")).strip()
        if val.isdigit():
            max_id = max(max_id, int(val))
    return str(max_id + 1)


def _find_child(parent: Any, local_name: str) -> Optional[Any]:
    for ch in parent:
        if _local_tag(ch) == local_name:
            return ch
    return None


def _find_child_or(parent: Any, local_name: str, fallback: Any) -> Any:
    found = _find_child(parent, local_name)
    return found if found is not None else fallback


def _ensure_header_root(doc: HwpxDocument) -> Tuple[str, Any]:
    header_name = next(
        (n for n in doc.entries if n.lower() == "contents/header.xml"),
        "Contents/header.xml",
    )
    if header_name in doc.entries:
        return header_name, _parse_xml(doc.entries[header_name])

    # 최소 header.xml 생성
    root = ET.Element(_qname(HH_NS, "head"))
    ref_list = ET.SubElement(root, _qname(HH_NS, "refList"))
    ET.SubElement(ref_list, _qname(HH_NS, "charProperties"), {"itemCnt": "1"}).append(
        ET.Element(_qname(HH_NS, "charPr"), {"id": "0", "height": "1000"})
    )
    ET.SubElement(ref_list, _qname(HH_NS, "paraProperties"), {"itemCnt": "1"}).append(
        ET.Element(_qname(HH_NS, "paraPr"), {"id": "0"})
    )
    doc.entries[header_name] = _serialize_xml(root)
    return header_name, root


def _ensure_script_char_pr(
    doc: HwpxDocument,
    base_char_pr_id: str,
    script_type: str,  # "subscript" or "superscript"
) -> str:
    """header.xml에서 base charPr을 복제해 아래첨자/위첨자 전용 charPr을 생성·반환합니다."""
    header_name, header_root = _ensure_header_root(doc)
    char_props = None
    for node in header_root.iter():
        if _local_tag(node) == "charProperties":
            char_props = node
            break
    if char_props is None:
        ref_list = _find_child(header_root, "refList")
        if ref_list is None:
            ref_list = ET.SubElement(header_root, _qname(HH_NS, "refList"))
        char_props = ET.SubElement(ref_list, _qname(HH_NS, "charProperties"))

    all_char_prs = [ch for ch in char_props if _local_tag(ch) == "charPr"]
    base_node = next(
        (ch for ch in all_char_prs if str(ch.get("id", "")) == str(base_char_pr_id)),
        all_char_prs[0] if all_char_prs else None,
    )
    if base_node is None:
        base_node = ET.SubElement(
            char_props, _qname(HH_NS, "charPr"), {"id": "0", "height": "1000"}
        )
        all_char_prs = [base_node]

    # 이미 동일 base + script_type 스타일이 생성되어 있으면 재사용
    marker_attr = f"_eduflow_{script_type}_of"
    for ch in all_char_prs:
        if ch.get(marker_attr) == str(base_char_pr_id):
            return str(ch.get("id"))

    new_id = _next_numeric_id(all_char_prs)
    cloned = deepcopy(base_node)
    cloned.set("id", new_id)
    cloned.set(marker_attr, str(base_char_pr_id))
    # 첨자 크기(약 65%) 및 오프셋 설정
    try:
        orig_h = int(cloned.get("height", "1000"))
        cloned.set("height", str(max(500, int(orig_h * 0.65))))
    except Exception:
        pass
    for tag_rem in ("subscript", "superscript"):
        old_el = _find_child(cloned, tag_rem)
        if old_el is not None:
            cloned.remove(old_el)
    ET.SubElement(cloned, _qname(HH_NS, script_type))
    char_props.append(cloned)
    char_props.set("itemCnt", str(len([c for c in char_props if _local_tag(c) == "charPr"])))
    doc.entries[header_name] = _serialize_xml(header_root)
    return new_id


def _clone_para_pr_with_indent(
    doc: HwpxDocument,
    base_para_pr_id: str,
    *,
    indent_pt: Optional[float] = None,
    left_margin_pt: Optional[float] = None,
    keep_with_next: Optional[bool] = None,
) -> str:
    """header.xml에서 paraPr을 복제해 새 ID를 발급하고 intent/left/keepWithNext를 수정합니다."""
    header_name, header_root = _ensure_header_root(doc)
    para_props = None
    for node in header_root.iter():
        if _local_tag(node) == "paraProperties":
            para_props = node
            break
    if para_props is None:
        ref_list = _find_child(header_root, "refList")
        if ref_list is None:
            ref_list = ET.SubElement(header_root, _qname(HH_NS, "refList"))
        para_props = ET.SubElement(ref_list, _qname(HH_NS, "paraProperties"))

    all_para_prs = [ch for ch in para_props if _local_tag(ch) == "paraPr"]
    base_node = next(
        (ch for ch in all_para_prs if str(ch.get("id", "")) == str(base_para_pr_id)),
        all_para_prs[0] if all_para_prs else None,
    )
    if base_node is None:
        base_node = ET.SubElement(para_props, _qname(HH_NS, "paraPr"), {"id": "0"})
        all_para_prs = [base_node]

    new_id = _next_numeric_id(all_para_prs)
    cloned = deepcopy(base_node)
    cloned.set("id", new_id)

    margin_el = _find_child(cloned, "margin")
    if margin_el is None:
        margin_el = ET.SubElement(cloned, _qname(HH_NS, "margin"))

    if indent_pt is not None:
        intent_el = _find_child(margin_el, "intent")
        if intent_el is None:
            intent_el = ET.SubElement(margin_el, _qname(HC_NS, "intent"))
        intent_el.set("value", str(int(round(float(indent_pt) * 100))))
        intent_el.set("unit", "HWPUNIT")

    if left_margin_pt is not None:
        left_el = _find_child(margin_el, "left")
        if left_el is None:
            left_el = ET.SubElement(margin_el, _qname(HC_NS, "left"))
        left_el.set("value", str(int(round(float(left_margin_pt) * 100))))
        left_el.set("unit", "HWPUNIT")

    if keep_with_next is not None:
        break_el = _find_child(cloned, "breakSetting")
        if break_el is None:
            break_el = ET.SubElement(cloned, _qname(HH_NS, "breakSetting"))
        break_el.set("keepWithNext", "1" if keep_with_next else "0")

    para_props.append(cloned)
    para_props.set("itemCnt", str(len([c for c in para_props if _local_tag(c) == "paraPr"])))
    doc.entries[header_name] = _serialize_xml(header_root)
    return new_id


# =============================================================================
# 3. 문단 텍스트/첨자/수식 및 DOM 맵 추출
# =============================================================================
def _extract_paragraph_plain_text(p_elem: Any) -> str:
    parts: List[str] = []
    for node in p_elem.iter():
        tag = _local_tag(node)
        if tag == "t" and node.text:
            parts.append(node.text)
        elif tag == "script" and node.text:
            parts.append(f"$${node.text}$$")
        elif tag in ("tab", "lineBreak"):
            parts.append(" ")
    return "".join(parts)


def _strip_linesegarray(p_elem: Any) -> None:
    for child in list(p_elem):
        if _local_tag(child) == "linesegarray":
            p_elem.remove(child)


_HWP_EQ_RESERVED_WORDS = {"rarrow", "item", "items", "iter", "italic"}


def _normalize_hwp_equation_script(script: str) -> str:
    """LaTeX 스타일 명령어(\\rm, \\it, \\times 등) 및 붙어 쓴 rm/it 명령어를 한글 수식 문법으로 정규화합니다."""
    s = (script or "").strip()
    if not s:
        return ""

    # LaTeX 백슬래시 명령어 -> 한글 수식 키워드
    latex_replacements = [
        (r"\\rm(?=\s|[A-Za-z{]|$)", "rm "),
        (r"\\it(?=\s|[A-Za-z{]|$)", "it "),
        (r"\\times\b", "times "),
        (r"\\cdot\b", "cdot "),
        (r"\\div\b", "div "),
        (r"\\pm\b", "pm "),
        (r"\\leq\b", "le "),
        (r"\\geq\b", "ge "),
        (r"\\neq\b", "!= "),
        (r"\\rightarrow\b", "-> "),
        (r"\\to\b", "-> "),
        (r"\\leftarrow\b", "<- "),
        (r"\\leftrightarrow\b", "<-> "),
    ]
    for pat, repl in latex_replacements:
        s = re.sub(pat, repl, s)

    # rmCH, itaq, rmM, rmg 등 붙어 쓴 명령어 분리 (예약어 제외)
    def _split_cmd(match: re.Match[str]) -> str:
        cmd = match.group(1)
        rest = match.group(2)
        full = f"{cmd}{rest}".lower()
        if full in _HWP_EQ_RESERVED_WORDS:
            return match.group(0)
        return f"{cmd} {rest}"

    s = re.sub(r"(?<![A-Za-z])(rm|it)([A-Za-z]+)", _split_cmd, s)
    s = re.sub(r" {2,}", " ", s).strip()
    return s


def _find_template_equation(
    doc: HwpxDocument,
    section_roots: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Any], int]:
    """문서 내 기존 <hp:equation> 템플릿과 현재 최대 수식 ID를 찾습니다."""
    template_eq: Optional[Any] = None
    max_id = 100000000

    roots_to_scan: List[Any] = []
    if section_roots:
        roots_to_scan.extend(section_roots.values())
    else:
        for sec_name in doc.section_names():
            try:
                roots_to_scan.append(_parse_xml(doc.entries[sec_name]))
            except Exception:
                continue

    for root in roots_to_scan:
        for node in root.iter():
            if _local_tag(node) == "equation":
                if template_eq is None:
                    template_eq = node
                raw_id = str(node.get("id", "")).strip()
                if raw_id.isdigit():
                    max_id = max(max_id, int(raw_id))

    return template_eq, max_id


def _estimate_equation_size(eq_script: str) -> Tuple[int, int]:
    """수식 스크립트의 보이는 문자 수 기준으로 인라인 (width, height) HWPUNIT 크기를 계산합니다."""
    visible = re.sub(r"\b(rm|it|bold|over|sqrt|left|right|times|cdot)\b", "", eq_script)
    visible = re.sub(r"[{}\s]", "", visible)
    vis_len = max(1, len(visible))
    width = max(1200, vis_len * 550)
    height = 1100
    return width, height


def _build_equation_element(
    doc: HwpxDocument,
    raw_script: str,
    section_roots: Optional[Dict[str, Any]] = None,
    id_counter: Optional[List[int]] = None,
) -> Any:
    """기존 <hp:equation> 템플릿을 복제하거나 기본 구조를 생성해 treatAsChar="1" 인라인 수식 노드를 만듭니다."""
    normalized_script = _normalize_hwp_equation_script(raw_script)
    template_eq, max_id = _find_template_equation(doc, section_roots=section_roots)
    if id_counter is not None:
        id_counter[0] = max(id_counter[0], max_id) + 1
        new_id = str(id_counter[0])
    else:
        new_id = str(max_id + 1)

    calc_w, calc_h = _estimate_equation_size(normalized_script)

    if template_eq is not None:
        eq_el = deepcopy(template_eq)
        eq_el.set("id", new_id)
    else:
        eq_el = ET.Element(
            _qname(HP_NS, "equation"),
            {
                "id": new_id,
                "zOrder": "0",
                "numberingType": "EQUATION",
                "textWrap": "TOP_AND_BOTTOM",
                "textFlow": "BOTH_SIDES",
                "lock": "0",
                "dropcapstyle": "None",
                "version": "Equation Version 60",
                "baseLine": "85",
                "textColor": "#000000",
                "baseUnit": "1000",
                "lineMode": "CHAR",
                "font": "HYhwpEQ",
            },
        )

    sz_el = _find_child(eq_el, "sz")
    if sz_el is None:
        sz_el = ET.SubElement(
            eq_el,
            _qname(HP_NS, "sz"),
            {
                "width": str(calc_w),
                "widthRelTo": "ABSOLUTE",
                "height": str(calc_h),
                "heightRelTo": "ABSOLUTE",
                "protect": "0",
            },
        )
    else:
        sz_el.set("width", str(calc_w))
        if not sz_el.get("height") or sz_el.get("height") == "0":
            sz_el.set("height", str(calc_h))

    pos_el = _find_child(eq_el, "pos")
    if pos_el is None:
        pos_el = ET.SubElement(
            eq_el,
            _qname(HP_NS, "pos"),
            {
                "treatAsChar": "1",
                "affectLSpacing": "0",
                "flowWithText": "1",
                "allowOverlap": "0",
                "holdAnchorAndSO": "0",
                "vertRelTo": "PARA",
                "horzRelTo": "PARA",
                "vertAlign": "TOP",
                "horzAlign": "LEFT",
                "vertOffset": "0",
                "horzOffset": "0",
            },
        )
    else:
        pos_el.set("treatAsChar", "1")

    sc_el = _find_child(eq_el, "script")
    if sc_el is None:
        sc_el = ET.SubElement(eq_el, _qname(HP_NS, "script"))
    sc_el.text = normalized_script
    return eq_el


def _build_rich_runs(
    doc: HwpxDocument,
    text: str,
    base_char_pr_id: str = "0",
    section_roots: Optional[Dict[str, Any]] = None,
    id_counter: Optional[List[int]] = None,
) -> List[Any]:
    """텍스트 내 _{...}, ^{...}, $$...$$ 마크업을 파싱해 <hp:run> 리스트로 변환합니다."""
    tokens = _RICH_TOKEN_RE.split(text or "")
    if not any(tokens):
        tokens = [""]

    runs: List[Any] = []
    for tok in tokens:
        if not tok and len(tokens) > 1:
            continue
        if tok.startswith("$$") and tok.endswith("$$") and len(tok) > 4:
            eq_script = tok[2:-2].strip()
            run_el = ET.Element(_qname(HP_NS, "run"), {"charPrIDRef": base_char_pr_id})
            eq_el = _build_equation_element(
                doc, eq_script, section_roots=section_roots, id_counter=id_counter
            )
            run_el.append(eq_el)
            runs.append(run_el)
        elif tok.startswith("_{") and tok.endswith("}"):
            sub_txt = tok[2:-1]
            sub_id = _ensure_script_char_pr(doc, base_char_pr_id, "subscript")
            run_el = ET.Element(_qname(HP_NS, "run"), {"charPrIDRef": sub_id})
            t_el = ET.SubElement(run_el, _qname(HP_NS, "t"))
            t_el.text = sub_txt
            runs.append(run_el)
        elif tok.startswith("^{") and tok.endswith("}"):
            sup_txt = tok[2:-1]
            sup_id = _ensure_script_char_pr(doc, base_char_pr_id, "superscript")
            run_el = ET.Element(_qname(HP_NS, "run"), {"charPrIDRef": sup_id})
            t_el = ET.SubElement(run_el, _qname(HP_NS, "t"))
            t_el.text = sup_txt
            runs.append(run_el)
        else:
            run_el = ET.Element(_qname(HP_NS, "run"), {"charPrIDRef": base_char_pr_id})
            t_el = ET.SubElement(run_el, _qname(HP_NS, "t"))
            t_el.text = tok
            runs.append(run_el)
    return runs


def _set_paragraph_rich_text(
    doc: HwpxDocument,
    p_elem: Any,
    text: str,
    section_roots: Optional[Dict[str, Any]] = None,
) -> None:
    """
    텍스트 내 _{...}(아래첨자), ^{...}(위첨자), $$...$$(한글 수식)을 파싱하여
    <hp:run> 노드로 분리 생성하고 <hp:linesegarray>를 제거합니다.
    """
    first_run = next((ch for ch in p_elem if _local_tag(ch) == "run"), None)
    base_char_pr_id = str(first_run.get("charPrIDRef", "0")) if first_run is not None else "0"

    # 기존 run / linesegarray 제거
    for ch in list(p_elem):
        if _local_tag(ch) in ("run", "linesegarray"):
            p_elem.remove(ch)

    id_counter = [0]
    for run_el in _build_rich_runs(
        doc, text, base_char_pr_id=base_char_pr_id, section_roots=section_roots, id_counter=id_counter
    ):
        p_elem.append(run_el)


def _is_plain_text_run(run_elem: Any) -> bool:
    """<hp:equation> 등 컨트롤이 없는 일반 텍스트 <hp:run>인지 판별합니다."""
    if _local_tag(run_elem) != "run":
        return False
    for ch in run_elem:
        if _local_tag(ch) not in ("t", "tab", "lineBreak"):
            return False
    return True


def _extract_run_text(run_elem: Any) -> str:
    parts: List[str] = []
    for ch in run_elem:
        tag = _local_tag(ch)
        if tag == "t" and ch.text:
            parts.append(ch.text)
        elif tag in ("tab", "lineBreak"):
            parts.append(" ")
    return "".join(parts)


_CHEM_MATCH_TRANS = str.maketrans(
    "₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻\u00a0",
    "01234567890123456789+- ",
)


def _normalize_match_text(text: str) -> str:
    """유니코드 위/아래첨자 및 비분리 공백을 일반 ASCII 문자로 정규화합니다."""
    return text.translate(_CHEM_MATCH_TRANS)


def _replace_with_normalized_match(
    text: str, old_text: str, new_text: str
) -> Tuple[str, bool]:
    """
    원문에서 old_text를 찾되, 완전 일치가 없으면 위/아래첨자·NBSP 정규화 인덱스 매핑으로
    원문 내 실제 구간을 찾아 new_text로 치환합니다.
    """
    if not old_text:
        return text, False
    if old_text in text:
        return text.replace(old_text, new_text), True

    norm_old = _normalize_match_text(old_text)
    if not norm_old:
        return text, False

    norm_text = _normalize_match_text(text)
    if norm_old not in norm_text:
        return text, False

    updated = text
    norm_updated = norm_text
    norm_new_len = len(_normalize_match_text(new_text))
    search_from = 0
    replaced = False

    while True:
        pos = norm_updated.find(norm_old, search_from)
        if pos == -1:
            break
        updated = updated[:pos] + new_text + updated[pos + len(norm_old) :]
        norm_updated = _normalize_match_text(updated)
        search_from = pos + norm_new_len
        replaced = True

    return updated, replaced


def _replace_text_in_paragraph(
    doc: HwpxDocument,
    p_elem: Any,
    old_text: str,
    new_text: str,
    section_roots: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    문단 내 기존 <hp:equation> 등 컨트롤 <hp:run>은 그대로 보존하면서,
    old_text가 포함된 일반 텍스트 <hp:run>만 새 텍스트/수식 <hp:run>으로 부분 치환합니다.
    """
    if not old_text:
        return False

    id_counter = [0]
    replaced = False

    # 1차: 단일 일반 텍스트 <hp:run> 내부 치환
    for child in list(p_elem):
        if not _is_plain_text_run(child):
            continue
        r_text = _extract_run_text(child)
        updated, did_replace = _replace_with_normalized_match(r_text, old_text, new_text)
        if did_replace:
            base_cid = str(child.get("charPrIDRef", "0"))
            new_runs = _build_rich_runs(
                doc, updated, base_char_pr_id=base_cid, section_roots=section_roots, id_counter=id_counter
            )
            idx = list(p_elem).index(child)
            p_elem.remove(child)
            for offset, nr in enumerate(new_runs):
                p_elem.insert(idx + offset, nr)
            replaced = True

    # 2차: 연속된 일반 텍스트 <hp:run> 여러 개에 걸쳐 old_text가 분할된 경우
    if not replaced:
        children = list(p_elem)
        seg_runs: List[Any] = []

        def _flush_segment(segment: List[Any]) -> bool:
            if not segment:
                return False
            combined = "".join(_extract_run_text(r) for r in segment)
            updated, did_replace = _replace_with_normalized_match(combined, old_text, new_text)
            if not did_replace:
                return False
            base_cid = str(segment[0].get("charPrIDRef", "0"))
            new_runs = _build_rich_runs(
                doc, updated, base_char_pr_id=base_cid, section_roots=section_roots, id_counter=id_counter
            )
            insert_idx = list(p_elem).index(segment[0])
            for r in segment:
                p_elem.remove(r)
            for offset, nr in enumerate(new_runs):
                p_elem.insert(insert_idx + offset, nr)
            return True

        for ch in children:
            if _is_plain_text_run(ch):
                seg_runs.append(ch)
            else:
                if _flush_segment(seg_runs):
                    replaced = True
                seg_runs = []
        if _flush_segment(seg_runs):
            replaced = True

    if replaced:
        _strip_linesegarray(p_elem)
    return replaced


def _collect_section_dom(doc: HwpxDocument) -> Tuple[Dict[str, Tuple[str, Any, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """
    모든 section*.xml을 파싱하여 (id_map, dom_blocks, section_roots)를 반환합니다.
    id_map[node_id] = (section_name, parent_elem, target_elem)
    """
    id_map: Dict[str, Tuple[str, Any, Any]] = {}
    blocks: List[Dict[str, Any]] = []
    section_roots: Dict[str, Any] = {}

    p_counter = 0
    tbl_counter = 0

    for sec_name in doc.section_names():
        root = _parse_xml(doc.entries[sec_name])
        section_roots[sec_name] = root

        def _walk(parent: Any) -> None:
            nonlocal p_counter, tbl_counter
            for child in list(parent):
                tag = _local_tag(child)
                if tag == "tbl":
                    tid = f"tbl_{tbl_counter}"
                    tbl_counter += 1
                    id_map[tid] = (sec_name, parent, child)
                    rows_data: List[List[Dict[str, str]]] = []
                    r_idx = 0
                    for tr in child:
                        if _local_tag(tr) != "tr":
                            continue
                        row_cells: List[Dict[str, str]] = []
                        c_idx = 0
                        for tc in tr:
                            if _local_tag(tc) != "tc":
                                continue
                            cid = f"{tid}_r{r_idx}_c{c_idx}"
                            id_map[cid] = (sec_name, tr, tc)
                            cell_lines: List[str] = []
                            for sub in tc.iter():
                                if _local_tag(sub) == "p":
                                    cell_lines.append(_extract_paragraph_plain_text(sub))
                            row_cells.append({"id": cid, "text": "\n".join(cell_lines).strip()})
                            c_idx += 1
                        rows_data.append(row_cells)
                        r_idx += 1
                    blocks.append({"id": tid, "type": "table", "rows": rows_data})
                elif tag == "p":
                    has_nested_tbl = any(_local_tag(n) == "tbl" for n in child.iter())
                    if has_nested_tbl:
                        _walk(child)
                    else:
                        pid = f"p_{p_counter}"
                        p_counter += 1
                        id_map[pid] = (sec_name, parent, child)
                        blocks.append(
                            {
                                "id": pid,
                                "type": "paragraph",
                                "text": _extract_paragraph_plain_text(child),
                            }
                        )
                else:
                    _walk(child)

        _walk(root)

    return id_map, blocks, section_roots


def extract_hwpx_dom_map(doc_or_path: HwpxDocument | Path | str) -> Dict[str, Any]:
    """AI 프롬프트에 전달할 경량 문서 구조 JSON 맵을 생성합니다."""
    doc = (
        doc_or_path
        if isinstance(doc_or_path, HwpxDocument)
        else HwpxDocument.load(doc_or_path)
    )
    _, blocks, _ = _collect_section_dom(doc)
    return {
        "file_name": doc.source_path.name if doc.source_path else "document.hwpx",
        "block_count": len(blocks),
        "blocks": blocks,
    }


def build_hwpx_agent_prompt(dom_map: Dict[str, Any], user_instruction: str) -> Tuple[str, str]:
    """(system_prompt, user_prompt)를 생성합니다."""
    system_prompt = (
        "당신은 대한민국 교직 실무용 HWPX 문서 편집 AI 에이전트입니다.\n"
        "문서 전체를 다시 쓰지 말고, 반드시 아래 JSON Patch 스키마에 맞는 ```json ... ``` 블록만 출력하세요.\n"
        "사용 가능한 op 목록:\n"
        "1. replace_text: {\"op\":\"replace_text\", \"target_id\":\"p_0\"|\"tbl_0_r0_c0\"|\"ALL\", \"old_text\":\"...\", \"new_text\":\"...\"}\n"
        "2. set_paragraph_text: {\"op\":\"set_paragraph_text\", \"target_id\":\"p_0\", \"new_text\":\"...\"}\n"
        "3. insert_paragraph_after: {\"op\":\"insert_paragraph_after\", \"target_id\":\"p_0\", \"text\":\"...\"}\n"
        "4. delete_paragraph: {\"op\":\"delete_paragraph\", \"target_id\":\"p_0\"}\n"
        "5. update_cell: {\"op\":\"update_cell\", \"target_id\":\"tbl_0_r0_c1\", \"new_text\":\"...\"}\n"
        "6. add_table_row: {\"op\":\"add_table_row\", \"target_id\":\"tbl_0\", \"after_row\":0, \"cells\":[\"...\"]}\n"
        "7. set_paragraph_indent: {\"op\":\"set_paragraph_indent\", \"target_id\":\"p_0\", \"indent_pt\":-12, \"left_margin_pt\":12}\n"
        "8. update_table_layout: {\"op\":\"update_table_layout\", \"target_id\":\"tbl_0\", \"left_indent_pt\":10, \"col_widths_ratio\":[1,3]}\n"
        "9. insert_exam_question: {\"op\":\"insert_exam_question\", \"target_id\":\"p_0\", \"number\":1, \"score\":\"3.2점\", \"question\":\"...\", \"bogi\":[\"ㄱ. ...\"], \"choices\":[\"① ...\"], \"answer\":\"③\", \"explanation\":\"...\"}\n"
        "화학식/첨자/수식은 H_{2}O, Fe^{3+}, $$x^2 + y^2 = r^2$$, $$rm {CH}_{3}COOH(it {aq})$$처럼 "
        "한글 수식 문법(rm, it 뒤 공백 유지, 백슬래시 금지)을 사용하세요."
    )
    dom_json = json.dumps(dom_map, ensure_ascii=False, indent=2)
    if len(dom_json) > 18000:
        dom_json = dom_json[:18000] + "\n...(이하 생략)"

    user_prompt = (
        f"### [교사 수정 지시사항]\n{user_instruction.strip()}\n\n"
        f"### [출력 형식 - 필수 준수]\n"
        f"```json\n"
        f'{{\n  "summary": "변경 사항 한 줄 요약",\n  "operations": [\n    {{"op": "replace_text", "target_id": "p_0", "old_text": "...", "new_text": "..."}}\n  ]\n}}\n'
        f"```\n\n"
        f"### [현재 HWPX 경량 DOM 맵]\n```json\n{dom_json}\n```"
    )
    return system_prompt, user_prompt


# =============================================================================
# 4. JSON Patch 연산자 실행 엔진
# =============================================================================
def _flush_sections(doc: HwpxDocument, section_roots: Dict[str, Any]) -> None:
    for sec_name, root in section_roots.items():
        doc.entries[sec_name] = _serialize_xml(root)


def apply_single_hwpx_operation(
    doc: HwpxDocument,
    op_data: Dict[str, Any],
) -> List[str]:
    """단일 JSON Patch 연산을 적용하고, 하이라이트할 변경 대상 ID 목록을 반환합니다."""
    op = str(op_data.get("op", "")).strip()
    target_id = str(op_data.get("target_id", "")).strip()
    id_map, _, section_roots = _collect_section_dom(doc)
    touched_ids: List[str] = []

    if op == "replace_text":
        old_text = str(op_data.get("old_text", ""))
        new_text = str(op_data.get("new_text", ""))
        targets = (
            [(k, v) for k, v in id_map.items() if k.startswith("p_") or "_r" in k]
            if target_id.upper() == "ALL"
            else ([(target_id, id_map[target_id])] if target_id in id_map else [])
        )
        for tid, (_, _, elem) in targets:
            tag = _local_tag(elem)
            if tag == "p":
                if _replace_text_in_paragraph(
                    doc, elem, old_text, new_text, section_roots=section_roots
                ):
                    touched_ids.append(tid)
            elif tag in ("tc", "tbl"):
                replaced_any = False
                for p_node in [n for n in elem.iter() if _local_tag(n) == "p"]:
                    if _replace_text_in_paragraph(
                        doc, p_node, old_text, new_text, section_roots=section_roots
                    ):
                        replaced_any = True
                if replaced_any:
                    touched_ids.append(tid)

    elif op == "set_paragraph_text":
        if target_id in id_map:
            _, _, elem = id_map[target_id]
            if _local_tag(elem) == "p":
                _set_paragraph_rich_text(doc, elem, str(op_data.get("new_text", "")))
                touched_ids.append(target_id)

    elif op == "insert_paragraph_after":
        if target_id in id_map:
            _, parent, elem = id_map[target_id]
            new_p = deepcopy(elem)
            _set_paragraph_rich_text(doc, new_p, str(op_data.get("text", "")))
            children = list(parent)
            idx = children.index(elem)
            parent.insert(idx + 1, new_p)
            touched_ids.append(target_id)

    elif op == "delete_paragraph":
        if target_id in id_map:
            _, parent, elem = id_map[target_id]
            parent.remove(elem)
            touched_ids.append(target_id)

    elif op == "update_cell":
        if target_id in id_map:
            _, _, tc = id_map[target_id]
            new_text = str(op_data.get("new_text", ""))
            lines = new_text.split("\n") if new_text else [""]
            sub_list = _find_child_or(tc, "subList", tc)
            existing_ps = [ch for ch in sub_list if _local_tag(ch) == "p"]
            template_p = (
                deepcopy(existing_ps[0])
                if existing_ps
                else ET.Element(_qname(HP_NS, "p"), {"paraPrIDRef": "0"})
            )
            for p_old in existing_ps:
                sub_list.remove(p_old)
            for ln in lines:
                new_p = deepcopy(template_p)
                _set_paragraph_rich_text(doc, new_p, ln)
                sub_list.append(new_p)
            touched_ids.append(target_id)

    elif op == "add_table_row":
        if target_id in id_map:
            _, _, tbl = id_map[target_id]
            trs = [ch for ch in tbl if _local_tag(ch) == "tr"]
            if trs:
                # 병합 셀(rowSpan > 1)이 없는 행 우선 선택
                tmpl_tr = trs[-1]
                for tr in trs:
                    has_merge = False
                    for tc in tr:
                        if _local_tag(tc) != "tc":
                            continue
                        cell_span = _find_child(tc, "cellSpan")
                        if cell_span is not None and int(cell_span.get("rowSpan", "1")) > 1:
                            has_merge = True
                            break
                    if not has_merge:
                        tmpl_tr = tr
                        break

                new_tr = deepcopy(tmpl_tr)
                cell_texts = [str(c) for c in (op_data.get("cells") or [])]
                tcs = [ch for ch in new_tr if _local_tag(ch) == "tc"]
                for c_idx, tc in enumerate(tcs):
                    txt = cell_texts[c_idx] if c_idx < len(cell_texts) else ""
                    sub_list = _find_child_or(tc, "subList", tc)
                    ps = [ch for ch in sub_list if _local_tag(ch) == "p"]
                    tmpl_p = deepcopy(ps[0]) if ps else ET.Element(_qname(HP_NS, "p"))
                    for p_old in ps:
                        sub_list.remove(p_old)
                    new_p = deepcopy(tmpl_p)
                    _set_paragraph_rich_text(doc, new_p, txt)
                    sub_list.append(new_p)

                after_row = op_data.get("after_row")
                if isinstance(after_row, int) and 0 <= after_row < len(trs):
                    tbl_children = list(tbl)
                    ins_idx = tbl_children.index(trs[after_row]) + 1
                    tbl.insert(ins_idx, new_tr)
                else:
                    tbl.append(new_tr)

                # rowCnt 및 모든 셀 rowAddr 보정
                updated_trs = [ch for ch in tbl if _local_tag(ch) == "tr"]
                tbl.set("rowCnt", str(len(updated_trs)))
                for r_i, tr_el in enumerate(updated_trs):
                    c_i = 0
                    for tc_el in tr_el:
                        if _local_tag(tc_el) != "tc":
                            continue
                        addr = _find_child(tc_el, "cellAddr")
                        if addr is None:
                            addr = ET.SubElement(tc_el, _qname(HP_NS, "cellAddr"))
                        addr.set("rowAddr", str(r_i))
                        addr.set("colAddr", str(c_i))
                        c_i += 1
                touched_ids.append(target_id)

    elif op == "set_paragraph_indent":
        if target_id in id_map:
            _, _, elem = id_map[target_id]
            if _local_tag(elem) == "p":
                base_pid = str(elem.get("paraPrIDRef", "0"))
                new_pid = _clone_para_pr_with_indent(
                    doc,
                    base_pid,
                    indent_pt=op_data.get("indent_pt"),
                    left_margin_pt=op_data.get("left_margin_pt"),
                )
                elem.set("paraPrIDRef", new_pid)
                touched_ids.append(target_id)

    elif op == "update_table_layout":
        if target_id in id_map:
            _, _, tbl = id_map[target_id]
            left_pt = op_data.get("left_indent_pt")
            if left_pt is not None:
                left_hwp = str(int(round(float(left_pt) * 100)))
                out_margin = _find_child(tbl, "outMargin")
                if out_margin is None:
                    out_margin = ET.SubElement(tbl, _qname(HP_NS, "outMargin"))
                out_margin.set("left", left_hwp)
                pos_el = _find_child(tbl, "pos")
                if pos_el is not None:
                    pos_el.set("horzOffset", left_hwp)

            ratios = op_data.get("col_widths_ratio")
            if isinstance(ratios, list) and ratios:
                float_ratios = [max(0.1, float(r)) for r in ratios]
                ratio_sum = sum(float_ratios)
                trs = [ch for ch in tbl if _local_tag(ch) == "tr"]
                if trs:
                    first_tcs = [ch for ch in trs[0] if _local_tag(ch) == "tc"]
                    total_w = 0
                    for tc in first_tcs:
                        csz = _find_child(tc, "cellSz")
                        if csz is not None and str(csz.get("width", "")).isdigit():
                            total_w += int(csz.get("width"))
                    if total_w <= 0:
                        sz = _find_child(tbl, "sz")
                        total_w = int(sz.get("width")) if (sz is not None and str(sz.get("width", "")).isdigit()) else 42520
                    col_widths = [int(total_w * (r / ratio_sum)) for r in float_ratios]
                    for tr in trs:
                        tcs = [ch for ch in tr if _local_tag(ch) == "tc"]
                        for c_idx, tc in enumerate(tcs):
                            if c_idx < len(col_widths):
                                csz = _find_child(tc, "cellSz")
                                if csz is None:
                                    csz = ET.SubElement(tc, _qname(HP_NS, "cellSz"))
                                csz.set("width", str(col_widths[c_idx]))
            touched_ids.append(target_id)

    elif op == "insert_exam_question":
        anchor_entry = id_map.get(target_id)
        if anchor_entry is None:
            # 마지막 문단 폴백
            p_keys = [k for k in id_map if k.startswith("p_")]
            if p_keys:
                anchor_entry = id_map[p_keys[-1]]
                target_id = p_keys[-1]
        if anchor_entry is not None:
            _, parent, anchor_elem = anchor_entry
            children = list(parent)
            insert_pos = children.index(anchor_elem) + 1

            num = op_data.get("number", 1)
            score = str(op_data.get("score", "")).strip()
            q_text = str(op_data.get("question", "")).strip()
            bogi_list = [str(b) for b in (op_data.get("bogi") or [])]
            choices = [str(c) for c in (op_data.get("choices") or [])]
            ans = str(op_data.get("answer", "")).strip()
            expl = str(op_data.get("explanation", "")).strip()

            base_para_id = str(anchor_elem.get("paraPrIDRef", "0"))
            stem_para_id = _clone_para_pr_with_indent(doc, base_para_id, keep_with_next=True)
            choice_para_id = _clone_para_pr_with_indent(
                doc, base_para_id, indent_pt=-12.0, left_margin_pt=12.0, keep_with_next=True
            )

            # 1) 발문 문단
            stem_str = f"{num}. {q_text}" + (f" [{score}]" if score else "")
            stem_p = ET.Element(_qname(HP_NS, "p"), {"paraPrIDRef": stem_para_id})
            _set_paragraph_rich_text(doc, stem_p, stem_str)
            parent.insert(insert_pos, stem_p)
            insert_pos += 1

            # 2) <보기> 1x1 테두리 박스 표
            if bogi_list:
                bogi_tbl = ET.Element(
                    _qname(HP_NS, "tbl"), {"rowCnt": "1", "colCnt": "1", "borderFillIDRef": "1"}
                )
                tr = ET.SubElement(bogi_tbl, _qname(HP_NS, "tr"))
                tc = ET.SubElement(tr, _qname(HP_NS, "tc"))
                ET.SubElement(tc, _qname(HP_NS, "cellAddr"), {"colAddr": "0", "rowAddr": "0"})
                ET.SubElement(tc, _qname(HP_NS, "cellSz"), {"width": "40000", "height": "2000"})
                sub_list = ET.SubElement(tc, _qname(HP_NS, "subList"))

                hdr_p = ET.SubElement(sub_list, _qname(HP_NS, "p"), {"paraPrIDRef": stem_para_id})
                _set_paragraph_rich_text(doc, hdr_p, "< 보 기 >")
                for b_line in bogi_list:
                    bp = ET.SubElement(sub_list, _qname(HP_NS, "p"), {"paraPrIDRef": base_para_id})
                    _set_paragraph_rich_text(doc, bp, b_line)

                parent.insert(insert_pos, bogi_tbl)
                insert_pos += 1

            # 3) 선택지 ①~⑤ (내어쓰기 + keepWithNext)
            for c_idx, ch_txt in enumerate(choices):
                cp_id = choice_para_id if c_idx < len(choices) - 1 else _clone_para_pr_with_indent(
                    doc, base_para_id, indent_pt=-12.0, left_margin_pt=12.0, keep_with_next=False
                )
                cp = ET.Element(_qname(HP_NS, "p"), {"paraPrIDRef": cp_id})
                _set_paragraph_rich_text(doc, cp, ch_txt)
                parent.insert(insert_pos, cp)
                insert_pos += 1

            if ans or expl:
                doc.exam_answers.append(
                    {"number": num, "answer": ans, "explanation": expl}
                )
            touched_ids.append(target_id)

    _flush_sections(doc, section_roots)
    return touched_ids


def apply_hwpx_operations(
    doc: HwpxDocument,
    operations: List[Dict[str, Any]],
    record_undo: bool = True,
) -> List[str]:
    """여러 JSON Patch 연산자를 순차 적용하고 변경된 ID 목록을 반환합니다."""
    if record_undo:
        doc.push_undo_snapshot()
    all_touched: List[str] = []
    for op_data in operations:
        if isinstance(op_data, dict):
            all_touched.extend(apply_single_hwpx_operation(doc, op_data))
    return list(dict.fromkeys(all_touched))


def apply_hwpx_patch(
    doc: HwpxDocument,
    operations: List[Dict[str, Any]],
    record_undo: bool = True,
) -> Dict[str, Any]:
    """여러 JSON Patch 연산자를 순차 적용하고 modified_ids 및 applied_count가 포함된 결과 딕셔너리를 반환합니다."""
    modified_ids = apply_hwpx_operations(doc, operations, record_undo=record_undo)
    return {
        "modified_ids": modified_ids,
        "applied_count": len(modified_ids),
        "touched_count": len(modified_ids),
    }


def export_exam_answers_hwpx(doc: HwpxDocument, out_path: Path | str) -> Path:
    """insert_exam_question으로 수집된 정답·해설을 별도의 HWPX 파일로 저장합니다."""
    ans_doc = create_minimal_hwpx_document(title="정답 및 해설")
    ops: List[Dict[str, Any]] = []
    anchor = "p_0"
    for item in doc.exam_answers:
        line = f"{item['number']}번 정답: {item['answer']} | 해설: {item['explanation']}"
        ops.append({"op": "insert_paragraph_after", "target_id": anchor, "text": line})
    if ops:
        apply_hwpx_operations(ans_doc, ops, record_undo=False)
    return ans_doc.save(out_path)


# =============================================================================
# 5. HTML 미리보기 렌더러 (하이라이트 지원)
# =============================================================================
def _render_paragraph_html(p_elem: Any) -> str:
    parts: List[str] = []
    for node in p_elem.iter():
        tag = _local_tag(node)
        if tag == "t" and node.text:
            txt = html.escape(node.text)
            # 부모 run의 charPrIDRef로 <sub>/<sup> 여부는 간단히 마크업 또는 태그 확인
            parts.append(txt)
        elif tag == "script" and node.text:
            parts.append(
                f"<span style='background:#EFF6FF;color:#1D4ED8;padding:1px 5px;"
                f"border-radius:3px;font-family:Consolas,monospace;'>[수식: {html.escape(node.text)}]</span>"
            )
    return "".join(parts) or "&nbsp;"


def _get_script_char_pr_map(doc: HwpxDocument) -> Dict[str, str]:
    """header.xml에서 subscript/superscript charPr ID 매핑을 읽습니다."""
    mapping: Dict[str, str] = {}
    header_name = next((n for n in doc.entries if n.lower() == "contents/header.xml"), None)
    if not header_name:
        return mapping
    try:
        root = _parse_xml(doc.entries[header_name])
        for ch in root.iter():
            if _local_tag(ch) == "charPr":
                cid = str(ch.get("id", ""))
                if _find_child(ch, "subscript") is not None:
                    mapping[cid] = "sub"
                elif _find_child(ch, "superscript") is not None:
                    mapping[cid] = "sup"
    except Exception:
        pass
    return mapping


def _render_paragraph_rich_html(p_elem: Any, script_map: Dict[str, str]) -> str:
    parts: List[str] = []
    for child in p_elem:
        if _local_tag(child) != "run":
            continue
        cid = str(child.get("charPrIDRef", ""))
        script_tag = script_map.get(cid, "")
        for sub in child:
            stag = _local_tag(sub)
            if stag == "t" and sub.text:
                esc = html.escape(sub.text)
                if script_tag == "sub":
                    parts.append(f"<sub>{esc}</sub>")
                elif script_tag == "sup":
                    parts.append(f"<sup>{esc}</sup>")
                else:
                    parts.append(esc)
            elif stag == "equation":
                sc = _find_child(sub, "script")
                eq_txt = html.escape(sc.text or "") if sc is not None else ""
                parts.append(
                    f"<span style='background:#EFF6FF;color:#1D4ED8;padding:1px 5px;"
                    f"border-radius:3px;font-family:Consolas,monospace;'>[수식: {eq_txt}]</span>"
                )
    return "".join(parts) or "&nbsp;"


def render_hwpx_to_html(
    doc: HwpxDocument,
    highlighted_ids: Optional[Set[str] | List[str]] = None,
    modified_ids: Optional[List[str]] = None,
) -> str:
    """HWPX 문서를 QTextBrowser용 HTML로 렌더링합니다. modified_ids에 고유 앵커(hwpx-focus-0..)와 강조 하이라이트를 부여합니다."""
    ordered_modified: List[str] = (
        list(dict.fromkeys(modified_ids))
        if modified_ids is not None
        else list(dict.fromkeys(highlighted_ids or []))
    )
    anchor_map: Dict[str, str] = {
        tid: f"hwpx-focus-{idx}" for idx, tid in enumerate(ordered_modified)
    }
    highlighted: Set[str] = set(highlighted_ids or []) | set(ordered_modified)
    script_map = _get_script_char_pr_map(doc)
    id_map, blocks, _ = _collect_section_dom(doc)

    html_parts: List[str] = [
        "<html><body style='font-family: \"Malgun Gothic\", sans-serif; color:#0F172A; "
        "background:#FFFFFF; line-height:1.6; padding:12px;'>"
    ]

    for blk in blocks:
        bid = blk["id"]
        is_hl = bid in highlighted
        anchor_name = anchor_map.get(bid, "")
        anchor_tag = f'<a name="{anchor_name}" id="{anchor_name}"></a>' if anchor_name else ""
        if blk["type"] == "paragraph":
            _, _, p_elem = id_map[bid]
            inner = _render_paragraph_rich_html(p_elem, script_map)
            bg = (
                "background:#FEF9C3; border-left:4px solid #2563EB; padding:4px 8px; border-radius:4px;"
                if is_hl
                else "margin:4px 0;"
            )
            html_parts.append(
                f"<p style='{bg}'>{anchor_tag}<span style='color:#94A3B8;font-size:11px;margin-right:6px;'>[{bid}]</span>{inner}</p>"
            )
        elif blk["type"] == "table":
            _, _, tbl_elem = id_map[bid]
            tbl_bg = "border:2px solid #2563EB; background:#EFF6FF;" if is_hl else "border:1px solid #CBD5E1;"
            html_parts.append(
                f"<div style='margin:8px 0;'>{anchor_tag}<span style='color:#64748B;font-size:11px;'>[{bid}]</span>"
                f"<table cellspacing='0' cellpadding='6' style='width:100%; border-collapse:collapse; {tbl_bg}'>"
            )
            trs = [ch for ch in tbl_elem if _local_tag(ch) == "tr"]
            for r_i, tr in enumerate(trs):
                html_parts.append("<tr>")
                c_i = 0
                for tc in tr:
                    if _local_tag(tc) != "tc":
                        continue
                    cid = f"{bid}_r{r_i}_c{c_i}"
                    c_hl = cid in highlighted
                    c_anchor = anchor_map.get(cid, "")
                    c_anchor_tag = f'<a name="{c_anchor}" id="{c_anchor}"></a>' if c_anchor else ""
                    c_bg = (
                        "background:#FEF9C3; border:1px solid #CBD5E1; border-left:4px solid #2563EB;"
                        if c_hl
                        else "border:1px solid #CBD5E1;"
                    )
                    sub_list = _find_child_or(tc, "subList", tc)
                    ps = [ch for ch in sub_list if _local_tag(ch) == "p"]
                    cell_html = "<br/>".join(_render_paragraph_rich_html(p, script_map) for p in ps) or "&nbsp;"
                    html_parts.append(f"<td style='{c_bg} vertical-align:top;'>{c_anchor_tag}{cell_html}</td>")
                    c_i += 1
                html_parts.append("</tr>")
            html_parts.append("</table></div>")

    html_parts.append("</body></html>")
    return "".join(html_parts)


def create_minimal_hwpx_document(title: str = "새 문서") -> HwpxDocument:
    """테스트 또는 정답지 생성용 최소 OCF HWPX 문서를 메모리에 생성합니다."""
    header_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<hh:head xmlns:hh="{HH_NS}" xmlns:hc="{HC_NS}">'
        '<hh:refList>'
        '<hh:charProperties itemCnt="1"><hh:charPr id="0" height="1000"/></hh:charProperties>'
        '<hh:paraProperties itemCnt="1"><hh:paraPr id="0"><hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:left value="0" unit="HWPUNIT"/></hh:margin></hh:paraPr></hh:paraProperties>'
        '</hh:refList></hh:head>'
    ).encode("utf-8")

    section_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<hs:sec xmlns:hs="{HS_NS}" xmlns:hp="{HP_NS}">'
        f'<hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:t>{html.escape(title)}</hp:t></hp:run></hp:p>'
        '</hs:sec>'
    ).encode("utf-8")

    entries = {
        "mimetype": b"application/hwp+zip",
        "Contents/header.xml": header_xml,
        "Contents/section0.xml": section_xml,
    }
    return HwpxDocument(entries=entries)


_HWP_COM_UNAVAILABLE_MSG = (
    "한컴오피스(한글) 또는 pywin32가 설치되어 있지 않아 .hwp 자동 변환을 사용할 수 없습니다. "
    "한글에서 HWPX로 저장 후 열어주세요."
)


def _convert_via_hwp_com(
    src_path: Path,
    dst_path: Path,
    src_fmt: str,
    dst_fmt: str,
) -> Path:
    """
    백그라운드 한컴오피스(한글) COM 독립 인스턴스(DispatchEx)를 통해
    HWP <-> HWPX 포맷을 안전하게 변환합니다. (QThread 스레드 안전)
    """
    src = Path(src_path).resolve()
    dst = Path(dst_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"변환할 원본 파일을 찾을 수 없습니다: {src}")

    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError(_HWP_COM_UNAVAILABLE_MSG) from exc

    dst.parent.mkdir(parents=True, exist_ok=True)
    hwp = None
    co_initialized = False
    try:
        pythoncom.CoInitialize()
        co_initialized = True
        try:
            hwp = win32com.client.DispatchEx("HWPFrame.HwpObject")
        except Exception as exc:
            raise RuntimeError(_HWP_COM_UNAVAILABLE_MSG) from exc

        try:
            hwp.XHwpWindows.Item(0).Visible = False
        except Exception:
            pass
        try:
            hwp.SetMessageBoxMode(0x00010000)
        except Exception:
            pass
        try:
            hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
        except Exception:
            pass

        hwp.Open(str(src.resolve()), src_fmt, "forceopen:true")
        saved_ok = hwp.SaveAs(str(dst.resolve()), dst_fmt, "")
        if saved_ok is False or not dst.is_file():
            hwp.HAction.GetDefault("FileSaveAs_S", hwp.HParameterSet.HFileOpenSave.HSet)
            hwp.HParameterSet.HFileOpenSave.filename = str(dst.resolve())
            hwp.HParameterSet.HFileOpenSave.Format = dst_fmt
            hwp.HAction.Execute("FileSaveAs_S", hwp.HParameterSet.HFileOpenSave.HSet)
    finally:
        if hwp is not None:
            try:
                hwp.Clear(1)
            except Exception:
                pass
            try:
                hwp.Quit()
            except Exception:
                pass
        if co_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    if not dst.is_file():
        raise RuntimeError(f"한글 COM 변환 결과 파일이 생성되지 않았습니다: {dst}")
    return dst


def convert_hwp_to_hwpx(hwp_path: Path, out_hwpx_path: Path) -> Path:
    """구형 .hwp 파일을 백그라운드 한글 COM으로 임시/대상 .hwpx 파일로 변환합니다."""
    return _convert_via_hwp_com(hwp_path, out_hwpx_path, "HWP", "HWPX")


def convert_hwpx_to_hwp(hwpx_path: Path, out_hwp_path: Path) -> Path:
    """수정된 .hwpx 파일을 백그라운드 한글 COM으로 .hwp 파일로 역변환 저장합니다."""
    return _convert_via_hwp_com(hwpx_path, out_hwp_path, "HWPX", "HWP")
