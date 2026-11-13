"""
수동 프롬프트 배치 분할 및 웹 AI JSON 응답 엑셀 매핑 서비스 (services/batch_prompt_service.py).
- UI 의존성이 없는 순수 비즈니스 로직 모듈로, 수동 배치 모달 및 향후 Playwright 자동화 워커에서 재사용합니다.
"""

from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Union

import pandas as pd

from services.excel_merger_service import identify_student_columns, normalize_student_id
from services.llm_client import extract_json_from_llm_response
from services.record_generator_service import (
    DEFAULT_1500_POLICY,
    LengthPolicy,
    calculate_neis_bytes,
    is_blank_or_failed,
    safe_save_excel,
)


BATCH_JSON_OUTPUT_RULE = """# 다중 학생 일괄 출력 규칙 (필수)
위 학생 전원을 누락 없이 처리하여, 다른 인사말이나 설명 없이 오직 아래 ```json 코드 블록 형식으로만 출력하세요.
```json
[
  {"학번": "10101", "이름": "고서윤", "결과": "여기에 해당 학생의 전체 채점 결과 또는 세특 문장 작성"}
]
```"""

METADATA_COLUMNS = {
    "학번",
    "이름",
    "누락 항목",
    "누락항목",
    "양식",
    "원본파일명",
    "생성된_과세특",
    "과세특_바이트",
    "AI_채점결과",
}


def estimate_tokens(text: str) -> int:
    """
    텍스트의 예상 토큰 수를 근사 계산하여 정수로 반환합니다.
    - 한글 및 비-ASCII 유니코드 문자: 1.8 토큰
    - ASCII 문자 (ord <= 127): 0.5 토큰
    """
    if not text:
        return 0
    total = 0.0
    for ch in str(text):
        if ord(ch) <= 127:
            total += 0.5
        else:
            total += 1.8
    if total <= 0:
        return 0
    return max(1, int(round(total)))


def normalize_student_id_str(val: Any) -> str:
    """학번 값을 비교 가능한 정규화 문자열로 변환합니다."""
    norm = normalize_student_id(val)
    if norm:
        return norm
    if val is None or pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2].strip()
    digits_only = re.sub(r"\s+", "", s)
    return digits_only


def _build_base_batch_template(
    prompt_template: str,
    reference_context: str = "",
    max_bytes: int = 1500,
) -> str:
    """참고자료 및 길이 치환 변수를 반영한 배치용 기본 프롬프트 뼈대를 생성합니다."""
    template = (prompt_template or "").strip()
    ref_section = ""
    if reference_context and reference_context.strip():
        ref_section = (
            "[참고자료]\n"
            "아래 참고자료는 수업 맥락, 성취기준, 교과 용어 및 기재 유의사항을 확인하는 용도로만 사용한다.\n"
            "참고자료에만 있고 학생 응답에 없는 활동, 역할, 발표, 성취는 추가하지 않는다.\n\n"
            f"{reference_context.strip()}"
        )

    policy = LengthPolicy(
        target_min_bytes=int(max_bytes * 0.85),
        hard_min_bytes=int(max_bytes * 0.75),
        max_bytes=max_bytes,
        max_retries=1,
    ) if max_bytes > 0 else DEFAULT_1500_POLICY

    max_b = policy.max_bytes or 1500
    target_b = policy.target_min_bytes or 1350
    hard_b = policy.hard_min_bytes or 1200

    return (
        template
        .replace("{student_name}", "각 학생(아래 제출물 목록 참조)")
        .replace("{reference_context}", str(reference_context or ""))
        .replace("{ref_section}", ref_section)
        .replace("{target_min_bytes}", str(target_b))
        .replace("{hard_min_bytes}", str(hard_b))
        .replace("{max_bytes}", str(max_b))
    )


def _assemble_batch_prompt(base_template: str, student_blocks: Sequence[str]) -> str:
    """기본 템플릿의 {answers_text} 자리에 학생 묶음 블록을 삽입하고 하단에 JSON 출력 규칙을 붙입니다."""
    joined_students = "\n\n".join(student_blocks).strip()
    if "{answers_text}" in base_template:
        body = base_template.replace("{answers_text}", joined_students)
    else:
        body = f"{base_template}\n\n[학생별 제출물 목록]\n{joined_students}"
    return f"{body.rstrip()}\n\n{BATCH_JSON_OUTPUT_RULE}"


def build_student_batches(
    df: pd.DataFrame,
    prompt_template: str,
    reference_context: str = "",
    max_students: int = 15,
    max_input_tokens: int = 15000,
    is_grading: bool = False,
    max_bytes: int = 1500,
) -> List[Dict[str, Any]]:
    """
    엑셀 DataFrame에서 미완료 학생만 추려 토큰 및 인원 상한에 맞게 배치로 분할합니다.

    각 배치 딕셔너리:
    - batch_index: 1부터 시작하는 배치 번호
    - students: [{"학번": str, "이름": str}, ...]
    - estimated_tokens: int
    - final_prompt: str
    """
    if df is None or df.empty:
        return []

    max_students = max(1, int(max_students))
    max_input_tokens = max(500, int(max_input_tokens))

    output_col = "AI_채점결과" if is_grading else "생성된_과세특"
    has_output_col = output_col in df.columns

    id_col, name_col = identify_student_columns(df)
    if not id_col:
        return []

    excluded_cols = set(METADATA_COLUMNS)
    excluded_cols.add(str(id_col))
    if name_col:
        excluded_cols.add(str(name_col))

    response_cols = [
        col for col in df.columns
        if str(col).strip() not in excluded_cols
        and str(col).strip().replace(" ", "") not in METADATA_COLUMNS
    ]

    pending_entries: List[Dict[str, str]] = []
    for idx in range(len(df)):
        if has_output_col and not is_blank_or_failed(df.iloc[idx][output_col]):
            continue

        sid = normalize_student_id_str(df.iloc[idx][id_col])
        raw_name = df.iloc[idx][name_col] if (name_col and name_col in df.columns) else ""
        sname = "" if pd.isna(raw_name) else str(raw_name).strip()
        if not sid and not sname:
            continue

        valid_answers: List[str] = []
        for col in response_cols:
            val = df.iloc[idx][col]
            if pd.notna(val) and str(val).strip():
                valid_answers.append(f"- {col}: {str(val).strip()}")

        if not valid_answers:
            continue

        answers_body = "\n".join(valid_answers)
        student_block = f"### [학생] 학번: {sid or '미상'} | 이름: {sname or '미상'}\n{answers_body}"
        pending_entries.append({
            "학번": sid,
            "이름": sname,
            "block": student_block,
        })

    if not pending_entries:
        return []

    base_template = _build_base_batch_template(
        prompt_template=prompt_template,
        reference_context=reference_context,
        max_bytes=max_bytes,
    )
    overhead_tokens = estimate_tokens(_assemble_batch_prompt(base_template, []))

    batches: List[Dict[str, Any]] = []
    current_students: List[Dict[str, str]] = []
    current_blocks: List[str] = []
    current_tokens = overhead_tokens

    def _flush_batch() -> None:
        if not current_students:
            return
        final_prompt = _assemble_batch_prompt(base_template, current_blocks)
        batches.append({
            "batch_index": len(batches) + 1,
            "students": list(current_students),
            "estimated_tokens": estimate_tokens(final_prompt),
            "final_prompt": final_prompt,
        })

    for entry in pending_entries:
        block_text = entry["block"]
        block_tokens = estimate_tokens(block_text) + 2  # 구분용 빈 줄('\n\n') 여유분

        exceeds_count = len(current_students) >= max_students
        exceeds_tokens = (current_tokens + block_tokens) > max_input_tokens

        if current_students and (exceeds_count or exceeds_tokens):
            _flush_batch()
            current_students = []
            current_blocks = []
            current_tokens = overhead_tokens

        current_students.append({"학번": entry["학번"], "이름": entry["이름"]})
        current_blocks.append(block_text)
        current_tokens += block_tokens

    _flush_batch()
    return batches


def extract_batch_items_from_response(response_text: str) -> List[Dict[str, str]]:
    """
    웹 AI 응답 텍스트에서 ```json ... ``` 배열을 추출해 표준화된 [{"학번", "이름", "결과"}, ...] 목록을 반환합니다.
    """
    parsed = extract_json_from_llm_response(response_text)
    raw_items: List[Any] = []
    if isinstance(parsed, dict):
        if isinstance(parsed.get("items"), list):
            raw_items = parsed["items"]
        else:
            raw_items = [parsed]
    elif isinstance(parsed, list):
        raw_items = parsed

    normalized_items: List[Dict[str, str]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        sid_raw = (
            item.get("학번")
            or item.get("student_id")
            or item.get("id")
            or ""
        )
        name_raw = (
            item.get("이름")
            or item.get("student_name")
            or item.get("name")
            or ""
        )
        res_raw = (
            item.get("결과")
            or item.get("result")
            or item.get("AI_채점결과")
            or item.get("생성된_과세특")
            or item.get("채점결과")
            or item.get("과세특")
            or ""
        )
        sid = normalize_student_id_str(sid_raw)
        sname = str(name_raw).strip() if name_raw is not None else ""
        result_text = str(res_raw).strip() if res_raw is not None else ""
        normalized_items.append({
            "학번": sid,
            "이름": sname,
            "결과": result_text,
        })

    if not normalized_items:
        raise ValueError("JSON에서 학생별 결과 배열([{\"학번\": ..., \"이름\": ..., \"결과\": ...}])을 찾지 못했습니다.")

    return normalized_items


def resolve_batch_output_path(
    excel_path: Union[Path, str],
    output_path: Optional[Union[Path, str]] = None,
    is_grading: bool = False,
) -> Path:
    """출력 엑셀 파일 경로를 결정합니다. 이미 접두사가 붙은 파일은 중복 접두사를 피합니다."""
    src = Path(excel_path)
    if output_path is not None and str(output_path).strip():
        return Path(output_path)

    prefix = "채점완료_" if is_grading else "과세특완료_"
    if src.name.startswith(prefix):
        return src
    return src.parent / f"{prefix}{src.name}"


def apply_batch_results_to_excel(
    excel_path: Union[Path, str],
    output_path: Optional[Union[Path, str]],
    parsed_items: Union[List[Dict[str, Any]], Dict[str, Any]],
    is_grading: bool = False,
) -> Dict[str, Any]:
    """
    파싱된 JSON 리스트에서 학번을 기준으로 엑셀 DataFrame의 행을 찾아 결과를 기록하고 안전 저장합니다.
    """
    src_path = Path(excel_path)
    if not src_path.exists():
        raise FileNotFoundError(f"대상 엑셀 파일을 찾을 수 없습니다: {src_path}")

    resolved_out = resolve_batch_output_path(src_path, output_path, is_grading=is_grading)
    target_excel = resolved_out if resolved_out.exists() else src_path

    try:
        df = pd.read_excel(target_excel)
    except PermissionError as exc:
        raise PermissionError(f"엑셀 파일이 열려 있어 읽을 수 없습니다: {target_excel.name}") from exc

    output_col = "AI_채점결과" if is_grading else "생성된_과세특"
    if output_col not in df.columns:
        df[output_col] = ""
    df[output_col] = df[output_col].apply(lambda x: "" if is_blank_or_failed(x) else str(x))

    if not is_grading:
        if "과세특_바이트" not in df.columns:
            df["과세특_바이트"] = 0
        df["과세특_바이트"] = pd.to_numeric(df["과세특_바이트"], errors="coerce").fillna(0).astype(int)

    if isinstance(parsed_items, dict):
        items_list = parsed_items.get("items", [parsed_items]) if isinstance(parsed_items.get("items"), list) else [parsed_items]
    else:
        items_list = list(parsed_items or [])

    id_col, _ = identify_student_columns(df)
    sid_to_indices: Dict[str, List[int]] = {}
    if id_col and not df.empty:
        for idx in range(len(df)):
            sid = normalize_student_id_str(df.iloc[idx][id_col])
            if sid:
                sid_to_indices.setdefault(sid, []).append(idx)

    applied_count = 0
    unmatched_ids: List[str] = []

    for item in items_list:
        if not isinstance(item, dict):
            continue
        raw_sid = item.get("학번") or item.get("student_id") or item.get("id") or ""
        sid = normalize_student_id_str(raw_sid)
        res_raw = (
            item.get("결과")
            or item.get("result")
            or item.get("AI_채점결과")
            or item.get("생성된_과세특")
            or item.get("채점결과")
            or item.get("과세특")
            or ""
        )
        res_text = "" if res_raw is None else str(res_raw).strip()

        if not sid or sid not in sid_to_indices:
            unmatched_ids.append(sid or str(raw_sid).strip() or "(학번없음)")
            continue

        for row_idx in sid_to_indices[sid]:
            df.iat[row_idx, df.columns.get_loc(output_col)] = res_text
            if not is_grading:
                df.iat[row_idx, df.columns.get_loc("과세특_바이트")] = calculate_neis_bytes(res_text)
        applied_count += 1

    saved_path = safe_save_excel(df, resolved_out)
    return {
        "applied_count": applied_count,
        "success_count": applied_count,
        "unmatched_ids": unmatched_ids,
        "output_path": str(saved_path.resolve()),
    }
