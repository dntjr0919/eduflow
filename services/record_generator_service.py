"""
엑셀 파일의 학생별 문항 응답 데이터에 프롬프트와 참고자료를 적용하여
Gemini API를 호출하고, NEIS 바이트 계산 및 이어쓰기를 지원하는 과세특/채점 자동 생성 서비스.
"""

import math
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from core.config_manager import ConfigManager
from services.llm_client import MultiLlmClient, normalize_provider_code

# =============================================================================
# 1. NEIS 바이트 계산 및 길이 제어 정책 (LengthPolicy)
# =============================================================================
def calculate_neis_bytes(text: Any) -> int:
    """
    나이스(NEIS) 생활기록부 바이트 계산:
    - 한글 및 기타 유니코드: 3바이트
    - 엔터/줄바꿈(\\n): 2바이트
    - 영문, 숫자, 기본 특수문자, 공백(ASCII <= 127): 1바이트
    """
    if not text or pd.isna(text):
        return 0
    clean_text = str(text).replace("\r\n", "\n")
    byte_count = 0
    for char in clean_text:
        if char == "\n":
            byte_count += 2
        elif ord(char) <= 127:
            byte_count += 1
        else:
            byte_count += 3
    return byte_count


@dataclass(frozen=True)
class LengthPolicy:
    target_min_bytes: Optional[int] = None
    hard_min_bytes: Optional[int] = None
    max_bytes: Optional[int] = None
    max_retries: int = 1

    @property
    def enabled(self) -> bool:
        return self.max_bytes is not None


NO_LENGTH_POLICY = LengthPolicy()

SUMMARY_750_POLICY = LengthPolicy(
    target_min_bytes=680,
    hard_min_bytes=620,
    max_bytes=750,
    max_retries=1,
)

DEFAULT_1500_POLICY = LengthPolicy(
    target_min_bytes=1350,
    hard_min_bytes=1200,
    max_bytes=1500,
    max_retries=1,
)

LENGTH_POLICIES: Dict[str, LengthPolicy] = {
    "prompt_세특요약 및 다듬기.txt": SUMMARY_750_POLICY,
}


def get_length_policy(prompt_file: Optional[Path | str], custom_max_bytes: Optional[int] = None) -> LengthPolicy:
    """프롬프트 파일에 따른 길이 정책을 반환합니다. custom_max_bytes가 지정되면 이를 우선 적용합니다."""
    if custom_max_bytes is not None and custom_max_bytes > 0:
        target_min = int(custom_max_bytes * 0.85)
        hard_min = int(custom_max_bytes * 0.75)
        return LengthPolicy(
            target_min_bytes=target_min,
            hard_min_bytes=hard_min,
            max_bytes=custom_max_bytes,
            max_retries=1,
        )

    if prompt_file is None:
        return DEFAULT_1500_POLICY

    filename = Path(prompt_file).name
    return LENGTH_POLICIES.get(filename, DEFAULT_1500_POLICY)


# =============================================================================
# 2. 참고자료 파싱 모듈 (doc_loader)
# =============================================================================
def extract_text_from_txt(file_path: Path) -> str:
    for enc in ["utf-8-sig", "utf-8", "cp949", "euc-kr"]:
        try:
            with open(file_path, "r", encoding=enc) as f:
                return f.read().strip()
        except UnicodeDecodeError:
            continue
    return ""


def extract_text_from_pdf(file_path: Path) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        text_list = []
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text_list.append(extracted)
        return "\n".join(text_list).strip()
    except Exception as exc:
        print(f"[참고자료 PDF 파싱 실패] {file_path.name}: {exc}")
        return ""


def extract_text_from_docx(file_path: Path) -> str:
    try:
        import docx
        doc = docx.Document(file_path)
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs).strip()
    except Exception:
        # docx 패키지가 없거나 실패 시 zipfile 기반 간단 파싱
        try:
            with zipfile.ZipFile(file_path) as zf:
                xml_data = zf.read("word/document.xml")
            root = ET.fromstring(xml_data)
            parts = []
            for node in root.iter():
                if node.tag.endswith("}t") and node.text:
                    parts.append(node.text)
            return " ".join(parts).strip()
        except Exception:
            return ""


def extract_text_from_xlsx(file_path: Path) -> str:
    try:
        df = pd.read_excel(file_path)
        rows_text = []
        for _, row in df.iterrows():
            row_vals = [
                str(val).strip()
                for val in row.values
                if pd.notna(val) and str(val).strip() != ""
            ]
            if row_vals:
                rows_text.append(" | ".join(row_vals))
        return "\n".join(rows_text).strip()
    except Exception:
        return ""


def extract_text_from_hwpx(file_path: Path) -> str:
    text_list = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            section_files = [
                f for f in zf.namelist()
                if f.startswith("Contents/section") and f.endswith(".xml")
            ]
            for sec in section_files:
                xml_data = zf.read(sec)
                root = ET.fromstring(xml_data)
                text = "".join(root.itertext()).strip()
                if text:
                    text_list.append(text)
    except Exception:
        pass
    return "\n".join(text_list).strip()


def load_all_references(ref_dir: Path | str) -> str:
    """지정된 폴더 내의 모든 참고자료 문서를 파싱하여 하나의 텍스트 컨텍스트로 반환합니다."""
    ref_path = Path(ref_dir)
    if not ref_path.exists():
        return ""

    files = [
        f for f in ref_path.rglob("*")
        if f.is_file() and not f.name.startswith("~$") and not f.name.startswith(".")
    ]

    parsed_sections: List[str] = []
    for f in sorted(files):
        ext = f.suffix.lower()
        content = ""
        if ext in [".txt", ".md", ".json"]:
            content = extract_text_from_txt(f)
        elif ext == ".pdf":
            content = extract_text_from_pdf(f)
        elif ext in [".docx", ".doc"]:
            content = extract_text_from_docx(f)
        elif ext in [".xlsx", ".xls", ".csv"]:
            content = extract_text_from_xlsx(f)
        elif ext == ".hwpx":
            content = extract_text_from_hwpx(f)

        if content:
            parsed_sections.append(f"=== [참고자료: {f.name}] ===\n{content}\n")

    return "\n\n".join(parsed_sections).strip()


# =============================================================================
# 3. 프롬프트 템플릿 관리 (prompts_memory)
# =============================================================================
DEFAULT_SYSTEM_PROMPT = """
[역할]
당신은 대한민국 중·고등학교 교사를 지원하는 NEIS 학교생활기록부 작성 전문 AI이다.
현재 작업은 교과 세부능력 및 특기사항 작성이며, 학생 응답과 교사의 관찰·평가 자료를 근거로 나이스에 입력 가능한 문장을 작성한다.

[기본 원칙]
1. 학생 응답과 교사가 제공한 관찰 기록에 있는 사실만 사용한다.
2. 없는 활동, 역할, 태도, 성취, 협업, 발표, 토론, 실험 과정, 결과를 추정하거나 추가하지 않는다.
3. 학생이 작성한 문장을 그대로 길게 옮기지 말고, 교사의 관찰자 시점에서 재구성한다.
4. 참고자료는 수업 맥락과 교과 용어를 정교하게 반영하는 용도로만 사용한다.
5. 참고자료의 문장을 그대로 베끼지 않는다.

[사용자 요청]
1. 바이트 제한 요청 : {max_bytes}바이트 이내로 작성

{ref_section}

[행동 중심 작성 원칙]
1. 학생의 내면 상태, 감정, 의도, 인지 수준을 추정·단정하는 표현을 사용하지 않는다.
2. '이해함, 파악함, 생각함, 깨달음, 흥미를 느낌, 노력함' 등의 표현 대신 교사가 확인 가능한 행동 중심으로 작성한다:
   발표함, 토론함, 비교함, 분류함, 분석함, 계산함, 해석함, 설명함, 제안함, 수정·보완함, 기록함
3. 위 행동 표현도 학생 응답에 실제 근거가 있을 때만 사용한다.

[문체 및 형식]
1. 실제 나이스 입력용 최종 문장만 작성한다.
2. 교사의 관찰자 시점으로 작성한다.
3. 학생 이름({student_name}), 학번, 1인칭 표현, 자기소개식 문체를 사용하지 않는다.
4. 모든 문장은 반드시 명사형 종결인 '~함', '~임'으로 끝낸다.
5. 결과는 줄바꿈 없는 하나의 완성된 문단으로 작성한다.
6. 제목, 설명, 따옴표 없이 결과 문장만 출력한다.
7. 나이스 기준 최대 {max_bytes}바이트 이내로 작성한다.

[학생 응답]
{answers_text}

[출력]
""".strip()


def list_prompt_files(prompts_dir: Optional[Path | str] = None) -> List[Path]:
    """prompts_memory 디렉토리 내의 .txt 프롬프트 파일 목록을 반환합니다."""
    if prompts_dir is None:
        prompts_dir = Path("prompts_memory")
    else:
        prompts_dir = Path(prompts_dir)

    if not prompts_dir.exists():
        return []

    return sorted(list(prompts_dir.glob("*.txt")))


def load_prompt_text(prompt_path: Optional[Path | str]) -> str:
    """프롬프트 텍스트를 읽어옵니다. 없으면 기본 프롬프트를 반환합니다."""
    if prompt_path is None:
        return DEFAULT_SYSTEM_PROMPT

    p = Path(prompt_path)
    if not p.exists():
        return DEFAULT_SYSTEM_PROMPT

    return extract_text_from_txt(p) or DEFAULT_SYSTEM_PROMPT


def build_student_prompt(
    template: str,
    student_name: str,
    answers_text: str,
    reference_context: str = "",
    length_policy: LengthPolicy = DEFAULT_1500_POLICY,
) -> str:
    """프롬프트 템플릿의 변수 치환을 수행합니다."""
    ref_section = ""
    if reference_context and reference_context.strip():
        ref_section = f"""
[참고자료]
아래 참고자료는 수업 맥락, 성취기준, 교과 용어 및 학교생활기록부 기재 유의사항을 확인하는 용도로만 사용한다.
참고자료에만 있고 학생 응답에 없는 활동, 역할, 발표, 성취는 추가하지 않는다.

{reference_context}
""".strip()

    max_b = length_policy.max_bytes or 1500
    target_b = length_policy.target_min_bytes or 1350
    hard_b = length_policy.hard_min_bytes or 1200

    prompt = (
        template
        .replace("{student_name}", str(student_name))
        .replace("{answers_text}", str(answers_text))
        .replace("{reference_context}", str(reference_context))
        .replace("{ref_section}", ref_section)
        .replace("{target_min_bytes}", str(target_b))
        .replace("{hard_min_bytes}", str(hard_b))
        .replace("{max_bytes}", str(max_b))
    )
    return prompt


# =============================================================================
# 4. 축약 / 확장 보정 프롬프트
# =============================================================================
def build_shorten_prompt(
    original_text: str,
    current_output: str,
    current_bytes: int,
    max_bytes: int,
) -> str:
    return f"""
현재 정제 결과는 {current_bytes}바이트로 최대 {max_bytes}바이트를 초과한다.

[기존 과세특 원문]
{original_text}

[현재 정제 결과]
{current_output}

현재 결과를 기존 원문과 대조하여, 원문에 있는 사실만 유지한 채
최대 {max_bytes}바이트 이내로 축약하라.

축약 우선순위:
1. 반복 표현
2. 유사 수식어
3. 형식 정보
4. 장황한 연결 표현
5. 중복 설명

반드시 유지:
- 활동 맥락 및 구체적 수행 행동
- 교과 개념 및 분석·해석 과정
- 근거 및 결과

금지:
- 원문에 없는 사실, 활동, 태도 추가
- 제목, 설명, 따옴표 출력
- `~함` 또는 `~임`이 아닌 종결
""".strip()


def build_expand_prompt(
    original_text: str,
    current_output: str,
    current_bytes: int,
    target_min_bytes: int,
    max_bytes: int,
) -> str:
    return f"""
현재 정제 결과는 {current_bytes}바이트로 권장 범위인 {target_min_bytes}~{max_bytes}바이트보다 짧다.

[기존 과세특 원문]
{original_text}

[현재 정제 결과]
{current_output}

현재 결과와 기존 원문을 비교하여, 현재 결과에서 빠진 원문 내 사실만 복원하라.

금지:
- 원문에 없는 사실 추가 또는 과장
- 단순 반복 문장으로 분량 채우기
- 제목, 머리말, 따옴표 출력
- `~함` 또는 `~임`이 아닌 종결
""".strip()


# =============================================================================
# 5. MultiLlmClient 통합 클라이언트 래퍼 및 자격증명 조회
# =============================================================================
def get_active_ai_info() -> Dict[str, Any]:
    """현재 ConfigManager에 설정된 AI 공급자 및 모델 정보를 조회합니다."""
    cfg = ConfigManager().get_section("api_and_integrations")
    provider = cfg.get("ai_provider", "gemini")
    code = normalize_provider_code(provider)

    if code == "openai":
        model = cfg.get("openai_model", "gpt-4o-mini")
        has_key = bool(cfg.get("openai_api_key", "").strip() or os.getenv("OPENAI_API_KEY", "").strip())
    elif code == "claude":
        model = cfg.get("claude_model", "claude-3-7-sonnet-latest")
        has_key = bool(cfg.get("claude_api_key", "").strip() or os.getenv("ANTHROPIC_API_KEY", "").strip())
    elif code == "openrouter":
        model = cfg.get("openrouter_model", "meta-llama/llama-3.3-70b-instruct:free")
        has_key = bool(cfg.get("openrouter_api_key", "").strip() or cfg.get("custom_api_key", "").strip())
    elif code in ("ollama", "custom"):
        model = cfg.get("ollama_model", cfg.get("custom_model", "llama3.2"))
        has_key = True  # 로컬 Ollama는 API 키 없이 동작 가능
    else:
        model = cfg.get("gemini_model", "gemini-1.5-flash")
        has_key = bool(cfg.get("gemini_api_key", "").strip() or os.getenv("GEMINI_API_KEY", "").strip())

    tested_prov = str(cfg.get("last_tested_provider", "") or "").strip()
    tested_req = str(cfg.get("last_tested_requested_model", "") or "").strip()
    tested_resp = str(cfg.get("last_tested_responded_model", "") or "").strip()
    tested_ok = bool(cfg.get("last_tested_ok", False))

    same_target = bool(tested_prov and tested_prov == code and tested_req == str(model).strip())
    connection_verified = bool(same_target and tested_ok and tested_resp)
    connection_failed = bool(same_target and not tested_ok)
    responded_model = tested_resp if connection_verified else ""

    return {
        "provider": code,
        "provider_display": provider,
        "model": model,
        "has_key": has_key,
        "responded_model": responded_model,
        "connection_verified": connection_verified,
        "connection_failed": connection_failed,
    }


def get_gemini_api_key() -> str:
    """하위 호환용: 현재 설정된 공급자의 API 키를 반환합니다."""
    info = get_active_ai_info()
    cfg = ConfigManager().get_section("api_and_integrations")
    code = info["provider"]
    if code == "gemini":
        return cfg.get("gemini_api_key", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    elif code == "openai":
        return cfg.get("openai_api_key", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
    elif code == "claude":
        return cfg.get("claude_api_key", "").strip() or os.getenv("ANTHROPIC_API_KEY", "").strip()
    elif code == "openrouter":
        return cfg.get("openrouter_api_key", "").strip() or cfg.get("custom_api_key", "").strip()
    return "local-ollama"


def extract_retry_seconds(error_text: str, default_seconds: int = 15) -> int:
    match = re.search(r"retryDelay': '(\d+)s'", error_text)
    if match:
        return int(match.group(1))
    match = re.search(r"Please retry in ([\d.]+)s", error_text)
    if match:
        return max(2, round(float(match.group(1))))
    return default_seconds


# =============================================================================
# 6. 학생 1명 세특 생성 및 바이트 제어 정책 적용
# =============================================================================
def generate_single_student_record(
    llm_client: MultiLlmClient,
    prompt_text: str,
    answers_text: str,
    length_policy: LengthPolicy,
    log_callback: Optional[Callable[[str, str], None]] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
) -> Tuple[str, int]:
    """단일 학생에 대해 MultiLlmClient를 호출하고 바이트 정책을 만족할 때까지 조정합니다."""
    max_retries = 3

    def send_prompt(current_prompt: str) -> str:
        attempt = 0
        while attempt < max_retries:
            if stop_checker and stop_checker():
                return "사용자에 의해 작업 중단됨"

            try:
                response = llm_client.generate(
                    prompt=current_prompt,
                    system_prompt="당신은 대한민국 중·고등학교 교사를 지원하는 NEIS 학교생활기록부 작성 전문 AI이다.",
                )
                if stop_checker and stop_checker():
                    return "사용자에 의해 작업 중단됨"

                if response and response.strip():
                    return response.strip()
                raise ValueError("AI 응답이 비어 있습니다.")
            except Exception as exc:
                if stop_checker and stop_checker():
                    return "사용자에 의해 작업 중단됨"

                err_str = str(exc)
                if "429" in err_str or "rate limit" in err_str.lower():
                    wait_sec = extract_retry_seconds(err_str, default_seconds=15)
                    if log_callback:
                        log_callback("WARN", f"API 요청 한도(429/RateLimit) 도달. {wait_sec}초 대기 후 재시도합니다...")
                    for _ in range(int(wait_sec * 10)):
                        if stop_checker and stop_checker():
                            return "사용자에 의해 작업 중단됨"
                        time.sleep(0.1)
                    attempt += 1
                    continue
                else:
                    if log_callback:
                        log_callback("ERROR", f"[{llm_client.provider.upper()}] 호출 오류: {exc}")
                    return f"생성 실패: {err_str}"
        return f"생성 실패: API 최대 재시도 초과 ({max_retries}회)"

    if stop_checker and stop_checker():
        return "사용자에 의해 작업 중단됨", 0

    initial_output = send_prompt(prompt_text)
    if stop_checker and stop_checker():
        return "사용자에 의해 작업 중단됨", 0

    if not length_policy.enabled or not initial_output or initial_output.startswith("생성 실패"):
        return initial_output, calculate_neis_bytes(initial_output)

    current_output = initial_output
    current_bytes = calculate_neis_bytes(current_output)
    max_b = length_policy.max_bytes or 1500

    if current_bytes > max_b and length_policy.max_retries > 0:
        if stop_checker and stop_checker():
            return "사용자에 의해 작업 중단됨", 0

        shorten_prompt = build_shorten_prompt(
            original_text=answers_text,
            current_output=current_output,
            current_bytes=current_bytes,
            max_bytes=max_b,
        )
        corrected = send_prompt(shorten_prompt)
        if stop_checker and stop_checker():
            return "사용자에 의해 작업 중단됨", 0

        if corrected and not corrected.startswith("생성 실패"):
            current_output = corrected
            current_bytes = calculate_neis_bytes(current_output)

    return current_output, current_bytes


# =============================================================================
# 7. 엑셀 기반 이어쓰기(Resume) 메인 서비스 함수
# =============================================================================
def is_blank_or_failed(value: Any) -> bool:
    if pd.isna(value):
        return True
    s = str(value).strip()
    return s == "" or s.lower() in ["nan", "none", "null"] or s.startswith("생성 실패")


def safe_save_excel(df: pd.DataFrame, file_path: Path) -> Path:
    """열려 있는 파일로 인한 PermissionError 시 대체 파일로 안전 저장."""
    file_path = Path(file_path)
    target = file_path
    for attempt in range(5):
        try:
            df.to_excel(target, index=False)
            return target
        except PermissionError:
            target = file_path.parent / f"{file_path.stem}_임시저장_{attempt+1}.xlsx"
    df.to_excel(target, index=False)
    return target


def generate_records_from_excel(
    excel_path: Path | str,
    output_path: Optional[Path | str] = None,
    prompt_file: Optional[Path | str] = None,
    reference_dir: Optional[Path | str] = None,
    max_bytes: int = 1500,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    엑셀 파일에서 학생 응답을 읽어 MultiLlmClient(Gemini, OpenAI, Claude, OpenRouter, Ollama)를 통해
    AI 과세특을 생성하고 엑셀 파일에 저장합니다.
    기존에 완료된 행은 자동으로 건너뛰어 안전하게 이어쓰기(Resume)를 지원합니다.
    """
    excel_path = Path(excel_path)
    if not excel_path.exists():
        raise FileNotFoundError(f"입력 엑셀 파일을 찾을 수 없습니다: {excel_path}")

    # 통합 LLM 클라이언트 초기화
    llm_client = MultiLlmClient(
        provider=provider,
        api_key=api_key,
        model_name=model_name,
        base_url=base_url,
    )

    # API 키 누락 검증 (로컬 Ollama는 예외)
    if not llm_client.api_key and llm_client.provider not in ("ollama", "custom"):
        raise ValueError(
            f"선택된 AI 공급자({llm_client.provider.upper()})의 API Key가 설정되지 않았습니다.\n"
            "[환경설정] 탭의 [API & 연동 설정]에서 API Key를 등록하거나 시스템 환경변수를 확인해 주세요."
        )

    # 결과 파일 경로 결정
    if output_path is None:
        output_path = excel_path.parent / f"과세특완료_{excel_path.name}"
    else:
        output_path = Path(output_path)

    # 기존 결과 파일이 존재하면 그것을 읽어 이어서 작업
    target_excel = output_path if output_path.exists() else excel_path
    try:
        df = pd.read_excel(target_excel)
    except PermissionError:
        raise PermissionError(f"엑셀 파일이 열려 있어 읽을 수 없습니다: {target_excel.name}")

    if df.empty:
        return {"total": 0, "completed": 0, "output_path": str(output_path), "records": []}

    # 열 표준화
    if "생성된_과세특" not in df.columns:
        df["생성된_과세특"] = ""
    if "과세특_바이트" not in df.columns:
        df["과세특_바이트"] = 0

    df["생성된_과세특"] = df["생성된_과세특"].apply(lambda x: "" if is_blank_or_failed(x) else str(x))
    df["과세특_바이트"] = pd.to_numeric(df["과세특_바이트"], errors="coerce").fillna(0).astype(int)

    # 학생 정보 열 판별
    cols = list(df.columns)
    id_col = cols[0]
    name_col = cols[1] if len(cols) > 1 else cols[0]
    # 응답 열들 ('생성된_과세특', '과세특_바이트', '누락 항목', '양식', '원본파일명' 제외)
    meta_cols = {"생성된_과세특", "과세특_바이트", "누락 항목", "양식", "원본파일명", id_col, name_col}
    response_cols = [c for c in cols if c not in meta_cols]

    # 프롬프트 및 참고자료 로드
    raw_prompt_template = load_prompt_text(prompt_file)
    length_policy = get_length_policy(prompt_file, custom_max_bytes=max_bytes)

    reference_context = ""
    if reference_dir:
        reference_context = load_all_references(reference_dir)
        if reference_context and log_callback:
            log_callback("INFO", f"참고자료 {len(reference_context):,}자를 로드하여 프롬프트에 주입합니다.")

    total_count = len(df)
    already_done = sum(1 for v in df["생성된_과세특"] if not is_blank_or_failed(v))

    if log_callback:
        log_callback(
            "INFO",
            f"총 {total_count}명 중 이미 완료된 {already_done}명을 제외하고 생성을 시작합니다. "
            f"(공급자: {llm_client.provider.upper()}, 모델: {llm_client.model_name})",
        )

    success_cnt = already_done
    actual_saved_path = output_path
    consecutive_errors = 0
    max_consecutive_errors = 3

    for idx in range(total_count):
        if stop_checker and stop_checker():
            if log_callback:
                log_callback("WARN", "사용자 요청으로 과세특 생성을 안전하게 일시 중단합니다.")
            break

        current_val = df.at[idx, "생성된_과세특"]
        sid = str(df.at[idx, id_col])
        sname = str(df.at[idx, name_col])

        # 이미 작성되어 있으면 패스
        if not is_blank_or_failed(current_val):
            continue

        pct = int(((idx + 1) / total_count) * 100)
        if progress_callback:
            progress_callback(pct, f"과세특 생성 중 ({idx + 1}/{total_count}): {sid} {sname}")

        # 응답 텍스트 결합
        valid_answers = []
        for col in response_cols:
            val = df.at[idx, col]
            if pd.notna(val) and str(val).strip():
                valid_answers.append(f"- {col}: {str(val).strip()}")

        combined_answers = "\n".join(valid_answers)
        if not combined_answers.strip():
            df.at[idx, "생성된_과세특"] = "응답 내용 없음"
            df.at[idx, "과세특_바이트"] = 0
            actual_saved_path = safe_save_excel(df, output_path)
            if log_callback:
                log_callback("INFO", f"[{sid} {sname}] 응답 내용 없음 (건너뜀)")
            continue

        prompt = build_student_prompt(
            template=raw_prompt_template,
            student_name=sname,
            answers_text=combined_answers,
            reference_context=reference_context,
            length_policy=length_policy,
        )

        res_text, res_bytes = generate_single_student_record(
            llm_client=llm_client,
            prompt_text=prompt,
            answers_text=combined_answers,
            length_policy=length_policy,
            log_callback=log_callback,
            stop_checker=stop_checker,
        )

        df.at[idx, "생성된_과세특"] = res_text
        df.at[idx, "과세특_바이트"] = res_bytes
        actual_saved_path = safe_save_excel(df, output_path)

        if res_text.startswith("생성 실패"):
            consecutive_errors += 1
            err_str = res_text[len("생성 실패:"):].strip() if "생성 실패:" in res_text else res_text
            if log_callback:
                log_callback("ERROR", f"[{sid} {sname}] 생성 실패 원인: {err_str}")
            if consecutive_errors >= max_consecutive_errors:
                if log_callback:
                    log_callback(
                        "ERROR",
                        f"연속 {consecutive_errors}회 AI API 호출 실패로 작업을 자동 일시 중단합니다. (최근 원인: {err_str}) "
                        "API Key 유효성, 크레딧 잔액 또는 공급자 모델 상태를 확인해 주세요.",
                    )
                break
        elif res_text == "사용자에 의해 작업 중단됨":
            break
        else:
            consecutive_errors = 0
            success_cnt += 1
            if log_callback:
                usage = getattr(llm_client, "last_token_usage", None)
                if isinstance(usage, dict) and ("input" in usage or "output" in usage):
                    in_tok = int(usage.get("input", 0) or 0)
                    out_tok = int(usage.get("output", 0) or 0)
                    log_callback(
                        "SUCCESS",
                        f"[{sid} {sname}] 생성 완료 ({res_bytes:,}B | 토큰: 입력 {in_tok:,} / 출력 {out_tok:,})",
                    )
                else:
                    log_callback("SUCCESS", f"[{sid} {sname}] 생성 완료 ({res_bytes:,}B)")
            if progress_callback:
                progress_callback(pct, f"{sid} {sname} 처리 완료 ({res_bytes}B)")

        # RPM 속도 제어 (1초 대기 -> 0.1초 슬라이스 루프로 즉각 중단 반영)
        for _ in range(10):
            if stop_checker and stop_checker():
                break
            time.sleep(0.1)

    return {
        "total": total_count,
        "completed": success_cnt,
        "output_path": str(actual_saved_path.resolve()),
        "records_df": df,
    }
