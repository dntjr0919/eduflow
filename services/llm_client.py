"""
멀티 LLM 공급자 통합 클라이언트 및 구조화 프롬프트/JSON 파싱 엔진 (services/llm_client.py).

지원 공급자:
1. Google Gemini (gemini): google-genai / google-generativeai SDK 또는 REST API 자동 폴백
2. OpenAI (openai): openai SDK 또는 REST API 자동 폴백 (GPT-4o, o3-mini 등)
3. Anthropic (claude): anthropic SDK 또는 REST API 자동 폴백 (Claude 3.7 / 3.5 Sonnet 등)
4. OpenRouter / Ollama / 기타 로컬 오픈소스 LLM (custom): OpenAI 호환 엔드포인트(base_url, api_key, model_name)

모든 공급자는 단일 표준 메서드:
    generate(prompt: str, system_prompt: str = None, image_bytes: bytes = None) -> str
형태로 호출할 수 있습니다.
"""

import abc
import base64
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from core.config_manager import ConfigManager


# =============================================================================
# 1. 안전한 외부 LLM 응답 JSON 추출 및 프롬프트 빌더 유틸리티
# =============================================================================
DEFAULT_JSON_SYSTEM_INSTRUCTION = (
    "당신은 대한민국 초·중·고등학교 교무·행정 문서 처리 전문 AI 어시스턴트입니다.\n"
    "반드시 아래 규칙을 엄격히 준수하여 응답하세요:\n"
    "1. 인사말, 부연 설명, 사족을 절대 작성하지 마세요.\n"
    "2. 오직 유효한 JSON 데이터만 ```json ... ``` 코드 블록 안에 담아 출력하세요.\n"
    "3. JSON 키(Key) 이름은 요청된 스키마와 정확히 일치해야 합니다."
)

PROMPT_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "ocr_correction": {
        "title": "OCR 텍스트 오탈자 교정 및 공문서 서식 정리",
        "schema": {
            "document_title": "추출된 문서 제목",
            "corrected_text": "오탈자와 띄어쓰기가 완벽히 교정되고 마크다운 서식이 적용된 본문 전체",
            "summary_3lines": [
                "핵심 요약 1줄",
                "핵심 요약 2줄",
                "핵심 요약 3줄",
            ],
            "action_items": [
                "교사/담당자가 처리해야 할 후속 조치 또는 기한 (없으면 빈 배열)"
            ],
        },
        "instruction": (
            "다음 스캔/OCR 추출 원문의 오탈자, 깨진 글자, 줄바꿈 오류를 문맥에 맞게 교정하고 "
            "교무 행정 문서 스타일로 깔끔하게 정리한 뒤 지정된 JSON 포맷으로만 출력하세요."
        ),
    },
    "report_summary": {
        "title": "공문 / 학교 보고서 핵심 요약 및 일정 추출",
        "schema": {
            "document_title": "문서/공문 제목",
            "sender_or_dept": "발신처 또는 담당 부서",
            "due_date": "제출 기한 또는 행사 일시 (YYYY-MM-DD, 없으면 미기재)",
            "summary_3lines": [
                "핵심 내용 요약 1",
                "핵심 내용 요약 2",
                "핵심 내용 요약 3",
            ],
            "corrected_text": "정리된 보고서 전문 텍스트",
        },
        "instruction": (
            "다음 업무 문서에서 핵심 내용, 기한, 담당 부서를 추출하여 지정된 JSON 포맷으로만 출력하세요."
        ),
    },
    "sorting_rule_suggest": {
        "title": "파일 목록 기반 자동 분류 규칙 추천",
        "schema": {
            "suggested_rules": [
                {
                    "rule_name": "규칙 이름 (예: 교외체험학습 서류)",
                    "match_type": "keyword",
                    "keywords": "체험학습,결석계,보고서",
                    "extensions": ".pdf,.hwp",
                    "category_folder": "03_출결_체험학습",
                }
            ],
            "corrected_text": "규칙 제안 사유 요약",
        },
        "instruction": (
            "다음 파일명 목록을 분석하여 교무 업무 폴더 자동 분류에 적합한 규칙들을 지정된 JSON 포맷으로만 출력하세요."
        ),
    },
}


def build_structured_prompt(
    task_key: str,
    raw_content: str,
    custom_instruction: str = "",
) -> Tuple[str, str]:
    """
    작업 유형(task_key)과 원문 데이터를 받아
    (system_prompt, user_prompt_with_json_schema) 튜플을 반환합니다.
    수동 프롬프트 모드에서는 두 텍스트를 합쳐 클립보드 복사용 전체 프롬프트로 사용합니다.
    """
    tpl = PROMPT_TEMPLATES.get(task_key, PROMPT_TEMPLATES["ocr_correction"])
    schema_str = json.dumps(tpl["schema"], ensure_ascii=False, indent=2)
    base_inst = custom_instruction.strip() or tpl["instruction"]

    user_prompt = (
        f"### [작업 지시사항]\n"
        f"{base_inst}\n\n"
        f"### [출력 규칙 - 매우 중요]\n"
        f"- 어떠한 인사말이나 설명도 쓰지 말고 오직 아래 형식의 ```json 코드 블록만 출력하세요.\n\n"
        f"```json\n{schema_str}\n```\n\n"
        f"### [분석 및 처리할 원문 데이터]\n"
        f"\"\"\"\n{raw_content.strip() or '(여기에 분석할 문서 텍스트나 내용을 입력하세요)'}\n\"\"\""
    )
    return DEFAULT_JSON_SYSTEM_INSTRUCTION, user_prompt


def extract_json_from_llm_response(response_text: str) -> Dict[str, Any]:
    """
    외부 LLM(ChatGPT, Claude 웹 등)이나 API가 서두/결론에 인사말을 덧붙였더라도
    정규표현식을 이용해 순수 JSON 데이터만 안전하게 추출하여 파싱합니다.
    """
    if not response_text or not response_text.strip():
        raise ValueError("응답 텍스트가 비어 있습니다.")

    cleaned = response_text.strip()

    # 1순위: ```json ... ``` 또는 ``` ... ``` 코드 블록 내부 추출
    code_block_match = re.search(
        r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE
    )
    candidate_str = code_block_match.group(1).strip() if code_block_match else cleaned

    # 2순위: 코드 블록이 없다면 가장 바깥쪽 { ... } 또는 [ ... ] 영역 추출
    if not code_block_match:
        brace_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", candidate_str)
        if brace_match:
            candidate_str = brace_match.group(1).strip()

    # JSON 파싱 시도 (후행 쉼표 등 경미한 문법 오류 자동 보정 포함)
    try:
        parsed = json.loads(candidate_str)
    except json.JSONDecodeError:
        fixed_str = re.sub(r",\s*([\}\]])", r"\1", candidate_str)
        try:
            parsed = json.loads(fixed_str)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"LLM 응답에서 유효한 JSON을 추출하지 못했습니다: {exc}\n\n[추출 시도 원문]:\n{candidate_str[:400]}"
            ) from exc

    if isinstance(parsed, list):
        return {"items": parsed, "corrected_text": json.dumps(parsed, ensure_ascii=False, indent=2)}
    if not isinstance(parsed, dict):
        return {"value": parsed, "corrected_text": str(parsed)}
    return parsed


def format_parsed_json_to_readable_text(data: Dict[str, Any]) -> str:
    """파싱된 JSON 딕셔너리를 교사가 바로 읽고 저장할 수 있는 깔끔한 마크다운 문서로 변환합니다."""
    lines: List[str] = []

    title = data.get("document_title")
    if title:
        lines.append(f"# 📄 {title}\n")

    sender = data.get("sender_or_dept")
    due = data.get("due_date")
    if sender or due:
        meta_parts = []
        if sender:
            meta_parts.append(f"**담당/발신**: {sender}")
        if due:
            meta_parts.append(f"**기한/일시**: {due}")
        lines.append(" | ".join(meta_parts) + "\n")

    summary_list = data.get("summary_3lines")
    if isinstance(summary_list, list) and summary_list:
        lines.append("## 📌 핵심 3줄 요약")
        for s in summary_list:
            lines.append(f"- {s}")
        lines.append("")

    actions = data.get("action_items")
    if isinstance(actions, list) and actions:
        lines.append("## ✅ 후속 조치 사항")
        for a in actions:
            lines.append(f"- [ ] {a}")
        lines.append("")

    corrected = data.get("corrected_text")
    if corrected:
        lines.append("## 📝 보정 및 정리된 본문")
        lines.append(str(corrected).strip())
        lines.append("")

    if not lines:
        return json.dumps(data, ensure_ascii=False, indent=2)

    return "\n".join(lines).strip()


# =============================================================================
# 2. 멀티 LLM 공급자 추상 인터페이스 및 구현체
# =============================================================================
_THINKING_BUDGET_MAP: Dict[str, Optional[int]] = {
    "off": 0,
    "low": 512,
    "auto": None,
}


def _resolve_generation_options(
    max_output_tokens: Optional[int] = None,
    thinking_budget_mode: Optional[str] = None,
) -> Tuple[int, str]:
    """ConfigManager 또는 명시적 인자에서 (max_output_tokens, thinking_budget_mode)를 결정합니다."""
    sec: Dict[str, Any] = {}
    if max_output_tokens is None or thinking_budget_mode is None:
        sec = ConfigManager().get_section("api_and_integrations")

    raw_tokens = max_output_tokens if max_output_tokens is not None else sec.get("max_output_tokens", 2048)
    try:
        resolved_tokens = max(1, int(raw_tokens))
    except (TypeError, ValueError):
        resolved_tokens = 2048

    raw_mode = thinking_budget_mode if thinking_budget_mode is not None else sec.get("thinking_budget_mode", "low")
    resolved_mode = str(raw_mode or "low").strip().lower()
    if resolved_mode not in _THINKING_BUDGET_MAP:
        resolved_mode = "low"

    return resolved_tokens, resolved_mode


def _extract_token_usage(usage_obj: Any) -> Optional[Dict[str, int]]:
    """SDK 객체 또는 REST 응답의 usage 메타데이터에서 입력/출력 토큰 수를 추출합니다."""
    if usage_obj is None:
        return None

    def _pick(keys: Tuple[str, ...]) -> Optional[int]:
        for key in keys:
            val = usage_obj.get(key) if isinstance(usage_obj, dict) else getattr(usage_obj, key, None)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
        return None

    input_tokens = _pick(
        ("prompt_token_count", "promptTokenCount", "prompt_tokens", "input_tokens", "inputTokens", "input")
    )
    output_tokens = _pick(
        ("candidates_token_count", "candidatesTokenCount", "completion_tokens", "output_tokens", "outputTokens", "output")
    )
    if input_tokens is None and output_tokens is None:
        return None
    return {
        "input": input_tokens if input_tokens is not None else 0,
        "output": output_tokens if output_tokens is not None else 0,
    }


class BaseLlmProvider(abc.ABC):
    """모든 LLM 공급자가 구현해야 하는 추상 기본 클래스."""

    last_responded_model: str = ""
    last_token_usage: Optional[Dict[str, int]] = None

    @abc.abstractmethod
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
    ) -> str:
        """프롬프트(및 선택적 이미지 바이트)를 전달하여 LLM 응답 문자열을 반환합니다."""


class GeminiProvider(BaseLlmProvider):
    """
    Google Gemini 공급자.
    1순위: google-genai SDK
    2순위: google-generativeai SDK
    3순위: 표준 REST API (설치된 SDK가 없어도 즉시 동작)
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = "gemini-1.5-flash",
        max_output_tokens: Optional[int] = None,
        thinking_budget_mode: Optional[str] = None,
    ):
        self.api_key = api_key.strip()
        self.model_name = model_name.strip() or "gemini-1.5-flash"
        self.max_output_tokens = max_output_tokens
        self.thinking_budget_mode = thinking_budget_mode
        self.last_responded_model = ""
        self.last_token_usage: Optional[Dict[str, int]] = None

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
    ) -> str:
        if not self.api_key:
            raise ValueError("Google Gemini API Key가 설정되지 않았습니다.")

        self.last_token_usage = None
        max_tokens, thinking_mode = _resolve_generation_options(
            self.max_output_tokens, self.thinking_budget_mode
        )
        thinking_budget = _THINKING_BUDGET_MAP.get(thinking_mode)

        # 1) 최신 google-genai SDK 시도
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore

            client = genai.Client(api_key=self.api_key)
            contents: List[Any] = [prompt]
            if image_bytes is not None:
                contents.append(
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
                )

            def _build_genai_config(include_thinking: bool) -> Any:
                cfg_kwargs: Dict[str, Any] = {
                    "system_instruction": system_prompt if system_prompt else None,
                    "max_output_tokens": max_tokens,
                }
                if include_thinking and thinking_budget is not None:
                    if hasattr(types, "ThinkingConfig"):
                        cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                            thinking_budget=thinking_budget
                        )
                    else:
                        cfg_kwargs["thinking_config"] = {"thinking_budget": thinking_budget}
                return types.GenerateContentConfig(**cfg_kwargs)

            if thinking_budget is not None:
                try:
                    resp = client.models.generate_content(
                        model=self.model_name,
                        contents=contents,
                        config=_build_genai_config(include_thinking=True),
                    )
                except Exception:
                    resp = client.models.generate_content(
                        model=self.model_name,
                        contents=contents,
                        config=_build_genai_config(include_thinking=False),
                    )
            else:
                resp = client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=_build_genai_config(include_thinking=False),
                )

            self.last_responded_model = str(getattr(resp, "model_version", None) or self.model_name)
            self.last_token_usage = _extract_token_usage(getattr(resp, "usage_metadata", None))
            return resp.text or ""
        except ImportError:
            pass
        except Exception as e:
            friendly_msg = _diagnose_llm_error(e, provider="Google Gemini")
            raise RuntimeError(friendly_msg) from e

        # 2) 레거시 google-generativeai SDK 시도
        try:
            import google.generativeai as genai_legacy  # type: ignore

            genai_legacy.configure(api_key=self.api_key)
            model = genai_legacy.GenerativeModel(
                model_name=self.model_name,
                system_instruction=system_prompt if system_prompt else None,
            )
            parts: List[Any] = [prompt]
            if image_bytes is not None:
                parts.append({"mime_type": mime_type, "data": image_bytes})
            resp = model.generate_content(
                parts,
                generation_config={"max_output_tokens": max_tokens},
            )
            self.last_responded_model = str(getattr(resp, "model_version", None) or self.model_name)
            self.last_token_usage = _extract_token_usage(getattr(resp, "usage_metadata", None))
            return resp.text or ""
        except ImportError:
            pass
        except Exception as e:
            friendly_msg = _diagnose_llm_error(e, provider="Google Gemini")
            raise RuntimeError(friendly_msg) from e

        # 3) REST API 직접 호출 (SDK 미설치 환경 완벽 호환)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent?key={self.api_key}"
        )
        user_parts: List[Dict[str, Any]] = [{"text": prompt}]
        if image_bytes is not None:
            user_parts.append(
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            )

        def _build_rest_payload(include_thinking: bool) -> Dict[str, Any]:
            gen_cfg: Dict[str, Any] = {"maxOutputTokens": max_tokens}
            if include_thinking and thinking_budget is not None:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": thinking_budget}
            body: Dict[str, Any] = {
                "contents": [{"role": "user", "parts": user_parts}],
                "generationConfig": gen_cfg,
            }
            if system_prompt:
                body["system_instruction"] = {"parts": [{"text": system_prompt}]}
            return body

        def _post_rest(body: Dict[str, Any]) -> Dict[str, Any]:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            if thinking_budget is not None:
                try:
                    data = _post_rest(_build_rest_payload(include_thinking=True))
                except Exception:
                    data = _post_rest(_build_rest_payload(include_thinking=False))
            else:
                data = _post_rest(_build_rest_payload(include_thinking=False))
        except urllib.error.HTTPError as e:
            friendly_msg = _diagnose_llm_error(e, provider="Google Gemini")
            raise RuntimeError(friendly_msg) from e
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            friendly_msg = _diagnose_llm_error(e, provider="Google Gemini")
            raise RuntimeError(friendly_msg) from e
        except Exception as e:
            friendly_msg = _diagnose_llm_error(e, provider="Google Gemini")
            raise RuntimeError(friendly_msg) from e

        self.last_responded_model = str(data.get("modelVersion") or self.model_name)
        self.last_token_usage = _extract_token_usage(
            data.get("usageMetadata") or data.get("usage_metadata") or data.get("usage")
        )
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts_out = candidates[0].get("content", {}).get("parts", [])
        return "\n".join(p.get("text", "") for p in parts_out if "text" in p)


def _diagnose_llm_error(e: Exception, provider: Optional[str] = None) -> str:
    """
    LLM API 호출 예외(SDK, HTTPError, Timeout 등)를 분석하여
    모든 AI 모델(Gemini, OpenAI, Claude, OpenRouter, Ollama 등)에 호환되는
    사용자 친화적 한글 안내 메시지를 반환합니다.
    """
    code = getattr(e, "status_code", None) or getattr(e, "code", None)
    err_str = str(e)
    err_lower = err_str.lower()
    exc_type_name = type(e).__name__.lower()

    # 공급자 명칭 레이블 구성
    p_label = ""
    if provider:
        p_clean = provider.strip()
        p_lower = p_clean.lower()
        if "openrouter" in p_lower:
            p_label = "OpenRouter"
        elif "gemini" in p_lower:
            p_label = "Google Gemini"
        elif "openai" in p_lower:
            p_label = "OpenAI"
        elif "claude" in p_lower or "anthropic" in p_lower:
            p_label = "Anthropic Claude"
        elif "ollama" in p_lower:
            p_label = "Ollama"
        else:
            p_label = p_clean
    prefix = f"[{p_label}] " if p_label else ""

    if (
        isinstance(e, (TimeoutError, socket.timeout))
        or "timeout" in exc_type_name
        or "timeout" in err_lower
        or "timed out" in err_lower
        or code in (502, 503, 504)
        or "502" in err_str
        or "503" in err_str
        or "504" in err_str
        or "bad gateway" in err_lower
        or "service unavailable" in err_lower
    ):
        return (
            f"{prefix}서버 응답 시간 초과(30초) 또는 AI 서비스 점검 중입니다. "
            "네트워크 연결을 확인하거나 일시적 대기열 지연일 수 있으니 다른 AI 모델(Gemini, OpenAI, Claude 등)로 변경하거나 잠시 후 다시 시도해 주세요."
        )

    if code == 401 or "401" in err_str or "unauthorized" in err_lower or "authentication" in err_lower:
        return f"{prefix}인증 실패 (401): API Key가 유효하지 않거나 만료되었습니다. [환경설정] 탭의 [API & 연동 설정]에서 API Key를 확인해 주세요."

    if code == 402 or "402" in err_str or "insufficient" in err_lower or "credit" in err_lower:
        return f"{prefix}크레딧/잔여량 부족 (402): 계정의 유료 잔액 또는 토큰 크레딧이 소진되었습니다. 결제 수단 및 충전 상태를 확인해 주세요."

    if code == 429 or "429" in err_str or "rate limit" in err_lower or "quota" in err_lower or "too many requests" in err_lower:
        return f"{prefix}요청 한도 도달 (Rate Limit / 429): 분당 요청 제한(RPM) 또는 사용 할당량을 초과했습니다. 잠시 후 재시도하거나 다른 모델로 전환해 주세요."

    if code == 404 or "404" in err_str or "not found" in err_lower:
        return f"{prefix}모델 조회 실패 (404): 지정된 AI 모델을 찾을 수 없거나 접근 권한이 없습니다. 모델명을 확인해 주세요."

    return f"{prefix}AI API 호출 오류: {err_str}"


class OpenAiCompatibleProvider(BaseLlmProvider):
    """
    OpenAI 공식 API 및 OpenAI 호환 엔드포인트(OpenRouter, Ollama, vLLM, LM Studio 등) 통합 공급자.
    1순위: openai 파이썬 SDK
    2순위: 표준 HTTP REST 호출
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = "gpt-4o",
        base_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        provider_name: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ):
        self.api_key = api_key.strip()
        self.model_name = model_name.strip() or "gpt-4o"
        raw_base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
        if ("11434" in raw_base or "ollama" in raw_base.lower()) and not raw_base.lower().endswith("/v1"):
            raw_base = f"{raw_base}/v1"
        self.base_url = raw_base
        self.extra_headers = dict(extra_headers or {})
        self.max_output_tokens = max_output_tokens
        self.last_responded_model = ""
        self.last_token_usage: Optional[Dict[str, int]] = None
        if provider_name:
            self.provider_name = provider_name
        elif "openrouter" in raw_base.lower():
            self.provider_name = "OpenRouter"
        elif "openai" in raw_base.lower():
            self.provider_name = "OpenAI"
        elif "11434" in raw_base.lower() or "ollama" in raw_base.lower():
            self.provider_name = "Ollama"
        else:
            self.provider_name = "AI"

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
    ) -> str:
        self.last_token_usage = None
        max_tokens, _ = _resolve_generation_options(self.max_output_tokens, None)

        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if image_bytes is not None:
            b64_img = base64.b64encode(image_bytes).decode("ascii")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{b64_img}"},
                        },
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": prompt})

        # 1) openai 공식 라이브러리가 설치되어 있으면 우선 사용
        try:
            from openai import OpenAI  # type: ignore

            client = OpenAI(
                api_key=self.api_key or "ollama",
                base_url=self.base_url,
                default_headers=self.extra_headers or None,
                timeout=30.0,
            )
            completion = client.chat.completions.create(
                model=self.model_name,
                messages=messages,  # type: ignore
                max_tokens=max_tokens,
                timeout=30.0,
            )
            self.last_responded_model = str(getattr(completion, "model", None) or self.model_name)
            self.last_token_usage = _extract_token_usage(getattr(completion, "usage", None))
            return completion.choices[0].message.content or ""
        except ImportError:
            pass
        except Exception as e:
            friendly_msg = _diagnose_llm_error(e, provider=self.provider_name)
            raise RuntimeError(friendly_msg) from e

        # 2) REST API 직접 호출
        endpoint = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json; charset=utf-8", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            friendly_msg = _diagnose_llm_error(e, provider=self.provider_name)
            raise RuntimeError(friendly_msg) from e
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            friendly_msg = _diagnose_llm_error(e, provider=self.provider_name)
            raise RuntimeError(friendly_msg) from e
        except Exception as e:
            friendly_msg = _diagnose_llm_error(e, provider=self.provider_name)
            raise RuntimeError(friendly_msg) from e

        self.last_responded_model = str(data.get("model") or self.model_name)
        self.last_token_usage = _extract_token_usage(data.get("usage"))
        choices = data.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""


class AnthropicClaudeProvider(BaseLlmProvider):
    """
    Anthropic Claude 공급자.
    1순위: anthropic 파이썬 SDK
    2순위: 표준 HTTP REST 호출
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = "claude-3-7-sonnet-latest",
        max_output_tokens: Optional[int] = None,
    ):
        self.api_key = api_key.strip()
        self.model_name = model_name.strip() or "claude-3-7-sonnet-latest"
        self.max_output_tokens = max_output_tokens
        self.last_responded_model = ""
        self.last_token_usage: Optional[Dict[str, int]] = None

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
    ) -> str:
        if not self.api_key:
            raise ValueError("Anthropic (Claude) API Key가 설정되지 않았습니다.")

        self.last_token_usage = None
        max_tokens, _ = _resolve_generation_options(self.max_output_tokens, None)

        user_content: List[Dict[str, Any]] = []
        if image_bytes is not None:
            b64_img = base64.b64encode(image_bytes).decode("ascii")
            user_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": b64_img,
                    },
                }
            )
        user_content.append({"type": "text", "text": prompt})

        # 1) anthropic 공식 SDK 시도
        try:
            import anthropic  # type: ignore

            client = anthropic.Anthropic(api_key=self.api_key)
            kwargs: Dict[str, Any] = {
                "model": self.model_name,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": user_content}],
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            msg = client.messages.create(**kwargs)
            self.last_responded_model = str(getattr(msg, "model", None) or self.model_name)
            self.last_token_usage = _extract_token_usage(getattr(msg, "usage", None))
            return "\n".join(
                block.text for block in msg.content if hasattr(block, "text")
            )
        except ImportError:
            pass
        except Exception as e:
            friendly_msg = _diagnose_llm_error(e, provider="Anthropic Claude")
            raise RuntimeError(friendly_msg) from e

        # 2) REST API 직접 호출
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_content}],
        }
        if system_prompt:
            payload["system"] = system_prompt

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            friendly_msg = _diagnose_llm_error(e, provider="Anthropic Claude")
            raise RuntimeError(friendly_msg) from e
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            friendly_msg = _diagnose_llm_error(e, provider="Anthropic Claude")
            raise RuntimeError(friendly_msg) from e
        except Exception as e:
            friendly_msg = _diagnose_llm_error(e, provider="Anthropic Claude")
            raise RuntimeError(friendly_msg) from e

        self.last_responded_model = str(data.get("model") or self.model_name)
        self.last_token_usage = _extract_token_usage(data.get("usage"))
        content_blocks = data.get("content", [])
        return "\n".join(
            b.get("text", "") for b in content_blocks if b.get("type") == "text"
        )


# =============================================================================
# 3. 통합 LLM 클라이언트 파사드 (MultiLlmClient)
# =============================================================================
class MultiLlmClient:
    """
    ConfigManager 설정 또는 명시적 파라미터를 기반으로
    선택된 공급자(gemini / openai / claude / custom)를 자동 라우팅하는 통합 클라이언트.
    """

    def __init__(
        self,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        base_url: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        thinking_budget_mode: Optional[str] = None,
    ):
        self.config = ConfigManager()
        sec = self.config.get_section("api_and_integrations")

        raw_provider = (provider or sec.get("ai_provider", "gemini")).lower().strip()
        self.provider = normalize_provider_code(raw_provider)
        self.api_key, self.model_name, self.base_url = self._resolve_credentials(
            sec, self.provider, api_key, model_name, base_url
        )
        self.max_output_tokens = max_output_tokens
        self.thinking_budget_mode = thinking_budget_mode
        self.last_responded_model: str = ""
        self.last_token_usage: Optional[Dict[str, int]] = None

    @staticmethod
    def _resolve_credentials(
        sec: Dict[str, Any],
        provider: str,
        override_key: Optional[str],
        override_model: Optional[str],
        override_url: Optional[str],
    ) -> Tuple[str, str, str]:
        if provider == "openai":
            key = override_key if override_key is not None else sec.get("openai_api_key", "")
            model = override_model or sec.get("openai_model", "gpt-4o-mini")
            url = override_url or "https://api.openai.com/v1"
        elif provider == "claude":
            key = override_key if override_key is not None else sec.get("claude_api_key", "")
            model = override_model or sec.get("claude_model", "claude-3-7-sonnet-latest")
            url = override_url or "https://api.anthropic.com"
        elif provider == "openrouter":
            key = override_key if override_key is not None else (
                sec.get("openrouter_api_key") or sec.get("custom_api_key", "")
            )
            model = (
                override_model
                or sec.get("openrouter_model")
                or "meta-llama/llama-3.3-70b-instruct:free"
            )
            url = (
                override_url
                or sec.get("openrouter_base_url")
                or "https://openrouter.ai/api/v1"
            )
        elif provider in ("ollama", "custom"):
            key = override_key if override_key is not None else sec.get("custom_api_key", "")
            model = (
                override_model
                or sec.get("ollama_model")
                or sec.get("custom_model", "llama3.2")
            )
            url = (
                override_url
                or sec.get("ollama_base_url")
                or sec.get("custom_base_url", "http://localhost:11434")
            )
        else:
            key = override_key if override_key is not None else sec.get("gemini_api_key", "")
            model = override_model or sec.get("gemini_model", "gemini-1.5-flash")
            url = override_url or "https://generativelanguage.googleapis.com"

        return str(key).strip(), str(model).strip(), str(url).strip()

    def is_manual_mode_active(self) -> bool:
        """
        사용자가 'API 키 없이 수동 프롬프트 모드로 사용'을 체크했거나,
        선택된 공급자에 필요한 API 키(로컬 Ollama 제외)가 비어 있으면 True를 반환합니다.
        """
        sec = self.config.get_section("api_and_integrations")
        if bool(sec.get("use_manual_prompt_mode", True)):
            return True
        if self.provider in ("ollama", "custom") and is_ollama_base_url(self.base_url):
            # 로컬 Ollama 등은 API Key 없이 Base URL만 있어도 동작 가능
            return not bool(self.base_url)
        return not bool(self.api_key)

    def _create_provider_instance(self) -> BaseLlmProvider:
        max_tokens, thinking_mode = _resolve_generation_options(
            self.max_output_tokens, self.thinking_budget_mode
        )
        if self.provider == "gemini":
            return GeminiProvider(
                api_key=self.api_key,
                model_name=self.model_name,
                max_output_tokens=max_tokens,
                thinking_budget_mode=thinking_mode,
            )
        elif self.provider == "openai":
            return OpenAiCompatibleProvider(
                api_key=self.api_key,
                model_name=self.model_name,
                base_url="https://api.openai.com/v1",
                provider_name="OpenAI",
                max_output_tokens=max_tokens,
            )
        elif self.provider == "claude":
            return AnthropicClaudeProvider(
                api_key=self.api_key,
                model_name=self.model_name,
                max_output_tokens=max_tokens,
            )
        elif self.provider == "openrouter":
            return OpenAiCompatibleProvider(
                api_key=self.api_key,
                model_name=self.model_name,
                base_url=self.base_url or "https://openrouter.ai/api/v1",
                extra_headers={
                    "HTTP-Referer": "https://github.com/dntjr0919/eduflow",
                    "X-Title": "EduFlow",
                    "User-Agent": "EduFlow-Client/2.0",
                },
                provider_name="OpenRouter",
                max_output_tokens=max_tokens,
            )
        elif self.provider in ("ollama", "custom"):
            return OpenAiCompatibleProvider(
                api_key=self.api_key,
                model_name=self.model_name,
                base_url=self.base_url or "http://localhost:11434/v1",
                provider_name="Ollama" if self.provider == "ollama" else "AI",
                max_output_tokens=max_tokens,
            )
        else:
            raise ValueError(f"지원하지 않는 AI 공급자입니다: {self.provider}")

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
    ) -> str:
        """모든 공급자에 대해 동일한 시그니처로 텍스트/멀티모달 생성을 수행합니다."""
        self.last_token_usage = None
        engine = self._create_provider_instance()
        text = engine.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
        self.last_responded_model = str(
            getattr(engine, "last_responded_model", "") or self.model_name
        ).strip()
        self.last_token_usage = getattr(engine, "last_token_usage", None)
        return text

    def test_connection(self) -> Tuple[bool, str]:
        """환경설정 탭의 [연결 테스트] 버튼에서 호출하는 빠른 상태 점검 메서드."""
        try:
            reply = self.generate(
                prompt="연결 테스트입니다. 오직 'OK' 두 글자만 출력하세요.",
                system_prompt="You are a connection tester. Reply with OK.",
            )
            responded = (self.last_responded_model or self.model_name).strip()
            preview = reply.strip()[:80]
            if responded and responded != self.model_name:
                model_desc = f"{self.model_name} (실제 응답 모델: {responded})"
            else:
                model_desc = responded or self.model_name
            return True, f"✅ [{self.provider.upper()} / {model_desc}] 연결 성공!\n응답: {preview}"
        except Exception as exc:
            return False, f"❌ [{self.provider.upper()} / {self.model_name}] 연결 실패:\n{exc}"


# =============================================================================
# 4. 공급자별 사용 가능 모델 목록 동적 조회 (Gemini / OpenAI / Claude / Ollama·OpenRouter)
# =============================================================================
# - SDK 설치 여부와 무관하게 동작하도록 각 공급자의 공식 REST 모델 목록 API를 urllib로 직접 호출합니다.
# - 텍스트 채팅(생성)에 적합한 모델만 필터링하고, 추천 기본 모델을 함께 반환합니다.

MODEL_LIST_TIMEOUT_SEC = 10
OLLAMA_DEFAULT_ROOT = "http://localhost:11434"

# 목록에 존재하면 최우선으로 추천하는 모델 (앞쪽일수록 우선)
RECOMMENDED_MODEL_CANDIDATES: Dict[str, List[str]] = {
    "gemini": ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-1.5-pro"],
    "openai": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"],
    "claude": ["claude-3-7-sonnet-latest", "claude-3-5-haiku-latest"],
    "openrouter": ["meta-llama/llama-3.3-70b-instruct:free", "meta-llama/llama-3.3-70b-instruct"],
    "ollama": [],
    "custom": [],
}


def normalize_provider_code(provider: str) -> str:
    """UI 표시명('Google Gemini', 'OpenRouter', 'Ollama (로컬)' 등) 또는 코드값을 내부 공급자 코드로 정규화합니다."""
    p = (provider or "").lower().strip()
    if "gemini" in p or "google" in p:
        return "gemini"
    if "claude" in p or "anthropic" in p:
        return "claude"
    if "openrouter" in p:
        return "openrouter"
    if "ollama" in p or "custom" in p:
        return "ollama"
    if "openai" in p or "gpt" in p:
        return "openai"
    return p or "gemini"


def is_ollama_base_url(base_url: str) -> bool:
    """Custom Base URL이 비어 있거나 로컬 Ollama 서버(기본 11434 포트)를 가리키는지 판별합니다."""
    url = (base_url or "").strip().lower()
    return (not url) or ("11434" in url) or ("ollama" in url)


def _ollama_root_url(base_url: str) -> str:
    """'http://localhost:11434/v1' 같은 OpenAI 호환 주소에서 Ollama 네이티브 루트 주소를 추출합니다."""
    url = (base_url or "").strip().rstrip("/") or OLLAMA_DEFAULT_ROOT
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    for suffix in ("/v1", "/api"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)]
    return url.rstrip("/")


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = MODEL_LIST_TIMEOUT_SEC) -> Dict[str, Any]:
    """GET 요청 후 JSON을 반환합니다. 실패 시 사용자 친화적인 RuntimeError를 발생시킵니다."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in (401, 403):
            raise RuntimeError(
                f"인증 실패 (HTTP {e.code}): API Key가 올바르지 않거나 모델 목록 조회 권한이 없습니다.\n{body}"
            ) from e
        if e.code == 404:
            raise RuntimeError(f"요청한 주소를 찾을 수 없습니다 (HTTP 404): {url}") from e
        raise RuntimeError(f"서버 오류 (HTTP {e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"서버에 연결할 수 없습니다: {e.reason}\n주소({url.split('?')[0]})와 네트워크/서버 실행 상태를 확인해 주세요."
        ) from e
    except TimeoutError as e:
        raise RuntimeError(f"서버 응답 시간이 초과되었습니다 ({timeout}초): {url.split('?')[0]}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"서버 응답을 해석할 수 없습니다 (JSON 아님): {url.split('?')[0]}") from e


def pick_recommended_model(provider: str, models: List[str]) -> str:
    """
    공급자별 최적 추천 모델을 선별합니다 (폐기 모델 및 404 유발 모델 제외).
    - Gemini: 1순위 1.5-flash (8b 제외), 2순위 일반 flash (2.5 제외)
    - OpenAI: gpt-4o-mini 우선
    - Claude: sonnet, haiku 우선
    """
    if not models:
        return ""

    p = (provider or "").lower().strip()
    if "gemini" in p or "google" in p:
        # 1순위: 1.5-flash 또는 최신 안정 flash 모델 (8b 제외)
        candidates = [m for m in models if "1.5-flash" in m and "8b" not in m]
        if candidates:
            if "gemini-1.5-flash" in candidates:
                return "gemini-1.5-flash"
            return candidates[0]
        # 2순위: 그 외 일반 flash 모델 (단, 에러를 유발하는 2.5-flash 제외)
        flash_models = [m for m in models if "flash" in m and "2.5" not in m]
        if flash_models:
            return flash_models[0]
        # 3순위: 2.5를 제외한 첫 번째 모델
        non_25 = [m for m in models if "2.5" not in m]
        return non_25[0] if non_25 else models[0]

    elif "openai" in p or "gpt" in p:
        candidates = [m for m in models if "gpt-4o-mini" in m]
        return candidates[0] if candidates else models[0]

    elif "claude" in p or "anthropic" in p:
        for cand in RECOMMENDED_MODEL_CANDIDATES.get("claude", []):
            if cand in models:
                return cand
        for kw in ("sonnet", "haiku"):
            hit = next((m for m in models if kw in m), None)
            if hit:
                return hit
        return models[0]

    elif "openrouter" in p:
        return next(
            (m for m in models if "llama-3.3-70b-instruct" in m or "free" in m),
            models[0] if models else "",
        )

    return models[0]


def _pick_recommended(provider_code: str, models: List[str], fallback_keywords: List[str]) -> str:
    return pick_recommended_model(provider_code, models)


def _list_gemini_models(api_key: str) -> Tuple[List[str], str]:
    models: List[str] = []
    page_token = ""
    excluded = ("embedding", "aqa", "imagen", "veo", "tts", "image-generation", "native-audio")
    for _ in range(10):  # 페이지네이션 안전 상한
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?pageSize=1000&key={urllib.parse.quote(api_key)}"
        )
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        data = _http_get_json(url)
        for m in data.get("models", []):
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            name = str(m.get("name", "")).replace("models/", "")
            if name and not any(ex in name for ex in excluded):
                models.append(name)
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    models = sorted(set(models), reverse=True)            # 최신 버전 우선
    models.sort(key=lambda x: "flash" not in x)            # flash 계열을 앞으로 (안정 정렬)
    return models, pick_recommended_model("gemini", models)


def _list_openai_models(api_key: str, base_url: str = "") -> Tuple[List[str], str]:
    url = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
    data = _http_get_json(f"{url}/models", headers={"Authorization": f"Bearer {api_key}"})
    prefixes = ("gpt-", "o1", "o3", "o4", "chatgpt")
    excluded = ("audio", "realtime", "transcribe", "transcription", "tts", "image", "search", "embedding", "moderation")
    models = sorted(
        {
            str(m.get("id", ""))
            for m in data.get("data", [])
            if str(m.get("id", "")).startswith(prefixes)
            and not any(ex in str(m.get("id", "")) for ex in excluded)
        },
        reverse=True,
    )
    return models, _pick_recommended("openai", models, ["mini", "turbo"])


def _list_claude_models(api_key: str) -> Tuple[List[str], str]:
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    models: List[str] = []
    after_id = ""
    for _ in range(10):
        url = "https://api.anthropic.com/v1/models?limit=1000"
        if after_id:
            url += f"&after_id={urllib.parse.quote(after_id)}"
        data = _http_get_json(url, headers=headers)
        # Anthropic API는 최신 출시 순으로 반환하므로 순서를 그대로 유지
        models.extend(str(m.get("id", "")) for m in data.get("data", []) if "claude" in str(m.get("id", "")))
        if not data.get("has_more"):
            break
        after_id = data.get("last_id", "")
        if not after_id:
            break
    models = list(dict.fromkeys(models))
    return models, _pick_recommended("claude", models, ["sonnet", "haiku"])


def _list_openrouter_models(api_key: str, base_url: str = "") -> Tuple[List[str], str]:
    """OpenRouter 공식 API 엔드포인트(/api/v1/models)에서 사용 가능한 모델 목록을 조회합니다."""
    if not api_key or not api_key.strip():
        return [], "OpenRouter API Key를 입력해주세요."
    url = "https://openrouter.ai/api/v1/models"
    if base_url and "openrouter.ai" in base_url:
        cleaned = base_url.strip().rstrip("/")
        url = cleaned if cleaned.endswith("/models") else f"{cleaned}/models"
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "HTTP-Referer": "https://github.com/dntjr0919/eduflow",
        "X-Title": "EduFlow",
        "User-Agent": "EduFlow-Client/2.0",
    }
    data = _http_get_json(url, headers=headers, timeout=10)
    models = [str(item["id"]) for item in data.get("data", []) if item.get("id")]
    models.sort()
    # 추천: 가성비/무료 모델 우선 매핑
    rec = next(
        (m for m in models if "llama-3.3-70b-instruct" in m or "free" in m),
        models[0] if models else "",
    )
    return models, rec


def _list_ollama_models(base_url: str = "") -> Tuple[List[str], str]:
    """로컬 Ollama REST API(/api/tags)에서 설치된 모델 목록을 조회합니다."""
    base = base_url.strip() if base_url else "http://localhost:11434"
    base = _ollama_root_url(base)
    url = f"{base}/api/tags"
    try:
        data = _http_get_json(url, headers={"User-Agent": "EduFlow-Client/2.0"}, timeout=4)
        models = [str(item["name"]) for item in data.get("models", []) if item.get("name")]
        return models, (models[0] if models else "")
    except RuntimeError as exc:
        raise RuntimeError(
            f"로컬 Ollama 서버({base})에 연결하지 못했습니다.\n"
            f"Ollama가 실행 중인지 확인해 주세요 (명령: ollama serve).\n\n상세: {exc}"
        ) from exc


def _list_custom_models(api_key: str, base_url: str) -> Tuple[List[str], str]:
    """하위 호환용: 로컬 Ollama(/api/tags) 또는 OpenRouter 등 OpenAI 호환 서버(/models) 조회."""
    if "openrouter" in (base_url or "").lower():
        return _list_openrouter_models(api_key, base_url)
    if is_ollama_base_url(base_url):
        return _list_ollama_models(base_url)
    url = base_url.strip().rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = _http_get_json(f"{url}/models", headers=headers)
    models = sorted({str(m.get("id", "")) for m in data.get("data", []) if m.get("id")})
    return models, (models[0] if models else "")


def fetch_available_models(provider: str, api_key: str = "", base_url: str = "") -> Tuple[List[str], str]:
    """
    공급자별 사용 가능 모델 목록과 추천 기본 모델명을 반환합니다.
    실패 시 원인을 담은 RuntimeError/ValueError를 발생시킵니다 (UI 경고 메시지용).
    반환값: (models: List[str], recommended_model: str)
    """
    provider_lower = (provider or "").lower().strip()
    code = normalize_provider_code(provider)
    key = (api_key or "").strip()
    url = (base_url or "").strip()

    # 1. OpenRouter (공식 API 엔드포인트)
    if "openrouter" in provider_lower or code == "openrouter":
        return _list_openrouter_models(key, url)

    # 2. Ollama (로컬 REST API)
    if "ollama" in provider_lower or code == "ollama":
        return _list_ollama_models(url)

    if code in ("gemini", "openai", "claude") and not key:
        raise ValueError(f"{provider} API Key를 먼저 입력해 주세요.")

    if code == "gemini":
        return _list_gemini_models(key)
    if code == "openai":
        return _list_openai_models(key)
    if code == "claude":
        return _list_claude_models(key)
    if code == "custom":
        return _list_custom_models(key, url)
    raise ValueError(f"지원하지 않는 AI 공급자입니다: {provider}")


def get_available_models(provider: str, api_key: str = "", base_url: str = "") -> Tuple[List[str], str]:
    """
    fetch_available_models의 안전 래퍼. 어떤 오류가 나도 예외를 던지지 않고 ([], "")를 반환합니다.
    반환값: (models: List[str], recommended_model: str)
    """
    try:
        return fetch_available_models(provider, api_key, base_url)
    except Exception as exc:
        print(f"[{provider} Model List Error]: {exc}")
        return [], ""
