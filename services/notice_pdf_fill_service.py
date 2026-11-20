"""
가정통신문 PDF 양식 좌표 매핑 기반 응답 일괄 합성·출력 서비스.
- 정규화 좌표(0.0 ~ 1.0, 좌상단 원점)를 ReportLab PDF 포인트(좌하단 원점)로 변환해
  원본 PDF 위에 텍스트, 체크마크(✓), Base64 서명 이미지를 합성합니다.
"""

from __future__ import annotations

import base64
import io
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_REGISTERED_FONT_NAME: Optional[str] = None


def _ensure_korean_font() -> str:
    """Windows 기본 한글 트루타입 폰트(맑은 고딕 등)를 ReportLab에 등록합니다."""
    global _REGISTERED_FONT_NAME
    if _REGISTERED_FONT_NAME:
        return _REGISTERED_FONT_NAME

    candidates = [
        ("MalgunGothic", Path(r"C:\Windows\Fonts\malgun.ttf")),
        ("NanumGothic", Path(r"C:\Windows\Fonts\NanumGothic.ttf")),
        ("Gulim", Path(r"C:\Windows\Fonts\gulim.ttc")),
    ]
    for font_name, font_path in candidates:
        if font_path.is_file():
            try:
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
                _REGISTERED_FONT_NAME = font_name
                return font_name
            except Exception:
                continue

    _REGISTERED_FONT_NAME = "Helvetica"
    return _REGISTERED_FONT_NAME


def normalized_rect_to_pdf_points(
    x_ratio: float,
    y_ratio: float,
    w_ratio: float,
    h_ratio: float,
    page_width: float,
    page_height: float,
) -> Tuple[float, float, float, float]:
    """
    좌상단 원점의 정규화 좌표(0.0~1.0)를
    ReportLab 좌하단 원점의 PDF 포인트 (x, y, w, h)로 변환합니다.
    """
    xr = max(0.0, min(1.0, float(x_ratio)))
    yr = max(0.0, min(1.0, float(y_ratio)))
    wr = max(0.005, min(1.0 - xr, float(w_ratio)))
    hr = max(0.005, min(1.0 - yr, float(h_ratio)))

    x_pt = xr * float(page_width)
    w_pt = max(4.0, wr * float(page_width))
    h_pt = max(4.0, hr * float(page_height))
    y_pt = float(page_height) - (yr + hr) * float(page_height)
    return x_pt, y_pt, w_pt, h_pt


def decode_signature_data_url(data_url: str) -> Optional[bytes]:
    """data:image/png;base64,... 형식 또는 순수 Base64 문자열을 이미지 바이트로 디코딩합니다."""
    raw = str(data_url or "").strip()
    if not raw:
        return None
    if raw.startswith("data:"):
        if "," not in raw:
            return None
        raw = raw.split(",", 1)[1].strip()
    if not raw or raw.startswith("http://") or raw.startswith("https://"):
        return None
    try:
        return base64.b64decode(raw, validate=False)
    except Exception:
        return None


def _parse_answers_dict(reply: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("answers", "reply_answers", "responses", "form_data"):
        val = reply.get(key)
        if isinstance(val, dict):
            return val
        if isinstance(val, str) and val.strip().startswith("{"):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


def extract_reply_field_value(
    reply: Dict[str, Any],
    field_key: str,
    *,
    student_meta: Optional[Dict[str, Any]] = None,
) -> Any:
    """회신 dict에서 시스템 필드 또는 reply_schema 항목 값을 추출합니다."""
    key = str(field_key or "").strip()
    meta = student_meta or {}
    answers = _parse_answers_dict(reply)

    if key in ("grade", "학년"):
        return reply.get("grade") or meta.get("grade") or ""
    if key in ("class_no", "class", "반"):
        return reply.get("class_no") or reply.get("class") or meta.get("class_no") or ""
    if key in ("number", "student_number", "번호"):
        num = reply.get("student_number") or reply.get("number") or meta.get("number")
        if num:
            return str(num)
        sid = str(reply.get("student_id") or meta.get("student_id") or "").strip()
        if sid.isdigit() and len(sid) >= 4:
            return str(int(sid[-2:]))
        return sid
    if key in ("name", "student_name", "이름"):
        return reply.get("student_name") or reply.get("name") or meta.get("name") or ""
    if key == "student_id":
        return reply.get("student_id") or meta.get("student_id") or ""

    if key in answers:
        return answers[key]
    if key in reply:
        return reply[key]
    if key == "consent" and reply.get("reply_choice"):
        return reply.get("reply_choice")
    return ""


def _option_is_selected(answer: Any, option: str) -> bool:
    opt = str(option or "").strip()
    if not opt:
        return False
    if isinstance(answer, (list, tuple, set)):
        return opt in [str(x).strip() for x in answer]
    ans_str = str(answer or "").strip()
    if not ans_str:
        return False
    if ans_str == opt:
        return True
    parts = [p.strip() for p in re.split(r"[,/|]", ans_str) if p.strip()]
    return opt in parts


def _draw_fitted_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    w: float,
    h: float,
    font_name: str,
) -> None:
    clean = str(text or "").strip()
    if not clean:
        return

    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()] or [clean]
    max_font = max(7.0, min(13.0, h * 0.62 if len(lines) == 1 else h / (len(lines) * 1.25)))
    c.setFont(font_name, max_font)

    # 단일 줄일 때 박스 너비를 넘으면 폰트를 축소
    if len(lines) == 1:
        font_sz = max_font
        while font_sz > 6.0 and pdfmetrics.stringWidth(lines[0], font_name, font_sz) > max(10.0, w - 4.0):
            font_sz -= 0.5
        c.setFont(font_name, font_sz)
        baseline_y = y + max(2.0, (h - font_sz) * 0.5)
        c.drawString(x + 2.0, baseline_y, lines[0])
        return

    line_h = max_font * 1.2
    cur_y = y + h - max_font - 2.0
    for ln in lines:
        if cur_y < y:
            break
        c.drawString(x + 2.0, cur_y, ln)
        cur_y -= line_h


def _build_overlay_pdf_bytes(
    page_sizes: List[Tuple[float, float]],
    field_map: List[Dict[str, Any]],
    reply: Dict[str, Any],
    *,
    student_meta: Optional[Dict[str, Any]] = None,
) -> bytes:
    buf = io.BytesIO()
    font_name = _ensure_korean_font()
    c = canvas.Canvas(buf)

    for page_idx, (pw, ph) in enumerate(page_sizes, start=1):
        c.setPageSize((pw, ph))
        page_fields = [f for f in field_map if int(f.get("page", 1)) == page_idx]
        for fld in page_fields:
            x, y, w, h = normalized_rect_to_pdf_points(
                fld.get("x_ratio", 0.0),
                fld.get("y_ratio", 0.0),
                fld.get("w_ratio", 0.1),
                fld.get("h_ratio", 0.04),
                pw,
                ph,
            )
            kind = str(fld.get("kind") or "text").strip().lower()
            key = str(fld.get("key") or "").strip()

            if kind == "signature":
                if key == "student_signature":
                    raw_sig = (
                        reply.get("student_signature_data_url")
                        or reply.get("signature_data_url")
                        or ""
                    )
                elif key == "parent_signature":
                    raw_sig = (
                        reply.get("parent_signature_data_url")
                        or reply.get("signature_data_url")
                        or ""
                    )
                else:
                    raw_sig = reply.get(key) or reply.get("signature_data_url") or ""
                sig_bytes = decode_signature_data_url(str(raw_sig or ""))
                if sig_bytes:
                    try:
                        img = ImageReader(io.BytesIO(sig_bytes))
                        c.drawImage(
                            img,
                            x,
                            y,
                            width=w,
                            height=h,
                            mask="auto",
                            preserveAspectRatio=True,
                            anchor="c",
                        )
                    except Exception:
                        pass
            elif kind == "option":
                ans = extract_reply_field_value(reply, key, student_meta=student_meta)
                if _option_is_selected(ans, str(fld.get("option") or "")):
                    mark_size = max(8.0, min(18.0, min(w, h) * 0.85))
                    c.setFont(font_name, mark_size)
                    c.drawCentredString(x + w / 2.0, y + max(1.0, (h - mark_size) * 0.35), "✓")
            else:
                ans = extract_reply_field_value(reply, key, student_meta=student_meta)
                if ans not in (None, ""):
                    _draw_fitted_text(c, str(ans), x, y, w, h, font_name)

        c.showPage()

    c.save()
    return buf.getvalue()


def fill_notice_pdf_for_reply(
    source_pdf_path: str | Path,
    field_map: List[Dict[str, Any]],
    reply: Dict[str, Any],
    output_pdf_path: str | Path,
    *,
    student_meta: Optional[Dict[str, Any]] = None,
) -> Path:
    """단일 학생 회신 데이터를 원본 PDF 위에 합성하여 새 PDF 파일로 저장합니다."""
    src = Path(source_pdf_path)
    if not src.is_file():
        raise FileNotFoundError(f"원본 PDF 파일을 찾을 수 없습니다: {src}")

    reader = PdfReader(str(src))
    page_sizes = [
        (float(page.mediabox.width), float(page.mediabox.height))
        for page in reader.pages
    ]
    overlay_bytes = _build_overlay_pdf_bytes(
        page_sizes,
        field_map,
        reply,
        student_meta=student_meta,
    )
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))

    writer = PdfWriter()
    for idx, page in enumerate(reader.pages):
        writer_page = writer.add_page(page)
        if idx < len(overlay_reader.pages):
            writer_page.merge_page(overlay_reader.pages[idx])

    out = Path(output_pdf_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fp:
        writer.write(fp)
    return out


def _safe_filename_part(val: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(val or "").strip()).strip("_") or "미상"


def batch_export_notice_pdfs(
    source_pdf_path: str | Path,
    field_map: List[Dict[str, Any]],
    replies: List[Dict[str, Any]],
    output_target: str | Path,
    *,
    mode: str = "individual",
    class_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    학급 전체 회신 목록을 원본 PDF에 일괄 합성합니다.
    - mode="individual": output_target 폴더 아래 `학번_이름.pdf` 개별 저장
    - mode="merged": output_target 파일 경로에 전체 학생 통합 PDF 1개 저장
    """
    src = Path(source_pdf_path)
    if not src.is_file():
        raise FileNotFoundError(f"원본 PDF 파일을 찾을 수 없습니다: {src}")
    if not replies:
        raise ValueError("출력할 학생 회신 데이터가 없습니다.")

    base_meta = dict(class_meta or {})
    norm_mode = str(mode or "individual").strip().lower()

    if norm_mode == "merged":
        writer = PdfWriter()
        for rep in replies:
            sid = str(rep.get("student_id") or "").strip()
            sname = str(rep.get("student_name") or rep.get("name") or "").strip()
            s_meta = {**base_meta, "student_id": sid, "name": sname}

            reader = PdfReader(str(src))
            page_sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages]
            overlay_bytes = _build_overlay_pdf_bytes(
                page_sizes, field_map, rep, student_meta=s_meta
            )
            overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
            for idx, page in enumerate(reader.pages):
                writer_page = writer.add_page(page)
                if idx < len(overlay_reader.pages):
                    writer_page.merge_page(overlay_reader.pages[idx])

        out_file = Path(output_target)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with out_file.open("wb") as fp:
            writer.write(fp)
        return {
            "mode": "merged",
            "count": len(replies),
            "output_file": str(out_file),
            "total_pages": len(writer.pages),
        }

    out_dir = Path(output_target)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_files: List[str] = []
    for idx, rep in enumerate(replies, start=1):
        sid = _safe_filename_part(str(rep.get("student_id") or f"student_{idx}"))
        sname = _safe_filename_part(str(rep.get("student_name") or rep.get("name") or "학생"))
        s_meta = {**base_meta, "student_id": str(rep.get("student_id") or ""), "name": sname}
        out_path = out_dir / f"{sid}_{sname}.pdf"
        fill_notice_pdf_for_reply(
            src,
            field_map,
            rep,
            out_path,
            student_meta=s_meta,
        )
        generated_files.append(str(out_path))

    return {
        "mode": "individual",
        "count": len(generated_files),
        "output_dir": str(out_dir),
        "files": generated_files,
    }


def render_pdf_pages_to_png_files(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    scale: float = 1.8,
) -> List[Path]:
    """PDF 각 페이지를 PNG 이미지 파일로 렌더링하여 경로 목록을 반환합니다."""
    src = Path(pdf_path)
    if not src.is_file():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {src}")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    doc = pdfium.PdfDocument(str(src))
    paths: List[Path] = []
    try:
        for idx in range(len(doc)):
            page = doc[idx]
            pil_img = page.render(scale=scale).to_pil().convert("RGB")
            img_path = out / f"page_{idx + 1}.png"
            pil_img.save(str(img_path), format="PNG")
            paths.append(img_path)
    finally:
        doc.close()
    return paths


def build_pdf_vision_mapping_prompt(
    palette_entries: List[Dict[str, Any]],
    *,
    page_count: int = 1,
    target_page: Optional[int] = None,
) -> str:
    """
    AI 비전 모델에게 PDF 페이지 이미지 내 각 항목의 기입 빈칸/체크박스(□) 좌표를
    0.0~1.0 정규화 비율 JSON 배열로 요청하는 프롬프트를 생성합니다.
    """
    lines: List[str] = []
    for idx, entry in enumerate(palette_entries or [], start=1):
        num = int(entry.get("num") or idx)
        kind = str(entry.get("kind") or "text")
        label = str(entry.get("label") or entry.get("key") or "").strip()
        opt = str(entry.get("option") or "").strip()
        if kind == "option" and opt:
            lines.append(f"[{num}] 체크박스(✓): {label} → 선택지 '{opt}'")
        elif kind == "signature":
            lines.append(f"[{num}] 서명란 이미지: {label}")
        else:
            lines.append(f"[{num}] 텍스트 입력칸: {label}")

    page_desc = (
        f"첨부된 이미지는 전체 {page_count}페이지 중 {target_page}페이지입니다."
        if target_page is not None
        else f"첨부된 이미지는 가정통신문 양식(총 {page_count}페이지)입니다."
    )
    items_block = "\n".join(lines)

    return (
        f"{page_desc}\n"
        "아래 번호가 매겨진 항목들이 실제로 기입되거나 체크(✓)되어야 하는 위치의 좌표를 찾아 "
        "JSON 배열로만 응답하세요. 다른 설명 문장은 출력하지 마세요.\n\n"
        "[중요 좌표 규칙 — 반드시 준수]\n"
        "1. 인쇄된 질문/라벨 글자 위를 덮지 마세요. 반드시 **라벨 글자 옆이나 아래의 빈 기입칸(밑줄/빈 공간), 또는 `□`/`☐` 체크박스 네모 내부 좌표**를 지정하세요.\n"
        "2. `x_ratio`, `y_ratio`는 이미지 좌상단(0.0, 0.0) 기준의 정규화 비율(0.0 ~ 1.0)입니다.\n"
        "3. `w_ratio`, `h_ratio`는 해당 기입칸 또는 체크박스 네모의 너비/높이 비율(0.0 ~ 1.0)입니다. 체크박스(✓)는 대략 0.025~0.04 크기로 지정하세요.\n"
        "4. 현재 페이지 이미지에 존재하지 않는 항목은 JSON 배열에서 제외하세요.\n\n"
        "[응답 JSON 형식]\n"
        "```json\n"
        "[\n"
        '  {"index": 1, "page": 1, "x_ratio": 0.62, "y_ratio": 0.31, "w_ratio": 0.08, "h_ratio": 0.03}\n'
        "]\n"
        "```\n\n"
        "[매핑할 항목 목록]\n"
        f"{items_block}"
    )


def parse_pdf_vision_mapping_response(
    raw_response: str,
    palette_entries: List[Dict[str, Any]],
    *,
    page_count: int = 1,
    default_page: int = 1,
) -> List[Dict[str, Any]]:
    """AI 비전 응답(JSON 배열 또는 객체)을 파싱해 정규화된 pdf_field_map 항목 목록으로 변환합니다."""
    raw = str(raw_response or "").strip()
    if not raw:
        raise ValueError("AI 응답 텍스트가 비어 있습니다.")

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()
    data: Any = None
    try:
        data = json.loads(cleaned)
    except Exception:
        arr_m = re.search(r"\[[\s\S]*\]", cleaned)
        obj_m = re.search(r"\{[\s\S]*\}", cleaned)
        snippet = arr_m.group(0) if arr_m else (obj_m.group(0) if obj_m else "")
        if not snippet:
            raise ValueError("응답에서 JSON 배열/객체를 찾을 수 없습니다.")
        data = json.loads(snippet)

    if isinstance(data, dict):
        for k in ("items", "fields", "mappings", "pdf_field_map", "boxes"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    if not isinstance(data, list):
        raise ValueError("AI 좌표 응답은 JSON 배열([ ... ]) 형식이어야 합니다.")

    entry_by_num: Dict[int, Dict[str, Any]] = {}
    for idx, entry in enumerate(palette_entries or [], start=1):
        num = int(entry.get("num") or idx)
        entry_by_num[num] = entry

    results: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_idx = item.get("index", item.get("num", item.get("id")))
        try:
            num = int(str(raw_idx).lstrip("#").strip())
        except Exception:
            continue
        entry = entry_by_num.get(num)
        if entry is None:
            continue

        try:
            xr = float(item.get("x_ratio", 0.0))
            yr = float(item.get("y_ratio", 0.0))
            wr = float(item.get("w_ratio", entry.get("w_ratio", 0.1)))
            hr = float(item.get("h_ratio", entry.get("h_ratio", 0.03)))
        except Exception:
            continue

        wr = max(0.015, min(0.95, wr))
        hr = max(0.015, min(0.95, hr))
        xr = max(0.0, min(1.0 - wr, xr))
        yr = max(0.0, min(1.0 - hr, yr))

        try:
            pg = int(item.get("page") or default_page)
        except Exception:
            pg = default_page
        pg = max(1, min(max(1, int(page_count)), pg))

        results.append(
            {
                "id": uuid.uuid4().hex[:10],
                "num": num,
                "palette_id": str(entry.get("palette_id") or ""),
                "key": str(entry.get("key") or ""),
                "label": str(entry.get("label") or ""),
                "kind": str(entry.get("kind") or "text"),
                "option": str(entry.get("option") or ""),
                "page": pg,
                "x_ratio": round(xr, 4),
                "y_ratio": round(yr, 4),
                "w_ratio": round(wr, 4),
                "h_ratio": round(hr, 4),
            }
        )
    return results


def request_pdf_vision_mapping_api(
    page_image_paths: List[Path],
    palette_entries: List[Dict[str, Any]],
    *,
    llm_client: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """MultiLlmClient 멀티모달 호출로 각 PDF 페이지 PNG에서 좌표 매핑을 자동 추출합니다."""
    from services.llm_client import MultiLlmClient

    client = llm_client or MultiLlmClient()
    total_pages = max(1, len(page_image_paths))
    merged_by_pid: Dict[str, Dict[str, Any]] = {}

    for pg_idx, img_path in enumerate(page_image_paths, start=1):
        p = Path(img_path)
        if not p.is_file():
            continue
        prompt = build_pdf_vision_mapping_prompt(
            palette_entries,
            page_count=total_pages,
            target_page=pg_idx,
        )
        raw_reply = client.generate(
            prompt=prompt,
            system_prompt=(
                "당신은 한국 학교 가정통신문 PDF 양식 좌표 매핑 전문 비전 AI입니다. "
                "라벨 글자가 아니라 실제 빈 기입칸·체크박스(□) 내부의 정규화 좌표만 JSON 배열로 출력하세요."
            ),
            image_bytes=p.read_bytes(),
            mime_type="image/png",
        )
        parsed = parse_pdf_vision_mapping_response(
            raw_reply,
            palette_entries,
            page_count=total_pages,
            default_page=pg_idx,
        )
        for box in parsed:
            pid = str(box.get("palette_id") or box.get("num"))
            if pid not in merged_by_pid:
                merged_by_pid[pid] = box

    return list(merged_by_pid.values())
