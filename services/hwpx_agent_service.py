"""
HWPX AI 문서 에이전트 XML 분석·패치 엔진 (services/hwpx_agent_service.py).
- lxml.etree 우선 사용 (미설치 시 xml.etree.ElementTree + HWPX 네임스페이스 접두사 명시 등록 폴백)
- OCF ZIP 규격 준수: 'mimetype' 파일은 반드시 첫 번째 엔트리 + ZIP_STORED(무압축), 나머지는 ZIP_DEFLATED
- 경량 DOM 맵 추출(extract_hwpx_dom_map) 및 8종 + insert_exam_question JSON Patch 연산자 지원
- 아래첨자(_{...}), 위첨자(^{...}), 수식($$...$$) 자동 변환 및 실행 취소(Undo) 스냅샷 스택 지원
"""

import ctypes
from contextlib import contextmanager
import html
import io
import json
import os
import re
import stat
import sys
import threading
import uuid
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from utils.path_helper import get_resource_path

try:
    from lxml import etree as ET  # type: ignore

    HAS_LXML = True
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore

    HAS_LXML = False

HWPX_NAMESPACES: Dict[str, str] = {
    "ha": "http://www.hancom.co.kr/hwpml/2011/app",
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hp10": "http://www.hancom.co.kr/hwpml/2016/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "hhs": "http://www.hancom.co.kr/hwpml/2011/history",
    "hm": "http://www.hancom.co.kr/hwpml/2011/master-page",
    "hpf": "http://www.hancom.co.kr/schema/2011/hpf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "opf": "http://www.idpf.org/2007/opf/",
    "ooxmlchart": "http://www.hancom.co.kr/hwpml/2016/ooxmlchart",
    "hwpunitchar": "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar",
    "epub": "http://www.idpf.org/2007/ops",
    "config": "urn:oasis:names:tc:opendocument:xmlns:config:1.0",
    "hv": "http://www.hancom.co.kr/hwpml/2011/version",
    "ocf": "urn:oasis:names:tc:opendocument:xmlns:container",
    "odf": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}

if not HAS_LXML:
    for _prefix, _uri in HWPX_NAMESPACES.items():
        ET.register_namespace(_prefix, _uri)

HP_NS = HWPX_NAMESPACES["hp"]
HS_NS = HWPX_NAMESPACES["hs"]
HC_NS = HWPX_NAMESPACES["hc"]
HH_NS = HWPX_NAMESPACES["hh"]
OPF_NS = HWPX_NAMESPACES["opf"]

_RICH_TOKEN_RE = re.compile(
    r"(\$\$[\s\S]+?\$\$|\*\*__.+?__\*\*|__\*\*.+?\*\*__|\*\*[^*]+?\*\*|__[^_]+?__|_\{[^{}]+\}|\^\{[^{}]+\})"
)


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
    root = ET.Element(_qname(HH_NS, "head"), {"version": "1.4", "secCnt": "1"})
    ref_list = ET.SubElement(root, _qname(HH_NS, "refList"))
    border_fills = ET.SubElement(ref_list, _qname(HH_NS, "borderFills"), {"itemCnt": "2"})
    bf_none = ET.SubElement(
        border_fills,
        _qname(HH_NS, "borderFill"),
        {"id": "1", "threeD": "0", "shadow": "0", "centerLine": "NONE", "breakCellSeparateLine": "0"},
    )
    ET.SubElement(bf_none, _qname(HH_NS, "slash"), {"type": "NONE", "Crooked": "0", "isCounter": "0"})
    ET.SubElement(bf_none, _qname(HH_NS, "backSlash"), {"type": "NONE", "Crooked": "0", "isCounter": "0"})
    for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
        ET.SubElement(bf_none, _qname(HH_NS, side), {"type": "NONE", "width": "0.1 mm", "color": "#000000"})
    ET.SubElement(bf_none, _qname(HH_NS, "diagonal"), {"type": "SOLID", "width": "0.1 mm", "color": "#000000"})

    bf_solid = ET.SubElement(
        border_fills,
        _qname(HH_NS, "borderFill"),
        {"id": "2", "threeD": "0", "shadow": "0", "centerLine": "NONE", "breakCellSeparateLine": "0"},
    )
    ET.SubElement(bf_solid, _qname(HH_NS, "slash"), {"type": "NONE", "Crooked": "0", "isCounter": "0"})
    ET.SubElement(bf_solid, _qname(HH_NS, "backSlash"), {"type": "NONE", "Crooked": "0", "isCounter": "0"})
    for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
        ET.SubElement(bf_solid, _qname(HH_NS, side), {"type": "SOLID", "width": "0.12 mm", "color": "#000000"})
    ET.SubElement(bf_solid, _qname(HH_NS, "diagonal"), {"type": "SOLID", "width": "0.1 mm", "color": "#000000"})

    ET.SubElement(ref_list, _qname(HH_NS, "charProperties"), {"itemCnt": "1"}).append(
        ET.Element(_qname(HH_NS, "charPr"), {"id": "0", "height": "1000", "textColor": "#000000", "shadeColor": "none"})
    )
    ET.SubElement(ref_list, _qname(HH_NS, "paraProperties"), {"itemCnt": "1"}).append(
        ET.Element(_qname(HH_NS, "paraPr"), {"id": "0"})
    )
    doc.entries[header_name] = _serialize_xml(root)
    return header_name, root


def _strip_custom_style_attrs(elem: Any) -> None:
    """OWPML 스키마 외 커스텀 속성(_eduflow_*)이 복제 전파되지 않도록 제거합니다."""
    for key in [k for k in list(elem.attrib.keys()) if k.startswith("_eduflow_")]:
        del elem.attrib[key]


def _ensure_table_border_fill_id(doc: HwpxDocument) -> str:
    """header.xml에서 4면 실선(SOLID) borderFill ID를 조회하고, 없으면 추가 후 ID를 반환합니다."""
    header_name, header_root = _ensure_header_root(doc)
    border_fills = None
    for node in header_root.iter():
        if _local_tag(node) == "borderFills":
            border_fills = node
            break
    if border_fills is None:
        ref_list = _find_child(header_root, "refList")
        if ref_list is None:
            ref_list = ET.SubElement(header_root, _qname(HH_NS, "refList"))
        border_fills = ET.SubElement(ref_list, _qname(HH_NS, "borderFills"))

    all_bfs = [ch for ch in border_fills if _local_tag(ch) == "borderFill"]
    fallback_solid_id: Optional[str] = None
    for bf in all_bfs:
        sides = {
            side: _find_child(bf, side)
            for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder")
        }
        if not all(
            el is not None and str(el.get("type", "")).strip().upper() == "SOLID"
            for el in sides.values()
        ):
            continue
        bf_id = str(bf.get("id", "")).strip()
        if not bf_id:
            continue
        if fallback_solid_id is None:
            fallback_solid_id = bf_id
        fill_brush = _find_child(bf, "fillBrush")
        win_brush = _find_child(fill_brush, "winBrush") if fill_brush is not None else None
        face_color = str(win_brush.get("faceColor", "")).strip().lower() if win_brush is not None else ""
        if face_color in ("", "none", "#ffffff", "transparent"):
            return bf_id

    if fallback_solid_id is not None:
        return fallback_solid_id

    new_id = _next_numeric_id(all_bfs)
    new_bf = ET.Element(
        _qname(HH_NS, "borderFill"),
        {
            "id": new_id,
            "threeD": "0",
            "shadow": "0",
            "centerLine": "NONE",
            "breakCellSeparateLine": "0",
        },
    )
    ET.SubElement(new_bf, _qname(HH_NS, "slash"), {"type": "NONE", "Crooked": "0", "isCounter": "0"})
    ET.SubElement(new_bf, _qname(HH_NS, "backSlash"), {"type": "NONE", "Crooked": "0", "isCounter": "0"})
    for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
        ET.SubElement(
            new_bf,
            _qname(HH_NS, side),
            {"type": "SOLID", "width": "0.12 mm", "color": "#000000"},
        )
    ET.SubElement(new_bf, _qname(HH_NS, "diagonal"), {"type": "SOLID", "width": "0.1 mm", "color": "#000000"})

    border_fills.append(new_bf)
    border_fills.set("itemCnt", str(len([c for c in border_fills if _local_tag(c) == "borderFill"])))
    doc.entries[header_name] = _serialize_xml(header_root)
    return new_id


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

    base_height = str(base_node.get("height", "1000"))
    try:
        target_height = str(max(500, int(int(base_height) * 0.65)))
    except Exception:
        target_height = base_height
    base_bold = _find_child(base_node, "bold") is not None
    base_color = str(base_node.get("textColor", "#000000"))
    other_script = "superscript" if script_type == "subscript" else "subscript"

    # 동일 속성의 기존 첨자 charPr 재사용 (비표준 속성 미사용)
    for ch in all_char_prs:
        if (
            _find_child(ch, script_type) is not None
            and _find_child(ch, other_script) is None
            and (_find_child(ch, "bold") is not None) == base_bold
            and str(ch.get("height", "1000")) == target_height
            and str(ch.get("textColor", "#000000")) == base_color
        ):
            return str(ch.get("id"))

    new_id = _next_numeric_id(all_char_prs)
    cloned = deepcopy(base_node)
    _strip_custom_style_attrs(cloned)
    cloned.set("id", new_id)
    cloned.set("height", target_height)
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
    _strip_custom_style_attrs(cloned)
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


def _char_pr_has_underline(char_pr_node: Any) -> bool:
    ul = _find_child(char_pr_node, "underline")
    return ul is not None and str(ul.get("type", "NONE")).strip().upper() != "NONE"


def _ensure_styled_char_pr(
    doc: HwpxDocument,
    base_char_pr_id: str = "0",
    *,
    bold: Optional[bool] = None,
    underline: Optional[bool] = None,
    font_size_pt: Optional[float] = None,
) -> str:
    """header.xml에서 charPr을 조회·복제·캐싱하여 bold/underline/font_size_pt 스타일 ID를 반환합니다."""
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

    resolved_base_id = str(base_node.get("id", "0"))
    if bold is None and underline is None and font_size_pt is None:
        return resolved_base_id

    base_has_bold = _find_child(base_node, "bold") is not None
    target_bold = bool(bold) if bold is not None else base_has_bold

    base_has_ul = _char_pr_has_underline(base_node)
    target_ul = bool(underline) if underline is not None else base_has_ul

    base_height = str(base_node.get("height", "1000"))
    if font_size_pt is not None:
        try:
            target_height = str(max(100, int(round(float(font_size_pt) * 100))))
        except (TypeError, ValueError):
            target_height = base_height
    else:
        target_height = base_height

    if target_bold == base_has_bold and target_ul == base_has_ul and target_height == base_height:
        return resolved_base_id

    base_color = str(base_node.get("textColor", "#000000"))
    for ch in all_char_prs:
        if (
            _find_child(ch, "subscript") is None
            and _find_child(ch, "superscript") is None
            and (_find_child(ch, "bold") is not None) == target_bold
            and _char_pr_has_underline(ch) == target_ul
            and str(ch.get("height", "1000")) == target_height
            and str(ch.get("textColor", "#000000")) == base_color
        ):
            return str(ch.get("id"))

    new_id = _next_numeric_id(all_char_prs)
    cloned = deepcopy(base_node)
    _strip_custom_style_attrs(cloned)
    cloned.set("id", new_id)
    cloned.set("height", target_height)

    existing_bold = _find_child(cloned, "bold")
    if target_bold and existing_bold is None:
        bold_el = ET.Element(_qname(HH_NS, "bold"))
        insert_before_tags = {"underline", "strikeout", "outline", "shadow", "subscript", "superscript"}
        ins_idx = next(
            (i for i, c in enumerate(list(cloned)) if _local_tag(c) in insert_before_tags),
            len(list(cloned)),
        )
        cloned.insert(ins_idx, bold_el)
    elif not target_bold and existing_bold is not None:
        cloned.remove(existing_bold)

    existing_ul = _find_child(cloned, "underline")
    if target_ul:
        if existing_ul is None:
            ul_el = ET.Element(
                _qname(HH_NS, "underline"),
                {"type": "BOTTOM", "shape": "SOLID", "color": base_color},
            )
            insert_before_tags = {"strikeout", "outline", "shadow", "subscript", "superscript"}
            ins_idx = next(
                (i for i, c in enumerate(list(cloned)) if _local_tag(c) in insert_before_tags),
                len(list(cloned)),
            )
            cloned.insert(ins_idx, ul_el)
        else:
            existing_ul.set("type", "BOTTOM")
            if not existing_ul.get("shape"):
                existing_ul.set("shape", "SOLID")
            if not existing_ul.get("color"):
                existing_ul.set("color", base_color)
    elif existing_ul is not None:
        existing_ul.set("type", "NONE")

    char_props.append(cloned)
    char_props.set("itemCnt", str(len([c for c in char_props if _local_tag(c) == "charPr"])))
    doc.entries[header_name] = _serialize_xml(header_root)
    return new_id


def _para_margin_sig(para_node: Any) -> Tuple[str, str]:
    margin_el = _find_child(para_node, "margin")
    if margin_el is None:
        return ("0", "0")
    intent_el = _find_child(margin_el, "intent")
    left_el = _find_child(margin_el, "left")
    return (
        str(intent_el.get("value", "0")) if intent_el is not None else "0",
        str(left_el.get("value", "0")) if left_el is not None else "0",
    )


def _ensure_styled_para_pr(
    doc: HwpxDocument,
    base_para_pr_id: str = "0",
    *,
    align: Optional[str] = None,
    page_break: Optional[bool] = None,
) -> str:
    """header.xml에서 paraPr을 조회·복제·캐싱하여 align/pageBreak 스타일 ID를 반환합니다."""
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

    resolved_base_id = str(base_node.get("id", "0"))
    align_norm = str(align).strip().upper() if align else ""
    if align_norm not in {"LEFT", "CENTER", "RIGHT", "JUSTIFY", "DISTRIBUTE"}:
        align_norm = ""

    if not align_norm and page_break is None:
        return resolved_base_id

    existing_align_el = _find_child(base_node, "align")
    base_align = str(existing_align_el.get("horizontal", "")).upper() if existing_align_el is not None else ""
    existing_break_el = _find_child(base_node, "breakSetting")
    base_pb = (existing_break_el.get("pageBreak") == "1") if existing_break_el is not None else False
    base_keep_next = (existing_break_el.get("keepWithNext") == "1") if existing_break_el is not None else False
    base_margin = _para_margin_sig(base_node)

    target_align = align_norm or base_align
    target_pb = bool(page_break) if page_break is not None else base_pb

    if target_align == base_align and target_pb == base_pb:
        return resolved_base_id

    for pr in all_para_prs:
        al_el = _find_child(pr, "align")
        pr_al = str(al_el.get("horizontal", "")).upper() if al_el is not None else ""
        br_el = _find_child(pr, "breakSetting")
        pr_pb = (br_el.get("pageBreak") == "1") if br_el is not None else False
        pr_kn = (br_el.get("keepWithNext") == "1") if br_el is not None else False
        if (
            pr_al == target_align
            and pr_pb == target_pb
            and pr_kn == base_keep_next
            and _para_margin_sig(pr) == base_margin
        ):
            return str(pr.get("id"))

    new_id = _next_numeric_id(all_para_prs)
    cloned = deepcopy(base_node)
    _strip_custom_style_attrs(cloned)
    cloned.set("id", new_id)

    if align_norm:
        align_el = _find_child(cloned, "align")
        if align_el is None:
            align_el = ET.Element(_qname(HH_NS, "align"))
            cloned.insert(0, align_el)
        align_el.set("horizontal", align_norm)
        if not align_el.get("vertical"):
            align_el.set("vertical", "BASELINE")

    if page_break is not None:
        break_el = _find_child(cloned, "breakSetting")
        if break_el is None:
            break_el = ET.SubElement(cloned, _qname(HH_NS, "breakSetting"))
        break_el.set("pageBreak", "1" if page_break else "0")

    para_props.append(cloned)
    para_props.set("itemCnt", str(len([c for c in para_props if _local_tag(c) == "paraPr"])))
    doc.entries[header_name] = _serialize_xml(header_root)
    return new_id


# =============================================================================
# 3. 문단 텍스트/첨자/수식 및 DOM 맵 추출
# =============================================================================
def _extract_paragraph_plain_text(p_elem: Any) -> str:
    parts: List[str] = []
    memo_depth = 0
    for node in p_elem.iter():
        tag = _local_tag(node)
        if tag == "fieldBegin" and str(node.get("type", "")).strip().upper() == "MEMO":
            memo_depth += 1
            continue
        if tag == "fieldEnd":
            if memo_depth > 0:
                memo_depth -= 1
            continue
        if memo_depth > 0:
            continue
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

_GREEK_LETTERS_PATTERN = (
    "alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|"
    "iota|kappa|lambda|mu|nu|xi|pi|varpi|rho|varrho|sigma|varsigma|"
    "tau|upsilon|phi|varphi|chi|psi|omega|"
    "Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Upsilon|Phi|Psi|Omega"
)


def _extract_braced_arg(text: str, start_idx: int) -> Optional[Tuple[str, int]]:
    """start_idx 이후 공백을 건너뛰고 중괄호 {...} 인자(중첩 지원)를 추출합니다."""
    n = len(text)
    i = start_idx
    while i < n and text[i].isspace():
        i += 1
    if i >= n or text[i] != "{":
        return None
    depth = 0
    content_start = i + 1
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[content_start:i], i + 1
        i += 1
    return None


def _replace_latex_frac_and_sqrt(text: str) -> str:
    """중첩 중괄호를 지원하는 \\frac{A}{B} -> {A} over {B}, \\sqrt{A} -> sqrt {A} 변환."""
    s = text
    # \frac{A}{B} (중첩 \frac 포함 반복 치환)
    while True:
        m = re.search(r"\\frac(?![A-Za-z])", s)
        if not m:
            break
        arg1 = _extract_braced_arg(s, m.end())
        if arg1 is None:
            break
        num_str, next_idx = arg1
        arg2 = _extract_braced_arg(s, next_idx)
        if arg2 is None:
            break
        den_str, end_idx = arg2
        s = f"{s[:m.start()]}{{{num_str}}} over {{{den_str}}}{s[end_idx:]}"

    # \sqrt{A}
    pos = 0
    while True:
        m = re.search(r"\\sqrt(?![A-Za-z])", s[pos:])
        if not m:
            break
        abs_start = pos + m.start()
        abs_end = pos + m.end()
        arg = _extract_braced_arg(s, abs_end)
        if arg is not None:
            inner, end_idx = arg
            repl = f"sqrt {{{inner}}}"
            s = s[:abs_start] + repl + s[end_idx:]
            pos = abs_start + len("sqrt {")
        else:
            s = s[:abs_start] + "sqrt " + s[abs_end:]
            pos = abs_start + len("sqrt ")
    return s


def _normalize_hwp_equation_script(script: str) -> str:
    """LaTeX 스타일 명령어(\\rm, \\it, \\frac, \\times 등) 및 붙어 쓴 rm/it 명령어를 한글 수식 문법으로 정규화합니다."""
    s = (script or "").strip()
    if not s:
        return ""

    s = _replace_latex_frac_and_sqrt(s)
    # \text{...}, \mathrm{...} -> rm {...}
    s = re.sub(r"\\(?:text|mathrm)\s*\{([^{}]*)\}", r"rm {\1}", s)

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
        (r"\\rightleftharpoons\b", "<-> "),
        (r"\\rightarrow\b", "-> "),
        (r"\\to\b", "-> "),
        (r"\\leftarrow\b", "<- "),
        (r"\\leftrightarrow\b", "<-> "),
        (rf"\\({_GREEK_LETTERS_PATTERN})\b", r"\1 "),
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
    is_tall = bool(re.search(r"\b(over|int|dint|oint|sum|lim)\b", eq_script))
    # 서식 키워드만 제거하고 times/cdot 등 실제 렌더링되는 기호는 2글자 폭으로 치환
    visible = re.sub(r"\b(rm|it|bold|over|sqrt|left|right)\b", "", eq_script)
    visible = re.sub(r"\b(times|cdot|div|pm|le|ge)\b", "XX", visible)
    visible = re.sub(r"[{}\s]", "", visible)
    vis_len = max(1, len(visible))
    width = max(1600, vis_len * 750 + 900)
    height = 2200 if is_tall else 1100
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
    is_tall = calc_h >= 2000

    if template_eq is not None:
        eq_el = deepcopy(template_eq)
        eq_el.set("id", new_id)
        if is_tall:
            eq_el.set("baseLine", "55")
        elif not eq_el.get("baseLine"):
            eq_el.set("baseLine", "85")
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
                "baseLine": "55" if is_tall else "85",
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
        pos_el.set("vertRelTo", "PARA")
        pos_el.set("horzRelTo", "PARA")
        pos_el.set("flowWithText", "1")

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
        elif (
            (tok.startswith("**__") and tok.endswith("__**") and len(tok) > 8)
            or (tok.startswith("__**") and tok.endswith("**__") and len(tok) > 8)
        ):
            inner_txt = tok[4:-4]
            styled_id = _ensure_styled_char_pr(doc, base_char_pr_id, bold=True, underline=True)
            run_el = ET.Element(_qname(HP_NS, "run"), {"charPrIDRef": styled_id})
            t_el = ET.SubElement(run_el, _qname(HP_NS, "t"))
            t_el.text = inner_txt
            runs.append(run_el)
        elif tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
            inner_txt = tok[2:-2]
            styled_id = _ensure_styled_char_pr(doc, base_char_pr_id, bold=True)
            run_el = ET.Element(_qname(HP_NS, "run"), {"charPrIDRef": styled_id})
            t_el = ET.SubElement(run_el, _qname(HP_NS, "t"))
            t_el.text = inner_txt
            runs.append(run_el)
        elif tok.startswith("__") and tok.endswith("__") and len(tok) > 4:
            inner_txt = tok[2:-2]
            styled_id = _ensure_styled_char_pr(doc, base_char_pr_id, underline=True)
            run_el = ET.Element(_qname(HP_NS, "run"), {"charPrIDRef": styled_id})
            t_el = ET.SubElement(run_el, _qname(HP_NS, "t"))
            t_el.text = inner_txt
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


def _run_has_sec_pr_or_ctrl(run_elem: Any) -> bool:
    """<hp:run> 내부에 섹션/단 제어(<hp:secPr>, <hp:ctrl>)가 포함되어 있는지 판별합니다."""
    if _local_tag(run_elem) != "run":
        return False
    return any(_local_tag(ch) in ("secPr", "ctrl") for ch in run_elem)


def _strip_sec_pr_and_ctrl(p_elem: Any) -> None:
    """문단 복제 시 <hp:secPr>/<hp:ctrl> 컨트롤과 linesegarray가 중복 전파되지 않도록 제거합니다."""
    for ch in list(p_elem):
        tag = _local_tag(ch)
        if tag == "linesegarray":
            p_elem.remove(ch)
        elif tag == "run":
            if _run_has_sec_pr_or_ctrl(ch):
                for sub in list(ch):
                    if _local_tag(sub) in ("secPr", "ctrl"):
                        ch.remove(sub)
                if len(list(ch)) == 0:
                    p_elem.remove(ch)


def _is_empty_secpr_placeholder(p_elem: Any) -> bool:
    """첫 문단이 <hp:secPr>만 담고 본문 텍스트/표/수식/페이지나누기가 없는 빈 플레이스홀더인지 판별합니다."""
    if _local_tag(p_elem) != "p":
        return False
    if p_elem.get("pageBreak") == "1":
        return False
    has_sec_pr = False
    for node in p_elem.iter():
        t = _local_tag(node)
        if t == "secPr":
            has_sec_pr = True
        elif t in ("tbl", "equation", "pic", "rect", "line", "ellipse"):
            return False
    if not has_sec_pr:
        return False
    return _extract_paragraph_plain_text(p_elem) == ""


_QUESTION_NUM_PREFIX_RE = re.compile(
    r"^(\s*(?:(?:서답형|서술형|논술형|단답형|문항|문제)\s*)?\d+\s*[.)]\s*)"
)


def _set_paragraph_rich_text(
    doc: HwpxDocument,
    p_elem: Any,
    text: str,
    section_roots: Optional[Dict[str, Any]] = None,
    id_counter: Optional[List[int]] = None,
    base_char_pr_id: Optional[str] = None,
) -> None:
    """
    텍스트 내 _{...}(아래첨자), ^{...}(위첨자), $$...$$(한글 수식), **굵게**, __밑줄__을 파싱하여
    <hp:run> 노드로 분리 생성하고 <hp:linesegarray>를 제거합니다.
    첫 문단의 <hp:secPr>/<hp:ctrl> 컨트롤 run은 보존합니다.
    기존 문단에 여러 run의 charPrIDRef가 섞여 있고 새 텍스트가 번호 접두사로 시작하면
    첫 번째와 두 번째 run의 charPrIDRef를 각각 유지합니다.
    """
    existing_non_sec_runs = [
        ch for ch in p_elem if _local_tag(ch) == "run" and not _run_has_sec_pr_or_ctrl(ch)
    ]
    first_run = (
        existing_non_sec_runs[0]
        if existing_non_sec_runs
        else next((ch for ch in p_elem if _local_tag(ch) == "run"), None)
    )
    resolved_char_pr_id = (
        str(base_char_pr_id)
        if base_char_pr_id is not None
        else (str(first_run.get("charPrIDRef", "0")) if first_run is not None else "0")
    )

    split_prefix_styles: Optional[Tuple[str, str, str, str]] = None
    if base_char_pr_id is None and len(existing_non_sec_runs) >= 2:
        cp0 = str(existing_non_sec_runs[0].get("charPrIDRef", "0"))
        cp1 = str(existing_non_sec_runs[1].get("charPrIDRef", "0"))
        if cp0 != cp1:
            m = _QUESTION_NUM_PREFIX_RE.match(text or "")
            if m:
                prefix = m.group(1)
                rest = (text or "")[m.end():]
                split_prefix_styles = (prefix, cp0, rest, cp1)

    # secPr/ctrl 컨트롤 run은 보존하고, 일반 본문 run / linesegarray만 제거
    for ch in list(p_elem):
        tag = _local_tag(ch)
        if tag == "linesegarray":
            p_elem.remove(ch)
        elif tag == "run":
            if _run_has_sec_pr_or_ctrl(ch):
                for sub in list(ch):
                    if _local_tag(sub) not in ("secPr", "ctrl"):
                        ch.remove(sub)
            else:
                p_elem.remove(ch)

    counter = id_counter if id_counter is not None else [0]
    if split_prefix_styles is not None:
        prefix, cp0, rest, cp1 = split_prefix_styles
        for run_el in _build_rich_runs(
            doc, prefix, base_char_pr_id=cp0, section_roots=section_roots, id_counter=counter
        ):
            p_elem.append(run_el)
        if rest:
            for run_el in _build_rich_runs(
                doc, rest, base_char_pr_id=cp1, section_roots=section_roots, id_counter=counter
            ):
                p_elem.append(run_el)
        return

    for run_el in _build_rich_runs(
        doc, text, base_char_pr_id=resolved_char_pr_id, section_roots=section_roots, id_counter=counter
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


def _is_equation_run(run_elem: Any) -> bool:
    """<hp:equation> 수식 노드를 포함하는 <hp:run>인지 판별합니다."""
    if _local_tag(run_elem) != "run":
        return False
    return any(_local_tag(ch) == "equation" for ch in run_elem)


def _is_replaceable_run(run_elem: Any) -> bool:
    """텍스트 치환 대상으로 취급할 수 있는 일반 텍스트 또는 수식 <hp:run>인지 판별합니다."""
    return _is_plain_text_run(run_elem) or _is_equation_run(run_elem)


def _extract_run_text(run_elem: Any) -> str:
    parts: List[str] = []
    for ch in run_elem:
        tag = _local_tag(ch)
        if tag == "t" and ch.text:
            parts.append(ch.text)
        elif tag == "equation":
            sc = _find_child(ch, "script")
            if sc is not None and sc.text:
                parts.append(f"$${sc.text}$$")
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


def _find_normalized_match_spans(text: str, old_text: str) -> List[Tuple[int, int]]:
    """
    원문에서 old_text와 일치하는 (start, end) 구간 목록을 찾습니다.
    1) 원문 정확 일치
    2) 위/아래첨자·NBSP 정규화 일치
    3) 연속 공백/줄바꿈 축약 일치
    4) 공백 완전 제거 일치
    5) 공백 및 중괄호({}) 제거 일치 (수식 내 {CH} vs CH 차이 보정)
    """
    if not old_text:
        return []

    spans: List[Tuple[int, int]] = []
    if old_text in text:
        pos = 0
        while True:
            found = text.find(old_text, pos)
            if found == -1:
                break
            spans.append((found, found + len(old_text)))
            pos = found + len(old_text)
        return spans

    norm_old = _normalize_match_text(old_text)
    if not norm_old:
        return []
    norm_text = _normalize_match_text(text)
    if norm_old in norm_text:
        pos = 0
        while True:
            found = norm_text.find(norm_old, pos)
            if found == -1:
                break
            spans.append((found, found + len(norm_old)))
            pos = found + len(norm_old)
        return spans

    # 3) 연속 공백/줄바꿈 축약 매칭 (원본 인덱스 매핑 유지)
    collapsed_old = re.sub(r"\s+", " ", norm_old).strip()
    if not collapsed_old:
        return []

    c_chars: List[str] = []
    c_starts: List[int] = []
    c_ends: List[int] = []
    i = 0
    n = len(norm_text)
    while i < n:
        if norm_text[i].isspace():
            j = i + 1
            while j < n and norm_text[j].isspace():
                j += 1
            if c_chars:
                c_chars.append(" ")
                c_starts.append(i)
                c_ends.append(j)
            i = j
        else:
            c_chars.append(norm_text[i])
            c_starts.append(i)
            c_ends.append(i + 1)
            i += 1
    if c_chars and c_chars[-1] == " ":
        c_chars.pop()
        c_starts.pop()
        c_ends.pop()

    collapsed_text = "".join(c_chars)
    if collapsed_old in collapsed_text:
        pos = 0
        while True:
            found = collapsed_text.find(collapsed_old, pos)
            if found == -1:
                break
            orig_start = c_starts[found]
            orig_end = c_ends[found + len(collapsed_old) - 1]
            spans.append((orig_start, orig_end))
            pos = found + len(collapsed_old)
        return spans

    # 4) 공백 완전 제거 비교 (원본 인덱스 역매핑 유지)
    no_ws_old = "".join(ch for ch in norm_old if not ch.isspace())
    if no_ws_old:
        no_ws_chars: List[str] = []
        no_ws_indices: List[int] = []
        for idx, ch in enumerate(norm_text):
            if not ch.isspace():
                no_ws_chars.append(ch)
                no_ws_indices.append(idx)
        no_ws_text = "".join(no_ws_chars)
        if no_ws_old in no_ws_text:
            pos = 0
            while True:
                found = no_ws_text.find(no_ws_old, pos)
                if found == -1:
                    break
                orig_start = no_ws_indices[found]
                orig_end = no_ws_indices[found + len(no_ws_old) - 1] + 1
                spans.append((orig_start, orig_end))
                pos = found + len(no_ws_old)
            return spans

    # 5) 공백 및 중괄호({}) 제거 비교 (예: "$$rm {CH}_{3}COOH(it aq)$$" vs "$$rmCH_{3}COOH(itaq)$$")
    if "$$" in norm_old or "{" in norm_old or "{" in norm_text:
        no_brace_old = "".join(ch for ch in norm_old if not ch.isspace() and ch not in "{}")
        if not no_brace_old:
            return []
        nb_chars: List[str] = []
        nb_indices: List[int] = []
        for idx, ch in enumerate(norm_text):
            if not ch.isspace() and ch not in "{}":
                nb_chars.append(ch)
                nb_indices.append(idx)
        nb_text = "".join(nb_chars)
        if no_brace_old in nb_text:
            pos = 0
            while True:
                found = nb_text.find(no_brace_old, pos)
                if found == -1:
                    break
                orig_start = nb_indices[found]
                orig_end = nb_indices[found + len(no_brace_old) - 1] + 1
                spans.append((orig_start, orig_end))
                pos = found + len(no_brace_old)
    return spans


def _replace_with_normalized_match(
    text: str, old_text: str, new_text: str
) -> Tuple[str, bool]:
    """
    원문에서 old_text를 찾되, 완전 일치가 없으면 위/아래첨자·NBSP·연속 공백 정규화 인덱스 매핑으로
    원문 내 실제 구간을 찾아 new_text로 치환합니다.
    """
    spans = _find_normalized_match_spans(text, old_text)
    if not spans:
        return text, False
    parts: List[str] = []
    cur = 0
    for s_idx, e_idx in spans:
        parts.append(text[cur:s_idx])
        parts.append(new_text)
        cur = e_idx
    parts.append(text[cur:])
    return "".join(parts), True


def _make_plain_run(cid: str, text: str) -> Any:
    run_el = ET.Element(_qname(HP_NS, "run"), {"charPrIDRef": cid})
    t_el = ET.SubElement(run_el, _qname(HP_NS, "t"))
    t_el.text = text
    return run_el


def _replace_text_in_paragraph(
    doc: HwpxDocument,
    p_elem: Any,
    old_text: str,
    new_text: str,
    section_roots: Optional[Dict[str, Any]] = None,
    id_counter: Optional[List[int]] = None,
) -> bool:
    """
    문단 내 일반 텍스트 <hp:run>과 수식(<hp:equation>) <hp:run> 모두에서
    old_text를 찾아 새 텍스트/수식 <hp:run>으로 부분 치환합니다.
    치환되지 않은 나머지 수식/컨트롤 <hp:run>은 원본 그대로 보존됩니다.
    """
    if not old_text:
        return False

    counter = id_counter if id_counter is not None else [0]
    replaced = False

    # 1차: 단일 <hp:run>(일반 텍스트 또는 수식) 내부 치환
    for child in list(p_elem):
        if not _is_replaceable_run(child):
            continue
        r_text = _extract_run_text(child)
        updated, did_replace = _replace_with_normalized_match(r_text, old_text, new_text)
        if did_replace:
            base_cid = str(child.get("charPrIDRef", "0"))
            new_runs = _build_rich_runs(
                doc, updated, base_char_pr_id=base_cid, section_roots=section_roots, id_counter=counter
            )
            idx = list(p_elem).index(child)
            p_elem.remove(child)
            for offset, nr in enumerate(new_runs):
                p_elem.insert(idx + offset, nr)
            replaced = True

    # 2차: 연속된 <hp:run>(텍스트 + 수식 조합 포함) 여러 개에 걸쳐 old_text가 분할된 경우
    if not replaced:
        children = list(p_elem)
        seg_runs: List[Any] = []

        def _flush_segment(segment: List[Any]) -> bool:
            if not segment:
                return False

            run_infos: List[Tuple[Any, str, str, int, int]] = []
            offset = 0
            for r in segment:
                r_txt = _extract_run_text(r)
                r_cid = str(r.get("charPrIDRef", "0"))
                run_infos.append((r, r_txt, r_cid, offset, offset + len(r_txt)))
                offset += len(r_txt)

            combined = "".join(info[1] for info in run_infos)
            matches = _find_normalized_match_spans(combined, old_text)

            if not matches:
                return False

            new_segment_runs: List[Any] = []
            cur = 0

            def _emit_plain_range(range_start: int, range_end: int) -> None:
                if range_end <= range_start:
                    return
                for r_el, r_txt, r_cid, r_start, r_end in run_infos:
                    if r_end <= range_start or r_start >= range_end:
                        continue
                    if r_start >= range_start and r_end <= range_end:
                        # 치환 구간과 겹치지 않는 run(수식 포함)은 원본 그대로 유지
                        new_segment_runs.append(r_el)
                    else:
                        s_lo = max(range_start, r_start) - r_start
                        s_hi = min(range_end, r_end) - r_start
                        part = r_txt[s_lo:s_hi]
                        if part:
                            if "$$" in part:
                                new_segment_runs.extend(
                                    _build_rich_runs(
                                        doc,
                                        part,
                                        base_char_pr_id=r_cid,
                                        section_roots=section_roots,
                                        id_counter=counter,
                                    )
                                )
                            else:
                                new_segment_runs.append(_make_plain_run(r_cid, part))

            for m_start, m_end in matches:
                _emit_plain_range(cur, m_start)

                start_cid = str(segment[0].get("charPrIDRef", "0"))
                for _, _, r_cid, r_start, r_end in run_infos:
                    if r_start <= m_start < r_end:
                        start_cid = r_cid
                        break

                new_segment_runs.extend(
                    _build_rich_runs(
                        doc,
                        new_text,
                        base_char_pr_id=start_cid,
                        section_roots=section_roots,
                        id_counter=counter,
                    )
                )
                cur = m_end

            _emit_plain_range(cur, len(combined))

            insert_idx = list(p_elem).index(segment[0])
            for r in segment:
                p_elem.remove(r)
            for ins_offset, nr in enumerate(new_segment_runs):
                p_elem.insert(insert_idx + ins_offset, nr)
            return True

        for ch in children:
            if _is_replaceable_run(ch):
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


def _get_header_style_maps(
    doc: HwpxDocument,
) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """header.xml에서 script_map, char_style_map(bold/font_size_pt), para_style_map(align/page_break)을 읽습니다."""
    script_map: Dict[str, str] = {}
    char_style_map: Dict[str, Dict[str, Any]] = {}
    para_style_map: Dict[str, Dict[str, Any]] = {}
    header_name = next((n for n in doc.entries if n.lower() == "contents/header.xml"), None)
    if not header_name:
        return script_map, char_style_map, para_style_map
    try:
        root = _parse_xml(doc.entries[header_name])
        for node in root.iter():
            tag = _local_tag(node)
            if tag == "charPr":
                cid = str(node.get("id", ""))
                if _find_child(node, "subscript") is not None:
                    script_map[cid] = "sub"
                elif _find_child(node, "superscript") is not None:
                    script_map[cid] = "sup"
                is_bold = _find_child(node, "bold") is not None
                font_pt: Optional[float] = None
                raw_h = node.get("height")
                if raw_h is not None:
                    try:
                        font_pt = round(int(raw_h) / 100.0, 2)
                    except (TypeError, ValueError):
                        font_pt = None
                char_style_map[cid] = {"bold": is_bold, "font_size_pt": font_pt}
            elif tag == "paraPr":
                pid = str(node.get("id", ""))
                align_el = _find_child(node, "align")
                align_val = (
                    str(align_el.get("horizontal", "")).strip().upper()
                    if align_el is not None
                    else ""
                )
                break_el = _find_child(node, "breakSetting")
                is_pb = bool(break_el is not None and break_el.get("pageBreak") == "1")
                para_style_map[pid] = {"align": align_val, "page_break": is_pb}
    except Exception:
        pass
    return script_map, char_style_map, para_style_map


def _collect_dom_from_roots(
    section_roots: Dict[str, Any],
    para_style_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Tuple[str, Any, Any]], List[Dict[str, Any]]]:
    """파싱된 section_roots 트리에서 (id_map, dom_blocks)를 추출합니다."""
    id_map: Dict[str, Tuple[str, Any, Any]] = {}
    blocks: List[Dict[str, Any]] = []
    p_style_map = para_style_map or {}

    p_counter = 0
    tbl_counter = 0

    for sec_name, root in section_roots.items():
        top_children = list(root)
        if (
            len(section_roots) == 1
            and len(top_children) == 1
            and _is_empty_secpr_placeholder(top_children[0])
        ):
            continue

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
                        para_id_ref = str(child.get("paraPrIDRef", ""))
                        is_pb = (child.get("pageBreak") == "1") or bool(
                            p_style_map.get(para_id_ref, {}).get("page_break")
                        )
                        blk_info: Dict[str, Any] = {
                            "id": pid,
                            "type": "paragraph",
                            "text": _extract_paragraph_plain_text(child),
                        }
                        if is_pb:
                            blk_info["page_break"] = True
                        blocks.append(blk_info)
                else:
                    _walk(child)

        _walk(root)

    return id_map, blocks


def _collect_section_dom(doc: HwpxDocument) -> Tuple[Dict[str, Tuple[str, Any, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """
    모든 section*.xml을 파싱하여 (id_map, dom_blocks, section_roots)를 반환합니다.
    id_map[node_id] = (section_name, parent_elem, target_elem)
    """
    section_roots: Dict[str, Any] = {}
    for sec_name in doc.section_names():
        section_roots[sec_name] = _parse_xml(doc.entries[sec_name])

    _, _, para_style_map = _get_header_style_maps(doc)
    id_map, blocks = _collect_dom_from_roots(section_roots, para_style_map=para_style_map)
    return id_map, blocks, section_roots


def _find_dom_id_for_element(section_roots: Dict[str, Any], target_elem: Any) -> Optional[str]:
    """수정된 section_roots 안에서 target_elem에 해당하는 최신 DOM ID(예: p_3, tbl_1)를 찾습니다."""
    id_map, _ = _collect_dom_from_roots(section_roots)
    for dom_id, (_, _, elem) in id_map.items():
        if elem is target_elem:
            return dom_id
    return None


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


def _compact_dom_map_for_prompt(dom_map: Dict[str, Any], max_chars: int = 45000) -> str:
    """dom_map을 JSON 문자열로 직렬화하되 max_chars 초과 시 유효한 JSON 형태를 유지하며 경량화합니다."""
    dom_json = json.dumps(dom_map, ensure_ascii=False, separators=(",", ":"))
    if len(dom_json) <= max_chars:
        return dom_json

    compact = deepcopy(dom_map) if isinstance(dom_map, dict) else {"blocks": []}
    blocks = compact.get("blocks")
    if not isinstance(blocks, list):
        return json.dumps({"truncated": True}, ensure_ascii=False, separators=(",", ":"))

    # 1단계: 연속된 빈 문단 축약
    collapsed: List[Dict[str, Any]] = []
    empty_run_start: Optional[Dict[str, Any]] = None
    empty_run_cnt = 0

    def _flush_empty_run() -> None:
        nonlocal empty_run_start, empty_run_cnt
        if empty_run_start is not None:
            entry = dict(empty_run_start)
            if empty_run_cnt > 1:
                entry["omitted_empty_count"] = empty_run_cnt - 1
            collapsed.append(entry)
            empty_run_start = None
            empty_run_cnt = 0

    for b in blocks:
        is_empty_p = (
            isinstance(b, dict)
            and b.get("type") == "paragraph"
            and not str(b.get("text", "")).strip()
            and not b.get("page_break")
        )
        if is_empty_p:
            if empty_run_start is None:
                empty_run_start = b
            empty_run_cnt += 1
        else:
            _flush_empty_run()
            collapsed.append(b)
    _flush_empty_run()
    compact["blocks"] = collapsed

    dom_json = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(dom_json) <= max_chars:
        return dom_json

    # 2단계: 긴 텍스트/표 셀 요약
    for b in compact["blocks"]:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "paragraph":
            txt = str(b.get("text", ""))
            if len(txt) > 200:
                b["text"] = txt[:200] + "..."
        elif b.get("type") == "table":
            rows = b.get("rows")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, list):
                        for cell in row:
                            if isinstance(cell, dict):
                                ctxt = str(cell.get("text", ""))
                                if len(ctxt) > 120:
                                    cell["text"] = ctxt[:120] + "..."
                if len(rows) > 8:
                    omitted_r = len(rows) - 6
                    b["rows"] = rows[:4] + [[{"id": f"{b.get('id','')}_omitted", "text": f"...({omitted_r}행 생략)"}]] + rows[-2:]

    dom_json = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(dom_json) <= max_chars:
        return dom_json

    # 3단계: 블록 단위 축약으로 유효한 JSON 유지
    compact["truncated"] = True
    cur_blocks = list(compact["blocks"])
    while len(cur_blocks) > 2:
        head_cnt = max(1, len(cur_blocks) // 3)
        tail_cnt = max(1, len(cur_blocks) // 3)
        if head_cnt + tail_cnt >= len(cur_blocks):
            cur_blocks = cur_blocks[:1] + cur_blocks[-1:]
        else:
            omitted = len(cur_blocks) - head_cnt - tail_cnt
            cur_blocks = (
                cur_blocks[:head_cnt]
                + [{"id": "_omitted", "type": "summary", "omitted_blocks": omitted}]
                + cur_blocks[-tail_cnt:]
            )
        compact["blocks"] = cur_blocks
        dom_json = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(dom_json) <= max_chars:
            return dom_json

    return dom_json


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
        "10. append_paragraph: {\"op\":\"append_paragraph\", \"text\":\"...\", \"bold\":false, \"font_size_pt\":10.0, \"align\":\"LEFT\"|\"CENTER\"|\"RIGHT\"}\n"
        "11. append_table: {\"op\":\"append_table\", \"headers\":[\"항목\",\"내용\"], \"rows\":[[\"...\",\"...\"]], \"col_widths_ratio\":[1,3]}\n"
        "12. insert_table_after: {\"op\":\"insert_table_after\", \"target_id\":\"p_0\", \"headers\":[\"항목\",\"내용\"], \"rows\":[[\"...\",\"...\"]], \"col_widths_ratio\":[1,3]}\n"
        "13. page_break: {\"op\":\"page_break\"}\n"
        "14. clone_block: {\"op\":\"clone_block\", \"source_id\":\"tbl_5\", \"after_id\":\"p_12\"}\n"
        "15. delete_block: {\"op\":\"delete_block\", \"target_id\":\"p_5\"|\"tbl_2\"}\n"
        "핵심 규칙:\n"
        "- 기존 서식에 `<보기>` 상자나 선택지 표(예: 3x3 상자, ①~⑤ 표)가 이미 있는 문서에 문항을 추가할 때는 `insert_exam_question` 대신 `clone_block`으로 기존 문항/표 서식을 복제한 뒤 `set_paragraph_text`/`update_cell`로 내용만 채우는 방식을 우선 사용하세요.\n"
        "- replace_text를 사용할 때 old_text는 DOM 맵에 있는 원문(공백, 중괄호 {}, $$...$$ 포함)을 임의로 고치지 말고 단 한 글자도 바꾸지 않은 채 그대로 복사하세요.\n"
        "- 표 셀(tbl_...) 내부의 수식($$...$$)이나 여러 줄 텍스트를 수정할 때는 replace_text 대신 update_cell을 우선 사용해도 됩니다.\n"
        "- new_text에는 지시사항에 맞게 실제로 수정된 최종 텍스트/수식을 반드시 반영하세요(원문 그대로 반환 금지).\n"
        "- 화학식/첨자/수식은 H_{2}O, Fe^{3+}, $$x^2 + y^2 = r^2$$, $$rm {CH}_{3}COOH(it {aq})$$처럼 "
        "한글 수식 문법(rm, it 뒤 공백 유지, 다문자 피연산자는 중괄호 {}로 감싸기, 백슬래시 금지)을 사용하세요."
    )
    dom_json = _compact_dom_map_for_prompt(dom_map, max_chars=45000)

    user_prompt = (
        f"### [교사 수정 지시사항]\n{user_instruction.strip()}\n\n"
        f"### [출력 형식 - 필수 준수]\n"
        f"```json\n"
        f'{{\n  "summary": "변경 사항 한 줄 요약",\n  "operations": [\n    {{"op": "replace_text", "target_id": "p_0", "old_text": "...", "new_text": "..."}}\n  ]\n}}\n'
        f"```\n\n"
        f"### [현재 HWPX 경량 DOM 맵]\n```json\n{dom_json}\n```"
    )
    return system_prompt, user_prompt


def build_hwpx_code_mode_prompt(dom_map: Dict[str, Any], user_instruction: str) -> Tuple[str, str]:
    """샌드박스 Python(lxml) 편집 모드용 (system_prompt, user_prompt)를 생성합니다."""
    system_prompt = (
        "당신은 대한민국 교직 실무용 HWPX 문서의 XML(lxml)을 직접 편집하는 Python 코드 생성 에이전트입니다.\n"
        "반드시 아래 시그니처의 함수 1개만 ```python ... ``` 코드블록 안에 작성하세요:\n"
        "```python\n"
        "def edit_hwpx(sec_root, helpers):\n"
        "    ...\n"
        "```\n\n"
        "보안 및 문법 제약 (위반 시 즉시 차단됨):\n"
        "- `import` / `from ... import` 문 절대 금지 (이미 전역에 `ET`, `deepcopy`, `re`, `helpers`가 주입되어 있음).\n"
        "- `open`, `eval`, `exec`, `compile`, `__import__`, `getattr`, `setattr`, `delattr`, `globals`, `locals` 및 `__` 던더 속성 접근 절대 금지.\n\n"
        "핵심 편집 규칙:\n"
        "1. 새 XML 태그를 맨땅에서 조립하지 말고, 기존 `sec_root`의 문단(`<hp:p>`)이나 표(`<hp:tbl>`) 블록을 `deepcopy`로 복제한 뒤 `helpers`로 텍스트와 ID만 갱신하세요.\n"
        "2. `secPr` 보존: 첫 문단 등에 들어 있는 `<hp:secPr>`/`<hp:ctrl>`은 절대 삭제하거나 손상시키지 마세요.\n"
        "3. 복제한 모든 블록에는 반드시 `helpers['reassign_cloned_ids'](cloned_elem)`을 호출해 `tbl`/`rect`/`p`/`equation`의 `id` 중복과 `linesegarray` 잔존을 방지하세요.\n"
        "4. 2단 편집 및 문항 서식 안내:\n"
        "   - `<보기>` 표나 `①~⑤` 선택지 표가 기존 문서에 있으면 그 표를 감싼 `<hp:p>`(또는 `helpers['wrap_table_in_paragraph'](tbl_clone)`)를 복제해 고정 2단 폭과 테두리 서식을 그대로 유지하세요.\n"
        "   - 새 너비 계산이 필요하면 `helpers['compute_content_width']()`로 현재 문서의 실제 1단 너비를 조회하세요.\n"
        "5. 사용 가능한 `helpers` 목록:\n"
        "   - `helpers['collect_dom_ids']()` -> `dict` (`id_map[dom_id] = (sec_name, parent_elem, target_elem)`)\n"
        "   - `helpers['set_paragraph_rich_text'](p_elem, text, base_char_pr_id=None)` (**굵게**, __밑줄__, **__굵게+밑줄__**, _{...}, ^{...}, $$...$$ 지원)\n"
        "   - `helpers['extract_paragraph_plain_text'](p_elem)` -> `str`\n"
        "   - `helpers['next_object_id']()` -> `str`\n"
        "   - `helpers['reassign_cloned_ids'](elem)` -> `None`\n"
        "   - `helpers['strip_sec_pr_and_ctrl'](p_elem)` / `helpers['strip_linesegarray'](p_elem)`\n"
        "   - `helpers['wrap_table_in_paragraph'](tbl_elem)` -> `<hp:p>`\n"
        "   - `helpers['local_tag'](elem)` / `helpers['find_child'](parent, tag_name)` / `helpers['HP_NS']`\n"
    )
    dom_json = _compact_dom_map_for_prompt(dom_map, max_chars=45000)
    user_prompt = (
        f"### [교사 복합 편집 지시사항]\n{user_instruction.strip()}\n\n"
        f"### [출력 형식 - 필수 준수]\n"
        f"`def edit_hwpx(sec_root, helpers):` 함수 1개만 ```python ... ``` 블록으로 출력하세요.\n\n"
        f"### [현재 HWPX 경량 DOM 맵]\n```json\n{dom_json}\n```"
    )
    return system_prompt, user_prompt


_CODE_MODE_COMPLEX_PATTERNS = (
    r"서식\s*복제",
    r"기존\s*서식",
    r"문항.*구성",
    r"유형별",
    r"틀\s*배치",
    r"<보기>.*복제",
    r"보기.*상자.*복제",
    r"선택지\s*표",
    r"레이아웃\s*재구성",
    r"Python\s*코드\s*모드",
    r"code_mode",
)


def should_use_hwpx_code_mode(
    user_instruction: str,
    dom_map: Optional[Dict[str, Any]] = None,
) -> bool:
    """지시사항이 복합 양식 변형/서식 복제 작업인지 판별해 code_mode 라우팅 여부를 반환합니다."""
    inst = str(user_instruction or "").strip()
    if not inst:
        return False
    for pat in _CODE_MODE_COMPLEX_PATTERNS:
        if re.search(pat, inst, re.IGNORECASE):
            return True
    return False


def execute_hwpx_hybrid_edit(
    doc: HwpxDocument,
    user_instruction: str,
    llm_generate_fn: Callable[[str, str], str],
    *,
    dom_map: Optional[Dict[str, Any]] = None,
    max_retries: int = 2,
    force_code_mode: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    단순 작업은 JSON Patch 경로로, 복합 양식 변형 또는 JSON Patch 실패 시에는
    샌드박스 Python(lxml) 코드 모드 + 최대 2회 Self-Correction 루프로 실행합니다.
    """
    from services.hwpx_sandbox_runner import (
        extract_python_code_from_llm_response,
        run_hwpx_edit_code,
    )
    from services.llm_client import extract_json_from_llm_response

    cur_dom = dom_map if isinstance(dom_map, dict) else extract_hwpx_dom_map(doc)
    use_code = (
        bool(force_code_mode)
        if force_code_mode is not None
        else should_use_hwpx_code_mode(user_instruction, cur_dom)
    )

    if not use_code:
        sys_p, usr_p = build_hwpx_agent_prompt(cur_dom, user_instruction)
        raw_reply = llm_generate_fn(usr_p, sys_p)
        try:
            parsed = extract_json_from_llm_response(raw_reply)
            ops = parsed.get("operations") if isinstance(parsed, dict) else None
            if isinstance(ops, list) and ops:
                patch_res = apply_hwpx_patch(doc, [o for o in ops if isinstance(o, dict)])
                if patch_res.get("applied_count", 0) > 0:
                    return {
                        "mode": "json_patch",
                        "success": True,
                        "parsed_json": parsed,
                        "raw_response": raw_reply,
                        "patch_result": patch_res,
                        "html_preview": render_hwpx_to_html(doc),
                    }
        except Exception:
            pass
        # JSON Patch 추출 또는 적용 실패 시 code_mode로 폴백

    sys_p, base_usr_p = build_hwpx_code_mode_prompt(cur_dom, user_instruction)
    cur_usr_p = base_usr_p
    last_error = ""
    last_raw = ""
    last_code = ""

    for attempt in range(max_retries + 1):
        raw_reply = llm_generate_fn(cur_usr_p, sys_p)
        last_raw = raw_reply
        code = extract_python_code_from_llm_response(raw_reply)
        last_code = code
        run_res = run_hwpx_edit_code(doc, code)
        if run_res.get("success"):
            return {
                "mode": "code_mode",
                "success": True,
                "code": code,
                "raw_response": raw_reply,
                "attempts": attempt + 1,
                "html_preview": render_hwpx_to_html(doc),
            }
        last_error = str(run_res.get("error", "알 수 없는 오류"))
        cur_usr_p = (
            f"{base_usr_p}\n\n"
            f"### [이전 코드 실행 오류 - 원인 수정 후 `def edit_hwpx(sec_root, helpers):` 전체를 다시 작성하세요]\n"
            f"```text\n{last_error}\n```"
        )

    return {
        "mode": "code_mode",
        "success": False,
        "code": last_code,
        "raw_response": last_raw,
        "error": last_error,
        "attempts": max_retries + 1,
    }


def _summarize_dom_map_for_create(dom_map: Optional[Dict[str, Any]]) -> str:
    """분할 생성 Part 2~N에 전달할 직전 DOM 상태 요약(마지막 블록 ID 및 표 행 정보 포함)을 생성합니다."""
    if not isinstance(dom_map, dict):
        return "(현재 문서 비어 있음)"
    blocks = dom_map.get("blocks") or []
    if not blocks:
        return "(현재 문서 비어 있음 — block_count=0)"

    lines: List[str] = [
        f"- 전체 블록 수: {len(blocks)}개, 마지막 블록 ID: {blocks[-1].get('id', '')}"
    ]
    # 최근 블록 요약 (최대 12개) + 기존 표 목록 요약
    table_summaries: List[str] = []
    for b in blocks:
        if b.get("type") == "table":
            rows = b.get("rows") or []
            last_row_idx = max(0, len(rows) - 1)
            hdr_preview = " | ".join(str(c.get("text", "")).replace("\n", " ")[:20] for c in (rows[0] if rows else []))
            table_summaries.append(
                f"  * {b.get('id')}: {len(rows)}행 (add_table_row 시 after_row={last_row_idx} 활용 가능, 첫 행: {hdr_preview})"
            )
    if table_summaries:
        lines.append("- 현재 문서 내 표 목록:")
        lines.extend(table_summaries[-6:])

    tail_blocks = blocks[-12:]
    if len(blocks) > len(tail_blocks):
        lines.append(f"- 앞부분 {len(blocks) - len(tail_blocks)}개 블록 생략 후 최근 {len(tail_blocks)}개 블록:")
    else:
        lines.append("- 현재 블록 목록:")

    for b in tail_blocks:
        bid = str(b.get("id", ""))
        if b.get("type") == "table":
            rows = b.get("rows") or []
            last_r = rows[-1] if rows else []
            last_r_txt = " | ".join(str(c.get("text", "")).replace("\n", " ")[:30] for c in last_r)
            lines.append(f"  [{bid}] <표 {len(rows)}행> 마지막 행(row={max(0, len(rows)-1)}): {last_r_txt}")
        else:
            pb_note = " [PAGE BREAK]" if b.get("page_break") else ""
            txt = str(b.get("text", "")).replace("\n", " ")[:100]
            lines.append(f"  [{bid}]{pb_note} {txt}")

    summary = "\n".join(lines)
    if len(summary) > 12000:
        summary = summary[-12000:]
    return summary


def build_hwpx_create_prompt(
    user_instruction: str,
    reference_chunk: str = "",
    part_index: int = 1,
    total_parts: int = 1,
    current_dom_map: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """새 HWPX 양식 생성용 (system_prompt, user_prompt)를 생성합니다."""
    total_parts = max(1, int(total_parts or 1))
    part_index = max(1, min(int(part_index or 1), total_parts))

    system_prompt = (
        "인사말·설명 문장 없이 반드시 유효한 ```json ... ``` 코드블록 1개만 출력하세요.\n"
        "당신은 대한민국 교직 실무용 새 HWPX 양식 생성 AI 에이전트입니다.\n"
        "매 호출은 독립된 새 세션이므로 이전 대화 맥락에 의존하지 말고 아래 지시사항·현재 문서 상태·참고 자료만으로 완결된 JSON을 생성하세요.\n"
        "반드시 아래 JSON 스키마에 맞는 ```json ... ``` 블록만 출력하세요.\n\n"
        "사용 가능한 op 목록:\n"
        "1. append_paragraph: {\"op\":\"append_paragraph\", \"text\":\"...\", \"bold\":true|false, \"font_size_pt\":14.0, \"align\":\"LEFT\"|\"CENTER\"|\"RIGHT\"}\n"
        "2. append_table: {\"op\":\"append_table\", \"headers\":[\"항목\",\"내용\"], \"rows\":[[\"...\",\"...\"]], \"col_widths_ratio\":[1,3]}\n"
        "3. insert_table_after: {\"op\":\"insert_table_after\", \"target_id\":\"p_0\", \"headers\":[\"...\"], \"rows\":[[\"...\"]], \"col_widths_ratio\":[1,3]}\n"
        "4. insert_exam_question: {\"op\":\"insert_exam_question\", \"target_id\":\"p_0\", \"number\":1, \"score\":\"3점\", \"question\":\"...\", \"bogi\":[\"ㄱ. ...\"], \"choices\":[\"① ...\"], \"answer\":\"③\", \"explanation\":\"...\"}\n"
        "5. page_break: {\"op\":\"page_break\"} (문서 끝에 페이지 나누기 추가)\n"
        "6. add_table_row: {\"op\":\"add_table_row\", \"target_id\":\"tbl_0\", \"after_row\":0, \"cells\":[\"...\"]}\n"
        "7. update_cell: {\"op\":\"update_cell\", \"target_id\":\"tbl_0_r0_c1\", \"new_text\":\"...\"}\n"
        "8. replace_text: {\"op\":\"replace_text\", \"target_id\":\"p_0\"|\"tbl_0_r0_c0\"|\"ALL\", \"old_text\":\"...\", \"new_text\":\"...\"}\n"
        "9. insert_paragraph_after: {\"op\":\"insert_paragraph_after\", \"target_id\":\"p_0\", \"text\":\"...\"}\n"
        "10. set_paragraph_indent: {\"op\":\"set_paragraph_indent\", \"target_id\":\"p_0\", \"indent_pt\":-12, \"left_margin_pt\":12}\n"
        "11. update_table_layout: {\"op\":\"update_table_layout\", \"target_id\":\"tbl_0\", \"left_indent_pt\":10, \"col_widths_ratio\":[1,3]}\n\n"
        "핵심 규칙:\n"
        "- 출력은 반드시 `summary`와 `operations` 배열을 포함한 단일 ```json ... ``` 블록으로 작성하세요.\n"
        "- 제목은 `append_paragraph`에 `bold: true`, `font_size_pt: 15.0~18.0`, `align: \"CENTER\"` 옵션을 지정해 시각적으로 구분하세요.\n"
        "- 표는 `append_table`을 사용해 헤더(`headers`)와 데이터 행(`rows`), 열 너비 비율(`col_widths_ratio`)을 균형 있게 구성하세요.\n"
        "- 화학식/첨자/수식은 H_{2}O, Fe^{3+}, $$x^2 + y^2 = r^2$$처럼 한글 수식 문법(백슬래시 금지)을 사용하세요."
    )

    sections: List[str] = []
    if total_parts > 1:
        sections.append(f"### [분할 생성 진행 상태: Part {part_index} / {total_parts}]")
        if part_index == 1:
            sections.append(
                "- 이번 단계는 **Part 1**입니다. 전체 HWPX 양식의 골격(문서 제목, 안내 문구, 기본 메타정보/표 구조 등)과 "
                "아래 참고 자료 Part 1의 내용을 누락 없이 생성하세요.\n"
                "- 이후 파트(Part 2~)가 뒤에 이어 붙을 수 있도록 마무리하세요."
            )
        else:
            dom_summary = _summarize_dom_map_for_create(current_dom_map)
            sections.append(
                f"- 이번 단계는 **Part {part_index}/{total_parts}**입니다.\n"
                f"- **중요:** 앞선 파트(Part 1~{part_index - 1})로 이미 생성된 내용을 절대 덮어쓰거나 삭제(`delete_paragraph` 등)하지 마세요!\n"
                f"- 반드시 직전 DOM의 마지막 블록 뒤에 `append_paragraph`, `append_table`, `add_table_row`, `insert_exam_question`, `page_break` 등을 사용해 순차적으로 이어 붙이세요.\n\n"
                f"#### [직전까지 생성된 HWPX DOM 요약]\n{dom_summary}"
            )
    else:
        if current_dom_map and (current_dom_map.get("blocks") or []):
            dom_summary = _summarize_dom_map_for_create(current_dom_map)
            sections.append(f"### [현재 문서 상태 참고]\n{dom_summary}")

    sections.append(f"### [교사 양식 생성 지시사항]\n{(user_instruction or '').strip() or '교사의 실무에 바로 쓸 수 있는 깔끔한 새 HWPX 양식을 생성하세요.'}")

    ref_text = (reference_chunk or "").strip()
    if ref_text:
        part_tag = f" (Part {part_index}/{total_parts})" if total_parts > 1 else ""
        sections.append(
            f"### [참고 문서 내용{part_tag}]\n{ref_text}\n\n"
            "(참고: 만약 현재 전달된 참고 내용만으로 부족해 다른 청크나 페이지가 추가로 필요하면, "
            "`{\"status\": \"need_more_chunks\", \"chunk_ids\": [\"...\"], \"pages\": [1, 2], \"reason\": \"필요 사유\"}` 형태로 요청할 수 있습니다.)"
        )

    sections.append(
        "### [출력 형식 - 필수 준수]\n"
        "인사말·설명 문장 없이 반드시 유효한 ```json ... ``` 코드블록 1개만 출력하세요.\n"
        "```json\n"
        "{\n"
        '  "summary": "생성/추가한 양식 내용 요약",\n'
        '  "operations": [\n'
        '    {"op": "append_paragraph", "text": "제목", "bold": true, "font_size_pt": 16.0, "align": "CENTER"},\n'
        '    {"op": "append_table", "headers": ["항목", "내용"], "rows": [["예시", "값"]], "col_widths_ratio": [1, 3]}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    user_prompt = "\n\n".join(sections)
    return system_prompt, user_prompt


# =============================================================================
# 4. JSON Patch 연산자 실행 엔진
# =============================================================================
def _flush_sections(doc: HwpxDocument, section_roots: Dict[str, Any]) -> None:
    for sec_name, root in section_roots.items():
        doc.entries[sec_name] = _serialize_xml(root)


def _get_target_section_root(
    doc: HwpxDocument,
    section_roots: Dict[str, Any],
) -> Optional[Tuple[str, Any]]:
    """문서 끝부분 추가 연산(append_*)이 대상이 될 섹션(기본 Contents/section0.xml)을 반환합니다."""
    if not section_roots:
        if "Contents/section0.xml" not in doc.entries:
            doc.entries["Contents/section0.xml"] = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
                f'<hs:sec xmlns:hs="{HS_NS}" xmlns:hp="{HP_NS}" xmlns:hc="{HC_NS}" xmlns:hh="{HH_NS}"></hs:sec>'
            ).encode("utf-8")
        section_roots["Contents/section0.xml"] = _parse_xml(doc.entries["Contents/section0.xml"])
    if len(section_roots) == 1:
        sec_name = next(iter(section_roots))
        return sec_name, section_roots[sec_name]
    sec_names = list(section_roots.keys())
    return sec_names[-1], section_roots[sec_names[-1]]


def _compute_section_content_width(
    section_roots: Dict[str, Any],
    default_width: int = 42520,
) -> int:
    """section0.xml의 pagePr(폭-좌우여백) 및 colPr(다단·단간격)을 읽어 실제 1단 너비를 계산합니다."""
    if not section_roots:
        return default_width
    sec_root = section_roots.get("Contents/section0.xml")
    if sec_root is None:
        sec_root = next(iter(section_roots.values()), None)
    if sec_root is None:
        return default_width

    page_w = 0
    margin_l = 0
    margin_r = 0
    margin_g = 0
    col_count = 1
    same_gap = 0

    for node in sec_root.iter():
        t = _local_tag(node)
        if t == "pagePr" and page_w == 0:
            try:
                page_w = int(node.get("width", "0") or 0)
            except ValueError:
                page_w = 0
            m_el = _find_child(node, "margin")
            if m_el is not None:
                try:
                    margin_l = int(m_el.get("left", "0") or 0)
                    margin_r = int(m_el.get("right", "0") or 0)
                    margin_g = int(m_el.get("gutter", "0") or 0)
                except ValueError:
                    pass
        elif t == "colPr":
            try:
                c_val = int(node.get("colCount", "1") or 1)
                if c_val >= 1:
                    col_count = c_val
                same_gap = int(node.get("sameGap", "0") or 0)
            except ValueError:
                pass

    content_w = page_w - margin_l - margin_r - margin_g
    if content_w <= 0:
        content_w = default_width

    if col_count >= 2:
        col_w = (content_w - same_gap * (col_count - 1)) // col_count
        return max(5000, col_w)
    return max(5000, content_w)


def _next_section_object_id(
    section_roots: Dict[str, Any],
    id_counter: Optional[List[int]] = None,
    *,
    min_base: int = 900000,
) -> str:
    """섹션 내 기존 tbl/equation/p id와 충돌하지 않는 고유 숫자 ID를 발급합니다."""
    used_ids: Set[str] = set()
    max_id = min_base
    for root in section_roots.values():
        for node in root.iter():
            if _local_tag(node) in ("tbl", "rect", "equation", "p"):
                raw_id = str(node.get("id", "")).strip()
                if raw_id:
                    used_ids.add(raw_id)
                    if raw_id.isdigit():
                        val = int(raw_id)
                        if val < 2000000000:
                            max_id = max(max_id, val)
    cand = (max(id_counter[0], max_id) + 1) if id_counter is not None else (max_id + 1)
    while str(cand) in used_ids:
        cand += 1
    if id_counter is not None:
        id_counter[0] = cand
    return str(cand)


def _consume_initial_secpr_placeholder(sec_root: Any) -> Optional[Any]:
    """섹션에 빈 <hp:secPr> 플레이스홀더 문단 1개만 있으면 해당 문단을 반환해 첫 블록으로 재사용합니다."""
    children = list(sec_root)
    if len(children) == 1 and _is_empty_secpr_placeholder(children[0]):
        return children[0]
    return None


def _wrap_table_in_paragraph(
    tbl_elem: Any,
    section_roots: Dict[str, Any],
    id_counter: Optional[List[int]] = None,
    *,
    para_pr_id: str = "0",
    char_pr_id: str = "0",
    target_p: Optional[Any] = None,
) -> Any:
    """<hp:tbl>을 표준 OWPML 구조(<hp:p><hp:run><hp:tbl>...</hp:tbl><hp:t/></hp:run></hp:p>)로 감쌉니다."""
    if target_p is not None:
        p_elem = target_p
        p_elem.set("paraPrIDRef", para_pr_id)
        if not p_elem.get("styleIDRef"):
            p_elem.set("styleIDRef", "0")
        p_elem.set("pageBreak", "0")
        p_elem.set("columnBreak", "0")
        p_elem.set("merged", "0")
        for ch in list(p_elem):
            tag = _local_tag(ch)
            if tag == "linesegarray":
                p_elem.remove(ch)
            elif tag == "run":
                if _run_has_sec_pr_or_ctrl(ch):
                    for sub in list(ch):
                        if _local_tag(sub) not in ("secPr", "ctrl"):
                            ch.remove(sub)
                else:
                    p_elem.remove(ch)
    else:
        p_id = _next_section_object_id(section_roots, id_counter)
        p_elem = ET.Element(
            _qname(HP_NS, "p"),
            {
                "id": p_id,
                "paraPrIDRef": para_pr_id,
                "styleIDRef": "0",
                "pageBreak": "0",
                "columnBreak": "0",
                "merged": "0",
            },
        )
    run_el = ET.SubElement(p_elem, _qname(HP_NS, "run"), {"charPrIDRef": char_pr_id})
    run_el.append(tbl_elem)
    ET.SubElement(run_el, _qname(HP_NS, "t"))
    return p_elem


def _resolve_block_insertion_target(
    section_roots: Dict[str, Any],
    parent: Any,
    elem: Any,
) -> Tuple[Any, int, str]:
    """
    <hp:p> 또는 <hp:run> 안에 감싸진 <hp:tbl> 뒤의 블록 삽입 위치((container, index, base_para_pr_id))를 반환합니다.
    """
    if _local_tag(elem) == "p" and _local_tag(parent) in ("sec", "subList"):
        children = list(parent)
        return parent, children.index(elem) + 1, str(elem.get("paraPrIDRef", "0"))

    for root in section_roots.values():
        parent_map: Dict[Any, Any] = {child: p for p in root.iter() for child in p}
        if elem not in parent_map and elem is not root:
            continue
        cur = elem
        while cur in parent_map:
            anc = parent_map[cur]
            if _local_tag(anc) in ("sec", "subList"):
                children = list(anc)
                if cur in children:
                    base_pid = str(cur.get("paraPrIDRef", "0")) if _local_tag(cur) == "p" else "0"
                    return anc, children.index(cur) + 1, base_pid
            cur = anc

    children = list(parent)
    idx = children.index(elem) + 1 if elem in children else len(children)
    base_pid = str(elem.get("paraPrIDRef", "0")) if _local_tag(elem) == "p" else "0"
    return parent, idx, base_pid


def _build_table_element(
    doc: HwpxDocument,
    op_data: Dict[str, Any],
    *,
    section_roots: Dict[str, Any],
    id_counter: List[int],
) -> Any:
    """headers, rows, col_widths_ratio 스펙으로 표준 <hp:tbl> 트리를 생성합니다."""
    raw_headers = op_data.get("headers")
    headers: List[str] = [str(x) for x in raw_headers] if isinstance(raw_headers, list) else []
    raw_rows = op_data.get("rows")
    rows: List[List[str]] = (
        [[str(cell) for cell in r] if isinstance(r, list) else [str(r)] for r in raw_rows]
        if isinstance(raw_rows, list)
        else []
    )
    has_headers = bool(headers)
    all_rows: List[List[str]] = ([headers] + rows) if has_headers else list(rows)
    if not all_rows:
        all_rows = [[""]]

    raw_ratios = op_data.get("col_widths_ratio")
    ratios: List[float] = []
    if isinstance(raw_ratios, list):
        for r in raw_ratios:
            try:
                val = float(r)
                if val > 0:
                    ratios.append(val)
            except (TypeError, ValueError):
                pass

    col_cnt = max([len(r) for r in all_rows] + [len(ratios)] + [1])
    normalized_rows = [list(r) + [""] * (col_cnt - len(r)) for r in all_rows]
    row_cnt = len(normalized_rows)

    total_w = _compute_section_content_width(section_roots, default_width=42520)
    if ratios:
        if len(ratios) < col_cnt:
            avg_r = sum(ratios) / len(ratios)
            ratios.extend([avg_r] * (col_cnt - len(ratios)))
        else:
            ratios = ratios[:col_cnt]
        ratio_sum = sum(ratios) or float(col_cnt)
        col_widths = [max(1000, int(round(total_w * (r / ratio_sum)))) for r in ratios]
    else:
        col_widths = [total_w // col_cnt] * col_cnt

    border_fill_id = _ensure_table_border_fill_id(doc)
    hdr_char_pr_id = _ensure_styled_char_pr(doc, "0", bold=True) if has_headers else "0"
    hdr_para_pr_id = _ensure_styled_para_pr(doc, "0", align="CENTER") if has_headers else "0"

    tbl_id = _next_section_object_id(section_roots, id_counter)
    total_h = max(2000, row_cnt * 2000)
    tbl = ET.Element(
        _qname(HP_NS, "tbl"),
        {
            "id": tbl_id,
            "zOrder": "0",
            "numberingType": "TABLE",
            "textWrap": "TOP_AND_BOTTOM",
            "textFlow": "BOTH_SIDES",
            "lock": "0",
            "dropcapstyle": "None",
            "pageBreak": "CELL",
            "repeatHeader": "1",
            "rowCnt": str(row_cnt),
            "colCnt": str(col_cnt),
            "cellSpacing": "0",
            "borderFillIDRef": border_fill_id,
            "noAdjust": "0",
        },
    )
    ET.SubElement(
        tbl,
        _qname(HP_NS, "sz"),
        {
            "width": str(sum(col_widths)),
            "widthRelTo": "ABSOLUTE",
            "height": str(total_h),
            "heightRelTo": "ABSOLUTE",
            "protect": "0",
        },
    )
    ET.SubElement(
        tbl,
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
    ET.SubElement(
        tbl,
        _qname(HP_NS, "outMargin"),
        {"left": "283", "right": "283", "top": "283", "bottom": "283"},
    )
    ET.SubElement(
        tbl,
        _qname(HP_NS, "inMargin"),
        {"left": "510", "right": "510", "top": "141", "bottom": "141"},
    )

    for r_idx, row_vals in enumerate(normalized_rows):
        tr = ET.SubElement(tbl, _qname(HP_NS, "tr"))
        is_hdr_row = has_headers and r_idx == 0
        cell_para_id = hdr_para_pr_id if is_hdr_row else "0"
        cell_char_id = hdr_char_pr_id if is_hdr_row else "0"

        for c_idx, cell_text in enumerate(row_vals):
            tc = ET.SubElement(
                tr,
                _qname(HP_NS, "tc"),
                {
                    "name": "",
                    "header": "1" if is_hdr_row else "0",
                    "hasMargin": "0",
                    "protect": "0",
                    "editable": "0",
                    "dirty": "0",
                    "borderFillIDRef": border_fill_id,
                },
            )
            sub_list = ET.SubElement(
                tc,
                _qname(HP_NS, "subList"),
                {
                    "id": "",
                    "textDirection": "HORIZONTAL",
                    "lineWrap": "BREAK",
                    "vertAlign": "CENTER",
                    "linkListIDRef": "0",
                    "linkListNextIDRef": "0",
                    "textWidth": "0",
                    "textHeight": "0",
                    "hasTextRef": "0",
                    "hasNumRef": "0",
                },
            )
            lines = cell_text.split("\n") if cell_text else [""]
            for ln in lines:
                cp = ET.SubElement(
                    sub_list,
                    _qname(HP_NS, "p"),
                    {
                        "id": _next_section_object_id(section_roots, id_counter),
                        "paraPrIDRef": cell_para_id,
                        "styleIDRef": "0",
                        "pageBreak": "0",
                        "columnBreak": "0",
                        "merged": "0",
                    },
                )
                _set_paragraph_rich_text(
                    doc,
                    cp,
                    ln,
                    section_roots=section_roots,
                    id_counter=id_counter,
                    base_char_pr_id=cell_char_id,
                )
            ET.SubElement(
                tc, _qname(HP_NS, "cellAddr"), {"colAddr": str(c_idx), "rowAddr": str(r_idx)}
            )
            ET.SubElement(
                tc, _qname(HP_NS, "cellSpan"), {"colSpan": "1", "rowSpan": "1"}
            )
            ET.SubElement(
                tc, _qname(HP_NS, "cellSz"), {"width": str(col_widths[c_idx]), "height": "2000"}
            )
            ET.SubElement(
                tc,
                _qname(HP_NS, "cellMargin"),
                {"left": "510", "right": "510", "top": "141", "bottom": "141"},
            )
    return tbl


def _find_enclosing_block_container(
    section_roots: Dict[str, Any],
    parent: Any,
    elem: Any,
) -> Tuple[Any, Any]:
    """tbl 등 중첩 요소를 감싸는 최상위 블록(<hp:p>)과 그 컨테이너(<hp:sec>/<hp:subList>)를 반환합니다."""
    if _local_tag(elem) == "p" and _local_tag(parent) in ("sec", "subList"):
        return parent, elem
    for root in section_roots.values():
        parent_map: Dict[Any, Any] = {child: p for p in root.iter() for child in p}
        if elem not in parent_map and elem is not root:
            continue
        cur = elem
        while cur in parent_map:
            anc = parent_map[cur]
            if _local_tag(anc) in ("sec", "subList"):
                return anc, cur
            cur = anc
    return parent, elem


def _reassign_cloned_ids_and_strip(cloned_elem: Any, section_roots: Dict[str, Any], counter: List[int]) -> None:
    """복제된 블록 트리 내부의 secPr/ctrl/linesegarray를 제거하고 tbl/rect/p/equation id를 재발급합니다."""
    nodes = [cloned_elem] + list(cloned_elem.iter())
    for node in nodes:
        t = _local_tag(node)
        if t == "p":
            _strip_sec_pr_and_ctrl(node)
        if t in ("tbl", "rect", "p", "equation"):
            node.set("id", _next_section_object_id(section_roots, counter))


def apply_single_hwpx_operation(
    doc: HwpxDocument,
    op_data: Dict[str, Any],
    id_counter: Optional[List[int]] = None,
    *,
    preloaded_dom: Optional[Tuple[Dict[str, Tuple[str, Any, Any]], Dict[str, Any]]] = None,
    flush: bool = True,
) -> List[str]:
    """단일 JSON Patch 연산을 적용하고, 하이라이트할 변경 대상 ID 목록을 반환합니다."""
    op = str(op_data.get("op", "")).strip()
    target_id = str(op_data.get("target_id", "")).strip()
    if preloaded_dom is not None:
        id_map, section_roots = preloaded_dom
    else:
        id_map, _, section_roots = _collect_section_dom(doc)
    touched_ids: List[str] = []
    counter = id_counter if id_counter is not None else [0]

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
                    doc, elem, old_text, new_text, section_roots=section_roots, id_counter=counter
                ):
                    touched_ids.append(tid)
            elif tag in ("tc", "tbl"):
                replaced_any = False
                for p_node in [n for n in elem.iter() if _local_tag(n) == "p"]:
                    if _replace_text_in_paragraph(
                        doc, p_node, old_text, new_text, section_roots=section_roots, id_counter=counter
                    ):
                        replaced_any = True
                if replaced_any:
                    touched_ids.append(tid)

    elif op == "set_paragraph_text":
        if target_id in id_map:
            _, _, elem = id_map[target_id]
            if _local_tag(elem) == "p":
                _set_paragraph_rich_text(
                    doc,
                    elem,
                    str(op_data.get("new_text", "")),
                    section_roots=section_roots,
                    id_counter=counter,
                )
                touched_ids.append(target_id)

    elif op == "insert_paragraph_after":
        if target_id in id_map:
            _, raw_parent, elem = id_map[target_id]
            parent, ins_idx, base_pid = _resolve_block_insertion_target(section_roots, raw_parent, elem)
            if _local_tag(elem) == "p":
                new_p = deepcopy(elem)
                _strip_sec_pr_and_ctrl(new_p)
                new_p.set("id", _next_section_object_id(section_roots, counter))
            else:
                new_p = ET.Element(
                    _qname(HP_NS, "p"),
                    {
                        "id": _next_section_object_id(section_roots, counter),
                        "paraPrIDRef": base_pid,
                        "styleIDRef": "0",
                        "pageBreak": "0",
                        "columnBreak": "0",
                        "merged": "0",
                    },
                )
            _set_paragraph_rich_text(
                doc,
                new_p,
                str(op_data.get("text", "")),
                section_roots=section_roots,
                id_counter=counter,
            )
            parent.insert(ins_idx, new_p)
            touched_ids.append(target_id)

    elif op == "clone_block":
        source_id = str(op_data.get("source_id") or target_id or "").strip()
        after_id = str(op_data.get("after_id") or target_id or source_id).strip()
        if source_id in id_map:
            _, _, src_elem = id_map[source_id]
            cloned_elem = deepcopy(src_elem)
            _reassign_cloned_ids_and_strip(cloned_elem, section_roots, counter)
            if _local_tag(cloned_elem) == "tbl":
                block_to_insert = _wrap_table_in_paragraph(cloned_elem, section_roots, counter)
            else:
                block_to_insert = cloned_elem

            if after_id in id_map:
                _, raw_parent, after_elem = id_map[after_id]
                parent, ins_idx, _ = _resolve_block_insertion_target(
                    section_roots, raw_parent, after_elem
                )
                parent.insert(ins_idx, block_to_insert)
            else:
                sec_info = _get_target_section_root(doc, section_roots)
                if sec_info is not None:
                    _, sec_root = sec_info
                    sec_root.append(block_to_insert)
            new_id = _find_dom_id_for_element(section_roots, cloned_elem)
            if new_id:
                touched_ids.append(new_id)

    elif op == "delete_block":
        if target_id in id_map:
            _, raw_parent, elem = id_map[target_id]
            if _local_tag(elem) == "tbl":
                container, block_elem = _find_enclosing_block_container(
                    section_roots, raw_parent, elem
                )
            else:
                container, block_elem = raw_parent, elem
            sec_runs = [
                ch
                for ch in block_elem
                if _local_tag(ch) == "run" and _run_has_sec_pr_or_ctrl(ch)
            ]
            if sec_runs:
                sibling_ps = [
                    ch for ch in container if _local_tag(ch) == "p" and ch is not block_elem
                ]
                if sibling_ps:
                    for offset, sr in enumerate(sec_runs):
                        sibling_ps[0].insert(offset, deepcopy(sr))
                    if block_elem in list(container):
                        container.remove(block_elem)
                else:
                    _set_paragraph_rich_text(
                        doc, block_elem, "", section_roots=section_roots, id_counter=counter
                    )
            else:
                if block_elem in list(container):
                    container.remove(block_elem)
            touched_ids.append(target_id)

    elif op == "delete_paragraph":
        if target_id in id_map:
            _, parent, elem = id_map[target_id]
            sec_runs = [ch for ch in elem if _local_tag(ch) == "run" and _run_has_sec_pr_or_ctrl(ch)]
            if sec_runs:
                sibling_ps = [ch for ch in parent if _local_tag(ch) == "p" and ch is not elem]
                if sibling_ps:
                    for offset, sr in enumerate(sec_runs):
                        sibling_ps[0].insert(offset, deepcopy(sr))
                    parent.remove(elem)
                else:
                    _set_paragraph_rich_text(
                        doc, elem, "", section_roots=section_roots, id_counter=counter
                    )
            else:
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
                _strip_sec_pr_and_ctrl(new_p)
                new_p.set("id", _next_section_object_id(section_roots, counter))
                _set_paragraph_rich_text(
                    doc, new_p, ln, section_roots=section_roots, id_counter=counter
                )
                sub_list.append(new_p)
            touched_ids.append(target_id)

    elif op == "add_table_row":
        if target_id in id_map:
            _, _, tbl = id_map[target_id]
            trs = [ch for ch in tbl if _local_tag(ch) == "tr"]
            if trs:
                # 병합 셀(rowSpan > 1)이 없는 데이터 행 우선 선택
                tmpl_tr = trs[-1]
                for tr in reversed(trs):
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
                    _strip_sec_pr_and_ctrl(new_p)
                    new_p.set("id", _next_section_object_id(section_roots, counter))
                    _set_paragraph_rich_text(
                        doc, new_p, txt, section_roots=section_roots, id_counter=counter
                    )
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

    elif op == "append_paragraph":
        sec_info = _get_target_section_root(doc, section_roots)
        if sec_info is not None:
            _, sec_root = sec_info
            text = str(op_data.get("text", ""))
            raw_bold = op_data.get("bold")
            bold_opt = bool(raw_bold) if raw_bold is not None else None
            raw_size = op_data.get("font_size_pt")
            size_opt: Optional[float] = None
            if raw_size is not None:
                try:
                    parsed_pt = float(raw_size)
                    if parsed_pt > 0:
                        size_opt = parsed_pt
                except (TypeError, ValueError):
                    pass
            align_opt = str(op_data.get("align", "")).strip() or None

            char_pr_id = _ensure_styled_char_pr(doc, "0", bold=bold_opt, font_size_pt=size_opt)
            para_pr_id = _ensure_styled_para_pr(doc, "0", align=align_opt)

            placeholder = _consume_initial_secpr_placeholder(sec_root)
            if placeholder is not None:
                new_p = placeholder
                new_p.set("paraPrIDRef", para_pr_id)
                new_p.set("styleIDRef", new_p.get("styleIDRef") or "0")
                new_p.set("pageBreak", "0")
                new_p.set("columnBreak", "0")
                new_p.set("merged", "0")
            else:
                new_p = ET.Element(
                    _qname(HP_NS, "p"),
                    {
                        "id": _next_section_object_id(section_roots, counter),
                        "paraPrIDRef": para_pr_id,
                        "styleIDRef": "0",
                        "pageBreak": "0",
                        "columnBreak": "0",
                        "merged": "0",
                    },
                )
            _set_paragraph_rich_text(
                doc,
                new_p,
                text,
                section_roots=section_roots,
                id_counter=counter,
                base_char_pr_id=char_pr_id,
            )
            if placeholder is None:
                sec_root.append(new_p)
            new_id = _find_dom_id_for_element(section_roots, new_p)
            if new_id:
                touched_ids.append(new_id)

    elif op == "append_table":
        sec_info = _get_target_section_root(doc, section_roots)
        if sec_info is not None:
            _, sec_root = sec_info
            new_tbl = _build_table_element(
                doc, op_data, section_roots=section_roots, id_counter=counter
            )
            placeholder = _consume_initial_secpr_placeholder(sec_root)
            if placeholder is not None:
                _wrap_table_in_paragraph(
                    new_tbl, section_roots, counter, target_p=placeholder
                )
            else:
                tbl_p = _wrap_table_in_paragraph(new_tbl, section_roots, counter)
                sec_root.append(tbl_p)
            new_id = _find_dom_id_for_element(section_roots, new_tbl)
            if new_id:
                touched_ids.append(new_id)

    elif op == "insert_table_after":
        new_tbl = _build_table_element(
            doc, op_data, section_roots=section_roots, id_counter=counter
        )
        if target_id in id_map:
            _, raw_parent, elem = id_map[target_id]
            parent, ins_idx, _ = _resolve_block_insertion_target(section_roots, raw_parent, elem)
            tbl_p = _wrap_table_in_paragraph(new_tbl, section_roots, counter)
            parent.insert(ins_idx, tbl_p)
        else:
            sec_info = _get_target_section_root(doc, section_roots)
            if sec_info is not None:
                _, sec_root = sec_info
                placeholder = _consume_initial_secpr_placeholder(sec_root)
                if placeholder is not None:
                    _wrap_table_in_paragraph(
                        new_tbl, section_roots, counter, target_p=placeholder
                    )
                else:
                    tbl_p = _wrap_table_in_paragraph(new_tbl, section_roots, counter)
                    sec_root.append(tbl_p)
        new_id = _find_dom_id_for_element(section_roots, new_tbl)
        if new_id:
            touched_ids.append(new_id)

    elif op == "page_break":
        sec_info = _get_target_section_root(doc, section_roots)
        if sec_info is not None:
            _, sec_root = sec_info
            pb_para_id = _ensure_styled_para_pr(doc, "0", page_break=True)
            placeholder = _consume_initial_secpr_placeholder(sec_root)
            if placeholder is not None:
                pb_p = placeholder
                pb_p.set("paraPrIDRef", pb_para_id)
                pb_p.set("pageBreak", "1")
            else:
                pb_p = ET.Element(
                    _qname(HP_NS, "p"),
                    {
                        "id": _next_section_object_id(section_roots, counter),
                        "paraPrIDRef": pb_para_id,
                        "styleIDRef": "0",
                        "pageBreak": "1",
                        "columnBreak": "0",
                        "merged": "0",
                    },
                )
            _set_paragraph_rich_text(
                doc, pb_p, "", section_roots=section_roots, id_counter=counter, base_char_pr_id="0"
            )
            if placeholder is None:
                sec_root.append(pb_p)
            new_id = _find_dom_id_for_element(section_roots, pb_p)
            if new_id:
                touched_ids.append(new_id)

    elif op == "insert_exam_question":
        # target_id가 생략되거나 "END"이면 문서 맨 끝에 순차 추가 (폴백 생략)
        if not target_id or target_id.upper() == "END":
            anchor_entry = None
        else:
            anchor_entry = id_map.get(target_id)
            if anchor_entry is None:
                p_keys = [k for k in id_map if k.startswith("p_")]
                if p_keys:
                    anchor_entry = id_map[p_keys[-1]]
                    target_id = p_keys[-1]

        placeholder_stem: Optional[Any] = None
        if anchor_entry is not None:
            _, raw_parent, anchor_elem = anchor_entry
            parent, insert_pos, base_para_id = _resolve_block_insertion_target(
                section_roots, raw_parent, anchor_elem
            )
        else:
            sec_info = _get_target_section_root(doc, section_roots)
            if sec_info is None:
                if flush:
                    _flush_sections(doc, section_roots)
                return touched_ids
            _, parent = sec_info
            placeholder_stem = _consume_initial_secpr_placeholder(parent)
            insert_pos = len(list(parent))
            base_para_id = "0"

        num = op_data.get("number", 1)
        score = str(op_data.get("score", "")).strip()
        q_text = str(op_data.get("question", "")).strip()
        bogi_list = [str(b) for b in (op_data.get("bogi") or [])]
        choices = [str(c) for c in (op_data.get("choices") or [])]
        ans = str(op_data.get("answer", "")).strip()
        expl = str(op_data.get("explanation", "")).strip()

        stem_para_id = _clone_para_pr_with_indent(doc, base_para_id, keep_with_next=True)
        choice_para_id = _clone_para_pr_with_indent(
            doc, base_para_id, indent_pt=-12.0, left_margin_pt=12.0, keep_with_next=True
        )

        # 1) 발문 문단
        stem_str = f"{num}. {q_text}" + (f" [{score}]" if score else "")
        if placeholder_stem is not None:
            stem_p = placeholder_stem
            stem_p.set("paraPrIDRef", stem_para_id)
            stem_p.set("styleIDRef", stem_p.get("styleIDRef") or "0")
            stem_p.set("pageBreak", "0")
            stem_p.set("columnBreak", "0")
            stem_p.set("merged", "0")
        else:
            stem_p = ET.Element(
                _qname(HP_NS, "p"),
                {
                    "id": _next_section_object_id(section_roots, counter),
                    "paraPrIDRef": stem_para_id,
                    "styleIDRef": "0",
                    "pageBreak": "0",
                    "columnBreak": "0",
                    "merged": "0",
                },
            )
        _set_paragraph_rich_text(
            doc, stem_p, stem_str, section_roots=section_roots, id_counter=counter
        )
        if placeholder_stem is None:
            parent.insert(insert_pos, stem_p)
            insert_pos += 1
        else:
            insert_pos = list(parent).index(stem_p) + 1

        # 2) <보기> 1x1 테두리 박스 표
        if bogi_list:
            bogi_w = str(_compute_section_content_width(section_roots, default_width=40000))
            border_fill_id = _ensure_table_border_fill_id(doc)
            bogi_tbl_id = _next_section_object_id(section_roots, counter)
            bogi_tbl = ET.Element(
                _qname(HP_NS, "tbl"),
                {
                    "id": bogi_tbl_id,
                    "zOrder": "0",
                    "numberingType": "TABLE",
                    "textWrap": "TOP_AND_BOTTOM",
                    "textFlow": "BOTH_SIDES",
                    "lock": "0",
                    "dropcapstyle": "None",
                    "pageBreak": "CELL",
                    "repeatHeader": "1",
                    "rowCnt": "1",
                    "colCnt": "1",
                    "cellSpacing": "0",
                    "borderFillIDRef": border_fill_id,
                    "noAdjust": "0",
                },
            )
            ET.SubElement(
                bogi_tbl,
                _qname(HP_NS, "sz"),
                {
                    "width": bogi_w,
                    "widthRelTo": "ABSOLUTE",
                    "height": "2000",
                    "heightRelTo": "ABSOLUTE",
                    "protect": "0",
                },
            )
            ET.SubElement(
                bogi_tbl,
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
            ET.SubElement(
                bogi_tbl,
                _qname(HP_NS, "outMargin"),
                {"left": "283", "right": "283", "top": "283", "bottom": "283"},
            )
            ET.SubElement(
                bogi_tbl,
                _qname(HP_NS, "inMargin"),
                {"left": "510", "right": "510", "top": "141", "bottom": "141"},
            )
            tr = ET.SubElement(bogi_tbl, _qname(HP_NS, "tr"))
            tc = ET.SubElement(
                tr,
                _qname(HP_NS, "tc"),
                {
                    "name": "",
                    "header": "0",
                    "hasMargin": "0",
                    "protect": "0",
                    "editable": "0",
                    "dirty": "0",
                    "borderFillIDRef": border_fill_id,
                },
            )
            sub_list = ET.SubElement(
                tc,
                _qname(HP_NS, "subList"),
                {
                    "id": "",
                    "textDirection": "HORIZONTAL",
                    "lineWrap": "BREAK",
                    "vertAlign": "CENTER",
                    "linkListIDRef": "0",
                    "linkListNextIDRef": "0",
                    "textWidth": "0",
                    "textHeight": "0",
                    "hasTextRef": "0",
                    "hasNumRef": "0",
                },
            )
            hdr_p = ET.SubElement(
                sub_list,
                _qname(HP_NS, "p"),
                {
                    "id": _next_section_object_id(section_roots, counter),
                    "paraPrIDRef": stem_para_id,
                    "styleIDRef": "0",
                    "pageBreak": "0",
                    "columnBreak": "0",
                    "merged": "0",
                },
            )
            _set_paragraph_rich_text(
                doc, hdr_p, "< 보 기 >", section_roots=section_roots, id_counter=counter
            )
            for b_line in bogi_list:
                bp = ET.SubElement(
                    sub_list,
                    _qname(HP_NS, "p"),
                    {
                        "id": _next_section_object_id(section_roots, counter),
                        "paraPrIDRef": base_para_id,
                        "styleIDRef": "0",
                        "pageBreak": "0",
                        "columnBreak": "0",
                        "merged": "0",
                    },
                )
                _set_paragraph_rich_text(
                    doc, bp, b_line, section_roots=section_roots, id_counter=counter
                )
            ET.SubElement(tc, _qname(HP_NS, "cellAddr"), {"colAddr": "0", "rowAddr": "0"})
            ET.SubElement(tc, _qname(HP_NS, "cellSpan"), {"colSpan": "1", "rowSpan": "1"})
            ET.SubElement(tc, _qname(HP_NS, "cellSz"), {"width": bogi_w, "height": "2000"})
            ET.SubElement(
                tc,
                _qname(HP_NS, "cellMargin"),
                {"left": "510", "right": "510", "top": "141", "bottom": "141"},
            )

            bogi_p = _wrap_table_in_paragraph(bogi_tbl, section_roots, counter)
            parent.insert(insert_pos, bogi_p)
            insert_pos += 1

        # 3) 선택지 ①~⑤ (내어쓰기 + keepWithNext)
        for c_idx, ch_txt in enumerate(choices):
            cp_id = choice_para_id if c_idx < len(choices) - 1 else _clone_para_pr_with_indent(
                doc, base_para_id, indent_pt=-12.0, left_margin_pt=12.0, keep_with_next=False
            )
            cp = ET.Element(
                _qname(HP_NS, "p"),
                {
                    "id": _next_section_object_id(section_roots, counter),
                    "paraPrIDRef": cp_id,
                    "styleIDRef": "0",
                    "pageBreak": "0",
                    "columnBreak": "0",
                    "merged": "0",
                },
            )
            _set_paragraph_rich_text(
                doc, cp, ch_txt, section_roots=section_roots, id_counter=counter
            )
            parent.insert(insert_pos, cp)
            insert_pos += 1

        if ans or expl:
            doc.exam_answers.append(
                {"number": num, "answer": ans, "explanation": expl}
            )
        new_stem_id = _find_dom_id_for_element(section_roots, stem_p)
        touched_ids.append(new_stem_id or target_id or "p_0")

    if flush:
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
    id_counter = [0]
    id_map, _, section_roots = _collect_section_dom(doc)
    for op_data in operations:
        if isinstance(op_data, dict):
            all_touched.extend(
                apply_single_hwpx_operation(
                    doc,
                    op_data,
                    id_counter=id_counter,
                    preloaded_dom=(id_map, section_roots),
                    flush=False,
                )
            )
    _flush_sections(doc, section_roots)
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
    ans_doc = create_minimal_hwpx_document(title="정답 및 해설", empty=True)
    ops: List[Dict[str, Any]] = [
        {"op": "append_paragraph", "text": "정답 및 해설", "bold": True, "font_size_pt": 14.0, "align": "CENTER"}
    ]
    sorted_answers = sorted(
        doc.exam_answers,
        key=lambda x: int(x["number"]) if str(x.get("number", "")).isdigit() else 9999,
    )
    for item in sorted_answers:
        line = f"{item['number']}번 정답: {item['answer']} | 해설: {item['explanation']}"
        ops.append({"op": "append_paragraph", "text": line})
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
    script_map, _, _ = _get_header_style_maps(doc)
    return script_map


def _render_paragraph_rich_html(
    p_elem: Any,
    script_map: Dict[str, str],
    char_style_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    parts: List[str] = []
    c_styles = char_style_map or {}
    for child in p_elem:
        if _local_tag(child) != "run":
            continue
        cid = str(child.get("charPrIDRef", ""))
        script_tag = script_map.get(cid, "")
        cstyle = c_styles.get(cid, {})
        is_bold = bool(cstyle.get("bold")) and not script_tag
        font_pt = cstyle.get("font_size_pt")
        span_styles: List[str] = []
        if is_bold:
            span_styles.append("font-weight:bold;")
        if font_pt is not None and not script_tag and abs(float(font_pt) - 10.0) >= 0.5:
            span_styles.append(f"font-size:{float(font_pt):g}pt;")
        style_prefix = f"<span style='{''.join(span_styles)}'>" if span_styles else ""
        style_suffix = "</span>" if span_styles else ""

        for sub in child:
            stag = _local_tag(sub)
            if stag == "t" and sub.text:
                esc = html.escape(sub.text)
                if script_tag == "sub":
                    parts.append(f"<sub>{esc}</sub>")
                elif script_tag == "sup":
                    parts.append(f"<sup>{esc}</sup>")
                else:
                    parts.append(f"{style_prefix}{esc}{style_suffix}")
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
    script_map, char_style_map, para_style_map = _get_header_style_maps(doc)
    id_map, blocks, _ = _collect_section_dom(doc)

    html_parts: List[str] = [
        "<html><body style='font-family: \"Malgun Gothic\", sans-serif; color:#0F172A; "
        "background:#FFFFFF; line-height:1.6; padding:12px;'>"
    ]

    if not blocks:
        html_parts.append(
            "<p style='color:#94A3B8; font-style:italic; text-align:center; padding:32px 12px;'>"
            "빈 HWPX 양식 문서입니다. 우측 AI 실행 도크에서 양식 생성을 실행해 주세요."
            "</p>"
        )

    for blk in blocks:
        bid = blk["id"]
        is_hl = bid in highlighted
        anchor_name = anchor_map.get(bid, "")
        anchor_tag = f'<a name="{anchor_name}" id="{anchor_name}"></a>' if anchor_name else ""
        if blk["type"] == "paragraph":
            _, _, p_elem = id_map[bid]
            para_id_ref = str(p_elem.get("paraPrIDRef", ""))
            pstyle = para_style_map.get(para_id_ref, {})
            align_val = str(pstyle.get("align", "")).upper()
            align_css = (
                f" text-align:{align_val.lower()};"
                if align_val in {"LEFT", "CENTER", "RIGHT", "JUSTIFY"}
                else ""
            )
            is_pb = bool(blk.get("page_break") or p_elem.get("pageBreak") == "1")
            if is_pb:
                pb_bg = "background:#FEF9C3;" if is_hl else ""
                html_parts.append(
                    f"<div style='margin:14px 0; text-align:center; {pb_bg}'>"
                    f"{anchor_tag}<hr style='border:none;border-top:2px dashed #94A3B8;margin:4px 0;'/>"
                    f"<span style='color:#64748B;font-size:11px;'>[{bid}] ─── 페이지 나누기 (Page Break) ───</span>"
                    f"</div>"
                )
                if _extract_paragraph_plain_text(p_elem).strip():
                    inner = _render_paragraph_rich_html(p_elem, script_map, char_style_map)
                    html_parts.append(
                        f"<p style='margin:4px 0;{align_css}'>"
                        f"<span style='color:#94A3B8;font-size:11px;margin-right:6px;'>[{bid}]</span>{inner}</p>"
                    )
                continue

            inner = _render_paragraph_rich_html(p_elem, script_map, char_style_map)
            bg = (
                "background:#FEF9C3; border-left:4px solid #2563EB; padding:4px 8px; border-radius:4px;"
                if is_hl
                else "margin:4px 0;"
            )
            html_parts.append(
                f"<p style='{bg}{align_css}'>{anchor_tag}<span style='color:#94A3B8;font-size:11px;margin-right:6px;'>[{bid}]</span>{inner}</p>"
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
                    cell_html = "<br/>".join(
                        _render_paragraph_rich_html(p, script_map, char_style_map) for p in ps
                    ) or "&nbsp;"
                    cell_align = ""
                    if ps:
                        c_al = str(
                            para_style_map.get(str(ps[0].get("paraPrIDRef", "")), {}).get("align", "")
                        ).upper()
                        if c_al in {"LEFT", "CENTER", "RIGHT", "JUSTIFY"}:
                            cell_align = f" text-align:{c_al.lower()};"
                    html_parts.append(f"<td style='{c_bg} vertical-align:top;{cell_align}'>{c_anchor_tag}{cell_html}</td>")
                    c_i += 1
                html_parts.append("</tr>")
            html_parts.append("</table></div>")

    html_parts.append("</body></html>")
    return "".join(html_parts)


_FULL_XMLNS_ATTRS = (
    'xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" '
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
    'xmlns:hp10="http://www.hancom.co.kr/hwpml/2016/paragraph" '
    'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" '
    'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
    'xmlns:hhs="http://www.hancom.co.kr/hwpml/2011/history" '
    'xmlns:hm="http://www.hancom.co.kr/hwpml/2011/master-page" '
    'xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:opf="http://www.idpf.org/2007/opf/" '
    'xmlns:ooxmlchart="http://www.hancom.co.kr/hwpml/2016/ooxmlchart" '
    'xmlns:hwpunitchar="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar" '
    'xmlns:epub="http://www.idpf.org/2007/ops" '
    'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0"'
)


def _build_standard_blank_hwpx_entries(title: str = "새 문서") -> Dict[str, bytes]:
    """한컴오피스 한글 OWPML(KS X 6101) 표준 규격의 빈 HWPX 패키지 엔트리들을 생성합니다."""
    escaped_title = html.escape(title or "새 문서")

    version_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" '
        'targetApplication="WORDPROCESSOR" major="5" minor="1" micro="0" buildNumber="1" '
        'os="1" xmlVersion="1.4" application="Hancom Office Hangul" '
        'appVersion="11, 0, 0, 0 WIN32LEWindows_Unknown_Version"/>'
    ).encode("utf-8")

    settings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        '<ha:HWPApplicationSetting xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" '
        'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0">'
        '<ha:CaretPosition listIDRef="0" paraIDRef="0" pos="0"/>'
        '</ha:HWPApplicationSetting>'
    ).encode("utf-8")

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        '<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf">'
        '<ocf:rootfiles>'
        '<ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>'
        '</ocf:rootfiles>'
        '</ocf:container>'
    ).encode("utf-8")

    manifest_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        '<odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>'
    ).encode("utf-8")

    content_hpf = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        f'<opf:package {_FULL_XMLNS_ATTRS} version="" unique-identifier="" id="">'
        '<opf:metadata>'
        f'<opf:title>{escaped_title}</opf:title>'
        '<opf:language>ko</opf:language>'
        '<opf:meta name="creator" content="text">EduFlow</opf:meta>'
        '<opf:meta name="subject" content="text"/>'
        '<opf:meta name="description" content="text"/>'
        '<opf:meta name="lastsaveby" content="text">EduFlow</opf:meta>'
        '</opf:metadata>'
        '<opf:manifest>'
        '<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
        '<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>'
        '<opf:item id="settings" href="settings.xml" media-type="application/xml"/>'
        '</opf:manifest>'
        '<opf:spine>'
        '<opf:itemref idref="header" linear="yes"/>'
        '<opf:itemref idref="section0" linear="yes"/>'
        '</opf:spine>'
        '</opf:package>'
    ).encode("utf-8")

    langs = ("HANGUL", "LATIN", "HANJA", "JAPANESE", "OTHER", "SYMBOL", "USER")
    fontface_blocks = "".join(
        f'<hh:fontface lang="{lang}" fontCnt="2">'
        '<hh:font id="0" face="함초롬돋움" type="TTF" isEmbedded="0">'
        '<hh:typeInfo familyType="FCAT_GOTHIC" weight="6" proportion="4" contrast="0" '
        'strokeVariation="1" armStyle="1" letterform="1" midline="1" xHeight="1"/>'
        '</hh:font>'
        '<hh:font id="1" face="함초롬바탕" type="TTF" isEmbedded="0">'
        '<hh:typeInfo familyType="FCAT_GOTHIC" weight="6" proportion="4" contrast="0" '
        'strokeVariation="1" armStyle="1" letterform="1" midline="1" xHeight="1"/>'
        '</hh:font>'
        '</hh:fontface>'
        for lang in langs
    )

    header_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        f'<hh:head {_FULL_XMLNS_ATTRS} version="1.4" secCnt="1">'
        '<hh:beginNum page="1" footnote="1" endnote="1" pic="1" tbl="1" equation="1"/>'
        '<hh:refList>'
        f'<hh:fontfaces itemCnt="7">{fontface_blocks}</hh:fontfaces>'
        '<hh:borderFills itemCnt="2">'
        '<hh:borderFill id="1" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
        '<hh:slash type="NONE" Crooked="0" isCounter="0"/>'
        '<hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
        '<hh:leftBorder type="NONE" width="0.1 mm" color="#000000"/>'
        '<hh:rightBorder type="NONE" width="0.1 mm" color="#000000"/>'
        '<hh:topBorder type="NONE" width="0.1 mm" color="#000000"/>'
        '<hh:bottomBorder type="NONE" width="0.1 mm" color="#000000"/>'
        '<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>'
        '</hh:borderFill>'
        '<hh:borderFill id="2" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
        '<hh:slash type="NONE" Crooked="0" isCounter="0"/>'
        '<hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
        '<hh:leftBorder type="SOLID" width="0.12 mm" color="#000000"/>'
        '<hh:rightBorder type="SOLID" width="0.12 mm" color="#000000"/>'
        '<hh:topBorder type="SOLID" width="0.12 mm" color="#000000"/>'
        '<hh:bottomBorder type="SOLID" width="0.12 mm" color="#000000"/>'
        '<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>'
        '</hh:borderFill>'
        '</hh:borderFills>'
        '<hh:charProperties itemCnt="1">'
        '<hh:charPr id="0" height="1000" textColor="#000000" shadeColor="none" '
        'useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="1">'
        '<hh:fontRef hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"/>'
        '<hh:ratio hangul="100" latin="100" hanja="100" japanese="100" other="100" symbol="100" user="100"/>'
        '<hh:spacing hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"/>'
        '<hh:relSz hangul="100" latin="100" hanja="100" japanese="100" other="100" symbol="100" user="100"/>'
        '<hh:offset hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"/>'
        '<hh:underline type="NONE" shape="SOLID" color="#000000"/>'
        '<hh:strikeout shape="NONE" color="#000000"/>'
        '<hh:outline type="NONE"/>'
        '<hh:shadow type="NONE" color="#C0C0C0" offsetX="10" offsetY="10"/>'
        '</hh:charPr>'
        '</hh:charProperties>'
        '<hh:tabProperties itemCnt="1">'
        '<hh:tabPr id="0" autoTabLeft="0" autoTabRight="0"/>'
        '</hh:tabProperties>'
        '<hh:numberings itemCnt="1">'
        '<hh:numbering id="1" start="0">'
        '<hh:paraHead start="1" level="1" align="LEFT" useInstWidth="1" autoIndent="1" '
        'widthAdjust="0" textOffsetType="PERCENT" textOffset="50" numFormat="DIGIT" '
        'charPrIDRef="4294967295" checkable="0">^1.</hh:paraHead>'
        '</hh:numbering>'
        '</hh:numberings>'
        '<hh:paraProperties itemCnt="1">'
        '<hh:paraPr id="0" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1" '
        'suppressLineNumbers="0" checked="0">'
        '<hh:align horizontal="JUSTIFY" vertical="BASELINE"/>'
        '<hh:heading type="NONE" idRef="0" level="0"/>'
        '<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="KEEP_WORD" '
        'widowOrphan="0" keepWithNext="0" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>'
        '<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>'
        '<hh:margin>'
        '<hc:intent value="0" unit="HWPUNIT"/>'
        '<hc:left value="0" unit="HWPUNIT"/>'
        '<hc:right value="0" unit="HWPUNIT"/>'
        '<hc:prev value="0" unit="HWPUNIT"/>'
        '<hc:next value="0" unit="HWPUNIT"/>'
        '</hh:margin>'
        '<hh:lineSpacing type="PERCENT" value="160" unit="HWPUNIT"/>'
        '<hh:border borderFillIDRef="1" offsetLeft="0" offsetRight="0" offsetTop="0" '
        'offsetBottom="0" connect="0" ignoreMargin="0"/>'
        '</hh:paraPr>'
        '</hh:paraProperties>'
        '<hh:styles itemCnt="1">'
        '<hh:style id="0" type="PARA" name="바탕글" engName="Normal" paraPrIDRef="0" '
        'charPrIDRef="0" nextStyleIDRef="0" langID="1042" lockForm="0"/>'
        '</hh:styles>'
        '</hh:refList>'
        '</hh:head>'
    ).encode("utf-8")

    section_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        f'<hs:sec {_FULL_XMLNS_ATTRS}>'
        '<hp:p id="2613180041" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
        '<hp:run charPrIDRef="0">'
        '<hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="1134" tabStop="8000" '
        'tabStopVal="4000" tabStopUnit="HWPUNIT" outlineShapeIDRef="1" memoShapeIDRef="0" '
        'textVerticalWidthHead="0" masterPageCnt="0">'
        '<hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0"/>'
        '<hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/>'
        '<hp:visibility hideFirstHeader="0" hideFirstFooter="0" hideFirstMasterPage="0" '
        'border="SHOW_ALL" fill="SHOW_ALL" hideFirstPageNum="0" hideFirstEmptyLine="0" showLineNumber="0"/>'
        '<hp:lineNumberShape restartType="0" countBy="0" distance="0" startNumber="0"/>'
        '<hp:pagePr landscape="WIDELY" width="59528" height="84186" gutterType="LEFT_ONLY">'
        '<hp:margin header="4252" footer="4252" gutter="0" left="8504" right="8504" top="5668" bottom="4252"/>'
        '</hp:pagePr>'
        '<hp:footNotePr>'
        '<hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>'
        '<hp:noteLine length="-1" type="SOLID" width="0.12 mm" color="#000000"/>'
        '<hp:noteSpacing betweenNotes="283" belowLine="567" aboveLine="850"/>'
        '<hp:numbering type="CONTINUOUS" newNum="1"/>'
        '<hp:placement place="EACH_COLUMN" beneathText="0"/>'
        '</hp:footNotePr>'
        '<hp:endNotePr>'
        '<hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>'
        '<hp:noteLine length="14692344" type="SOLID" width="0.12 mm" color="#000000"/>'
        '<hp:noteSpacing betweenNotes="0" belowLine="567" aboveLine="850"/>'
        '<hp:numbering type="CONTINUOUS" newNum="1"/>'
        '<hp:placement place="END_OF_DOCUMENT" beneathText="0"/>'
        '</hp:endNotePr>'
        '<hp:pageBorderFill type="BOTH" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER">'
        '<hp:offset left="1417" right="1417" top="1417" bottom="1417"/>'
        '</hp:pageBorderFill>'
        '<hp:pageBorderFill type="EVEN" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER">'
        '<hp:offset left="1417" right="1417" top="1417" bottom="1417"/>'
        '</hp:pageBorderFill>'
        '<hp:pageBorderFill type="ODD" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER">'
        '<hp:offset left="1417" right="1417" top="1417" bottom="1417"/>'
        '</hp:pageBorderFill>'
        '</hp:secPr>'
        '<hp:ctrl><hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1" sameGap="0"/></hp:ctrl>'
        '</hp:run>'
        '<hp:run charPrIDRef="0"><hp:t/></hp:run>'
        '</hp:p>'
        '</hs:sec>'
    ).encode("utf-8")

    return {
        "mimetype": b"application/hwp+zip",
        "version.xml": version_xml,
        "settings.xml": settings_xml,
        "META-INF/container.xml": container_xml,
        "META-INF/manifest.xml": manifest_xml,
        "Contents/content.hpf": content_hpf,
        "Contents/header.xml": header_xml,
        "Contents/section0.xml": section_xml,
    }


def create_minimal_hwpx_document(title: str = "새 문서", empty: bool = False) -> HwpxDocument:
    """표준 빈 HWPX 템플릿(assets/blank_template.hwpx)을 기반으로 OWPML 호환 문서를 메모리에 생성합니다."""
    entries: Optional[Dict[str, bytes]] = None
    try:
        tmpl_path = Path(get_resource_path("assets/blank_template.hwpx"))
        if tmpl_path.is_file():
            with zipfile.ZipFile(tmpl_path, "r") as zf:
                entries = {name: zf.read(name) for name in zf.namelist()}
    except Exception:
        entries = None

    if not entries:
        entries = _build_standard_blank_hwpx_entries(title=title)
    elif "mimetype" not in entries:
        entries["mimetype"] = b"application/hwp+zip"

    doc = HwpxDocument(entries=entries)

    # content.hpf 제목 갱신
    hpf_name = next((n for n in doc.entries if n.lower() == "contents/content.hpf"), None)
    if hpf_name:
        try:
            hpf_root = _parse_xml(doc.entries[hpf_name])
            for node in hpf_root.iter():
                if _local_tag(node) == "title":
                    node.text = title or "새 문서"
                    break
            doc.entries[hpf_name] = _serialize_xml(hpf_root)
        except Exception:
            pass

    # 첫 번째 section0.xml의 첫 문단 <hp:secPr>/<hp:ctrl>는 보존하고 본문만 초기화
    sec0_name = next((n for n in doc.section_names()), "Contents/section0.xml")
    if sec0_name not in doc.entries:
        fallback = _build_standard_blank_hwpx_entries(title=title)
        doc.entries[sec0_name] = fallback["Contents/section0.xml"]
    else:
        try:
            sec_root = _parse_xml(doc.entries[sec0_name])
            p_nodes = [ch for ch in sec_root if _local_tag(ch) == "p"]
            if p_nodes:
                first_p = p_nodes[0]
                for extra in list(sec_root):
                    if extra is not first_p:
                        sec_root.remove(extra)
                _set_paragraph_rich_text(
                    doc,
                    first_p,
                    "" if empty else str(title),
                    section_roots={sec0_name: sec_root},
                    base_char_pr_id="0",
                )
            else:
                fallback = _build_standard_blank_hwpx_entries(title=title)
                sec_root = _parse_xml(fallback["Contents/section0.xml"])
                first_p = next(ch for ch in sec_root if _local_tag(ch) == "p")
                if not empty:
                    _set_paragraph_rich_text(
                        doc, first_p, str(title), section_roots={sec0_name: sec_root}, base_char_pr_id="0"
                    )
            doc.entries[sec0_name] = _serialize_xml(sec_root)
        except Exception:
            fallback = _build_standard_blank_hwpx_entries(title=title)
            doc.entries[sec0_name] = fallback["Contents/section0.xml"]

    _ensure_table_border_fill_id(doc)
    return doc


_HWP_COM_UNAVAILABLE_MSG = (
    "한컴오피스(한글) 또는 pywin32가 설치되어 있지 않아 .hwp 자동 변환을 사용할 수 없습니다. "
    "한글에서 HWPX로 저장 후 열어주세요."
)


def _bring_hwp_security_popup_to_front(
    stop_event: threading.Event,
    interval_sec: float = 0.15,
) -> None:
    """
    백그라운드 한글 COM(HWPFrame.HwpObject)이 띄우는 파일 접근 보안 경고창을
    프로세스명(hwp.exe) 기준으로 직접 탐색해 화면 최상단(HWND_TOPMOST)에 강제 노출합니다.
    """
    if sys.platform != "win32":
        return

    try:
        import os
        from ctypes import wintypes

        # 전역 windll 오염 방지를 위해 독립 WinDLL 인스턴스 사용
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SW_SHOWNORMAL = 1
        SW_RESTORE = 9
        HWND_TOPMOST = wintypes.HWND(-1)
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_SHOWWINDOW = 0x0040

        current_pid = os.getpid()
        pid_is_hwp_cache: Dict[int, bool] = {}
        attempt_counts: Dict[int, int] = {}

        def _is_hwp_process(pid_val: int) -> bool:
            if pid_val in pid_is_hwp_cache:
                return pid_is_hwp_cache[pid_val]
            is_hwp = False
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_val)
            if h_proc:
                try:
                    buf = ctypes.create_unicode_buffer(512)
                    size = wintypes.DWORD(512)
                    if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                        exe_path = (buf.value or "").lower()
                        is_hwp = exe_path.endswith("\\hwp.exe") or exe_path.endswith("/hwp.exe")
                finally:
                    kernel32.CloseHandle(h_proc)
            pid_is_hwp_cache[pid_val] = is_hwp
            return is_hwp

        def _get_window_text(hwnd: int) -> str:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            return buf.value or ""

        def _is_hwp_security_dialog(hwnd: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return False

            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == 0 or pid.value == current_pid:
                return False

            # 1순위: hwp.exe 프로세스가 화면에 띄운 visible 창이면 즉시 대상 확정
            if _is_hwp_process(pid.value):
                return True

            # 2순위: 보조 제목 매칭
            title = _get_window_text(hwnd).strip()
            return title in ("한글", "HWP", "Hwp", "한컴오피스 한글")

        def _force_foreground(hwnd: int) -> None:
            fg_hwnd = user32.GetForegroundWindow()
            cur_tid = kernel32.GetCurrentThreadId()
            fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
            target_tid = user32.GetWindowThreadProcessId(hwnd, None)

            attached_fg = False
            attached_target = False
            try:
                if fg_tid and fg_tid != cur_tid:
                    attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
                if target_tid and target_tid != cur_tid and target_tid != fg_tid:
                    attached_target = bool(user32.AttachThreadInput(cur_tid, target_tid, True))

                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                else:
                    user32.ShowWindow(hwnd, SW_SHOWNORMAL)

                user32.SetWindowPos(
                    hwnd,
                    HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW,
                )
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached_target:
                    user32.AttachThreadInput(cur_tid, target_tid, False)
                if attached_fg:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)

        def _enum_top_window(hwnd: int, _lparam: int) -> bool:
            try:
                hwnd_int = int(hwnd)
                if not _is_hwp_security_dialog(hwnd_int):
                    return True
                cnt = attempt_counts.get(hwnd_int, 0)
                fg = user32.GetForegroundWindow()
                fg_int = int(fg) if fg else 0
                if cnt == 0 or (cnt < 10 and fg_int != hwnd_int):
                    attempt_counts[hwnd_int] = cnt + 1
                    _force_foreground(hwnd_int)
            except Exception:
                pass
            return True

        enum_cb = WNDENUMPROC(_enum_top_window)
        while not stop_event.is_set():
            try:
                user32.EnumWindows(enum_cb, 0)
            except Exception:
                pass
            if stop_event.wait(interval_sec):
                break
    except Exception:
        return


_HWP_FILE_LOCKED_MSG = (
    "다른 한글 창에서 해당 파일이 열려 있어 저장할 수 없습니다. "
    "한글 창을 닫거나 다른 이름으로 저장해 주세요."
)


def _ensure_writable_if_exists(path: Path) -> None:
    """대상 파일이 존재하면 읽기 전용 속성을 해제해 쓰기 가능 상태로 만듭니다."""
    if path.exists():
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            pass


@contextmanager
def _hwp_security_popup_watcher(interval_sec: float = 0.15) -> Iterator[None]:
    """hwp.Open(...) 및 SaveAs(...) 호출 동안 보안 경고창 최상위 노출 데몬 스레드를 실행합니다."""
    if sys.platform != "win32":
        yield
        return

    stop_event = threading.Event()
    watcher = threading.Thread(
        target=_bring_hwp_security_popup_to_front,
        args=(stop_event, interval_sec),
        daemon=True,
    )
    watcher.start()
    try:
        yield
    finally:
        stop_event.set()
        try:
            watcher.join(timeout=0.5)
        except Exception:
            pass


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
    _ensure_writable_if_exists(dst)
    temp_dst = dst.parent / f".~eduflow_tmp_{uuid.uuid4().hex[:8]}_{dst.name}"
    temp_resolved_str = str(temp_dst.resolve())

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

        with _hwp_security_popup_watcher():
            hwp.Open(str(src.resolve()), src_fmt, "forceopen:true")
            saved_ok = hwp.SaveAs(temp_resolved_str, dst_fmt, "")
            if saved_ok is False or not temp_dst.is_file():
                hwp.HAction.GetDefault("FileSaveAs_S", hwp.HParameterSet.HFileOpenSave.HSet)
                hwp.HParameterSet.HFileOpenSave.filename = temp_resolved_str
                hwp.HParameterSet.HFileOpenSave.Format = dst_fmt
                hwp.HAction.Execute("FileSaveAs_S", hwp.HParameterSet.HFileOpenSave.HSet)
    except Exception:
        temp_dst.unlink(missing_ok=True)
        raise
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

    if not temp_dst.is_file():
        if dst.exists():
            raise PermissionError(_HWP_FILE_LOCKED_MSG)
        raise RuntimeError(f"한글 COM 변환 결과 파일이 생성되지 않았습니다: {dst}")

    _ensure_writable_if_exists(temp_dst)
    _ensure_writable_if_exists(dst)
    try:
        os.replace(temp_dst, dst)
    except (PermissionError, OSError) as exc:
        temp_dst.unlink(missing_ok=True)
        raise PermissionError(_HWP_FILE_LOCKED_MSG) from exc

    if not dst.is_file():
        raise RuntimeError(f"한글 COM 변환 결과 파일이 생성되지 않았습니다: {dst}")
    return dst


def convert_hwp_to_hwpx(hwp_path: Path, out_hwpx_path: Path) -> Path:
    """구형 .hwp 파일을 백그라운드 한글 COM으로 임시/대상 .hwpx 파일로 변환합니다."""
    return _convert_via_hwp_com(hwp_path, out_hwpx_path, "HWP", "HWPX")


def convert_hwpx_to_hwp(hwpx_path: Path, out_hwp_path: Path) -> Path:
    """수정된 .hwpx 파일을 백그라운드 한글 COM으로 .hwp 파일로 역변환 저장합니다."""
    return _convert_via_hwp_com(hwpx_path, out_hwp_path, "HWPX", "HWP")


# =============================================================================
# 8. 새 HWPX 양식 생성용 참고 문서 통합 파서 및 청크 분할
# =============================================================================
def _format_hwpx_dom_as_reference_text(dom_map: Dict[str, Any]) -> str:
    """extract_hwpx_dom_map 결과를 참고용 구조화 텍스트로 포맷합니다."""
    lines: List[str] = []
    for blk in dom_map.get("blocks") or []:
        if blk.get("type") == "table":
            lines.append(f"[표 {blk.get('id', '')}]")
            for row in blk.get("rows") or []:
                row_txt = " | ".join(str(c.get("text", "")).replace("\n", " / ").strip() for c in row)
                if row_txt.strip(" |"):
                    lines.append(f"| {row_txt} |")
        else:
            txt = str(blk.get("text", "")).strip()
            if txt:
                lines.append(txt)
    return "\n\n".join(lines).strip()


def _extract_docx_reference_text(path: Path) -> str:
    """python-docx 우선, 미설치 시 word/document.xml 직접 파싱으로 문단 및 표(| 구분)를 추출합니다."""
    try:
        import docx  # type: ignore

        doc_obj = docx.Document(str(path))
        # 본문 블록(문단/표) 순서대로 추출
        w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        lines: List[str] = []
        body = doc_obj.element.body
        p_idx = 0
        t_idx = 0
        for child in body:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "p" and p_idx < len(doc_obj.paragraphs):
                txt = doc_obj.paragraphs[p_idx].text.strip()
                p_idx += 1
                if txt:
                    lines.append(txt)
            elif tag == "tbl" and t_idx < len(doc_obj.tables):
                tbl = doc_obj.tables[t_idx]
                t_idx += 1
                for row in tbl.rows:
                    row_txt = " | ".join(c.text.replace("\n", " / ").strip() for c in row.cells)
                    if row_txt.strip(" |"):
                        lines.append(row_txt)
        if lines:
            return "\n\n".join(lines)
        _ = w_ns
    except ImportError:
        pass

    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    p_tag = f"{{{w_ns}}}p"
    tbl_tag = f"{{{w_ns}}}tbl"
    tr_tag = f"{{{w_ns}}}tr"
    tc_tag = f"{{{w_ns}}}tc"
    t_tag = f"{{{w_ns}}}t"
    br_tag = f"{{{w_ns}}}br"
    tab_tag = f"{{{w_ns}}}tab"
    body_tag = f"{{{w_ns}}}body"

    def _p_text(p_node: Any) -> str:
        buf: List[str] = []
        for node in p_node.iter():
            if node.tag == t_tag and node.text:
                buf.append(node.text)
            elif node.tag == br_tag:
                buf.append("\n")
            elif node.tag == tab_tag:
                buf.append("\t")
        return "".join(buf).strip()

    with zipfile.ZipFile(path, "r") as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        root = ET.fromstring(zf.read("word/document.xml"))

    body = root.find(body_tag)
    if body is None:
        body = root

    lines = []
    for child in body:
        if child.tag == p_tag:
            txt = _p_text(child)
            if txt:
                lines.append(txt)
        elif child.tag == tbl_tag:
            for tr in child:
                if tr.tag != tr_tag:
                    continue
                row_cells: List[str] = []
                for tc in tr:
                    if tc.tag != tc_tag:
                        continue
                    cell_ps = [_p_text(p) for p in tc if p.tag == p_tag]
                    row_cells.append(" / ".join(cp for cp in cell_ps if cp))
                row_line = " | ".join(row_cells)
                if row_line.strip(" |"):
                    lines.append(row_line)
    return "\n\n".join(lines).strip()


def _extract_excel_reference_text(path: Path) -> str:
    """openpyxl / pandas로 .xlsx / .xls 시트별 헤더와 데이터 행을 추출합니다."""
    ext = path.suffix.lower()
    sections: List[str] = []

    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        try:
            import openpyxl  # type: ignore

            wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
            try:
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    sheet_lines: List[str] = [f"[시트: {sheet_name}]"]
                    has_row = False
                    for row in ws.iter_rows(values_only=True):
                        cells = [str(v).replace("\n", " / ").strip() if v is not None else "" for v in row]
                        if any(cells):
                            sheet_lines.append(" | ".join(cells))
                            has_row = True
                    if has_row:
                        sections.append("\n".join(sheet_lines))
                return "\n\n".join(sections).strip()
            finally:
                wb.close()
        except Exception:
            sections = []

    import pandas as pd  # type: ignore

    sheets = pd.read_excel(str(path), sheet_name=None, dtype=str)
    for sheet_name, df in sheets.items():
        if df is None or df.empty:
            continue
        df_clean = df.fillna("")
        sheet_lines = [f"[시트: {sheet_name}]"]
        headers = [str(col).strip() for col in df_clean.columns]
        if any(headers):
            sheet_lines.append(" | ".join(headers))
        for _, row in df_clean.iterrows():
            vals = [str(v).replace("\n", " / ").strip() for v in row.tolist()]
            if any(vals):
                sheet_lines.append(" | ".join(vals))
        sections.append("\n".join(sheet_lines))
    return "\n\n".join(sections).strip()


def _extract_pdf_reference_text(path: Path) -> str:
    """통합 PDF 엔진(PyMuPDF/pypdfium2/pypdf)으로 텍스트 레이어를 추출합니다."""
    from services.hwpx_ref_session_service import inspect_pdf_document

    info = inspect_pdf_document(path)
    if info.get("is_scanned"):
        return ""
    pages: List[str] = []
    for idx, txt in enumerate(info.get("page_texts") or [], start=1):
        clean = (txt or "").strip()
        if clean:
            pages.append(f"[페이지 {idx}]\n{clean}")
    return "\n\n".join(pages).strip()


def _extract_text_file_with_encoding(path: Path) -> str:
    """UTF-8 / CP949 자동 감지 인코딩으로 텍스트·마크다운·CSV 파일을 읽습니다."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _extract_reference_text_from_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".hwpx":
        try:
            doc = HwpxDocument.load(path)
            txt = _format_hwpx_dom_as_reference_text(extract_hwpx_dom_map(doc))
            if txt:
                return txt
        except Exception:
            pass
        from services import multi_doc_extractor_service

        return str(multi_doc_extractor_service.parse_hwpx_content(path) or "").strip()

    if ext == ".hwp":
        from services import multi_doc_extractor_service

        ole_text = ""
        try:
            ole_text = str(multi_doc_extractor_service._extract_hwp_text_olefile(path) or "").strip()
        except Exception:
            ole_text = ""
        if ole_text:
            return ole_text

        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            converted_hwpx = convert_hwp_to_hwpx(path, Path(tmp_dir) / f"{path.stem}.hwpx")
            doc = HwpxDocument.load(converted_hwpx)
            return _format_hwpx_dom_as_reference_text(extract_hwpx_dom_map(doc))

    if ext == ".docx":
        return _extract_docx_reference_text(path)

    if ext == ".pptx":
        a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
        p_tag = f"{{{a_ns}}}p"
        t_tag = f"{{{a_ns}}}t"
        slides_out: List[str] = []
        with zipfile.ZipFile(path, "r") as zf:
            slide_names = sorted(
                [n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")],
                key=lambda s: int(re.search(r"(\d+)\.xml$", s).group(1)) if re.search(r"(\d+)\.xml$", s) else 0,
            )
            for s_idx, s_name in enumerate(slide_names, start=1):
                root = ET.fromstring(zf.read(s_name))
                p_lines: List[str] = []
                for p_node in root.iter(p_tag):
                    runs = [t_node.text for t_node in p_node.iter(t_tag) if t_node.text]
                    line_txt = "".join(runs).strip()
                    if line_txt:
                        p_lines.append(line_txt)
                if p_lines:
                    slides_out.append(f"[슬라이드 {s_idx}]\n" + "\n".join(p_lines))
        return "\n\n".join(slides_out).strip()

    if ext in (".xlsx", ".xls", ".xlsm"):
        return _extract_excel_reference_text(path)

    if ext == ".pdf":
        return _extract_pdf_reference_text(path)

    return _extract_text_file_with_encoding(path)


def _split_text_into_chunks(text: str, max_chars_per_chunk: int) -> List[str]:
    """문단(\n\n) 및 줄(\n) 경계를 유지하며 max_chars_per_chunk 이하의 청크 리스트로 분할합니다."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    limit = max(100, int(max_chars_per_chunk or 8000))
    if len(cleaned) <= limit:
        return [cleaned]

    chunks: List[str] = []
    current_parts: List[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current_parts, current_len
        if current_parts:
            chunks.append("\n\n".join(current_parts))
            current_parts = []
            current_len = 0

    def _append_unit(unit: str, sep_len: int = 2) -> None:
        nonlocal current_len
        u = unit.strip()
        if not u:
            return
        if len(u) <= limit:
            needed = len(u) + (sep_len if current_parts else 0)
            if current_parts and current_len + needed > limit:
                _flush()
            current_parts.append(u)
            current_len = len("\n\n".join(current_parts))
            return

        # 단일 문단이 limit를 초과하면 줄(\n) 단위로 재분할
        _flush()
        line_buf: List[str] = []
        line_buf_len = 0
        for ln in u.splitlines():
            ln_s = ln.rstrip()
            if not ln_s:
                continue
            if len(ln_s) > limit:
                if line_buf:
                    chunks.append("\n".join(line_buf))
                    line_buf = []
                    line_buf_len = 0
                for start in range(0, len(ln_s), limit):
                    chunks.append(ln_s[start : start + limit])
            else:
                need = len(ln_s) + (1 if line_buf else 0)
                if line_buf and line_buf_len + need > limit:
                    chunks.append("\n".join(line_buf))
                    line_buf = [ln_s]
                    line_buf_len = len(ln_s)
                else:
                    line_buf.append(ln_s)
                    line_buf_len += need
        if line_buf:
            chunks.append("\n".join(line_buf))

    for para in cleaned.split("\n\n"):
        _append_unit(para)
    _flush()
    return [c for c in chunks if c.strip()]


def extract_hwpx_reference_chunks(
    file_paths: List[Path | str],
    max_chars_per_chunk: int = 8000,
) -> List[Dict[str, Any]]:
    """
    참고 문서(.hwpx, .hwp, .docx, .xlsx/.xls, .pdf, .txt/.md/.csv)를 세션 캐시에 인덱싱하고
    파트 목록([{"part_index": 1, "total_parts": N, "chunk_id": ..., "content": "...", ...}])으로 반환합니다.
    """
    if not file_paths:
        return []

    from services.hwpx_ref_session_service import build_reference_index_summary

    summary = build_reference_index_summary(file_paths, max_chars_per_chunk=max_chars_per_chunk)
    chunks = summary.get("chunks") or []
    limit = max(100, int(max_chars_per_chunk or 8000))

    # 인접한 cached 청크가 max_chars_per_chunk 이하이면 하나로 병합(소형 다중 파일 호환)
    merged_items: List[Dict[str, Any]] = []
    for ch in chunks:
        item = dict(ch)
        if item.get("status") == "cached" and item.get("content"):
            fname = item.get("file_name") or ""
            if fname and not str(item["content"]).startswith("=== [참고 문서:"):
                item["content"] = f"=== [참고 문서: {fname}] ===\n{item['content']}"
            if len(str(item["content"])) > limit:
                for sub_txt in _split_text_into_chunks(str(item["content"]), max_chars_per_chunk=limit):
                    sub_item = dict(item)
                    sub_item["content"] = sub_txt
                    sub_item["char_count"] = len(sub_txt)
                    merged_items.append(sub_item)
                continue
            if (
                merged_items
                and merged_items[-1].get("status") == "cached"
                and len(str(merged_items[-1].get("content", ""))) + 2 + len(str(item["content"])) <= limit
            ):
                merged_items[-1]["content"] = f"{merged_items[-1]['content']}\n\n{item['content']}"
                merged_items[-1]["char_count"] = len(merged_items[-1]["content"])
                continue
        merged_items.append(item)

    total = len(merged_items)
    for idx, item in enumerate(merged_items, start=1):
        item["part_index"] = idx
        item["total_parts"] = total
    return merged_items
