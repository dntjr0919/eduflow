"""
가정통신문 PDF/이미지 AI OCR 구조화 JSON 추출 및 정규화 서비스.
- 기존 MultiLlmClient 및 텍스트 추출 모듈을 재사용합니다.
"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.llm_client import MultiLlmClient, extract_json_from_llm_response
from services.multi_doc_extractor_service import extract_raw_text_preview

VALID_REPLY_FIELD_TYPES = ("radio", "checkbox", "text", "textarea")

NOTICE_CATEGORIES = ("가정통신문", "수요조사", "체험학습", "동의서", "기타")

NOTICE_OCR_JSON_PROMPT = (
    "첨부된 가정통신문 PDF(또는 이미지)를 정확히 판독하여 아래 JSON 스키마로만 응답하세요.\n"
    "코드블록(```json ... ```)을 포함해도 되며, 다른 설명 문장은 출력하지 마세요.\n"
    "{\n"
    '  "title": "가정통신문 제목",\n'
    '  "category": "가정통신문 | 수요조사 | 체험학습 | 동의서 중 택1",\n'
    '  "content": "학생·학부모용 핵심 요약 본문",\n'
    '  "due_date": "YYYY-MM-DD (회신 마감일이 없으면 빈 문자열 \\"\\")",\n'
    '  "requires_reply": true,\n'
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
    "- type은 radio, checkbox, text, textarea 중 하나만 사용하세요.\n"
    "- 회신/서명이 필요 없는 단순 안내문이면 requires_reply는 false, reply_schema는 []로 응답하세요."
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

    content = str(data.get("content") or data.get("body") or "").strip()
    category = str(data.get("category") or "가정통신문").strip() or "가정통신문"

    if "requires_reply" in data:
        requires_reply = _normalize_bool(data.get("requires_reply"), default=bool(reply_schema))
    elif "requires_signature" in data:
        requires_reply = _normalize_bool(data.get("requires_signature"), default=bool(reply_schema))
    else:
        requires_reply = bool(reply_schema)

    return {
        "title": str(data.get("title") or "").strip(),
        "category": category,
        "content": content,
        "body": content,
        "due_date": due_date,
        "deadline": due_date,
        "requires_reply": requires_reply,
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


def extract_notice_from_pdf(
    pdf_path: str | Path,
    instruction: str = "",
    llm_client: Optional[MultiLlmClient] = None,
) -> Dict[str, Any]:
    """PDF/HWP/HWPX/이미지에서 기존 텍스트 추출 및 MultiLlmClient OCR을 호출해 구조화 JSON을 추출합니다."""
    p = Path(str(pdf_path or "").strip())
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"가정통신문 파일을 찾을 수 없습니다: {p}")

    client = llm_client or MultiLlmClient()
    ext = p.suffix.lower()

    local_text = extract_notice_source_text(p, max_chars=12000)

    image_bytes: Optional[bytes] = None
    mime_type = "application/pdf"
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
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
            "요청된 JSON 스키마만 정확하게 출력하세요."
        ),
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    parsed = parse_notice_ai_json(raw_reply)
    parsed["source_path"] = str(p)
    return parsed
