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
import urllib.error
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
class BaseLlmProvider(abc.ABC):
    """모든 LLM 공급자가 구현해야 하는 추상 기본 클래스."""

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

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash"):
        self.api_key = api_key.strip()
        self.model_name = model_name.strip() or "gemini-2.5-flash"

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
    ) -> str:
        if not self.api_key:
            raise ValueError("Google Gemini API Key가 설정되지 않았습니다.")

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
            cfg = types.GenerateContentConfig(
                system_instruction=system_prompt if system_prompt else None
            )
            resp = client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=cfg,
            )
            return resp.text or ""
        except ImportError:
            pass

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
            resp = model.generate_content(parts)
            return resp.text or ""
        except ImportError:
            pass

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

        payload: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": user_parts}]
        }
        if system_prompt:
            payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini API HTTP {e.code} 오류: {err_body}") from e

        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts_out = candidates[0].get("content", {}).get("parts", [])
        return "\n".join(p.get("text", "") for p in parts_out if "text" in p)


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
    ):
        self.api_key = api_key.strip()
        self.model_name = model_name.strip() or "gpt-4o"
        self.base_url = (base_url or "https://api.openai.com/v1").strip().rstrip("/")

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
    ) -> str:
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
            )
            completion = client.chat.completions.create(
                model=self.model_name,
                messages=messages,  # type: ignore
            )
            return completion.choices[0].message.content or ""
        except ImportError:
            pass

        # 2) REST API 직접 호출
        endpoint = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model_name,
            "messages": messages,
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI/Custom API HTTP {e.code} 오류: {err_body}") from e

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

    def __init__(self, api_key: str, model_name: str = "claude-3-7-sonnet-latest"):
        self.api_key = api_key.strip()
        self.model_name = model_name.strip() or "claude-3-7-sonnet-latest"

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
    ) -> str:
        if not self.api_key:
            raise ValueError("Anthropic (Claude) API Key가 설정되지 않았습니다.")

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
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": user_content}],
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            msg = client.messages.create(**kwargs)
            return "\n".join(
                block.text for block in msg.content if hasattr(block, "text")
            )
        except ImportError:
            pass

        # 2) REST API 직접 호출
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 4096,
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
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Anthropic API HTTP {e.code} 오류: {err_body}") from e

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
    ):
        self.config = ConfigManager()
        sec = self.config.get_section("api_and_integrations")

        self.provider = (provider or sec.get("ai_provider", "gemini")).lower().strip()
        self.api_key, self.model_name, self.base_url = self._resolve_credentials(
            sec, self.provider, api_key, model_name, base_url
        )

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
            model = override_model or sec.get("openai_model", "gpt-4o")
            url = override_url or "https://api.openai.com/v1"
        elif provider == "claude":
            key = override_key if override_key is not None else sec.get("claude_api_key", "")
            model = override_model or sec.get("claude_model", "claude-3-7-sonnet-latest")
            url = override_url or "https://api.anthropic.com"
        elif provider == "custom":
            key = override_key if override_key is not None else sec.get("custom_api_key", "")
            model = override_model or sec.get("custom_model", "llama3.2")
            url = override_url or sec.get("custom_base_url", "http://localhost:11434/v1")
        else:
            key = override_key if override_key is not None else sec.get("gemini_api_key", "")
            model = override_model or sec.get("gemini_model", "gemini-2.5-flash")
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
        if self.provider == "custom":
            # 로컬 Ollama 등은 API Key 없이 Base URL만 있어도 동작 가능
            return not bool(self.base_url)
        return not bool(self.api_key)

    def _create_provider_instance(self) -> BaseLlmProvider:
        if self.provider == "gemini":
            return GeminiProvider(api_key=self.api_key, model_name=self.model_name)
        elif self.provider == "openai":
            return OpenAiCompatibleProvider(
                api_key=self.api_key,
                model_name=self.model_name,
                base_url="https://api.openai.com/v1",
            )
        elif self.provider == "claude":
            return AnthropicClaudeProvider(
                api_key=self.api_key,
                model_name=self.model_name,
            )
        elif self.provider == "custom":
            return OpenAiCompatibleProvider(
                api_key=self.api_key,
                model_name=self.model_name,
                base_url=self.base_url,
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
        engine = self._create_provider_instance()
        return engine.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )

    def test_connection(self) -> Tuple[bool, str]:
        """환경설정 탭의 [연결 테스트] 버튼에서 호출하는 빠른 상태 점검 메서드."""
        try:
            reply = self.generate(
                prompt="연결 테스트입니다. 오직 'OK' 두 글자만 출력하세요.",
                system_prompt="You are a connection tester. Reply with OK.",
            )
            preview = reply.strip()[:80]
            return True, f"✅ [{self.provider.upper()} / {self.model_name}] 연결 성공!\n응답: {preview}"
        except Exception as exc:
            return False, f"❌ [{self.provider.upper()} / {self.model_name}] 연결 실패:\n{exc}"
