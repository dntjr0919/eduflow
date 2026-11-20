"""
Firebase Cloud Firestore 통합 DB 동기화 서비스 (PyQt6 데스크톱 앱용).
- 연결 테스트 (test_connection)
- 전체 Pull 및 로컬 SQLite 동기화 (sync_all: Schedules / Tasks / Notifications)
- 작업 상태 / 일정 / 알림 / 시간표 / 급식 Batch 업서트
- QThread 백그라운드 실행용 FirestoreSyncWorker
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.base_worker import BaseWorker
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from core.firebase_client import get_firestore_client, has_local_credentials
from services.schedule_parser_service import (
    infer_schedule_category,
    normalize_date_str,
    normalize_time_str,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_VERCEL_API_URL = "https://m.eduflow.kro.kr/api/firestore"
DEFAULT_VERCEL_TIMEOUT = (5, 45)
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

INT_FIELDS = {
    "grade",
    "class_no",
    "target_grade",
    "target_class",
    "student_no",
    "period",
    "due_business_days",
}

BOOL_FIELDS = {
    "is_done",
    "is_read",
    "is_active",
    "is_published",
    "is_pinned",
    "requires_reply",
    "teacher_confirmed",
    "consultation_enabled",
}

FIRESTORE_BATCH_LIMIT = 400


def _is_truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val or "").strip().lower()
    return s in ("1", "true", "yes", "y", "예", "모바일", "mobile", "on", "checked")


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


def _normalize_doc_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, val in row.items():
        if key in INT_FIELDS:
            out[key] = _to_int_or_none(val)
        elif key in BOOL_FIELDS:
            out[key] = _is_truthy(val)
        else:
            out[key] = "" if val is None else val
    return out


def _resolve_doc_id(collection_name: str, row: Dict[str, Any], index: int = 0) -> str:
    raw_id = str(row.get("remote_id") or "").strip()
    if raw_id:
        return raw_id.replace("/", "_")
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(f"{collection_name}:{index}:{payload}".encode("utf-8")).hexdigest()[:16]
    return f"auto_{digest}"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_sha256_hex(val: Any) -> bool:
    return bool(re.fullmatch(r"[a-fA-F0-9]{64}", str(val or "").strip()))


def hash_password_sha256(password: Any) -> str:
    """새 비밀번호를 소문자 64자리 SHA-256 hex 해시 문자열로 변환합니다."""
    return hashlib.sha256(str(password or "").encode("utf-8")).hexdigest().lower()


def verify_account_password(stored_hash: Any, input_password: Any) -> bool:
    """
    계정_교사/계정_학생/계정_학부모의 password_hash 검증.
    초기 평문 비밀번호(stored == input)와 변경된 SHA-256 해시(stored == sha256(input))를 모두 허용합니다.
    """
    stored = str(stored_hash or "").strip()
    raw_input = str(input_password or "")
    if not stored:
        return True
    if not raw_input:
        return False
    if stored == raw_input:
        return True
    sha_hex = hash_password_sha256(raw_input)
    return stored.lower() == sha_hex.lower()


class FirestoreSyncService:
    """Firebase Cloud Firestore와 로컬 SQLite DatabaseManager 간의 양방향 동기화 서비스."""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
        firestore_client: Any = None,
    ):
        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()
        self._firestore_client = firestore_client
        self._http_session: Any = None

    def _get_http_session(self) -> Any:
        """일시적 소켓 끊김(WinError 10054) 및 502/503/504 재시도를 지원하는 공용 Session."""
        if self._http_session is None:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            session = requests.Session()
            retry_kwargs: Dict[str, Any] = {
                "total": 2,
                "connect": 2,
                "read": 2,
                "status": 2,
                "backoff_factor": 0.5,
                "status_forcelist": [502, 503, 504],
                "raise_on_status": False,
            }
            try:
                retry = Retry(allowed_methods=["POST", "GET"], **retry_kwargs)
            except TypeError:
                retry = Retry(method_whitelist=["POST", "GET"], **retry_kwargs)

            adapter = HTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._http_session = session
        return self._http_session

    def _cred_path(self) -> Optional[str]:
        p = str(self.config.get("api_and_integrations", "google_credentials_path", "") or "").strip()
        return p or None

    def get_backend_mode(self) -> str:
        """로컬 서비스 계정 JSON 키(또는 환경변수) 유무에 따라 'direct_admin' 또는 'vercel_api'를 반환합니다."""
        if self._firestore_client is not None:
            return "direct_admin"
        if has_local_credentials(self._cred_path()):
            return "direct_admin"
        return "vercel_api"

    def is_vercel_api_mode(self) -> bool:
        return self.get_backend_mode() == "vercel_api"

    def get_vercel_api_url(self) -> str:
        url = str(
            self.config.get("api_and_integrations", "vercel_api_url", DEFAULT_VERCEL_API_URL) or ""
        ).strip()
        return url or DEFAULT_VERCEL_API_URL

    def _post_vercel_api(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        import requests

        url = self.get_vercel_api_url()
        tp = self.config.get_section("teacher_profile") or {}
        body: Dict[str, Any] = {"action": action}
        if tp.get("teacher_id"):
            body["auth_teacher_id"] = str(tp.get("teacher_id") or "").strip()
        if tp.get("auth_password_hash"):
            body["auth_password_hash"] = str(tp.get("auth_password_hash") or "").strip()
        if payload:
            body.update(payload)

        headers = {
            "User-Agent": DEFAULT_BROWSER_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        is_mocked_post = getattr(requests.post, "__module__", "") != "requests.api"
        if is_mocked_post:
            try:
                resp = requests.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=DEFAULT_VERCEL_TIMEOUT,
                )
            except TypeError:
                resp = requests.post(url, json=body, timeout=DEFAULT_VERCEL_TIMEOUT)
        else:
            session = self._get_http_session()
            resp = session.post(
                url,
                json=body,
                headers=headers,
                timeout=DEFAULT_VERCEL_TIMEOUT,
            )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok or (isinstance(data, dict) and data.get("ok") is False):
            err_msg = (data.get("error") if isinstance(data, dict) else "") or f"HTTP {resp.status_code}"
            raise RuntimeError(err_msg)
        return data if isinstance(data, dict) else {"ok": True}

    def _get_client(self) -> Any:
        if self._firestore_client is None:
            self._firestore_client = get_firestore_client(self._cred_path())
        return self._firestore_client

    def _find_account_doc(self, role: str, user_id: str) -> Tuple[Any, Optional[str], Optional[Dict[str, Any]]]:
        role_key = str(role or "student").strip().lower()
        col_map = {
            "teacher": ("계정_교사", "teacher_id"),
            "student": ("계정_학생", "student_id"),
            "parent": ("계정_학부모", "parent_id"),
        }
        col_name, id_field = col_map.get(role_key, ("계정_학생", "student_id"))
        fs = self._get_client()
        col_ref = fs.collection(col_name)
        uid = str(user_id or "").strip()
        if not uid:
            return col_ref, None, None
        snap = col_ref.document(uid).get()
        if snap.exists:
            data = snap.to_dict() or {}
            data.setdefault("remote_id", snap.id)
            return col_ref, snap.id, data
        for field in (id_field, "login_id", "custom_id"):
            for d in col_ref.where(field, "==", uid).limit(1).stream():
                data = d.to_dict() or {}
                data.setdefault("remote_id", d.id)
                return col_ref, d.id, data
        return col_ref, None, None

    def authenticate_account(
        self,
        role: str,
        user_id: str,
        input_password: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        계정 로그인 검증(평문/SHA-256 모두 호환) 및 평문 저장 계정의 SHA-256 자동 업그레이드.
        Vercel API 모드에서는 서버 `/api/firestore`의 `login` 액션을 통해 검증합니다.
        """
        if self.is_vercel_api_mode():
            try:
                res = self._post_vercel_api(
                    "login",
                    {
                        "role": role,
                        "user_id": str(user_id or "").strip(),
                        "password": str(input_password or ""),
                        "password_hash": hash_password_sha256(input_password) if input_password else "",
                    },
                )
                user = res.get("user")
                if isinstance(user, dict):
                    return True, user, ""
                return False, None, str(res.get("error") or "로그인 응답이 올바르지 않습니다.")
            except Exception as exc:
                return False, None, str(exc)

        col_ref, doc_id, user_doc = self._find_account_doc(role, user_id)
        if not user_doc or not doc_id:
            return False, None, "등록되지 않은 계정입니다."
        status = str(user_doc.get("status") or "active").strip().lower()
        if status == "pending":
            return False, None, "담임 교사(또는 관리자) 승인 대기 중인 계정입니다."
        if status == "rejected":
            return False, None, "가입 신청이 반려된 계정입니다."
        if status not in ("", "active"):
            return False, None, "비활성화된 계정입니다."
        if bool(user_doc.get("custom_id_changed")) and user_doc.get("custom_id"):
            if str(user_id).strip() != str(user_doc.get("custom_id")).strip():
                return False, None, "커스텀 ID로 변경된 계정입니다. 변경한 ID로 로그인해 주세요."
        stored_pw = str(user_doc.get("password_hash") or "").strip()
        if not verify_account_password(stored_pw, input_password):
            return False, None, "비밀번호가 일치하지 않습니다."
        if stored_pw and not _is_sha256_hex(stored_pw) and input_password:
            try:
                col_ref.document(doc_id).set(
                    {"password_hash": hash_password_sha256(input_password), "updated_at": _now_str()},
                    merge=True,
                )
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"비밀번호 해시 자동 업그레이드 실패 ({doc_id}): {exc}")
        safe_user = dict(user_doc)
        safe_user.pop("password_hash", None)
        return True, safe_user, ""

    def change_account_password(
        self,
        role: str,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> Tuple[bool, str]:
        """
        현재 비밀번호 확인(평문/SHA-256 호환) 후 새 비밀번호를 SHA-256 해시로 저장합니다.
        """
        if not str(new_password or ""):
            return False, "새 비밀번호를 입력해 주세요."
        col_ref, doc_id, user_doc = self._find_account_doc(role, user_id)
        if not user_doc or not doc_id:
            return False, "계정을 찾을 수 없습니다."
        if not verify_account_password(user_doc.get("password_hash"), current_password):
            return False, "현재 비밀번호가 일치하지 않습니다."
        col_ref.document(doc_id).set(
            {
                "password_hash": hash_password_sha256(new_password),
                "updated_at": _now_str(),
            },
            merge=True,
        )
        return True, "비밀번호가 변경되었습니다."

    def get_teacher_profile(self, teacher_id: str) -> Optional[Dict[str, Any]]:
        """Firestore `계정_교사` 컬렉션에서 교사 프로필을 조회합니다."""
        tid = str(teacher_id or "").strip()
        if not tid:
            return None
        if self.is_vercel_api_mode():
            try:
                res = self._post_vercel_api("get_teacher_profile", {"teacher_id": tid})
                prof = res.get("profile")
                return prof if isinstance(prof, dict) else None
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 교사 프로필 조회 실패 ({tid}): {exc}")
                return None
        try:
            fs = self._get_client()
            snap = fs.collection("계정_교사").document(tid).get()
            if snap.exists:
                data = snap.to_dict() or {}
                data.setdefault("remote_id", snap.id)
                return data
            for doc in fs.collection("계정_교사").where("teacher_id", "==", tid).limit(1).stream():
                data = doc.to_dict() or {}
                data.setdefault("remote_id", doc.id)
                return data
            return None
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"교사 프로필 조회 실패 ({tid}): {exc}")
            return None

    def upsert_teacher_profile(self, profile: Dict[str, Any]) -> bool:
        """교사 소속 학교·학년·반 및 NEIS/컴시간 코드를 Firestore `계정_교사`에 저장합니다."""
        if not isinstance(profile, dict):
            return False
        tid = str(profile.get("teacher_id") or profile.get("remote_id") or "").strip()
        if not tid:
            return False
        neis_sc = str(profile.get("school_code") or profile.get("neis_school_code") or "").strip()
        doc_data: Dict[str, Any] = {
            "remote_id": tid,
            "teacher_id": tid,
            "name": str(profile.get("name") or tid).strip(),
            "school_name": str(profile.get("school_name") or "").strip(),
            "school_code": neis_sc,
            "grade": _to_int_or_none(profile.get("grade")),
            "class_no": _to_int_or_none(profile.get("class_no")),
            "neis_office_code": str(profile.get("neis_office_code") or "").strip(),
            "neis_school_code": neis_sc,
            "updated_at": _now_str(),
        }
        comcigan_sc = str(profile.get("comcigan_school_code") or "").strip()
        if comcigan_sc:
            doc_data["comcigan_school_code"] = comcigan_sc
        if self.is_vercel_api_mode():
            try:
                self._post_vercel_api("upsert_teacher_profile", doc_data)
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 교사 프로필 저장 실패 ({tid}): {exc}")
                return False
        try:
            fs = self._get_client()
            fs.collection("계정_교사").document(tid).set(doc_data, merge=True)
            return True
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"교사 프로필 저장 실패 ({tid}): {exc}")
            return False

    def update_teacher_comcigan_code(self, teacher_id: str, comcigan_school_code: str) -> bool:
        """교사 문서(`계정_교사`)에 5자리 컴시간 학교코드를 저장합니다."""
        tid = str(teacher_id or "").strip()
        cc = str(comcigan_school_code or "").strip()
        if not tid or not cc:
            return False
        if self.is_vercel_api_mode():
            try:
                self._post_vercel_api(
                    "upsert_teacher_profile",
                    {"teacher_id": tid, "comcigan_school_code": cc},
                )
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 컴시간 학교코드 저장 실패 ({tid}): {exc}")
                return False
        try:
            fs = self._get_client()
            fs.collection("계정_교사").document(tid).set(
                {"comcigan_school_code": cc, "updated_at": _now_str()},
                merge=True,
            )
            return True
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"컴시간 학교코드 저장 실패 ({tid}): {exc}")
            return False

    @staticmethod
    def extract_birth_6digits(student: Dict[str, Any]) -> str:
        """명렬표 학생의 extra_data/필드에서 생년월일/주민번호 앞 6자리를 추출합니다."""
        extra = student.get("extra_data")
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        sources: List[Tuple[str, Any]] = []
        if isinstance(extra, dict):
            sources.extend(extra.items())
        for k in ("birth_date", "rrn", "주민번호", "주민등록번호", "생년월일", "생일"):
            if k in student:
                sources.append((k, student[k]))

        for key, val in sources:
            k_norm = str(key or "").replace(" ", "").lower()
            if any(tok in k_norm for tok in ("주민", "생년월일", "생일", "birth", "rrn")):
                digits = re.sub(r"\D", "", str(val or ""))
                if len(digits) >= 13:
                    return digits[:6]
                if len(digits) == 8 and digits.startswith(("19", "20")):
                    return digits[2:8]
                if len(digits) >= 6:
                    return digits[:6]
        return ""

    def provision_homeroom_accounts(
        self,
        students: List[Dict[str, Any]],
        school_code: str = "",
        school_name: str = "",
    ) -> Dict[str, int]:
        """
        나이스(NEIS) 담임학급 명렬표 학생 목록을 기반으로 Firestore에 학생·학부모 초기 계정을 일괄 생성합니다.
        이미 존재하는 계정(또는 커스텀 ID/비밀번호 변경 계정)은 덮어쓰지 않고 신규 계정만 Batch Upsert합니다.
        """
        if self.is_vercel_api_mode():
            res = self._post_vercel_api(
                "provision_homeroom_accounts",
                {
                    "students": list(students or []),
                    "school_code": str(school_code or "").strip(),
                    "school_name": str(school_name or "").strip(),
                },
            )
            return {
                "students_created": int(res.get("students_created") or 0),
                "students_skipped": int(res.get("students_skipped") or 0),
                "parents_created": int(res.get("parents_created") or 0),
                "parents_skipped": int(res.get("parents_skipped") or 0),
            }

        fs = self._get_client()
        col_stu = fs.collection("계정_학생")
        col_par = fs.collection("계정_학부모")

        sc = str(school_code or "").strip()
        sn = str(school_name or "").strip()
        now = _now_str()

        # 기존 계정 ID 집합 조회(덮어쓰기 방지)
        existing_stu_ids = set()
        for d in col_stu.stream():
            existing_stu_ids.add(d.id)
            data = d.to_dict() or {}
            if data.get("student_id"):
                existing_stu_ids.add(str(data["student_id"]))

        existing_par_ids = set()
        for d in col_par.stream():
            existing_par_ids.add(d.id)
            data = d.to_dict() or {}
            if data.get("parent_id"):
                existing_par_ids.add(str(data["parent_id"]))

        pending_writes: List[Tuple[Any, Dict[str, Any]]] = []
        students_created = 0
        students_skipped = 0
        parents_created = 0
        parents_skipped = 0

        for s in students:
            sid = str(s.get("student_id") or "").strip()
            if not sid:
                continue
            name = str(s.get("name") or "").strip()
            grade_val = _to_int_or_none(s.get("grade"))
            class_val = _to_int_or_none(s.get("class_no"))
            num_val = _to_int_or_none(s.get("number") or s.get("student_no"))
            init_pw = self.extract_birth_6digits(s) or sid
            pw_hash = hash_password_sha256(init_pw)

            stu_doc_id = f"{sc}_{sid}" if sc else sid
            par_login_id = f"p{sid}"
            par_doc_id = f"{sc}_{par_login_id}" if sc else par_login_id

            if stu_doc_id in existing_stu_ids or (not sc and sid in existing_stu_ids):
                students_skipped += 1
            else:
                pending_writes.append((
                    col_stu.document(stu_doc_id),
                    {
                        "remote_id": stu_doc_id,
                        "student_id": sid,
                        "login_id": sid,
                        "name": name,
                        "grade": grade_val,
                        "class_no": class_val,
                        "student_no": num_val,
                        "school_code": sc,
                        "school_name": sn,
                        "password_hash": pw_hash,
                        "custom_id": "",
                        "custom_id_changed": False,
                        "status": "active",
                        "updated_at": now,
                    },
                ))
                existing_stu_ids.add(stu_doc_id)
                students_created += 1

            if par_doc_id in existing_par_ids or (not sc and par_login_id in existing_par_ids):
                parents_skipped += 1
            else:
                pending_writes.append((
                    col_par.document(par_doc_id),
                    {
                        "remote_id": par_doc_id,
                        "parent_id": par_login_id,
                        "login_id": par_login_id,
                        "name": f"{name} 학부모" if name else "학부모",
                        "student_id": sid,
                        "student_name": name,
                        "grade": grade_val,
                        "class_no": class_val,
                        "student_no": num_val,
                        "school_code": sc,
                        "school_name": sn,
                        "password_hash": pw_hash,
                        "custom_id": "",
                        "custom_id_changed": False,
                        "status": "active",
                        "updated_at": now,
                    },
                ))
                existing_par_ids.add(par_doc_id)
                parents_created += 1

        for start in range(0, len(pending_writes), FIRESTORE_BATCH_LIMIT):
            batch = fs.batch()
            for doc_ref, doc_data in pending_writes[start : start + FIRESTORE_BATCH_LIMIT]:
                batch.set(doc_ref, doc_data, merge=True)
            batch.commit()

        return {
            "students_created": students_created,
            "students_skipped": students_skipped,
            "parents_created": parents_created,
            "parents_skipped": parents_skipped,
        }

    def is_super_admin(self, requester_id: str) -> bool:
        """최고 관리자(유우석 또는 is_admin == True) 여부를 검증합니다."""
        rid = str(requester_id or "").strip()
        if not rid:
            return False
        if rid == "유우석":
            return True
        try:
            fs = self._get_client()
            snap = fs.collection("계정_교사").document(rid).get()
            if snap.exists and bool((snap.to_dict() or {}).get("is_admin") is True):
                return True
        except Exception:
            pass
        return False

    def get_pending_registrations(
        self,
        grade: Optional[int] = None,
        class_no: Optional[int] = None,
        school_code: str = "",
        school_name: str = "",
        requester_id: str = "",
    ) -> List[Dict[str, Any]]:
        """
        담임 학급의 가입 승인 대기(status == 'pending') 학생·학부모 목록을 조회합니다.
        최고 관리자(dntjr0919)인 경우 계정_교사의 승인 대기 신청도 함께 반환합니다.
        """
        if self.is_vercel_api_mode():
            res = self._post_vercel_api(
                "get_pending_registrations",
                {
                    "grade": grade,
                    "class_no": class_no,
                    "school_code": str(school_code or "").strip(),
                    "school_name": str(school_name or "").strip(),
                    "requester_id": str(requester_id or "").strip(),
                },
            )
            return list(res.get("registrations") or [])

        fs = self._get_client()
        sc = str(school_code or "").strip()
        sn = str(school_name or "").strip()
        results: List[Dict[str, Any]] = []
        for role_key, col_name in (("student", "계정_학생"), ("parent", "계정_학부모")):
            for doc in fs.collection(col_name).where("status", "==", "pending").stream():
                data = doc.to_dict() or {}
                data["remote_id"] = doc.id
                data["role"] = role_key
                if sc and data.get("school_code") and str(data["school_code"]).strip() != sc:
                    continue
                if sn and data.get("school_name") and str(data["school_name"]).strip() != sn:
                    continue
                if grade is not None and data.get("grade") is not None and int(data["grade"]) != int(grade):
                    continue
                if class_no is not None and data.get("class_no") is not None and int(data["class_no"]) != int(class_no):
                    continue
                results.append(data)

        if self.is_super_admin(requester_id):
            for doc in fs.collection("계정_교사").where("status", "==", "pending").stream():
                data = doc.to_dict() or {}
                data["remote_id"] = doc.id
                data["role"] = "teacher"
                results.append(data)
        return results

    def review_registration(
        self,
        role: str,
        remote_id: str,
        approve: bool = True,
        requester_id: str = "",
    ) -> bool:
        """가입 승인 대기 계정을 승인(active) 또는 반려(rejected) 처리합니다."""
        role_key = str(role or "student").strip().lower()
        if role_key == "teacher":
            if not self.is_super_admin(requester_id):
                raise PermissionError("신규 교사 가입 승인·반려는 최고 관리자(유우석)만 처리할 수 있습니다.")
            col_name = "계정_교사"
        elif role_key == "parent":
            col_name = "계정_학부모"
        else:
            col_name = "계정_학생"

        rid = str(remote_id or "").strip()
        if not rid:
            return False
        if self.is_vercel_api_mode():
            self._post_vercel_api(
                "review_registration",
                {
                    "role": role_key,
                    "remote_id": rid,
                    "approve": bool(approve),
                    "requester_id": str(requester_id or "").strip(),
                },
            )
            return True
        fs = self._get_client()
        fs.collection(col_name).document(rid).set(
            {
                "status": "active" if approve else "rejected",
                "updated_at": _now_str(),
            },
            merge=True,
        )
        return True

    def fetch_homeroom_students(
        self,
        school_code: str = "",
        school_name: str = "",
        grade: Optional[int] = None,
        class_no: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Firestore `계정_학생` 컬렉션에서 교사 소속 학교·학년·반 명렬표를 조회합니다."""
        if self.is_vercel_api_mode():
            res = self._post_vercel_api(
                "fetch_homeroom_students",
                {
                    "school_code": str(school_code or "").strip(),
                    "school_name": str(school_name or "").strip(),
                    "grade": grade,
                    "class_no": class_no,
                },
            )
            return list(res.get("students") or [])

        fs = self._get_client()
        ref: Any = fs.collection("계정_학생")
        if grade is not None and class_no is not None:
            ref = ref.where("grade", "==", int(grade)).where("class_no", "==", int(class_no))
        sc = str(school_code or "").strip()
        sn = str(school_name or "").strip()
        rows: List[Dict[str, Any]] = []
        for doc in ref.stream():
            d = doc.to_dict() or {}
            d.setdefault("remote_id", doc.id)
            status = str(d.get("status") or "active").strip().lower()
            if status in ("pending", "rejected", "inactive", "disabled"):
                continue
            doc_sc = str(d.get("school_code") or d.get("neis_school_code") or "").strip()
            doc_sn = str(d.get("school_name") or "").strip()
            if sc and doc_sc and doc_sc != sc:
                continue
            if sn and doc_sn and doc_sn != sn:
                continue
            if grade is not None and d.get("grade") is not None and _to_int_or_none(d.get("grade")) != int(grade):
                continue
            if class_no is not None and d.get("class_no") is not None and _to_int_or_none(d.get("class_no")) != int(class_no):
                continue
            rows.append(d)

        def _sort_key(item: Dict[str, Any]) -> Tuple[int, str]:
            num = _to_int_or_none(item.get("student_no") or item.get("number"))
            return (num if num is not None else 9999, str(item.get("student_id") or item.get("name") or ""))

        rows.sort(key=_sort_key)
        return rows

    def fetch_homeroom_attendance(
        self,
        grade: Optional[int] = None,
        class_no: Optional[int] = None,
        school_code: str = "",
        school_name: str = "",
    ) -> List[Dict[str, Any]]:
        """Firestore `일일_출결` 컬렉션에서 해당 학급의 모바일 출결 내역(미확인·전체 날짜 포함)을 조회합니다."""
        g_int = _to_int_or_none(grade)
        c_int = _to_int_or_none(class_no)
        sc = str(school_code or "").strip()
        sn = str(school_name or "").strip()

        if self.is_vercel_api_mode():
            res = self._post_vercel_api(
                "fetch_homeroom_attendance",
                {
                    "grade": g_int,
                    "class_no": c_int,
                    "school_code": sc,
                    "school_name": sn,
                    "include_all_dates": True,
                    "include_unconfirmed": True,
                },
            )
            rows = list(res.get("attendance") or [])
            for r in rows:
                if isinstance(r, dict) and r.get("student_id") is not None:
                    r["student_id"] = re.sub(r"^(\d+)\.0+$", r"\1", str(r.get("student_id") or "").strip())
            return rows

        fs = self._get_client()
        ref: Any = fs.collection("일일_출결")
        rows: List[Dict[str, Any]] = []
        for doc in ref.stream():
            d = doc.to_dict() or {}
            d.setdefault("remote_id", doc.id)
            doc_sc = str(d.get("school_code") or d.get("neis_school_code") or "").strip()
            doc_sn = str(d.get("school_name") or "").strip()
            if sc and doc_sc and doc_sc != sc:
                continue
            if sn and doc_sn and doc_sn != sn:
                continue
            doc_g = _to_int_or_none(d.get("grade"))
            doc_c = _to_int_or_none(d.get("class_no"))
            if g_int is not None and doc_g is not None and doc_g != g_int:
                continue
            if c_int is not None and doc_c is not None and doc_c != c_int:
                continue
            if d.get("student_id") is not None:
                d["student_id"] = re.sub(r"^(\d+)\.0+$", r"\1", str(d.get("student_id") or "").strip())
            rows.append(d)
        rows.sort(key=lambda x: str(x.get("date") or x.get("updated_at") or ""), reverse=True)
        return rows

    def upload_file(
        self,
        file_name: str,
        file_base64: str = "",
        *,
        file_bytes: Optional[bytes] = None,
        file_path: str = "",
        mime_type: str = "image/jpeg",
        folder_name: str = "EduFlow_Student_Photos",
    ) -> Dict[str, Any]:
        """
        Eduflow_mobile `/api/firestore`의 `upload_file` 액션(또는 GAS 엔드포인트)을 호출해
        Google Drive에 파일을 업로드하고 `{ok, file_id, url, download_url}`을 반환합니다.
        Direct Admin 및 Vercel API 모드 모두에서 동일하게 동작합니다.
        """
        import base64
        import mimetypes
        from pathlib import Path

        clean_name = str(file_name or "").strip()
        b64_str = str(file_base64 or "").strip()

        if not b64_str and file_bytes is not None:
            b64_str = base64.b64encode(bytes(file_bytes)).decode("ascii")

        if not b64_str and file_path:
            p = Path(str(file_path).strip())
            if not p.exists() or not p.is_file():
                return {
                    "ok": False,
                    "file_id": "",
                    "url": "",
                    "download_url": "",
                    "error": f"파일을 찾을 수 없습니다: {file_path}",
                }
            if not clean_name:
                clean_name = p.name
            if not mime_type:
                guessed, _ = mimetypes.guess_type(str(p))
                mime_type = guessed or "application/octet-stream"
            b64_str = base64.b64encode(p.read_bytes()).decode("ascii")

        if not b64_str:
            return {
                "ok": False,
                "file_id": "",
                "url": "",
                "download_url": "",
                "error": "업로드할 파일 데이터(file_base64)가 비어 있습니다.",
            }

        if not clean_name:
            clean_name = f"photo_{int(datetime.now().timestamp() * 1000)}.jpg"

        req_payload = {
            "file_name": clean_name,
            "mime_type": str(mime_type or "image/jpeg").strip() or "image/jpeg",
            "file_base64": b64_str,
            "folder_name": str(folder_name or "EduFlow_Student_Photos").strip() or "EduFlow_Student_Photos",
        }

        last_err = ""
        # 1) Vercel /api/firestore `upload_file` 액션 우선 호출 (Direct Admin 및 Vercel API 모드 모두 지원)
        try:
            res = self._post_vercel_api("upload_file", req_payload)
            file_id = str(
                res.get("file_id")
                or res.get("photo_file_id")
                or res.get("attachment_file_id")
                or ""
            ).strip()
            file_url = str(
                res.get("download_url")
                or res.get("url")
                or res.get("photo_drive_url")
                or res.get("attachment_url")
                or ""
            ).strip()
            if not file_url and file_id:
                file_url = f"https://drive.google.com/uc?export=view&id={file_id}"
            if file_url or file_id:
                return {
                    "ok": True,
                    "file_id": file_id,
                    "url": str(res.get("url") or file_url).strip(),
                    "download_url": file_url,
                }
        except Exception as exc:
            last_err = str(exc)
            self.db.add_log("WARN", "FirestoreSync", f"Vercel API upload_file 실패 ({clean_name}): {exc}")

        # 2) GAS URL이 설정된 경우 폴백 지원
        gas_url = str(
            self.config.get("api_and_integrations", "google_apps_script_url", "") or ""
        ).strip()
        if gas_url:
            try:
                sess = self._get_http_session()
                gas_payload = dict(req_payload, action="upload_notice_file")
                resp = sess.post(gas_url, json=gas_payload, timeout=DEFAULT_VERCEL_TIMEOUT)
                if resp.ok:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("ok"):
                        file_id = str(
                            data.get("file_id") or data.get("attachment_file_id") or ""
                        ).strip()
                        file_url = str(
                            data.get("download_url") or data.get("url") or data.get("attachment_url") or ""
                        ).strip()
                        if not file_url and file_id:
                            file_url = f"https://drive.google.com/uc?export=view&id={file_id}"
                        if file_url or file_id:
                            return {
                                "ok": True,
                                "file_id": file_id,
                                "url": str(data.get("url") or file_url).strip(),
                                "download_url": file_url,
                            }
            except Exception as exc:
                last_err = str(exc)
                self.db.add_log("WARN", "FirestoreSync", f"GAS 파일 업로드 실패 ({clean_name}): {exc}")

        return {
            "ok": False,
            "file_id": "",
            "url": "",
            "download_url": "",
            "error": last_err or "Google Drive 파일 업로드에 실패했습니다.",
        }

    @classmethod
    def normalize_student_face_photo_doc(
        cls,
        raw: Dict[str, Any],
        default_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        `학생_안면데이터` 컬렉션 문서 스키마로 정규화합니다:
        {remote_id: "{school_code}_{student_id}", school_code, school_name, grade, class_no,
         student_id, student_name, photo_file_id, photo_drive_url, updated_at}
        """
        meta = default_meta or {}
        school_code = str(
            raw.get("school_code")
            or raw.get("neis_school_code")
            or meta.get("school_code")
            or ""
        ).strip()
        school_name = str(
            raw.get("school_name")
            or meta.get("school_name")
            or ""
        ).strip()
        student_id = re.sub(
            r"^(\d+)\.0+$",
            r"\1",
            str(raw.get("student_id") or "").strip(),
        )
        student_name = str(
            raw.get("student_name")
            or raw.get("name")
            or ""
        ).strip()

        grade = _to_int_or_none(raw.get("grade") if raw.get("grade") is not None else meta.get("grade"))
        class_no = _to_int_or_none(raw.get("class_no") if raw.get("class_no") is not None else meta.get("class_no"))
        if (grade is None or class_no is None) and len(student_id) == 5 and student_id.isdigit():
            if grade is None:
                grade = _to_int_or_none(student_id[0])
            if class_no is None:
                class_no = _to_int_or_none(student_id[1:3])

        photo_file_id = str(
            raw.get("photo_file_id")
            or raw.get("file_id")
            or ""
        ).strip()
        photo_drive_url = str(
            raw.get("photo_drive_url")
            or raw.get("download_url")
            or raw.get("url")
            or ""
        ).strip()
        if not photo_drive_url and photo_file_id:
            photo_drive_url = f"https://drive.google.com/uc?export=view&id={photo_file_id}"

        raw_remote_id = str(raw.get("remote_id") or "").strip().replace("/", "_")
        if raw_remote_id:
            remote_id = raw_remote_id
        elif school_code and student_id:
            remote_id = f"{school_code}_{student_id}"
        else:
            remote_id = f"_{student_id}" if student_id else ""

        updated_at = str(raw.get("updated_at") or "").strip() or _now_str()
        return {
            "remote_id": remote_id,
            "school_code": school_code,
            "school_name": school_name,
            "grade": grade,
            "class_no": class_no,
            "student_id": student_id,
            "student_name": student_name,
            "photo_file_id": photo_file_id,
            "photo_drive_url": photo_drive_url,
            "updated_at": updated_at,
        }

    def upsert_student_face_photos_batch(
        self,
        items: List[Dict[str, Any]],
        *,
        school_code: str = "",
        school_name: str = "",
        grade: Optional[int] = None,
        class_no: Optional[int] = None,
        chunk_size: int = FIRESTORE_BATCH_LIMIT,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        """
        학생 얼굴 사진 메타데이터 목록을 Firestore `학생_안면데이터` 컬렉션에 일괄 저장합니다.
        Direct Admin 모드(Firestore Batch Upsert) 및 Vercel API 모드(`upsert_student_face_photos_batch`) 모두 지원합니다.
        """
        if not items:
            return False

        tp = self.config.get_section("teacher_profile") or {}
        default_meta = {
            "school_code": str(
                school_code or tp.get("school_code") or tp.get("neis_school_code") or ""
            ).strip(),
            "school_name": str(school_name or tp.get("school_name") or "").strip(),
            "grade": grade if grade is not None else _to_int_or_none(tp.get("grade")),
            "class_no": class_no if class_no is not None else _to_int_or_none(tp.get("class_no")),
        }

        normalized: List[Dict[str, Any]] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            doc = self.normalize_student_face_photo_doc(raw_item, default_meta=default_meta)
            if not doc.get("student_id") or not doc.get("remote_id"):
                continue
            normalized.append(doc)

        if not normalized:
            return False

        if self.is_vercel_api_mode():
            try:
                self._post_vercel_api(
                    "upsert_student_face_photos_batch",
                    {
                        "items": normalized,
                        "photos": normalized,
                        "rows": normalized,
                        "school_code": default_meta["school_code"],
                        "school_name": default_meta["school_name"],
                        "grade": default_meta["grade"],
                        "class_no": default_meta["class_no"],
                    },
                )
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 학생 안면데이터 일괄 저장 실패: {exc}")
                return False

        return self._batch_upsert(
            collection_name="학생_안면데이터",
            rows=normalized,
            label="학생 안면데이터 Batch Upsert",
            chunk_size=chunk_size,
            progress_callback=progress_callback,
        )

    def upload_notice_attachment(
        self,
        file_path: str,
        *,
        folder_name: str = "EduFlow_Notices",
    ) -> Dict[str, Any]:
        """가정통신문 원본 파일(PDF/HWP/HWPX/이미지)을 Google Drive(GAS 또는 연동 API)에 업로드하고 URL·파일 ID를 반환합니다."""
        import base64
        import mimetypes
        from pathlib import Path

        p = Path(str(file_path or "").strip())
        if not p.exists() or not p.is_file():
            return {"ok": False, "attachment_url": "", "attachment_file_id": "", "error": "파일을 찾을 수 없습니다."}

        raw_bytes = p.read_bytes()
        ext = p.suffix.lower()
        guessed, _ = mimetypes.guess_type(str(p))
        if ext == ".pdf":
            mime_type = "application/pdf"
        elif ext == ".hwp":
            mime_type = "application/x-hwp"
        elif ext == ".hwpx":
            mime_type = "application/haansofthwpx"
        else:
            mime_type = guessed or "application/octet-stream"

        req_payload = {
            "action": "upload_notice_file",
            "file_name": p.name,
            "mime_type": mime_type,
            "file_base64": base64.b64encode(raw_bytes).decode("ascii"),
            "folder_name": str(folder_name or "EduFlow_Notices").strip(),
        }

        gas_url = str(
            self.config.get("api_and_integrations", "google_apps_script_url", "") or ""
        ).strip()
        if gas_url:
            try:
                sess = self._get_http_session()
                resp = sess.post(gas_url, json=req_payload, timeout=DEFAULT_VERCEL_TIMEOUT)
                if resp.ok:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("ok"):
                        att_url = str(
                            data.get("attachment_url") or data.get("url") or data.get("download_url") or ""
                        ).strip()
                        att_id = str(data.get("attachment_file_id") or data.get("file_id") or "").strip()
                        if att_url:
                            return {
                                "ok": True,
                                "attachment_url": att_url,
                                "attachment_file_id": att_id,
                            }
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"GAS Google Drive 업로드 실패 ({p.name}): {exc}")

        if self.is_vercel_api_mode():
            try:
                res = self._post_vercel_api("upload_notice_file", req_payload)
                att_url = str(
                    res.get("attachment_url") or res.get("url") or res.get("download_url") or ""
                ).strip()
                att_id = str(res.get("attachment_file_id") or res.get("file_id") or "").strip()
                if att_url:
                    return {
                        "ok": True,
                        "attachment_url": att_url,
                        "attachment_file_id": att_id,
                    }
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API Google Drive 업로드 실패 ({p.name}): {exc}")

        return {
            "ok": False,
            "attachment_url": "",
            "attachment_file_id": "",
            "error": "Google Drive 업로드 엔드포인트(GAS URL 또는 연동 API)가 설정되지 않았거나 응답이 없습니다.",
        }

    def publish_mobile_notice(self, notice_data: Dict[str, Any]) -> Tuple[bool, str]:
        """가정통신문을 Firestore `가정통신문_목록`에 저장(후보 저장 또는 즉시 게시)합니다."""
        if not isinstance(notice_data, dict):
            return False, ""
        rid = str(notice_data.get("remote_id") or f"noti_doc_{int(datetime.now().timestamp() * 1000)}").strip()
        target_grade = _to_int_or_none(
            notice_data.get("target_grade")
            if notice_data.get("target_grade") is not None
            else notice_data.get("grade")
        )
        target_class = _to_int_or_none(
            notice_data.get("target_class")
            if notice_data.get("target_class") is not None
            else notice_data.get("class_no")
        )

        raw_schema = notice_data.get("reply_schema")
        if isinstance(raw_schema, str):
            try:
                raw_schema = json.loads(raw_schema)
            except Exception:
                raw_schema = []
        if not isinstance(raw_schema, list):
            raw_schema = []

        reply_schema_json = str(notice_data.get("reply_schema_json") or "").strip()
        if not reply_schema_json:
            reply_schema_json = json.dumps(raw_schema, ensure_ascii=False)

        reply_fields = notice_data.get("reply_fields")
        if not isinstance(reply_fields, list):
            reply_fields = [
                str(item.get("label") or item.get("key") or "").strip()
                for item in raw_schema
                if isinstance(item, dict) and str(item.get("label") or item.get("key") or "").strip()
            ]

        is_published = _is_truthy(notice_data.get("is_published")) if "is_published" in notice_data else True
        requires_reply = (
            _is_truthy(notice_data.get("requires_reply"))
            if "requires_reply" in notice_data
            else bool(raw_schema or reply_fields)
        )

        raw_sig_mode = str(notice_data.get("signature_mode") or "single").strip().lower()
        signature_mode = "both" if raw_sig_mode == "both" else "single"

        now_ts = _now_str()
        payload = {
            "remote_id": rid,
            "title": str(notice_data.get("title") or "").strip(),
            "category": str(notice_data.get("category") or "가정통신문").strip() or "가정통신문",
            "content": str(notice_data.get("content") or "").strip(),
            "due_date": str(notice_data.get("due_date") or "").strip(),
            "requires_reply": requires_reply,
            "signature_mode": signature_mode,
            "reply_schema": raw_schema,
            "reply_schema_json": reply_schema_json,
            "reply_fields": reply_fields,
            "attachment_url": str(notice_data.get("attachment_url") or "").strip(),
            "attachment_file_id": str(notice_data.get("attachment_file_id") or "").strip(),
            "target_grade": target_grade,
            "target_class": target_class,
            "grade": target_grade,
            "class_no": target_class,
            "school_code": str(notice_data.get("school_code") or "").strip(),
            "school_name": str(notice_data.get("school_name") or "").strip(),
            "author_name": str(notice_data.get("author_name") or "담임교사").strip(),
            "is_published": is_published,
            "created_at": str(notice_data.get("created_at") or now_ts).strip(),
            "updated_at": now_ts,
        }
        if self.is_vercel_api_mode():
            res = self._post_vercel_api("publish_mobile_notice", payload)
            return True, str(res.get("remote_id") or rid)
        fs = self._get_client()
        fs.collection("가정통신문_목록").document(rid).set(payload, merge=True)
        return True, rid

    def fetch_notice_replies(self, notice_id: str) -> List[Dict[str, Any]]:
        """특정 가정통신문(`notice_id`)의 회신 및 서명 내역을 조회합니다."""
        nid = str(notice_id or "").strip()
        if not nid:
            return []
        if self.is_vercel_api_mode():
            res = self._post_vercel_api("fetch_notice_replies", {"notice_id": nid})
            return list(res.get("replies") or [])
        fs = self._get_client()
        rows: List[Dict[str, Any]] = []
        for doc in fs.collection("가정통신문_회신").where("notice_id", "==", nid).stream():
            d = doc.to_dict() or {}
            d.setdefault("remote_id", doc.id)
            rows.append(d)
        return rows

    def send_class_notification(
        self,
        title: str,
        message: str,
        target_role: str = "all",
        grade: Optional[int] = None,
        class_no: Optional[int] = None,
        school_code: str = "",
        school_name: str = "",
        sender: str = "",
        student_id: str = "",
        channel: str = "desktop_broadcast",
        notice_id: str = "",
    ) -> Tuple[bool, str]:
        """Firestore `Notifications`에 학급 공지/긴급 알림을 등록합니다."""
        rid = f"noti_{int(datetime.now().timestamp() * 1000)}"
        int_grade = _to_int_or_none(grade)
        int_class = _to_int_or_none(class_no)
        payload = {
            "remote_id": rid,
            "title": str(title or "").strip(),
            "message": str(message or "").strip(),
            "channel": str(channel or "desktop_broadcast").strip(),
            "target_role": str(target_role or "all").strip().lower(),
            "sender": str(sender or "담임교사").strip(),
            "grade": int_grade,
            "class_no": int_class,
            "target_grade": int_grade,
            "target_class": int_class,
            "school_code": str(school_code or "").strip(),
            "school_name": str(school_name or "").strip(),
            "student_id": str(student_id or "").strip(),
            "is_read": False,
            "created_at": _now_str(),
            "updated_at": _now_str(),
        }
        if str(notice_id or "").strip():
            payload["notice_id"] = str(notice_id).strip()
        if self.is_vercel_api_mode():
            res = self._post_vercel_api("send_class_notification", payload)
            return True, str(res.get("remote_id") or rid)
        fs = self._get_client()
        fs.collection("Notifications").document(rid).set(payload, merge=True)
        return True, rid

    def upsert_board_notice(self, notice: Dict[str, Any]) -> Tuple[bool, str]:
        """Firestore `학급_공지게시판` 컬렉션에 학급 공지 게시물을 등록·수정합니다."""
        row = dict(notice or {})
        raw_id = str(row.get("remote_id") or "").strip().replace("/", "_")
        rid = raw_id or f"board_{int(datetime.now().timestamp() * 1000)}"
        src_type = str(row.get("source_type") or "teacher").strip().lower()
        if src_type not in ("schoolbell", "kakao", "teacher"):
            src_type = "teacher"
        now_str = _now_str()
        title = str(row.get("title") or "").strip()
        content = str(row.get("content") or "").strip()
        summary = str(row.get("summary") or "").strip() or content[:180].replace("\n", " ").strip()
        payload: Dict[str, Any] = {
            "remote_id": rid,
            "title": title,
            "summary": summary,
            "content": content or summary,
            "source_type": src_type,
            "source_label": str(row.get("source_label") or "").strip(),
            "source_url": str(row.get("source_url") or "").strip(),
            "target_grade": _to_int_or_none(row.get("target_grade") if "target_grade" in row else row.get("grade")),
            "target_class": _to_int_or_none(row.get("target_class") if "target_class" in row else row.get("class_no")),
            "school_code": str(row.get("school_code") or "").strip(),
            "school_name": str(row.get("school_name") or "").strip(),
            "author_name": str(row.get("author_name") or "담임교사").strip(),
            "is_pinned": _is_truthy(row.get("is_pinned")),
            "published_at": str(row.get("published_at") or "").strip() or now_str,
            "updated_at": str(row.get("updated_at") or "").strip() or now_str,
        }
        if self.is_vercel_api_mode():
            res = self._post_vercel_api("upsert_board_notice", payload)
            return True, str(res.get("remote_id") or rid)
        fs = self._get_client()
        fs.collection("학급_공지게시판").document(rid).set(payload, merge=True)
        return True, rid

    def fetch_sent_notifications(
        self,
        grade: Optional[int] = None,
        class_no: Optional[int] = None,
        limit: int = 50,
        school_code: str = "",
        school_name: str = "",
    ) -> List[Dict[str, Any]]:
        """Firestore `Notifications` 발송 이력을 조회합니다."""
        if self.is_vercel_api_mode():
            res = self._post_vercel_api(
                "fetch_sent_notifications",
                {
                    "grade": grade,
                    "class_no": class_no,
                    "limit": limit,
                    "school_code": str(school_code or "").strip(),
                    "school_name": str(school_name or "").strip(),
                },
            )
            return list(res.get("notifications") or [])
        fs = self._get_client()
        sc = str(school_code or "").strip()
        sn = str(school_name or "").strip()
        rows: List[Dict[str, Any]] = []
        for doc in fs.collection("Notifications").limit(limit).stream():
            d = doc.to_dict() or {}
            d.setdefault("remote_id", doc.id)
            doc_sc = str(d.get("school_code") or d.get("neis_school_code") or "").strip()
            doc_sn = str(d.get("school_name") or "").strip()
            if sc and doc_sc and doc_sc != sc:
                continue
            if sn and doc_sn and doc_sn != sn:
                continue
            if grade is not None and d.get("grade") is not None and int(d["grade"]) != int(grade):
                continue
            if class_no is not None and d.get("class_no") is not None and int(d["class_no"]) != int(class_no):
                continue
            rows.append(d)
        rows.sort(key=lambda x: str(x.get("created_at") or x.get("updated_at") or ""), reverse=True)
        return rows

    def fetch_chat_history(
        self,
        grade: Optional[int] = None,
        class_no: Optional[int] = None,
        student_id: str = "",
        school_code: str = "",
        school_name: str = "",
    ) -> List[Dict[str, Any]]:
        """Firestore `1:1_상담채팅` 대화 내역을 조회합니다."""
        if self.is_vercel_api_mode():
            res = self._post_vercel_api(
                "fetch_chat_history",
                {
                    "grade": grade,
                    "class_no": class_no,
                    "student_id": str(student_id or "").strip(),
                    "school_code": str(school_code or "").strip(),
                    "school_name": str(school_name or "").strip(),
                },
            )
            return list(res.get("messages") or [])
        fs = self._get_client()
        ref: Any = fs.collection("1:1_상담채팅")
        sid = str(student_id or "").strip()
        if sid:
            ref = ref.where("room_id", "==", f"ROOM-{sid}")
        elif grade is not None and class_no is not None:
            ref = ref.where("grade", "==", int(grade)).where("class_no", "==", int(class_no))
        sc = str(school_code or "").strip()
        sn = str(school_name or "").strip()
        rows: List[Dict[str, Any]] = []
        for doc in ref.stream():
            d = doc.to_dict() or {}
            d.setdefault("remote_id", doc.id)
            doc_sc = str(d.get("school_code") or d.get("neis_school_code") or "").strip()
            doc_sn = str(d.get("school_name") or "").strip()
            if sc and doc_sc and doc_sc != sc:
                continue
            if sn and doc_sn and doc_sn != sn:
                continue
            rows.append(d)
        rows.sort(key=lambda x: str(x.get("created_at") or ""))
        return rows

    def test_connection(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        if self.is_vercel_api_mode():
            try:
                res = self._post_vercel_api("health_check", {})
                url = self.get_vercel_api_url()
                return True, f"연결 성공: Vercel API 모드 ({url})", res
            except Exception as exc:
                return False, f"Vercel API 연결 테스트 실패: {exc}", {}
        try:
            fs = self._get_client()
            counts = {
                "schedules": sum(1 for _ in fs.collection("Schedules").limit(500).stream()),
                "tasks": sum(1 for _ in fs.collection("Tasks").limit(500).stream()),
                "notifications": sum(1 for _ in fs.collection("Notifications").limit(500).stream()),
            }
            data = {
                "ok": True,
                "backend": "firestore",
                "counts": counts,
            }
            msg = (
                "연결 성공: Cloud Firestore "
                f"(일정 {counts['schedules']}건 · 작업 {counts['tasks']}건 · "
                f"알림 {counts['notifications']}건)"
            )
            return True, msg, data
        except Exception as exc:
            return False, f"Firestore 연결 테스트 실패: {exc}", {}

    def sync_from_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Firestore 조회 결과(또는 테스트 페이로드)를 로컬 SQLite와 동기화합니다.
        이미 acknowledged / done 으로 기록된 remote_id는 pending으로 되돌리지 않으며
        예정된 작업 목록(pending_mobile_tasks)에서도 자동 제외됩니다.
        """
        schedules_added = 0
        schedules_updated = 0
        todos_added = 0
        todos_updated = 0
        mobile_tasks_added = 0
        mobile_tasks_updated = 0
        notifications_added = 0

        # 1. Schedules 동기화
        for raw_sch in payload.get("schedules", []) or []:
            if not isinstance(raw_sch, dict):
                continue
            title = str(raw_sch.get("title", "")).strip()
            if not title:
                continue
            rid = str(raw_sch.get("remote_id", "")).strip()
            s_date, e_date = normalize_date_str(raw_sch.get("start_date", ""))
            if not s_date:
                continue
            if raw_sch.get("end_date"):
                end_s, end_e = normalize_date_str(raw_sch.get("end_date"))
                if (end_e or end_s) and (end_e or end_s) >= s_date:
                    e_date = end_e or end_s
            s_time = normalize_time_str(raw_sch.get("start_time", ""))
            category = infer_schedule_category(title, str(raw_sch.get("category", "")))
            reminder_type = str(raw_sch.get("reminder_type", "none") or "none").strip()
            memo = str(raw_sch.get("memo", "") or "").strip()

            _, created = self.db.upsert_remote_schedule(
                remote_id=rid,
                title=title,
                category=category,
                start_date=s_date,
                end_date=e_date or s_date,
                start_time=s_time,
                reminder_type=reminder_type,
                memo=memo,
            )
            if created:
                schedules_added += 1
            else:
                schedules_updated += 1

        # 2. Tasks 동기화 (모바일 예약 작업 vs 일반 할일)
        for raw_task in payload.get("tasks", []) or []:
            if not isinstance(raw_task, dict):
                continue
            title = str(raw_task.get("title", "")).strip()
            if not title:
                continue
            rid = str(raw_task.get("remote_id", "")).strip()
            task_type = str(raw_task.get("task_type", "general") or "general").strip()
            is_mobile_flag = raw_task.get("is_mobile_task")
            if is_mobile_flag is None:
                is_mobile = task_type.lower() != "todo"
            else:
                is_mobile = _is_truthy(is_mobile_flag)

            if is_mobile:
                if not rid:
                    rid = f"mob_{abs(hash((title, raw_task.get('scheduled_time', ''), raw_task.get('target_info', ''))))}"
                remote_status = str(raw_task.get("exec_status", "pending") or "pending").strip().lower()
                existing_state = self.db.get_sync_state(rid)
                if existing_state is None and remote_status in ("acknowledged", "done"):
                    tid, created = self.db.upsert_remote_mobile_task(
                        remote_id=rid,
                        title=title,
                        task_type=task_type,
                        scheduled_time=str(raw_task.get("scheduled_time", "") or "").strip(),
                        target_info=str(raw_task.get("target_info", "") or "").strip(),
                        action_payload=raw_task.get("action_payload", "{}"),
                        memo=str(raw_task.get("memo", "") or "").strip(),
                    )
                    if tid > 0:
                        if remote_status == "done":
                            self.db.complete_mobile_task(tid)
                        else:
                            self.db.acknowledge_mobile_task(tid)
                    continue

                _, created = self.db.upsert_remote_mobile_task(
                    remote_id=rid,
                    title=title,
                    task_type=task_type,
                    scheduled_time=str(raw_task.get("scheduled_time", "") or "").strip(),
                    target_info=str(raw_task.get("target_info", "") or "").strip(),
                    action_payload=raw_task.get("action_payload", "{}"),
                    memo=str(raw_task.get("memo", "") or "").strip(),
                )
                if created:
                    mobile_tasks_added += 1
                else:
                    mobile_tasks_updated += 1
            else:
                due_s, _ = normalize_date_str(raw_task.get("due_date", ""))
                prio = str(raw_task.get("priority", "normal") or "normal").strip().lower()
                memo = str(raw_task.get("memo", "") or "").strip()
                is_done = _is_truthy(raw_task.get("is_done", 0))
                _, created = self.db.upsert_remote_todo(
                    remote_id=rid,
                    title=title,
                    due_date=due_s,
                    priority=prio,
                    memo=memo,
                    is_done=is_done,
                )
                if created:
                    todos_added += 1
                else:
                    todos_updated += 1

        # 3. Notifications 동기화
        for raw_noti in payload.get("notifications", []) or []:
            if not isinstance(raw_noti, dict):
                continue
            title = str(raw_noti.get("title", "")).strip()
            msg = str(raw_noti.get("message", "")).strip()
            if not title and not msg:
                continue
            rid = str(raw_noti.get("remote_id", "")).strip() or None
            channel = str(raw_noti.get("channel", "firestore") or "firestore").strip()
            sender = str(raw_noti.get("sender", "") or "").strip()
            remote_read = 1 if _is_truthy(raw_noti.get("is_read", 0)) else 0
            _, created = self.db.add_notification(
                title=title or "외부 알림",
                message=msg,
                channel=channel,
                sender=sender,
                remote_id=rid,
                is_read=remote_read,
            )
            if created:
                notifications_added += 1

        pending_mobile = self.db.get_mobile_tasks(pending_only=True)
        return {
            "ok": True,
            "schedules_added": schedules_added,
            "schedules_updated": schedules_updated,
            "todos_added": todos_added,
            "todos_updated": todos_updated,
            "mobile_tasks_added": mobile_tasks_added,
            "mobile_tasks_updated": mobile_tasks_updated,
            "notifications_added": notifications_added,
            "pending_mobile_tasks": pending_mobile,
            "server_time": str(payload.get("server_time") or _now_str()),
        }

    def fetch_collection_rows(self, collection_name: str, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        """지정한 Firestore 컬렉션의 문서 목록을 조회합니다."""
        col_name = str(collection_name or "").strip()
        if not col_name:
            return []
        try:
            fs = self._get_client()
            rows: List[Dict[str, Any]] = []
            for doc in fs.collection(col_name).stream():
                data = doc.to_dict() or {}
                if not data.get("remote_id"):
                    data["remote_id"] = doc.id
                rows.append(data)
            return rows
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"컬렉션 조회 실패 ({col_name}): {exc}")
            return []

    def fetch_sheet_rows(self, sheet_name: str, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        """기존 호출부 호환용 래퍼: Firestore 컬렉션 행 목록을 조회합니다."""
        return self.fetch_collection_rows(sheet_name)

    def sync_all(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            schedules = self.fetch_collection_rows("Schedules")
            tasks = self.fetch_collection_rows("Tasks")
            notifications = self.fetch_collection_rows("Notifications")
            payload = {
                "ok": True,
                "schedules": schedules,
                "tasks": tasks,
                "notifications": notifications,
                "server_time": _now_str(),
            }
            summary = self.sync_from_payload(payload)
            self.db.add_log(
                "INFO",
                "FirestoreSync",
                (
                    f"동기화 완료: 일정 +{summary['schedules_added']}, "
                    f"할일 +{summary['todos_added']}, "
                    f"모바일 예약 +{summary['mobile_tasks_added']}, "
                    f"외부 알림 +{summary['notifications_added']}"
                ),
            )
            return summary
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"전체 동기화 실패: {exc}")
            return {
                "ok": False,
                "skipped": False,
                "error": str(exc),
                "pending_mobile_tasks": self.db.get_mobile_tasks(pending_only=True),
            }

    def push_task_status(
        self,
        remote_id: str,
        exec_status: str,
        is_done: bool = False,
        memo: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        rid = str(remote_id or "").strip().replace("/", "_")
        if not rid:
            return False
        update_data: Dict[str, Any] = {
            "remote_id": rid,
            "exec_status": str(exec_status or "").strip(),
            "is_done": bool(is_done),
            "updated_at": _now_str(),
        }
        if memo is not None:
            update_data["memo"] = str(memo)
        try:
            fs = self._get_client()
            fs.collection("Tasks").document(rid).set(update_data, merge=True)
            return True
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"상태 Push 실패 ({rid}): {exc}")
            return False

    def _batch_upsert(
        self,
        collection_name: str,
        rows: List[Dict[str, Any]],
        label: str = "",
        chunk_size: int = FIRESTORE_BATCH_LIMIT,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        if not rows:
            return False
        size = max(1, min(int(chunk_size or FIRESTORE_BATCH_LIMIT), 450))
        total_batches = (len(rows) + size - 1) // size
        try:
            fs = self._get_client()
            col_ref = fs.collection(collection_name)

            for batch_idx, start in enumerate(range(0, len(rows), size), start=1):
                if progress_callback is not None:
                    try:
                        progress_callback(batch_idx, total_batches)
                    except Exception:
                        pass
                chunk = rows[start : start + size]
                batch = fs.batch()
                for offset, raw_row in enumerate(chunk):
                    if not isinstance(raw_row, dict):
                        continue
                    doc_id = _resolve_doc_id(collection_name, raw_row, start + offset)
                    doc_data = _normalize_doc_fields(raw_row)
                    doc_data["remote_id"] = doc_id
                    if not doc_data.get("updated_at"):
                        doc_data["updated_at"] = _now_str()
                    batch.set(col_ref.document(doc_id), doc_data, merge=True)
                batch.commit()

            return True
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"{label or collection_name} Batch 업서트 실패: {exc}")
            return False

    def upsert_timetable_base(
        self,
        rows: List[Dict[str, Any]],
        *args: Any,
        chunk_size: int = FIRESTORE_BATCH_LIMIT,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        **kwargs: Any,
    ) -> bool:
        """컴시간 알리미 등에서 파싱한 기본 시간표를 Firestore `시간표_기본` 컬렉션으로 Batch Upsert합니다."""
        if self.is_vercel_api_mode():
            if not rows:
                return False
            try:
                self._post_vercel_api("sync_timetables_batch", {"base_rows": list(rows)})
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 기본 시간표 Upsert 실패: {exc}")
                return False
        return self._batch_upsert(
            collection_name="시간표_기본",
            rows=rows,
            label="기본 시간표 Batch Upsert",
            chunk_size=chunk_size,
            progress_callback=progress_callback,
        )

    def push_timetable_changes(
        self,
        rows: List[Dict[str, Any]],
        *args: Any,
        chunk_size: int = FIRESTORE_BATCH_LIMIT,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        **kwargs: Any,
    ) -> bool:
        """컴시간 알리미 등에서 파싱한 시간표 변동 내역을 Firestore `시간표_변동` 컬렉션으로 Batch Upsert합니다."""
        if self.is_vercel_api_mode():
            if not rows:
                return False
            try:
                self._post_vercel_api("sync_timetables_batch", {"change_rows": list(rows)})
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 시간표 변동 Push 실패: {exc}")
                return False
        return self._batch_upsert(
            collection_name="시간표_변동",
            rows=rows,
            label="시간표 변동 Batch Upsert",
            chunk_size=chunk_size,
            progress_callback=progress_callback,
        )

    def delete_timetable_docs(
        self,
        collection_name: str,
        remote_ids: List[str],
    ) -> bool:
        """더 이상 존재하지 않는 시간표 문서(`remote_id` 목록)를 삭제합니다."""
        ids = [str(r).strip().replace("/", "_") for r in (remote_ids or []) if str(r).strip()]
        if not ids:
            return True
        if collection_name not in ("시간표_기본", "시간표_변동"):
            return False
        if self.is_vercel_api_mode():
            key = "delete_base_ids" if collection_name == "시간표_기본" else "delete_change_ids"
            try:
                self._post_vercel_api("sync_timetables_batch", {key: ids})
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 시간표 삭제 실패 ({collection_name}): {exc}")
                return False
        try:
            fs = self._get_client()
            col_ref = fs.collection(collection_name)
            for start in range(0, len(ids), FIRESTORE_BATCH_LIMIT):
                batch = fs.batch()
                for rid in ids[start : start + FIRESTORE_BATCH_LIMIT]:
                    batch.delete(col_ref.document(rid))
                batch.commit()
            return True
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"{collection_name} Batch 삭제 실패: {exc}")
            return False

    def has_timetable_base_docs(
        self,
        school_code: str = "",
        comcigan_school_code: str = "",
        school_name: str = "",
    ) -> Optional[bool]:
        """Firestore `시간표_기본`에 해당 학교 문서가 최소 1건 이상 존재하는지 1건 조회로 확인합니다."""
        sc = str(school_code or "").strip()
        cc = str(comcigan_school_code or "").strip()
        sn = str(school_name or "").strip()
        if self.is_vercel_api_mode():
            try:
                res = self._post_vercel_api(
                    "check_timetable_exists",
                    {"school_code": sc, "comcigan_school_code": cc, "school_name": sn},
                )
                return bool(res.get("exists"))
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 시간표 존재 확인 실패: {exc}")
                return None
        try:
            fs = self._get_client()
            col_ref = fs.collection("시간표_기본")
            for field, val in (("school_code", sc), ("comcigan_school_code", cc), ("school_name", sn)):
                if val:
                    for _ in col_ref.where(field, "==", val).limit(1).stream():
                        return True
            if not (sc or cc or sn):
                for _ in col_ref.limit(1).stream():
                    return True
            return False
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"시간표 존재 확인 실패: {exc}")
            return None

    def upsert_meal_plans(
        self,
        rows: List[Dict[str, Any]],
        chunk_size: int = FIRESTORE_BATCH_LIMIT,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        """NEIS 급식 식단표 데이터를 Firestore `급식_식단표` 컬렉션으로 Batch Upsert합니다."""
        if self.is_vercel_api_mode():
            if not rows:
                return False
            try:
                self._post_vercel_api("sync_meals_batch", {"meal_rows": list(rows)})
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 급식 식단표 Upsert 실패: {exc}")
                return False
        return self._batch_upsert(
            collection_name="급식_식단표",
            rows=rows,
            label="NEIS 급식 식단표 Batch Upsert",
            chunk_size=chunk_size,
            progress_callback=progress_callback,
        )

    def sync_neis_meals(
        self,
        office_code: str,
        school_code: str,
        from_ymd: str,
        to_ymd: str,
        api_key: str = "",
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[bool, int]:
        """NEIS Open API(mealServiceDietInfo)에서 급식을 조회하여 `급식_식단표`에 Batch Upsert합니다."""
        import requests

        office = str(office_code or "").strip()
        school = str(school_code or "").strip()
        if not office or not school:
            return False, 0

        params: Dict[str, str] = {
            "Type": "json",
            "pIndex": "1",
            "pSize": "100",
            "ATPT_OFCDC_SC_CODE": office,
            "SD_SCHUL_CODE": school,
            "MLSV_FROM_YMD": str(from_ymd or "").replace("-", ""),
            "MLSV_TO_YMD": str(to_ymd or "").replace("-", ""),
        }
        if api_key:
            params["KEY"] = str(api_key).strip()

        try:
            resp = requests.get("https://open.neis.go.kr/hub/mealServiceDietInfo", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            blocks = data.get("mealServiceDietInfo", [])
            raw_rows: List[Dict[str, Any]] = []
            for blk in blocks:
                if isinstance(blk, dict) and "row" in blk:
                    raw_rows = blk["row"]
                    break

            meal_rows: List[Dict[str, Any]] = []
            for item in raw_rows:
                ymd = str(item.get("MLSV_YMD") or "").strip()
                if len(ymd) == 8:
                    date_str = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
                else:
                    date_str = ymd
                meal_type = str(item.get("MMEAL_SC_NM") or "중식").strip()
                dish_raw = str(item.get("DDISH_NM") or "").replace("<br/>", "\n")
                dish_clean = re.sub(r"\s*\([0-9.]+\)", "", dish_raw).strip()
                cal = str(item.get("CAL_INFO") or "").strip()
                rid = f"meal_{ymd}_{meal_type}"
                meal_rows.append(
                    {
                        "remote_id": rid,
                        "date": date_str,
                        "meal_type": meal_type,
                        "menu": dish_clean,
                        "calories": cal,
                        "school_code": school,
                        "office_code": office,
                        "updated_at": _now_str(),
                    }
                )
            if not meal_rows:
                return True, 0
            ok = self.upsert_meal_plans(meal_rows, progress_callback=progress_callback)
            return ok, len(meal_rows) if ok else 0
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"NEIS 급식 동기화 실패: {exc}")
            return False, 0

    def _default_teacher_meta(self) -> Dict[str, str]:
        tp = self.config.get_section("teacher_profile") or {}
        return {
            "school_code": str(tp.get("school_code") or tp.get("neis_school_code") or "").strip(),
            "teacher_id": str(tp.get("teacher_id") or "").strip(),
            "teacher_name": str(tp.get("name") or tp.get("teacher_id") or "").strip(),
        }

    @staticmethod
    def _normalize_roster_student_entry(student: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
        serial_raw = student.get("serial_no")
        if serial_raw is None:
            serial_raw = student.get("seq_no")
        if serial_raw is None:
            serial_raw = index + 1
        return {
            "student_id": str(student.get("student_id") or "").strip(),
            "name": str(student.get("name") or "").strip(),
            "number": _to_int_or_none(student.get("number")),
            "serial_no": _to_int_or_none(serial_raw),
            "grade": _to_int_or_none(student.get("grade")),
            "class_no": _to_int_or_none(student.get("class_no")),
        }

    @classmethod
    def normalize_subject_roster_doc(
        cls,
        raw: Dict[str, Any],
        default_meta: Optional[Dict[str, str]] = None,
        index: int = 0,
    ) -> Dict[str, Any]:
        meta = default_meta or {}
        school_code = str(raw.get("school_code") or meta.get("school_code") or "").strip()
        teacher_id = str(raw.get("teacher_id") or meta.get("teacher_id") or "").strip()
        teacher_name = str(raw.get("teacher_name") or meta.get("teacher_name") or teacher_id).strip()
        roster_type = str(raw.get("roster_type") or raw.get("group_type") or "subject").strip().lower()
        if roster_type not in ("subject", "club"):
            roster_type = "subject"
        subject = str(raw.get("subject") or raw.get("group_name") or "").strip()
        class_label = str(raw.get("class_label") or raw.get("extra_info") or "").strip()

        raw_students = raw.get("students") if isinstance(raw.get("students"), list) else []
        students = [
            cls._normalize_roster_student_entry(s, idx)
            for idx, s in enumerate(raw_students)
            if isinstance(s, dict) and (str(s.get("name") or "").strip() or str(s.get("student_id") or "").strip())
        ]

        grade = _to_int_or_none(raw.get("grade"))
        class_no = _to_int_or_none(raw.get("class_no"))
        if (grade is None or class_no is None) and class_label:
            m = re.search(r"(\d+)\s*학년\s*(\d+)\s*반", class_label) or re.search(r"(\d+)\s*-\s*(\d+)", class_label)
            if m:
                if grade is None:
                    grade = _to_int_or_none(m.group(1))
                if class_no is None:
                    class_no = _to_int_or_none(m.group(2))
        if grade is None and students:
            grades = {s["grade"] for s in students if s.get("grade") is not None}
            if len(grades) == 1:
                grade = next(iter(grades))
        if class_no is None and students:
            classes = {s["class_no"] for s in students if s.get("class_no") is not None}
            if len(classes) == 1:
                class_no = next(iter(classes))

        remote_id = str(raw.get("remote_id") or "").strip().replace("/", "_")
        if not remote_id:
            key_src = f"{school_code}|{teacher_id}|{roster_type}|{subject}|{class_label}"
            digest = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:12]
            remote_id = f"sr_{roster_type}_{digest}"

        updated_at = str(raw.get("updated_at") or "").strip() or _now_str()
        return {
            "remote_id": remote_id,
            "school_code": school_code,
            "teacher_id": teacher_id,
            "teacher_name": teacher_name,
            "roster_type": roster_type,
            "subject": subject,
            "class_label": class_label,
            "grade": grade,
            "class_no": class_no,
            "students": students,
            "updated_at": updated_at,
        }

    @classmethod
    def build_subject_roster_payload(
        cls,
        group: Dict[str, Any],
        students: List[Dict[str, Any]],
        school_code: str = "",
        teacher_id: str = "",
        teacher_name: str = "",
    ) -> Dict[str, Any]:
        """로컬 `roster_groups` + `roster_students` 레코드를 `교과_명렬표` 동기화 페이로드로 변환합니다."""
        raw = {
            "school_code": school_code,
            "teacher_id": teacher_id,
            "teacher_name": teacher_name,
            "roster_type": str(group.get("group_type") or "subject").strip().lower(),
            "subject": str(group.get("group_name") or "").strip(),
            "class_label": str(group.get("extra_info") or "").strip(),
            "students": list(students or []),
        }
        return cls.normalize_subject_roster_doc(raw)

    def sync_subject_rosters_batch(
        self,
        rosters: List[Dict[str, Any]],
        chunk_size: int = FIRESTORE_BATCH_LIMIT,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        """교과(`subject`) 및 동아리(`club`) 명렬표를 Firestore `교과_명렬표` 컬렉션으로 Batch Upsert합니다."""
        if not rosters:
            return False
        meta = self._default_teacher_meta()
        normalized = [
            self.normalize_subject_roster_doc(r, default_meta=meta, index=idx)
            for idx, r in enumerate(rosters)
            if isinstance(r, dict)
        ]
        if not normalized:
            return False
        if self.is_vercel_api_mode():
            try:
                self._post_vercel_api("sync_subject_rosters_batch", {"rosters": normalized})
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 교과·동아리 명렬표 동기화 실패: {exc}")
                return False
        return self._batch_upsert(
            collection_name="교과_명렬표",
            rows=normalized,
            label="교과·동아리 명렬표 Batch Upsert",
            chunk_size=chunk_size,
            progress_callback=progress_callback,
        )

    @staticmethod
    def normalize_lesson_progress_doc(
        raw: Dict[str, Any],
        default_meta: Optional[Dict[str, str]] = None,
        index: int = 0,
    ) -> Dict[str, Any]:
        meta = default_meta or {}
        school_code = str(raw.get("school_code") or meta.get("school_code") or "").strip()
        teacher_id = str(raw.get("teacher_id") or meta.get("teacher_id") or "").strip()

        raw_date = str(raw.get("date") or "").strip()
        if raw_date:
            s_date, _ = normalize_date_str(raw_date)
            date_str = s_date or raw_date
        else:
            date_str = ""

        period = _to_int_or_none(raw.get("period"))
        subject = str(raw.get("subject") or "").strip()
        class_label = str(raw.get("class_label") or "").strip()
        roster_key = str(raw.get("roster_key") or "").strip()
        unit_title = str(raw.get("unit_title") or "").strip()
        lesson_no = str(raw.get("lesson_no") if raw.get("lesson_no") is not None else "").strip()
        content = str(raw.get("content") or "").strip()
        next_plan = str(raw.get("next_plan") or "").strip()
        memo = str(raw.get("memo") or "").strip()
        updated_at = str(raw.get("updated_at") or "").strip() or _now_str()

        remote_id = str(raw.get("remote_id") or "").strip().replace("/", "_")
        if not remote_id:
            key_src = f"{school_code}|{teacher_id}|{roster_key}|{subject}|{class_label}|{unit_title}|{date_str}|{period}|{lesson_no}|{index}"
            digest = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:12]
            remote_id = f"lp_{digest}"

        doc: Dict[str, Any] = {
            "remote_id": remote_id,
            "school_code": school_code,
            "teacher_id": teacher_id,
            "date": date_str,
            "period": period,
            "subject": subject,
            "class_label": class_label,
            "roster_key": roster_key,
            "unit_title": unit_title,
            "lesson_no": lesson_no,
            "content": content,
            "next_plan": next_plan,
            "memo": memo,
            "updated_at": updated_at,
        }
        if raw.get("status") is not None:
            doc["status"] = str(raw.get("status") or "").strip()
        if raw.get("rel_path") is not None:
            doc["rel_path"] = str(raw.get("rel_path") or "").strip()
        return doc

    def sync_lesson_progress_batch(
        self,
        items: List[Dict[str, Any]],
        chunk_size: int = FIRESTORE_BATCH_LIMIT,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        """수업 진도 관리 기록을 Firestore `수업_진도` 컬렉션으로 Batch Upsert합니다."""
        if not items:
            return False
        meta = self._default_teacher_meta()
        normalized = [
            self.normalize_lesson_progress_doc(item, default_meta=meta, index=idx)
            for idx, item in enumerate(items)
            if isinstance(item, dict)
        ]
        if not normalized:
            return False
        if self.is_vercel_api_mode():
            try:
                self._post_vercel_api("sync_lesson_progress_batch", {"items": normalized})
                return True
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 수업 진도 동기화 실패: {exc}")
                return False
        return self._batch_upsert(
            collection_name="수업_진도",
            rows=normalized,
            label="수업 진도 Batch Upsert",
            chunk_size=chunk_size,
            progress_callback=progress_callback,
        )

    def fetch_lesson_progress(
        self,
        teacher_id: str = "",
        school_code: str = "",
        roster_key: str = "",
        subject: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> List[Dict[str, Any]]:
        """Firestore `수업_진도` 컬렉션에서 진도 기록을 조회합니다."""
        meta = self._default_teacher_meta()
        tid = str(teacher_id or meta.get("teacher_id") or "").strip()
        sc = str(school_code or meta.get("school_code") or "").strip()
        rk = str(roster_key or "").strip()
        subj = str(subject or "").strip()
        d_from = str(date_from or "").strip()
        d_to = str(date_to or "").strip()

        raw_rows: List[Dict[str, Any]] = []
        if self.is_vercel_api_mode():
            payload: Dict[str, Any] = {}
            if tid:
                payload["teacher_id"] = tid
            if sc:
                payload["school_code"] = sc
            if rk:
                payload["roster_key"] = rk
            if subj:
                payload["subject"] = subj
            if d_from:
                payload["date_from"] = d_from
            if d_to:
                payload["date_to"] = d_to
            try:
                res = self._post_vercel_api("fetch_lesson_progress", payload)
                items = res.get("items") or res.get("rows") or res.get("progress") or []
                if isinstance(items, list):
                    raw_rows = [r for r in items if isinstance(r, dict)]
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 수업 진도 조회 실패: {exc}")
                return []
        else:
            try:
                fs = self._get_client()
                col_ref = fs.collection("수업_진도")
                query = col_ref.where("teacher_id", "==", tid) if tid else col_ref
                for doc in query.stream():
                    data = doc.to_dict() or {}
                    if not data.get("remote_id"):
                        data["remote_id"] = doc.id
                    raw_rows.append(data)
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"수업 진도 조회 실패: {exc}")
                return []

        results: List[Dict[str, Any]] = []
        for idx, row in enumerate(raw_rows):
            norm = self.normalize_lesson_progress_doc(row, default_meta=meta, index=idx)
            if sc and norm.get("school_code") and norm["school_code"] != sc:
                continue
            if rk and norm.get("roster_key") and norm["roster_key"] != rk:
                continue
            if subj and norm.get("subject") and norm["subject"] != subj:
                continue
            if d_from and norm.get("date") and norm["date"] < d_from:
                continue
            if d_to and norm.get("date") and norm["date"] > d_to:
                continue
            results.append(norm)

        results.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("updated_at") or "")), reverse=True)
        return results

    def fetch_timetable_base(
        self,
        teacher_name: str = "",
        school_code: str = "",
    ) -> List[Dict[str, Any]]:
        """Firestore `시간표_기본` 컬렉션에서 교사/학교 기준 기본 시간표를 조회합니다."""
        meta = self._default_teacher_meta()
        t_name = str(teacher_name or meta.get("teacher_name") or "").strip()
        sc = str(school_code or meta.get("school_code") or "").strip()

        raw_rows: List[Dict[str, Any]] = []
        if self.is_vercel_api_mode():
            try:
                res = self._post_vercel_api(
                    "fetch_timetable_base",
                    {"teacher": t_name, "school_code": sc},
                )
                items = res.get("rows") or res.get("base_rows") or res.get("timetable") or []
                if isinstance(items, list):
                    raw_rows = [r for r in items if isinstance(r, dict)]
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"Vercel API 기본 시간표 조회 실패: {exc}")
                return []
        else:
            try:
                fs = self._get_client()
                for doc in fs.collection("시간표_기본").stream():
                    d = doc.to_dict() or {}
                    d.setdefault("remote_id", doc.id)
                    raw_rows.append(d)
            except Exception as exc:
                self.db.add_log("WARN", "FirestoreSync", f"기본 시간표 조회 실패: {exc}")
                return []

        filtered: List[Dict[str, Any]] = []
        for row in raw_rows:
            doc_sc = str(row.get("school_code") or row.get("neis_school_code") or "").strip()
            if sc and doc_sc and doc_sc != sc:
                continue
            doc_teacher = str(row.get("teacher") or row.get("teacher_name") or "").strip()
            if t_name and doc_teacher and t_name not in doc_teacher and doc_teacher not in t_name:
                continue
            filtered.append(row)
        return filtered

    def push_local_item(
        self,
        action: str,
        item_data: Dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        action_key = str(action or "").strip().lower()
        collection_map = {
            "upsert_schedule": "Schedules",
            "push_schedule": "Schedules",
            "upsert_task": "Tasks",
            "push_task": "Tasks",
            "push_notification": "Notifications",
            "upsert_notification": "Notifications",
        }
        col_name = collection_map.get(action_key)
        if not col_name or not isinstance(item_data, dict):
            return False
        try:
            fs = self._get_client()
            doc_id = _resolve_doc_id(col_name, item_data, 0)
            doc_data = _normalize_doc_fields(item_data)
            doc_data["remote_id"] = doc_id
            if not doc_data.get("updated_at"):
                doc_data["updated_at"] = _now_str()
            fs.collection(col_name).document(doc_id).set(doc_data, merge=True)
            return True
        except Exception as exc:
            self.db.add_log("WARN", "FirestoreSync", f"항목 Push 실패 ({action}): {exc}")
            return False


class FirestoreSyncWorker(BaseWorker):
    """QThread 백그라운드에서 Cloud Firestore 동기화를 실행하는 워커."""

    def __init__(
        self,
        sync_service: Optional[FirestoreSyncService] = None,
        apps_script_url: str = "",
        sheet_url: str = "",
        task_name: str = "Cloud Firestore 동기화",
    ):
        super().__init__(task_name=task_name)
        self.sync_service = sync_service or FirestoreSyncService()

    def execute(self) -> Dict[str, Any]:
        self.emit_progress(20, "Cloud Firestore에서 최신 데이터 조회 중...")
        result = self.sync_service.sync_all()
        if result.get("ok"):
            self.emit_progress(100, "Cloud Firestore 동기화 완료")
        else:
            self.emit_progress(100, str(result.get("error", "동기화 오류")))
        return result
