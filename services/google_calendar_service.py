"""
Google Calendar & Google Tasks 멀티 계정(개인 / Workspace) 연동 서비스.
- OAuth 2.0 (InstalledAppFlow) 로그인 및 계정별 token.json 자동 리프레시
  - personal: data/gcal_token_personal.json
  - workspace: data/gcal_token_workspace.json
- Google Calendar: 단방향 내보내기(push_schedules) 및 가져오기(pull_schedules, 신규 일정은 '개인' 분류)
- Google Tasks: 할일 가져오기(pull_tasks) 및 내보내기(push_tasks)
"""

from __future__ import annotations

import os
import re
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from wsgiref.simple_server import WSGIRequestHandler, make_server

from core.config_manager import ConfigManager
from core.database import DatabaseManager
from utils.path_helper import get_data_path

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/drive.file",
]

ACCOUNT_PROFILES: Tuple[str, ...] = ("personal", "workspace")

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials as GoogleCredentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build as gcal_build
    from googleapiclient.errors import HttpError as GoogleHttpError
    from googleapiclient.http import MediaFileUpload

    HAS_GOOGLE_CALENDAR_LIBS = True
except Exception:
    GoogleAuthRequest = None  # type: ignore
    GoogleCredentials = None  # type: ignore
    InstalledAppFlow = None  # type: ignore
    gcal_build = None  # type: ignore
    GoogleHttpError = Exception  # type: ignore
    MediaFileUpload = None  # type: ignore
    HAS_GOOGLE_CALENDAR_LIBS = False


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class GoogleCalendarService:
    """eduflow 로컬 일정·할일과 Google Calendar / Google Tasks를 연동하는 서비스."""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
    ):
        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()

    @staticmethod
    def _normalize_account(account: Optional[str]) -> str:
        acc = str(account or "personal").strip().lower()
        return acc if acc in ACCOUNT_PROFILES else "personal"

    def _resolve_token_path(self, account: Optional[str] = None) -> Path:
        acc = self._normalize_account(account)
        if acc == "workspace":
            cfg_key = "google_calendar_token_workspace_path"
            default_rel = "data/gcal_token_workspace.json"
        else:
            cfg_key = "google_calendar_token_personal_path"
            default_rel = "data/gcal_token_personal.json"

        raw = str(
            self.config.get("api_and_integrations", cfg_key, default_rel)
            or default_rel
        ).strip()
        if os.path.isabs(raw):
            p = Path(raw)
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        return Path(get_data_path(raw, ensure_parent=True))

    def _get_credentials_path(self, override: str = "") -> str:
        return str(
            override
            or self.config.get("api_and_integrations", "google_calendar_credentials_path", "")
            or ""
        ).strip()

    def _get_calendar_id(self) -> str:
        cid = str(
            self.config.get("api_and_integrations", "google_calendar_id", "primary")
            or "primary"
        ).strip()
        return cid or "primary"

    def _get_default_export_account(self) -> str:
        raw = str(
            self.config.get("api_and_integrations", "google_calendar_export_account", "personal")
            or "personal"
        ).strip()
        return self._normalize_account(raw)

    def is_connected(self, account: Optional[str] = None) -> bool:
        if account is None:
            return any(self.is_connected(acc) for acc in ACCOUNT_PROFILES)
        token_path = self._resolve_token_path(account)
        if not token_path.exists():
            return False
        if not HAS_GOOGLE_CALENDAR_LIBS or GoogleCredentials is None:
            return False
        try:
            creds = GoogleCredentials.from_authorized_user_file(str(token_path), SCOPES)
            return bool(creds and (creds.valid or creds.refresh_token))
        except Exception:
            return False

    def get_connected_accounts(self) -> List[str]:
        return [acc for acc in ACCOUNT_PROFILES if self.is_connected(acc)]

    @staticmethod
    def _run_oauth_local_server_with_cancel(
        flow: Any,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        timeout_sec: int = 120,
        open_browser: bool = True,
    ) -> Tuple[Any, str]:
        """
        timeout=1.0초 루프 기반 wsgiref 로컬 서버로 OAuth 콜백을 대기합니다.
        cancel_check()가 True이거나 timeout_sec를 초과하면 즉시 server_close() 후 종료합니다.
        """
        captured: Dict[str, str] = {"uri": ""}

        class _QuietHandler(WSGIRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        def _wsgi_app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> List[bytes]:
            query = str(environ.get("QUERY_STRING") or "")
            path_info = str(environ.get("PATH_INFO") or "/")
            host = str(environ.get("HTTP_HOST") or f"localhost:{server.server_port}")
            captured["uri"] = f"http://{host}{path_info}" + (f"?{query}" if query else "")
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            html = (
                "<html><body style='font-family:sans-serif;padding:24px;'>"
                "<h3>Google 계정 인증이 완료되었습니다.</h3>"
                "<p>이 브라우저 창을 닫고 EduFlow 앱으로 돌아가 주세요.</p>"
                "</body></html>"
            )
            return [html.encode("utf-8")]

        server = make_server("127.0.0.1", 0, _wsgi_app, handler_class=_QuietHandler)
        server.timeout = 1.0
        started_at = time.monotonic()
        max_wait = max(1, int(timeout_sec or 120))

        try:
            flow.redirect_uri = f"http://localhost:{server.server_port}/"
            auth_url, _ = flow.authorization_url(
                access_type="offline",
                prompt="consent",
            )
            if open_browser and auth_url:
                webbrowser.open(str(auth_url), new=1, autoraise=True)

            while not captured["uri"]:
                if cancel_check is not None and cancel_check():
                    return None, "cancelled"
                if (time.monotonic() - started_at) >= max_wait:
                    return None, "timeout"
                server.handle_request()
        finally:
            try:
                server.server_close()
            except Exception:
                pass

        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        flow.fetch_token(authorization_response=captured["uri"])
        return flow.credentials, ""

    def connect(
        self,
        credentials_path: str = "",
        account: str = "personal",
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        timeout_sec: int = 120,
        flow_override: Any = None,
        open_browser: bool = True,
    ) -> Tuple[bool, str]:
        acc = self._normalize_account(account)
        if flow_override is None:
            if not HAS_GOOGLE_CALENDAR_LIBS or InstalledAppFlow is None:
                return (
                    False,
                    "Google Calendar/Tasks 연동 패키지(google-api-python-client, google-auth-oauthlib)가 설치되어 있지 않습니다.\n"
                    "pip install google-api-python-client google-auth-oauthlib",
                )
            cred_file = self._get_credentials_path(credentials_path)
            if not cred_file or not Path(cred_file).is_file():
                return False, "Google OAuth credentials.json 파일을 먼저 선택해 주세요."
        else:
            cred_file = self._get_credentials_path(credentials_path)

        try:
            flow = (
                flow_override
                if flow_override is not None
                else InstalledAppFlow.from_client_secrets_file(cred_file, SCOPES)
            )
            creds, reason = self._run_oauth_local_server_with_cancel(
                flow,
                cancel_check=cancel_check,
                timeout_sec=timeout_sec,
                open_browser=open_browser,
            )
            if reason == "cancelled":
                return False, "사용자가 Google 계정 인증을 취소했습니다."
            if reason == "timeout":
                return False, f"Google 계정 인증 대기 시간({timeout_sec}초)이 초과되었습니다."

            token_path = self._resolve_token_path(acc)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            self.config.set("api_and_integrations", "google_calendar_enabled", True)
            if cred_file:
                self.config.set("api_and_integrations", "google_calendar_credentials_path", cred_file)
            label = "Workspace" if acc == "workspace" else "개인"
            return True, f"{label} Google 계정 연결이 완료되었습니다."
        except Exception as exc:
            return False, f"Google OAuth 로그인 실패: {exc}"

    def disconnect(self, account: str = "personal") -> None:
        acc = self._normalize_account(account)
        token_path = self._resolve_token_path(acc)
        if token_path.exists():
            try:
                token_path.unlink()
            except Exception:
                pass
        if not self.get_connected_accounts():
            self.config.set("api_and_integrations", "google_calendar_enabled", False)

    def _build_service(
        self,
        api_name: str = "calendar",
        api_version: str = "v3",
        account: str = "personal",
    ) -> Any:
        if not HAS_GOOGLE_CALENDAR_LIBS or GoogleCredentials is None or gcal_build is None:
            raise RuntimeError(
                "Google Calendar/Tasks 연동 패키지가 설치되어 있지 않습니다. "
                "(pip install google-api-python-client google-auth-oauthlib)"
            )
        acc = self._normalize_account(account)
        token_path = self._resolve_token_path(acc)
        if not token_path.exists():
            label = "Workspace" if acc == "workspace" else "개인"
            raise RuntimeError(
                f"{label} Google 계정이 연결되어 있지 않습니다. [환경설정 & 명렬표]에서 먼저 연결해 주세요."
            )
        creds = GoogleCredentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(GoogleAuthRequest())
                token_path.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise RuntimeError("Google 인증 토큰이 만료되었습니다. 다시 로그인해 주세요.")
        return gcal_build(api_name, api_version, credentials=creds, cache_discovery=False)

    # =========================================================================
    # Google Calendar (일정 내보내기 / 가져오기)
    # =========================================================================
    @staticmethod
    def build_event_payload(schedule: Dict[str, Any]) -> Dict[str, Any]:
        title = str(schedule.get("title") or "").strip() or "(제목 없음)"
        category = str(schedule.get("category") or "학사일정").strip()
        memo = str(schedule.get("memo") or "").strip()
        s_date_str = str(schedule.get("start_date") or "").strip()
        e_date_str = str(schedule.get("end_date") or s_date_str).strip() or s_date_str
        s_time = str(schedule.get("start_time") or "").strip()

        desc_parts = [f"[EduFlow · {category}]"]
        if memo:
            desc_parts.append(memo)
        description = "\n".join(desc_parts)

        if not s_time:
            try:
                end_d = date.fromisoformat(e_date_str[:10]) + timedelta(days=1)
                end_exclusive = end_d.isoformat()
            except Exception:
                end_exclusive = e_date_str[:10]
            return {
                "summary": title,
                "description": description,
                "start": {"date": s_date_str[:10]},
                "end": {"date": end_exclusive},
            }

        hhmm = s_time[:5]
        try:
            start_dt = datetime.strptime(f"{s_date_str[:10]} {hhmm}", "%Y-%m-%d %H:%M")
        except Exception:
            start_dt = datetime.strptime(f"{s_date_str[:10]} 09:00", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=1)
        return {
            "summary": title,
            "description": description,
            "start": {
                "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:00+09:00"),
                "timeZone": "Asia/Seoul",
            },
            "end": {
                "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:00+09:00"),
                "timeZone": "Asia/Seoul",
            },
        }

    def push_schedules(
        self,
        exclude_personal: Optional[bool] = None,
        account: Optional[str] = None,
        service_override: Any = None,
    ) -> Dict[str, Any]:
        """로컬 schedules 테이블의 일정을 지정된 Google Calendar 계정으로 단방향 푸시합니다."""
        try:
            target_acc = self._normalize_account(
                account if account is not None else self._get_default_export_account()
            )
            service = (
                service_override
                if service_override is not None
                else self._build_service("calendar", "v3", account=target_acc)
            )
            cal_id = self._get_calendar_id()
            if exclude_personal is None:
                exclude_personal = bool(
                    self.config.get("api_and_integrations", "google_calendar_exclude_personal", False)
                )

            events_api = service.events()
            schedules = self.db.get_schedules()
            inserted = 0
            patched = 0
            skipped = 0
            deleted = 0

            for sch in schedules:
                cat = str(sch.get("category") or "").strip()
                if exclude_personal and cat == "개인":
                    skipped += 1
                    continue

                sch_id = int(sch["id"])
                gcal_id = str(sch.get("gcal_event_id") or "").strip()
                gcal_upd = str(sch.get("gcal_updated_at") or "").strip()
                local_upd = str(sch.get("updated_at") or "").strip()
                payload = self.build_event_payload(sch)

                if not gcal_id:
                    created = events_api.insert(calendarId=cal_id, body=payload).execute()
                    new_eid = str((created or {}).get("id") or "").strip()
                    if new_eid:
                        self.db.update_schedule_gcal_meta(
                            sch_id, new_eid, _now_str(), gcal_account=target_acc
                        )
                        inserted += 1
                elif not gcal_upd or (local_upd and local_upd > gcal_upd):
                    events_api.patch(calendarId=cal_id, eventId=gcal_id, body=payload).execute()
                    self.db.update_schedule_gcal_meta(
                        sch_id, gcal_id, _now_str(), gcal_account=target_acc
                    )
                    patched += 1
                else:
                    skipped += 1

            pending_del = self.db.get_pending_gcal_deletions()
            cleared_del: List[str] = []
            for del_eid in pending_del:
                try:
                    events_api.delete(calendarId=cal_id, eventId=del_eid).execute()
                    deleted += 1
                    cleared_del.append(del_eid)
                except GoogleHttpError as http_exc:
                    status = getattr(getattr(http_exc, "resp", None), "status", None)
                    if status in (404, 410):
                        cleared_del.append(del_eid)
                except Exception:
                    pass
            if cleared_del:
                self.db.clear_gcal_deletions(cleared_del)

            self.db.add_log(
                "INFO",
                "GoogleCalendar",
                f"Google 캘린더({target_acc}) 푸시 완료: 추가 {inserted}, 수정 {patched}, 삭제 {deleted}, 건너뜀 {skipped}",
            )
            return {
                "ok": True,
                "account": target_acc,
                "inserted": inserted,
                "patched": patched,
                "deleted": deleted,
                "skipped": skipped,
            }
        except Exception as exc:
            self.db.add_log("WARN", "GoogleCalendar", f"Google 캘린더 동기화 실패: {exc}")
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _format_rfc3339_bound(val: Any, *, is_end: bool = False) -> str:
        if isinstance(val, datetime):
            return val.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        if isinstance(val, date):
            hhmmss = "23:59:59" if is_end else "00:00:00"
            return f"{val.isoformat()}T{hhmmss}+09:00"
        s = str(val or "").strip()
        if len(s) == 10 and re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            hhmmss = "23:59:59" if is_end else "00:00:00"
            return f"{s}T{hhmmss}+09:00"
        return s

    @staticmethod
    def _parse_google_event_dates(event: Dict[str, Any]) -> Tuple[str, str, str]:
        start_obj = event.get("start") or {}
        end_obj = event.get("end") or {}

        if start_obj.get("date"):
            s_date = str(start_obj["date"])[:10]
            raw_e = str(end_obj.get("date") or s_date)[:10]
            try:
                e_dt = date.fromisoformat(raw_e) - timedelta(days=1)
                e_date = e_dt.isoformat() if e_dt.isoformat() >= s_date else s_date
            except Exception:
                e_date = s_date
            return s_date, e_date, ""

        s_dt_raw = str(start_obj.get("dateTime") or "").strip()
        e_dt_raw = str(end_obj.get("dateTime") or s_dt_raw).strip()
        s_date = s_dt_raw[:10] if len(s_dt_raw) >= 10 else date.today().isoformat()
        s_time = s_dt_raw[11:16] if len(s_dt_raw) >= 16 and "T" in s_dt_raw else ""
        e_date = e_dt_raw[:10] if len(e_dt_raw) >= 10 else s_date
        if e_date < s_date:
            e_date = s_date
        return s_date, e_date, s_time

    def pull_schedules(
        self,
        time_min: Any = None,
        time_max: Any = None,
        account: Optional[str] = None,
        service_override: Any = None,
    ) -> Dict[str, Any]:
        """
        연결된 Google Calendar(개인/Workspace 또는 지정 계정)에서 일정을 조회해 로컬 schedules로 가져옵니다.
        - 동일 gcal_event_id가 이미 있으면 기존 카테고리를 유지한 채 제목/날짜/메모 갱신
        - 신규 일정은 학생·학부모 노출 방지를 위해 category='개인'으로 저장
        """
        try:
            if service_override is not None:
                target_accounts = [self._normalize_account(account)]
            elif account is not None:
                target_accounts = [self._normalize_account(account)]
            else:
                target_accounts = self.get_connected_accounts() or ["personal"]

            cal_id = self._get_calendar_id()
            today = date.today()
            t_min = self._format_rfc3339_bound(
                time_min if time_min is not None else (today - timedelta(days=30)),
                is_end=False,
            )
            t_max = self._format_rfc3339_bound(
                time_max if time_max is not None else (today + timedelta(days=180)),
                is_end=True,
            )

            existing_by_gcal: Dict[str, Dict[str, Any]] = {
                str(s.get("gcal_event_id") or "").strip(): s
                for s in self.db.get_schedules()
                if str(s.get("gcal_event_id") or "").strip()
            }

            added = 0
            updated = 0
            skipped = 0

            for acc in target_accounts:
                service = (
                    service_override
                    if service_override is not None
                    else self._build_service("calendar", "v3", account=acc)
                )
                events_api = service.events()
                page_token: Optional[str] = None

                while True:
                    kwargs: Dict[str, Any] = {
                        "calendarId": cal_id,
                        "timeMin": t_min,
                        "timeMax": t_max,
                        "singleEvents": True,
                        "orderBy": "startTime",
                    }
                    if page_token:
                        kwargs["pageToken"] = page_token
                    resp = events_api.list(**kwargs).execute() or {}
                    items = resp.get("items") or []

                    for ev in items:
                        if not isinstance(ev, dict):
                            continue
                        if str(ev.get("status") or "").strip().lower() == "cancelled":
                            skipped += 1
                            continue
                        gcal_id = str(ev.get("id") or "").strip()
                        if not gcal_id:
                            skipped += 1
                            continue

                        title = str(ev.get("summary") or "").strip() or "(제목 없음)"
                        memo = str(ev.get("description") or "").strip()
                        s_date, e_date, s_time = self._parse_google_event_dates(ev)

                        existing = existing_by_gcal.get(gcal_id)
                        if existing is not None:
                            sch_id = int(existing["id"])
                            self.db.update_schedule(
                                sch_id,
                                title=title,
                                category=str(existing.get("category") or "학사일정"),
                                start_date=s_date,
                                end_date=e_date,
                                start_time=s_time,
                                reminder_type=str(existing.get("reminder_type") or "none"),
                                memo=memo,
                                reset_reminder_if_changed=False,
                            )
                            self.db.update_schedule_gcal_meta(
                                sch_id, gcal_id, _now_str(), gcal_account=acc
                            )
                            updated += 1
                        else:
                            new_id = self.db.add_schedule(
                                title=title,
                                category="개인",
                                start_date=s_date,
                                end_date=e_date,
                                start_time=s_time,
                                memo=memo,
                                is_read=1,
                            )
                            self.db.update_schedule_gcal_meta(
                                new_id, gcal_id, _now_str(), gcal_account=acc
                            )
                            existing_by_gcal[gcal_id] = {
                                "id": new_id,
                                "category": "개인",
                                "gcal_event_id": gcal_id,
                            }
                            added += 1

                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break

            self.db.add_log(
                "INFO",
                "GoogleCalendar",
                f"Google 캘린더 가져오기 완료({','.join(target_accounts)}): 추가 {added}, 갱신 {updated}, 건너뜀 {skipped}",
            )
            return {
                "ok": True,
                "accounts": target_accounts,
                "added": added,
                "updated": updated,
                "skipped": skipped,
            }
        except Exception as exc:
            self.db.add_log("WARN", "GoogleCalendar", f"Google 캘린더 가져오기 실패: {exc}")
            return {"ok": False, "error": str(exc)}

    # =========================================================================
    # Google Tasks (할일 가져오기 / 내보내기)
    # =========================================================================
    @staticmethod
    def build_task_payload(todo: Dict[str, Any]) -> Dict[str, Any]:
        title = str(todo.get("title") or "").strip() or "(할일 없음)"
        memo = str(todo.get("memo") or "").strip()
        due_str = str(todo.get("due_date") or "").strip()
        is_done = bool(todo.get("is_done"))

        payload: Dict[str, Any] = {
            "title": title,
            "notes": memo,
            "status": "completed" if is_done else "needsAction",
        }
        if due_str and len(due_str) >= 10:
            payload["due"] = f"{due_str[:10]}T00:00:00.000Z"
        return payload

    def pull_tasks(
        self,
        account: Optional[str] = None,
        service_override: Any = None,
    ) -> Dict[str, Any]:
        """Google Tasks('@default') 목록을 조회해 로컬 todos 테이블에 반영합니다."""
        try:
            if service_override is not None:
                target_accounts = [self._normalize_account(account)]
            elif account is not None:
                target_accounts = [self._normalize_account(account)]
            else:
                target_accounts = self.get_connected_accounts() or ["personal"]

            existing_by_gtask: Dict[str, Dict[str, Any]] = {
                str(t.get("gtask_id") or "").strip(): t
                for t in self.db.get_todos()
                if str(t.get("gtask_id") or "").strip()
            }

            added = 0
            updated = 0
            skipped = 0

            for acc in target_accounts:
                service = (
                    service_override
                    if service_override is not None
                    else self._build_service("tasks", "v1", account=acc)
                )
                tasks_api = service.tasks()
                page_token: Optional[str] = None

                while True:
                    kwargs: Dict[str, Any] = {
                        "tasklist": "@default",
                        "showCompleted": True,
                        "showHidden": True,
                    }
                    if page_token:
                        kwargs["pageToken"] = page_token
                    resp = tasks_api.list(**kwargs).execute() or {}
                    items = resp.get("items") or []

                    for item in items:
                        if not isinstance(item, dict) or item.get("deleted"):
                            skipped += 1
                            continue
                        gtask_id = str(item.get("id") or "").strip()
                        title = str(item.get("title") or "").strip()
                        if not gtask_id or not title:
                            skipped += 1
                            continue

                        memo = str(item.get("notes") or "").strip()
                        due_raw = str(item.get("due") or "").strip()
                        due_date = due_raw[:10] if len(due_raw) >= 10 else ""
                        is_done = str(item.get("status") or "").strip().lower() == "completed"

                        existing = existing_by_gtask.get(gtask_id)
                        if existing is not None:
                            tid = int(existing["id"])
                            self.db.update_todo(
                                tid,
                                title=title,
                                due_date=due_date,
                                priority=str(existing.get("priority") or "normal"),
                                memo=memo,
                            )
                            self.db.set_todo_done(tid, is_done)
                            self.db.update_todo_gtask_meta(tid, gtask_id, acc, _now_str())
                            updated += 1
                        else:
                            new_tid = self.db.add_todo(
                                title=title,
                                due_date=due_date,
                                priority="normal",
                                memo=memo,
                                is_read=1,
                            )
                            if is_done:
                                self.db.set_todo_done(new_tid, True)
                            self.db.update_todo_gtask_meta(new_tid, gtask_id, acc, _now_str())
                            existing_by_gtask[gtask_id] = {
                                "id": new_tid,
                                "gtask_id": gtask_id,
                                "gtask_account": acc,
                            }
                            added += 1

                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break

            self.db.add_log(
                "INFO",
                "GoogleTasks",
                f"Google Tasks 가져오기 완료({','.join(target_accounts)}): 추가 {added}, 갱신 {updated}, 건너뜀 {skipped}",
            )
            return {
                "ok": True,
                "accounts": target_accounts,
                "added": added,
                "updated": updated,
                "skipped": skipped,
            }
        except Exception as exc:
            self.db.add_log("WARN", "GoogleTasks", f"Google Tasks 가져오기 실패: {exc}")
            return {"ok": False, "error": str(exc)}

    def push_tasks(
        self,
        account: Optional[str] = None,
        service_override: Any = None,
    ) -> Dict[str, Any]:
        """로컬 todos 테이블의 할일을 지정된 Google Tasks('@default')로 내보냅니다."""
        try:
            target_acc = self._normalize_account(
                account if account is not None else self._get_default_export_account()
            )
            service = (
                service_override
                if service_override is not None
                else self._build_service("tasks", "v1", account=target_acc)
            )
            tasks_api = service.tasks()
            todos = self.db.get_todos()

            inserted = 0
            patched = 0
            skipped = 0

            for td in todos:
                tid = int(td["id"])
                gtask_id = str(td.get("gtask_id") or "").strip()
                gtask_acc = str(td.get("gtask_account") or "").strip()
                gtask_upd = str(td.get("gtask_updated_at") or "").strip()
                local_upd = str(td.get("updated_at") or "").strip()
                payload = self.build_task_payload(td)

                if not gtask_id:
                    created = tasks_api.insert(tasklist="@default", body=payload).execute()
                    new_gid = str((created or {}).get("id") or "").strip()
                    if new_gid:
                        self.db.update_todo_gtask_meta(tid, new_gid, target_acc, _now_str())
                        inserted += 1
                elif (not gtask_acc or gtask_acc == target_acc) and (
                    not gtask_upd or (local_upd and local_upd > gtask_upd)
                ):
                    tasks_api.patch(
                        tasklist="@default", task=gtask_id, body=payload
                    ).execute()
                    self.db.update_todo_gtask_meta(tid, gtask_id, target_acc, _now_str())
                    patched += 1
                else:
                    skipped += 1

            self.db.add_log(
                "INFO",
                "GoogleTasks",
                f"Google Tasks({target_acc}) 내보내기 완료: 추가 {inserted}, 수정 {patched}, 건너뜀 {skipped}",
            )
            return {
                "ok": True,
                "account": target_acc,
                "inserted": inserted,
                "patched": patched,
                "skipped": skipped,
            }
        except Exception as exc:
            self.db.add_log("WARN", "GoogleTasks", f"Google Tasks 내보내기 실패: {exc}")
            return {"ok": False, "error": str(exc)}

    # =========================================================================
    # Google Drive (OAuth 계정 직접 파일 업로드)
    # =========================================================================
    def _resolve_or_create_drive_folder(self, drive_service: Any, folder_name: str) -> Optional[str]:
        safe_name = str(folder_name or "EduFlow_가정통신문").strip().replace("'", "\\'")
        if not safe_name:
            return None
        files_api = drive_service.files()
        list_res = files_api.list(
            q=f"name='{safe_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
            pageSize=1,
        ).execute() or {}
        existing = list_res.get("files") or []
        if existing:
            return str(existing[0].get("id") or "").strip() or None

        created = files_api.create(
            body={
                "name": str(folder_name or "EduFlow_가정통신문").strip(),
                "mimeType": "application/vnd.google-apps.folder",
            },
            fields="id",
        ).execute() or {}
        return str(created.get("id") or "").strip() or None

    def upload_file_to_drive(
        self,
        file_path: str,
        account: Optional[str] = None,
        folder_name: str = "EduFlow_가정통신문",
        service_override: Any = None,
    ) -> Dict[str, Any]:
        """
        연결된 OAuth 계정(개인/Workspace)의 Google Drive에 파일을 직접 업로드하고
        anyone 읽기 권한을 부여한 뒤 webViewLink / webContentLink를 반환합니다.
        """
        import mimetypes

        p = Path(str(file_path or "").strip())
        if not p.exists() or not p.is_file():
            return {"ok": False, "error": "업로드할 파일을 찾을 수 없습니다."}

        try:
            if account is not None:
                target_acc = self._normalize_account(account)
            else:
                connected = self.get_connected_accounts()
                target_acc = connected[0] if connected else self._get_default_export_account()

            drive_service = (
                service_override
                if service_override is not None
                else self._build_service("drive", "v3", account=target_acc)
            )
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

            folder_id = self._resolve_or_create_drive_folder(drive_service, folder_name)
            body: Dict[str, Any] = {"name": p.name}
            if folder_id:
                body["parents"] = [folder_id]

            if MediaFileUpload is not None:
                media = MediaFileUpload(str(p), mimetype=mime_type, resumable=True)
            else:
                media = {"mimetype": mime_type, "path": str(p)}

            created = (
                drive_service.files()
                .create(
                    body=body,
                    media_body=media,
                    fields="id, webViewLink, webContentLink",
                )
                .execute()
                or {}
            )
            file_id = str(created.get("id") or "").strip()
            if not file_id:
                return {"ok": False, "error": "Google Drive 파일 ID를 반환받지 못했습니다."}

            try:
                drive_service.permissions().create(
                    fileId=file_id,
                    body={"role": "reader", "type": "anyone"},
                ).execute()
            except Exception:
                pass

            web_view = str(created.get("webViewLink") or "").strip() or f"https://drive.google.com/file/d/{file_id}/view"
            web_content = str(created.get("webContentLink") or "").strip() or f"https://drive.google.com/uc?export=download&id={file_id}"
            self.db.add_log(
                "INFO",
                "GoogleDrive",
                f"OAuth Drive 업로드 완료({target_acc}): {p.name} ({file_id})",
            )
            return {
                "ok": True,
                "account": target_acc,
                "file_id": file_id,
                "attachment_file_id": file_id,
                "attachment_url": web_view,
                "webViewLink": web_view,
                "webContentLink": web_content,
            }
        except Exception as exc:
            self.db.add_log("WARN", "GoogleDrive", f"OAuth Drive 업로드 실패 ({p.name}): {exc}")
            return {"ok": False, "error": str(exc)}
