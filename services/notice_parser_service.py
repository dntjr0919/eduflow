"""
가정통신문 PDF/이미지 AI OCR 구조화 JSON 추출 및 정규화 서비스.
- 기존 MultiLlmClient 및 텍스트 추출 모듈을 재사용합니다.
"""

from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.llm_client import MultiLlmClient, extract_json_from_llm_response
from services.multi_doc_extractor_service import (
    PDF_MIN_CHARS_PER_PAGE,
    _extract_pdf_text_layers,
    extract_raw_text_preview,
)

IMAGE_NOTICE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

VALID_REPLY_FIELD_TYPES = ("radio", "checkbox", "text", "textarea")

VALID_SIGNATURE_MODES = ("none", "student", "parent", "both")

NOTICE_CATEGORIES = ("가정통신문", "수요조사", "체험학습", "동의서", "기타")

NOTICE_OCR_JSON_PROMPT = (
    "첨부된 가정통신문 PDF(또는 이미지/본문 텍스트)를 정확히 판독하여 아래 JSON 스키마로만 응답하세요.\n"
    "코드블록(```json ... ```)을 포함해도 되며, 다른 설명 문장은 출력하지 마세요.\n"
    "{\n"
    '  "title": "가정통신문 제목",\n'
    '  "category": "가정통신문 | 수요조사 | 체험학습 | 동의서 중 택1",\n'
    '  "corrected_text": "안내 문구, 제출 기한, 기준, 유의사항, 문의처 등 원문 핵심 내용을 누락 없이 읽기 좋게 전체 정리한 본문",\n'
    '  "content": "corrected_text와 동일한 전체 정리 본문",\n'
    '  "due_date": "YYYY-MM-DD (회신 마감일이 없으면 빈 문자열 \\"\\")",\n'
    '  "requires_reply": true,\n'
    '  "signature_mode": "none | student | parent | both (기본 parent)",\n'
    '  "reply_schema": [\n'
    "    {\n"
    '      "key": "consent",\n'
    '      "label": "참가 동의 여부",\n'
    '      "type": "radio",\n'
    '      "options": ["동의", "미동의"],\n'
    '      "required": true\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "[필수 추출 규칙 — 반드시 준수]\n"
    "1. corrected_text(및 content) 작성 규칙: 문서를 2~3줄로 과도하게 요약하지 마세요. "
    "가정통신문의 안내 문구, 일시·장소, 대상, 제출 기한, 참여/선정 기준, 준비물·유의사항, 문의처 등 원문의 핵심 내용을 누락 없이 읽기 좋게 전체 정리하세요.\n"
    "2. reply_schema 누락·병합 금지 규칙: 문서에 포함된 모든 설문 문항, 표 안의 개별 질문(예: 18개 건강생활행동 설문 문항 각각의 문항), "
    "동의 여부(동의/비동의), 각종 기입란을 임의로 하나로 합치거나 생략하지 말고 문항 하나당 항목 하나씩 전부 개별 추출하세요.\n"
    "3. 선택지(type/options) 생성 규칙: type은 radio, checkbox, text, textarea 중 하나만 사용하세요. "
    "예/아니오, 동의/비동의, 동의/미동의, 찬성/반대, 복수 선택 체크박스 등 선택지가 있는 문항은 "
    "반드시 type을 \"radio\" 또는 \"checkbox\"로 지정하고, 선택지 목록을 \"options\" 배열(예: [\"예\", \"아니오\"], [\"동의\", \"비동의\"])로 정확히 생성하세요.\n"
    "4. signature_mode: 회신·서명이 필요 없으면 \"none\", 학생 서명만 필요하면 \"student\", 학부모(보호자) 서명만 필요하면 \"parent\", "
    "원문에 '학생 성명 (서명/인)'과 '학부모(보호자) 성명 (서명/인)'처럼 학생과 학부모 서명이 각각 별도로 요구되는 경우 \"both\"로 응답하세요.\n"
    "5. 회신/서명이 전혀 필요 없는 단순 안내문이면 requires_reply는 false, signature_mode는 \"none\", reply_schema는 []로 응답하세요."
)


def _normalize_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val or "").strip().lower()
    if not s:
        return default
    if s in ("1", "true", "yes", "y", "예", "필요", "on", "checked"):
        return True
    if s in ("0", "false", "no", "n", "아니오", "불필요", "off", "unchecked"):
        return False
    return default


def _infer_field_from_label(label: str, idx: int) -> Dict[str, Any]:
    clean = str(label or "").strip()
    lower = clean.lower()
    if "동의" in clean or "참가" in clean or "여부" in clean or "희망" in clean:
        opts = ["동의", "미동의"]
        m = re.search(r"\(([^)]+)\)", clean)
        if m:
            raw_opts = [o.strip() for o in re.split(r"[/,|]", m.group(1)) if o.strip()]
            if len(raw_opts) >= 2:
                opts = raw_opts
        return {
            "key": f"consent_{idx}" if idx > 1 else "consent",
            "label": clean,
            "type": "radio",
            "options": opts,
            "required": True,
        }
    if "사유" in clean or "비고" in clean or "의견" in clean or "건의" in clean or "memo" in lower:
        return {
            "key": f"field_{idx}",
            "label": clean,
            "type": "textarea",
            "options": [],
            "required": False,
        }
    return {
        "key": f"field_{idx}",
        "label": clean,
        "type": "text",
        "options": [],
        "required": True,
    }


def normalize_reply_schema(
    raw_schema: Any,
    fallback_fields: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """reply_schema 배열 또는 레거시 reply_fields 문자열 목록을 표준 입력칸 스키마로 정규화합니다."""
    if isinstance(raw_schema, str):
        s = raw_schema.strip()
        if s.startswith("["):
            try:
                raw_schema = json.loads(s)
            except Exception:
                raw_schema = []
        else:
            raw_schema = []

    items: List[Dict[str, Any]] = []
    used_keys: set[str] = set()

    if isinstance(raw_schema, list):
        for idx, entry in enumerate(raw_schema, start=1):
            if isinstance(entry, str):
                text = entry.strip()
                if not text:
                    continue
                item = _infer_field_from_label(text, idx)
            elif isinstance(entry, dict):
                label = str(entry.get("label") or entry.get("title") or entry.get("name") or "").strip()
                raw_key = str(entry.get("key") or entry.get("id") or "").strip()
                if not label and not raw_key:
                    continue
                if not label:
                    label = raw_key
                ftype = str(entry.get("type") or "text").strip().lower()
                if ftype not in VALID_REPLY_FIELD_TYPES:
                    ftype = "text"
                raw_opts = entry.get("options")
                if isinstance(raw_opts, list):
                    options = [str(o).strip() for o in raw_opts if str(o).strip()]
                elif isinstance(raw_opts, str):
                    options = [o.strip() for o in re.split(r"[,/|]", raw_opts) if o.strip()]
                else:
                    options = []
                if ftype in ("radio", "checkbox") and not options:
                    options = ["예", "아니오"]
                key = raw_key or f"field_{idx}"
                item = {
                    "key": key,
                    "label": label,
                    "type": ftype,
                    "options": options,
                    "required": _normalize_bool(entry.get("required"), default=True),
                }
            else:
                continue

            base_key = item["key"]
            candidate = base_key
            suffix = 2
            while candidate in used_keys:
                candidate = f"{base_key}_{suffix}"
                suffix += 1
            item["key"] = candidate
            used_keys.add(candidate)
            items.append(item)

    if not items and fallback_fields:
        for idx, lbl in enumerate(fallback_fields, start=1):
            clean = str(lbl or "").strip()
            if not clean:
                continue
            item = _infer_field_from_label(clean, idx)
            base_key = item["key"]
            candidate = base_key
            suffix = 2
            while candidate in used_keys:
                candidate = f"{base_key}_{suffix}"
                suffix += 1
            item["key"] = candidate
            used_keys.add(candidate)
            items.append(item)

    return items


def parse_notice_ai_json(raw_or_dict: Any) -> Dict[str, Any]:
    """AiExecutionDock(API/수동복붙/Playwright) 및 에이전트 가정통신문 OCR JSON 응답을 정규화합니다."""
    if isinstance(raw_or_dict, dict):
        data = dict(raw_or_dict)
    else:
        raw_text = str(raw_or_dict or "").strip()
        if not raw_text:
            raise ValueError("가정통신문 JSON 응답이 비어 있습니다.")
        try:
            data = extract_json_from_llm_response(raw_text)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", raw_text)
            if not m:
                raise ValueError("응답에서 JSON 객체를 찾을 수 없습니다.")
            data = json.loads(m.group(0))

    if not isinstance(data, dict):
        raise ValueError("가정통신문 JSON 응답은 객체(dict) 형태여야 합니다.")

    raw_fields = data.get("reply_fields")
    if isinstance(raw_fields, list):
        legacy_fields = [str(x).strip() for x in raw_fields if str(x).strip()]
    elif isinstance(raw_fields, str):
        legacy_fields = [x.strip() for x in raw_fields.split(",") if x.strip()]
    else:
        legacy_fields = []

    reply_schema = normalize_reply_schema(data.get("reply_schema"), fallback_fields=legacy_fields)
    if not legacy_fields and reply_schema:
        legacy_fields = [str(item.get("label") or item.get("key") or "").strip() for item in reply_schema]
        legacy_fields = [x for x in legacy_fields if x]

    due_date = str(data.get("due_date") or data.get("deadline") or "").strip()
    if due_date.lower() in ("none", "null", "n/a", "없음", "해당없음"):
        due_date = ""

    corrected_text = str(data.get("corrected_text") or "").strip()
    content = str(data.get("content") or data.get("body") or "").strip()
    if not content and corrected_text:
        content = corrected_text
    category = str(data.get("category") or "가정통신문").strip() or "가정통신문"

    if "requires_reply" in data:
        requires_reply = _normalize_bool(data.get("requires_reply"), default=bool(reply_schema))
    elif "requires_signature" in data:
        requires_reply = _normalize_bool(data.get("requires_signature"), default=bool(reply_schema))
    else:
        requires_reply = bool(reply_schema)

    raw_sig_mode = str(data.get("signature_mode") or "").strip().lower()
    if raw_sig_mode in ("none", "열람용", "없음", "서명없음", "서명 없음"):
        signature_mode = "none"
    elif raw_sig_mode in ("both", "dual", "double", "2", "학생·학부모", "학생학부모"):
        signature_mode = "both"
    elif raw_sig_mode in ("student", "학생", "학생만"):
        signature_mode = "student"
    elif raw_sig_mode in ("parent", "single", "학부모", "학부모만", "보호자"):
        signature_mode = "parent"
    else:
        signature_mode = "parent" if requires_reply else "none"

    return {
        "title": str(data.get("title") or "").strip(),
        "category": category,
        "corrected_text": corrected_text,
        "content": content,
        "body": content,
        "due_date": due_date,
        "deadline": due_date,
        "requires_reply": requires_reply,
        "signature_mode": signature_mode,
        "reply_schema": reply_schema,
        "reply_fields": legacy_fields,
    }


def extract_notice_source_text(file_path: str | Path, max_chars: int = 12000) -> str:
    """PDF/HWP/HWPX/텍스트 파일에서 기존 추출 모듈을 재사용해 본문·표 텍스트를 추출합니다."""
    p = Path(str(file_path or "").strip())
    if not p.exists() or not p.is_file():
        return ""
    ext = p.suffix.lower()
    text = ""
    try:
        text = extract_raw_text_preview(p, max_chars=max_chars) or ""
    except Exception:
        text = ""

    # .hwp 바이너리 olefile 추출이 불가할 경우 기존 한글 COM(.hwp -> .hwpx) 변환 후 재추출 시도
    if ext == ".hwp" and (not text or text.startswith("(HWP 바이너리 텍스트 미리보기 불가")):
        try:
            import tempfile
            from services.hwpx_agent_service import convert_hwp_to_hwpx

            with tempfile.TemporaryDirectory() as tmp_dir:
                converted_hwpx = convert_hwp_to_hwpx(p, Path(tmp_dir) / f"{p.stem}.hwpx")
                if converted_hwpx and Path(converted_hwpx).exists():
                    converted_txt = extract_raw_text_preview(converted_hwpx, max_chars=max_chars) or ""
                    if converted_txt and not converted_txt.startswith("("):
                        text = converted_txt
        except Exception:
            pass

    if text.startswith("(미리보기를 지원하지 않는 형식"):
        return ""
    return text.strip()


def _extract_local_notice_metadata(text: str, file_path: Path) -> Dict[str, str]:
    """로컬 추출 텍스트와 파일명에서 제목·마감일·분류를 1차 추정합니다."""
    clean_text = str(text or "").strip()
    title = ""
    for line in clean_text.splitlines()[:20]:
        s = line.strip()
        if not s:
            continue
        m_title = re.match(r"^(?:제목|건명)\s*[:：]\s*(.+)$", s)
        if m_title and m_title.group(1).strip():
            title = m_title.group(1).strip()[:120]
            break
    if not title:
        title = re.sub(r"[_\-]+", " ", file_path.stem).strip()

    due_date = ""
    due_keywords = ("마감", "제출", "기한", "까지", "회신")
    for line in clean_text.splitlines():
        if not any(kw in line for kw in due_keywords):
            continue
        m_full = re.search(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?", line)
        if m_full:
            y, mo, d = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                due_date = f"{y:04d}-{mo:02d}-{d:02d}"
                break
        m_short = re.search(r"(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?", line)
        if m_short:
            y = datetime.now().year
            mo, d = int(m_short.group(1)), int(m_short.group(2))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                due_date = f"{y:04d}-{mo:02d}-{d:02d}"
                break

    combined = f"{file_path.stem}\n{clean_text[:1500]}"
    if "체험학습" in combined or "현장체험" in combined:
        category = "체험학습"
    elif "수요조사" in combined or "신청서" in combined:
        category = "수요조사"
    elif "동의서" in combined:
        category = "동의서"
    else:
        category = "가정통신문"

    return {
        "title": title,
        "due_date": due_date,
        "category": category,
    }


def inspect_notice_file_locally(file_path: str | Path, max_chars: int = 12000) -> Dict[str, Any]:
    """파일 형식별 로컬 파싱 가능 여부를 판별하고, 가능하면 본문 텍스트 및 메타데이터를 반환합니다."""
    p = Path(str(file_path or "").strip())
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"가정통신문 파일을 찾을 수 없습니다: {p}")

    ext = p.suffix.lower()
    if ext in IMAGE_NOTICE_EXTS:
        return {
            "kind": "image",
            "needs_ai_ocr": True,
            "text": "",
            "title": "",
            "due_date": "",
            "category": "",
        }

    if ext == ".pdf":
        full_text = ""
        avg_chars = 0.0
        try:
            page_texts, avg_chars = _extract_pdf_text_layers(p)
            full_text = "\n\n".join(t for t in page_texts if t).strip()
        except Exception:
            full_text = ""
            avg_chars = 0.0

        if avg_chars < PDF_MIN_CHARS_PER_PAGE or not full_text:
            return {
                "kind": "scanned_pdf",
                "needs_ai_ocr": True,
                "text": "",
                "avg_chars": avg_chars,
                "title": "",
                "due_date": "",
                "category": "",
            }

        trimmed = full_text[:max_chars]
        meta = _extract_local_notice_metadata(trimmed, p)
        return {
            "kind": "text_pdf",
            "needs_ai_ocr": False,
            "text": trimmed,
            "avg_chars": avg_chars,
            **meta,
        }

    # .hwp, .hwpx 등 문서 파일: AI 호출 없이 로컬 텍스트 추출 수행
    raw_text = extract_notice_source_text(p, max_chars=max_chars)
    if raw_text.startswith("(") and (
        "미리보기 불가" in raw_text or "텍스트 없음" in raw_text or "읽기 실패" in raw_text
    ):
        raw_text = ""

    meta = _extract_local_notice_metadata(raw_text, p)
    return {
        "kind": ext.lstrip(".") or "document",
        "needs_ai_ocr": False,
        "text": raw_text,
        **meta,
    }


def extract_notice_from_text(
    content_text: str,
    instruction: str = "",
    llm_client: Optional[MultiLlmClient] = None,
    source_path: str = "",
) -> Dict[str, Any]:
    """사용자 확인·편집 본문 텍스트를 기반으로 가정통신문 요약 및 회신 입력칸(reply_schema) JSON을 추출합니다."""
    clean_text = str(content_text or "").strip()
    if not clean_text:
        raise ValueError("분석할 가정통신문 본문 텍스트가 비어 있습니다.")

    client = llm_client or MultiLlmClient()
    prompt_parts = [NOTICE_OCR_JSON_PROMPT]
    if instruction.strip():
        prompt_parts.append(f"\n[추가 요청사항]\n{instruction.strip()}")
    prompt_parts.append(f"\n[가정통신문 본문(사용자 확인·편집 텍스트)]\n{clean_text[:12000]}")

    raw_reply = client.generate(
        prompt="\n".join(prompt_parts),
        system_prompt=(
            "당신은 한국 초·중·고등학교 가정통신문 구조화 및 학부모 회신서 설계 전문 AI입니다. "
            "corrected_text는 2~3줄 요약이 아니라 안내 문구·제출 기한·기준·유의사항·문의처 등 원문 핵심 내용을 누락 없이 전체 정리하고, "
            "reply_schema는 문서·표 안의 모든 설문 문항과 동의 여부·기입란을 합치거나 생략하지 말고 문항당 하나씩 개별 추출하며, "
            "예/아니오·동의/비동의 등 선택지 문항은 반드시 radio/checkbox와 options 배열로 생성해 요청된 JSON 스키마만 정확하게 출력하세요."
        ),
        image_bytes=None,
    )
    parsed = parse_notice_ai_json(raw_reply)
    if source_path:
        parsed["source_path"] = source_path
    return parsed


def extract_notice_from_pdf(
    pdf_path: str | Path,
    instruction: str = "",
    llm_client: Optional[MultiLlmClient] = None,
    override_text: str = "",
) -> Dict[str, Any]:
    """PDF/HWP/HWPX/이미지에서 기존 텍스트 추출 및 MultiLlmClient OCR을 호출해 구조화 JSON을 추출합니다."""
    p = Path(str(pdf_path or "").strip())
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"가정통신문 파일을 찾을 수 없습니다: {p}")

    client = llm_client or MultiLlmClient()
    edited_text = str(override_text or "").strip()
    if edited_text:
        return extract_notice_from_text(
            edited_text,
            instruction=instruction,
            llm_client=client,
            source_path=str(p),
        )

    ext = p.suffix.lower()

    local_text = extract_notice_source_text(p, max_chars=12000)

    image_bytes: Optional[bytes] = None
    mime_type = "application/pdf"
    if ext in IMAGE_NOTICE_EXTS:
        image_bytes = p.read_bytes()
        guessed, _ = mimetypes.guess_type(str(p))
        mime_type = guessed or "image/png"
    elif ext == ".pdf" and client.provider == "gemini":
        image_bytes = p.read_bytes()
        mime_type = "application/pdf"

    prompt_parts = [NOTICE_OCR_JSON_PROMPT]
    if instruction.strip():
        prompt_parts.append(f"\n[추가 요청사항]\n{instruction.strip()}")
    if local_text.strip():
        prompt_parts.append(f"\n[추출된 문서 텍스트 참고]\n{local_text.strip()}")
    else:
        prompt_parts.append(f"\n[첨부 파일명]\n{p.name}")

    raw_reply = client.generate(
        prompt="\n".join(prompt_parts),
        system_prompt=(
            "당신은 한국 초·중·고등학교 가정통신문 OCR 및 학부모 회신서 구조화 전문 AI입니다. "
            "corrected_text는 2~3줄 요약이 아니라 안내 문구·제출 기한·기준·유의사항·문의처 등 원문 핵심 내용을 누락 없이 전체 정리하고, "
            "reply_schema는 문서·표 안의 모든 설문 문항과 동의 여부·기입란을 합치거나 생략하지 말고 문항당 하나씩 개별 추출하며, "
            "예/아니오·동의/비동의 등 선택지 문항은 반드시 radio/checkbox와 options 배열로 생성해 요청된 JSON 스키마만 정확하게 출력하세요."
        ),
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    parsed = parse_notice_ai_json(raw_reply)
    parsed["source_path"] = str(p)
    return parsed


def convert_hwp_to_pdf(
    hwp_path: str | Path,
    output_dir: Optional[str | Path] = None,
) -> Optional[Path]:
    """
    Windows 한컴오피스 COM(HWPFrame.HwpObject)을 이용해 .hwp / .hwpx 파일을 임시 .pdf 파일로 변환합니다.
    한글 미설치 또는 COM 오류 시 예외를 발생시키지 않고 None을 반환합니다.
    """
    import tempfile

    from services.multi_doc_extractor_service import (
        HAS_WIN32COM,
        _hwp_security_popup_watcher,
        pythoncom,
        win32com,
    )

    p = Path(str(hwp_path or "").strip())
    if not p.is_file() or p.suffix.lower() not in (".hwp", ".hwpx"):
        return None
    if not HAS_WIN32COM or win32com is None or pythoncom is None:
        return None

    out_dir = (
        Path(output_dir)
        if output_dir is not None
        else Path(tempfile.mkdtemp(prefix="eduflow_notice_pdf_"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / f"{p.stem}.pdf"

    hwp = None
    com_initialized = False
    try:
        pythoncom.CoInitialize()
        com_initialized = True
        hwp = win32com.client.Dispatch("HWPFrame.HwpObject")
        try:
            hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
        except Exception:
            pass
        try:
            hwp.SetMessageBoxMode(0x00010000)
        except Exception:
            pass

        fmt = "HWPX" if p.suffix.lower() == ".hwpx" else "HWP"
        with _hwp_security_popup_watcher():
            opened = hwp.Open(str(p.resolve()), fmt, "forceopen:true")
            if not opened:
                opened = hwp.Open(str(p.resolve()), "", "forceopen:true")
        if not opened:
            return None

        if hwp.SaveAs(str(out_pdf.resolve()), "PDF") and out_pdf.is_file() and out_pdf.stat().st_size > 0:
            return out_pdf
        return None
    except Exception:
        return None
    finally:
        if hwp is not None:
            try:
                hwp.Quit()
            except Exception:
                pass
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
