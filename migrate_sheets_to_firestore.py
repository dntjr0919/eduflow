"""
일회성 데이터 마이그레이션 스크립트: Google Sheets(eduflow_db) -> Firebase Cloud Firestore.

사용법:
    python migrate_sheets_to_firestore.py

동작:
1. config.json의 google_apps_script_url / gas_api_secret (또는 환경변수)을 사용해
   기존 Google Sheets 15개 시트 데이터를 모두 조회합니다.
2. core.firebase_client.get_firestore_client()로 Firestore에 연결합니다.
3. 각 행의 remote_id 컬럼 값을 Firestore Document ID로 고정하고,
   지정된 정수(Number) / 불리언(Boolean) 필드를 형변환하여 Batch 업서트합니다.
4. 컬렉션별 이관 건수를 터미널에 출력합니다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.firebase_client import get_firestore_client

TARGET_SHEETS: List[str] = [
    "계정_교사",
    "계정_학생",
    "계정_학부모",
    "일일_출결",
    "설정_서류규정",
    "서류_추적",
    "가정통신문_목록",
    "가정통신문_회신",
    "시간표_기본",
    "시간표_변동",
    "급식_식단표",
    "1:1_상담채팅",
    "Schedules",
    "Tasks",
    "Notifications",
]

INT_FIELDS = {
    "grade",
    "class_no",
    "student_no",
    "period",
    "due_business_days",
}

BOOL_FIELDS = {
    "is_done",
    "is_read",
    "is_active",
    "is_published",
    "teacher_confirmed",
    "consultation_enabled",
}

BATCH_SIZE = 400


def _to_int_or_none(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _to_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val or "").strip().lower()
    if s in ("1", "true", "yes", "y", "예", "on", "checked"):
        return True
    return False


def _transform_row(row: Dict[str, Any]) -> Dict[str, Any]:
    transformed: Dict[str, Any] = {}
    for key, val in row.items():
        if key in INT_FIELDS:
            transformed[key] = _to_int_or_none(val)
        elif key in BOOL_FIELDS:
            transformed[key] = _to_bool(val)
        else:
            transformed[key] = "" if val is None else val
    return transformed


def _resolve_doc_id(collection_name: str, row: Dict[str, Any], index: int) -> Tuple[str, bool]:
    raw_id = str(row.get("remote_id") or "").strip()
    if raw_id:
        safe_id = raw_id.replace("/", "_")
        return safe_id, False

    raw_payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(f"{collection_name}:{index}:{raw_payload}".encode("utf-8")).hexdigest()[:16]
    return f"auto_{digest}", True


def _load_gas_config() -> Tuple[str, str]:
    gas_url = os.environ.get("GAS_URL", "").strip()
    gas_secret = os.environ.get("GAS_API_SECRET", "").strip()

    config_path = Path(__file__).resolve().parent / "config.json"
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        api_cfg = cfg.get("api_and_integrations", {})
        if not gas_url:
            gas_url = str(api_cfg.get("google_apps_script_url") or "").strip()
        if not gas_secret:
            gas_secret = str(api_cfg.get("gas_api_secret") or "").strip()

    if not gas_url:
        raise RuntimeError(
            "Google Apps Script URL을 찾을 수 없습니다. "
            "config.json의 api_and_integrations.google_apps_script_url 또는 GAS_URL 환경변수를 확인하세요."
        )
    return gas_url, gas_secret


def _fetch_sheet_rows(gas_url: str, gas_secret: str, sheet_name: str) -> List[Dict[str, Any]]:
    params: Dict[str, str] = {"action": "list_sheet", "sheet": sheet_name}
    if gas_secret:
        params["api_secret"] = gas_secret

    resp = requests.get(gas_url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error") if isinstance(data, dict) else "Invalid response"
        raise RuntimeError(f"시트 '{sheet_name}' 조회 실패: {err}")

    rows = data.get("rows", [])
    return rows if isinstance(rows, list) else []


def migrate_all() -> Dict[str, int]:
    gas_url, gas_secret = _load_gas_config()
    db = get_firestore_client()

    results: Dict[str, int] = {}
    fallback_count = 0

    print("=" * 60)
    print("EduFlow Google Sheets -> Cloud Firestore 마이그레이션 시작")
    print("=" * 60)

    for sheet_name in TARGET_SHEETS:
        rows = _fetch_sheet_rows(gas_url, gas_secret, sheet_name)
        col_ref = db.collection(sheet_name)

        batch = db.batch()
        batch_ops = 0
        migrated = 0

        for idx, raw_row in enumerate(rows):
            if not isinstance(raw_row, dict):
                continue
            doc_id, is_fallback = _resolve_doc_id(sheet_name, raw_row, idx)
            if is_fallback:
                fallback_count += 1
            doc_data = _transform_row(raw_row)
            doc_data["remote_id"] = doc_id

            doc_ref = col_ref.document(doc_id)
            batch.set(doc_ref, doc_data, merge=True)
            batch_ops += 1
            migrated += 1

            if batch_ops >= BATCH_SIZE:
                batch.commit()
                batch = db.batch()
                batch_ops = 0

        if batch_ops > 0:
            batch.commit()

        results[sheet_name] = migrated
        print(f"  [완료] {sheet_name:<15} : {migrated:>5}건 이관")

    total = sum(results.values())
    print("-" * 60)
    print(f"총 {len(TARGET_SHEETS)}개 컬렉션 / 총 {total}건 이관 완료")
    if fallback_count:
        print(f"  (참고: remote_id가 비어 있어 자동 ID 생성된 문서 {fallback_count}건)")
    print("=" * 60)
    return results


if __name__ == "__main__":
    try:
        migrate_all()
    except Exception as exc:
        print(f"[오류] 마이그레이션 중단: {exc}", file=sys.stderr)
        sys.exit(1)
