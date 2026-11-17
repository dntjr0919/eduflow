"""
카톡 대화(.txt / 붙여넣기) 및 학교종이 긴 공지 AI 요약·추출 서비스.
- PC/모바일 카카오톡 대화 내보내기(.txt) 또는 메신저 텍스트에서 시스템 메시지·단순 감탄사를 제거하고 최근 대화를 정제합니다.
- AI에게 전달할 프롬프트를 생성하고, JSON 배열(title, summary, content, source_label, event_date)을 파싱합니다.
- 학교종이 긴 공지 원문을 3줄 요약하는 프롬프트/파서도 함께 제공합니다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.llm_client import MultiLlmClient, extract_json_from_llm_response

# PC 카카오톡 Windows 내보내기: [홍길동] [오후 3:15] 메시지
_WIN_KAKAO_LINE_RE = re.compile(
    r"^\[(?P<sender>[^\]]+)\]\s*\[(?P<time>(?:오전|오후|AM|PM)?\s*\d{1,2}:\d{2})\]\s*(?P<body>.*)$"
)
# 모바일/macOS 카카오톡 내보내기: 2026. 11. 17. 오후 3:15, 홍길동 : 메시지
_MOBILE_KAKAO_LINE_RE = re.compile(
    r"^(?P<date>\d{4}[.\-년\s]+\d{1,2}[.\-월\s]+\d{1,2}[일.]?\s*(?:오전|오후|AM|PM)?\s*\d{1,2}:\d{2})\s*,\s*(?P<sender>[^:]+?)\s*:\s*(?P<body>.*)$"
)
# 날짜 구분선: --------------- 2026년 11월 17일 화요일 ---------------
_DATE_HEADER_RE = re.compile(
    r"^-*\s*(?P<date>\d{4}년\s*\d{1,2}월\s*\d{1,2}일(?:\s*[월화수목금토일]요일)?)\s*-*$"
)
_EXPORT_META_RE = re.compile(
    r"^(?:저장한 날짜|.*님과 카카오톡 대화|Talk_.*\.txt)\s*[:：]?"
)

_SYSTEM_MSG_PATTERNS = (
    re.compile(r".+님이 들어왔습니다\.?$"),
    re.compile(r".+님이 나갔습니다\.?$"),
    re.compile(r".+님을 내보냈습니다\.?$"),
    re.compile(r".+님이 .+님을 초대했습니다\.?$"),
    re.compile(r"^삭제된 메시지입니다\.?$"),
    re.compile(r"^채팅방 관리자가 메시지를 가렸습니다\.?$"),
    re.compile(r"^(?:사진|사진\s*\d+장|동영상|이모티콘|보이스톡|페이스톡|선물|송금)$"),
    re.compile(r"^(?:파일|샵검색|지도|연락처)\s*[:：].*$"),
)

_FILLER_MSG_RE = re.compile(
    r"^(?:"
    r"네+|넵+|예+|응+|웅+|어+|아+|오+|음+|흠+|"
    r"감사합니다+|감사해요+|감사요+|고맙습니다+|고마워요+|"
    r"확인했습니다+|확인했어요+|확인요+|확인+|"
    r"알겠습니다+|알겠어요+|알겠습니당+|"
    r"좋아요+|굿+|오키+|오케이+|ㅇㅋ|ㅇㅇ|ㄱㅅ|ㄴㄴ|ㅂㅂ|"
    r"[ㅋㅎㅠㅜㅡ]+|"
    r"[\s~!?.^👍👌👏🙏❤️💕😊😄😁🤗]+"
    r")[\s~!?.^]*$",
    re.IGNORECASE,
)


def read_kakao_export_file(file_path: str | Path) -> str:
    """카카오톡 .txt 내보내기 파일을 인코딩 자동 감지(UTF-8/CP949)로 읽습니다."""
    p = Path(file_path)
    raw_bytes = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _is_system_or_filler(body: str) -> bool:
    s = str(body or "").strip()
    if not s:
        return True
    for pat in _SYSTEM_MSG_PATTERNS:
        if pat.match(s):
            return True
    if len(s) <= 20 and _FILLER_MSG_RE.match(s):
        return True
    return False


def parse_kakao_chat_text(
    raw_text: str,
    max_messages: int = 120,
    max_chars: int = 12000,
) -> str:
    """
    카카오톡 내보내기(.txt) 또는 메신저 붙여넣기 텍스트에서
    불필요한 시스템 메시지·단순 감탄사를 제거하고 최근 대화만 추출합니다.
    """
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    lines = text.split("\n")
    messages: List[Dict[str, str]] = []
    current_date = ""
    has_kakao_structure = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if _EXPORT_META_RE.match(line):
            continue

        m_date = _DATE_HEADER_RE.match(line)
        if m_date:
            has_kakao_structure = True
            current_date = m_date.group("date").strip()
            continue

        m_win = _WIN_KAKAO_LINE_RE.match(line)
        if m_win:
            has_kakao_structure = True
            messages.append(
                {
                    "date": current_date,
                    "time": m_win.group("time").strip(),
                    "sender": m_win.group("sender").strip(),
                    "body": m_win.group("body").strip(),
                }
            )
            continue

        m_mob = _MOBILE_KAKAO_LINE_RE.match(line)
        if m_mob:
            has_kakao_structure = True
            messages.append(
                {
                    "date": m_mob.group("date").strip(),
                    "time": "",
                    "sender": m_mob.group("sender").strip(),
                    "body": m_mob.group("body").strip(),
                }
            )
            continue

        if has_kakao_structure and messages:
            messages[-1]["body"] = (messages[-1]["body"] + "\n" + line).strip()
        else:
            messages.append({"date": current_date, "time": "", "sender": "", "body": line})

    cleaned: List[str] = []
    last_date = ""
    for msg in messages:
        body = msg["body"].strip()
        if _is_system_or_filler(body):
            continue
        d = msg.get("date", "").strip()
        sender = msg.get("sender", "").strip()
        t = msg.get("time", "").strip()
        if d and d != last_date and len(d) <= 24:
            cleaned.append(f"[{d}]")
            last_date = d
        if sender:
            prefix = f"[{sender}" + (f" {t}]" if t else "]")
            cleaned.append(f"{prefix} {body}")
        else:
            cleaned.append(body)

    if not cleaned:
        return ""

    recent = cleaned[-max(1, int(max_messages)) :]
    joined = "\n".join(recent)
    if len(joined) > max_chars:
        joined = joined[-max_chars:]
        first_nl = joined.find("\n")
        if 0 <= first_nl < 200:
            joined = joined[first_nl + 1 :]
    return joined.strip()


def build_kakao_notice_digest_prompt(
    cleaned_text: str,
    source_label: str = "카카오톡 대화",
) -> Tuple[str, str]:
    """카톡/메신저 대화에서 학급 공지사항을 추출·요약하는 (system_prompt, user_prompt)를 생성합니다."""
    system_prompt = (
        "당신은 초·중·고등학교 담임교사의 학급 공지 정리 비서입니다.\n"
        "대화 내용이나 안내 텍스트에서 학생·학부모에게 공유해야 할 실질적인 공지사항만 추출해 "
        "반드시 JSON 배열([ ... ]) 형식으로만 출력하세요. 마크다운 설명이나 여분 텍스트는 넣지 마세요.\n\n"
        "각 배열 원소의 스키마:\n"
        "{\n"
        '  "title": "핵심 공지 제목 (간결하고 명확하게)",\n'
        '  "summary": "학생·학부모가 한눈에 볼 수 있는 2~3줄 핵심 요약",\n'
        '  "content": "일시·장소·준비물·유의사항 등 빠짐없는 상세 본문",\n'
        f'  "source_label": "{source_label}",\n'
        '  "event_date": "관련 날짜(YYYY-MM-DD, 없으면 빈 문자열 \\"\\")"\n'
        "}\n\n"
        "규칙:\n"
        "1. 단순 잡담·인사·감탄사는 제외하고 공지성 정보(행사, 준비물, 일정 변경, 제출 안내, 안전 안내 등)만 추려내세요.\n"
        "2. 공지할 내용이 전혀 없으면 빈 배열 []을 반환하세요."
    )
    user_prompt = (
        f"[출처: {source_label}]\n"
        "아래 대화/텍스트에서 학급 공지사항을 추출하여 JSON 배열로 정리해 주세요:\n\n"
        f"{str(cleaned_text or '').strip()}"
    )
    return system_prompt, user_prompt


def build_schoolbell_summary_prompt(
    title: str,
    content: str,
    source_label: str = "학교종이",
    published_at: str = "",
) -> Tuple[str, str]:
    """긴 학교종이 공지 원문을 3줄 요약하기 위한 (system_prompt, user_prompt)를 생성합니다."""
    system_prompt = (
        "당신은 초·중·고등학교 담임교사의 공지 요약 비서입니다.\n"
        "긴 학교 공지/가정통신문 원문을 학생·학부모가 모바일에서 바로 읽기 좋게 핵심 3줄로 요약하고 "
        "반드시 JSON 배열([ { ... } ]) 형식으로만 응답하세요.\n\n"
        "스키마:\n"
        "[\n"
        "  {\n"
        '    "title": "공지 제목",\n'
        '    "summary": "1) 핵심 내용\\n2) 일시·대상\\n3) 준비물·제출/유의사항 (최대 3줄)",\n'
        '    "content": "원문 핵심 내용을 정돈한 본문",\n'
        f'    "source_label": "{source_label}",\n'
        '    "event_date": "관련 일자(YYYY-MM-DD, 없으면 \\"\\")"\n'
        "  }\n"
        "]"
    )
    user_prompt = (
        f"[제목] {str(title or '').strip()}\n"
        f"[작성일] {str(published_at or '').strip()}\n"
        f"[출처] {source_label}\n\n"
        f"[원문 본문]\n{str(content or '').strip()[:10000]}"
    )
    return system_prompt, user_prompt


def _extract_json_array_or_obj(raw_response: Any) -> Any:
    if isinstance(raw_response, (list, dict)):
        return raw_response
    text = str(raw_response or "").strip()
    if not text:
        raise ValueError("AI 응답이 비어 있습니다.")

    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    arr_start = cleaned.find("[")
    arr_end = cleaned.rfind("]")
    if arr_start != -1 and arr_end > arr_start:
        try:
            return json.loads(cleaned[arr_start : arr_end + 1])
        except Exception:
            pass

    return extract_json_from_llm_response(cleaned)


def parse_notice_digest_ai_response(
    raw_response: Any,
    default_source_label: str = "카카오톡 대화",
) -> List[Dict[str, Any]]:
    """AI 응답에서 공지 요약 JSON 배열(title, summary, content, source_label, event_date)을 파싱·정규화합니다."""
    parsed = _extract_json_array_or_obj(raw_response)

    if isinstance(parsed, dict):
        for key in ("notices", "items", "results", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = [parsed]

    if not isinstance(parsed, list):
        raise ValueError("AI 응답에서 공지 JSON 배열을 찾을 수 없습니다.")

    out: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        content = str(item.get("content") or summary or "").strip()
        source_label = str(item.get("source_label") or default_source_label).strip() or default_source_label
        event_date = str(item.get("event_date") or "").strip()

        if not title and not summary and not content:
            continue
        if not title:
            title = (summary or content).splitlines()[0][:50].strip()
        if not summary:
            summary = content[:180].replace("\n", " ").strip()
            if len(content) > 180:
                summary += "..."

        out.append(
            {
                "title": title,
                "summary": summary,
                "content": content or summary,
                "source_label": source_label,
                "event_date": event_date,
            }
        )
    return out


def digest_kakao_text_with_ai(
    raw_text: str,
    source_label: str = "카카오톡 대화",
    llm_client: Optional[MultiLlmClient] = None,
) -> List[Dict[str, Any]]:
    """카톡/메신저 텍스트를 정제한 뒤 LLM API로 공지 JSON 배열을 추출합니다."""
    cleaned = parse_kakao_chat_text(raw_text)
    if not cleaned:
        raise ValueError("추출할 대화 내용이 없습니다.")
    sys_p, usr_p = build_kakao_notice_digest_prompt(cleaned, source_label=source_label)
    client = llm_client or MultiLlmClient()
    raw_resp = client.generate(prompt=usr_p, system_prompt=sys_p)
    return parse_notice_digest_ai_response(raw_resp, default_source_label=source_label)


def summarize_schoolbell_notice_with_ai(
    notice: Dict[str, Any],
    llm_client: Optional[MultiLlmClient] = None,
) -> Dict[str, Any]:
    """학교종이 공지 1건을 LLM API로 3줄 요약하여 병합된 딕셔너리를 반환합니다."""
    title = str(notice.get("title") or "").strip()
    content = str(notice.get("content") or "").strip()
    source_label = str(notice.get("source_label") or "학교종이").strip()
    published_at = str(notice.get("published_at") or "").strip()
    sys_p, usr_p = build_schoolbell_summary_prompt(
        title=title,
        content=content,
        source_label=source_label,
        published_at=published_at,
    )
    client = llm_client or MultiLlmClient()
    raw_resp = client.generate(prompt=usr_p, system_prompt=sys_p)
    items = parse_notice_digest_ai_response(raw_resp, default_source_label=source_label)
    if not items:
        return dict(notice)
    first = items[0]
    merged = dict(notice)
    if first.get("title"):
        merged["title"] = first["title"]
    if first.get("summary"):
        merged["summary"] = first["summary"]
    if first.get("content"):
        merged["content"] = first["content"]
    if first.get("event_date"):
        merged["event_date"] = first["event_date"]
    return merged
