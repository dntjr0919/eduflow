"""
Google Sheets + Google Apps Script 웹 앱 JSON API 양방향 동기화 서비스.
- 연결 테스트 (GET ?action=ping)
- 전체 Pull 및 로컬 SQLite 동기화 (GET ?action=pull_all)
- 작업 상태 / 일정 / 알림 Push (POST JSON)
- QThread 백그라운드 실행용 GoogleSheetsSyncWorker
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from core.base_worker import BaseWorker
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.schedule_parser_service import (
    infer_schedule_category,
    normalize_date_str,
    normalize_time_str,
)


def extract_spreadsheet_id(sheet_url_or_id: str) -> str:
    """Google 스프레드시트 URL 또는 순수 문서 ID 문자열에서 Spreadsheet ID를 추출합니다."""
    raw = str(sheet_url_or_id or "").strip()
    if not raw:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    if m:
        return m.group(1).strip()
    if "://" not in raw and "/" not in raw and " " not in raw:
        return raw
    return ""


def _is_truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val or "").strip().lower()
    return s in ("1", "true", "yes", "y", "예", "모바일", "mobile")


def _http_get_json(url: str, params: Dict[str, str], timeout: float = 10.0) -> Dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
    full_url = f"{url}{'&' if '?' in url else '?'}{query}" if query else url

    try:
        import requests

        resp = requests.get(full_url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Apps Script 응답이 JSON 객체 형식이 아닙니다.")
        return data
    except ImportError:
        req = urllib.request.Request(full_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Apps Script 응답이 JSON 객체 형식이 아닙니다.")
        return data


def _http_post_json(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    try:
        import requests

        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data
        return {"ok": True}
    except ImportError:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"ok": True}


class GoogleSheetsSyncService:
    """Google Apps Script 웹 앱과 로컬 SQLite DB 간 동기화를 담당하는 서비스."""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
    ):
        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()

    def _resolve_urls(
        self, apps_script_url: str = "", sheet_url: str = ""
    ) -> Tuple[str, str]:
        gas_url = str(
            apps_script_url
            or self.config.get("api_and_integrations", "google_apps_script_url", "")
            or ""
        ).strip()
        s_url = str(
            sheet_url
            or self.config.get("api_and_integrations", "google_sheet_url", "")
            or ""
        ).strip()
        return gas_url, extract_spreadsheet_id(s_url)

    def _resolve_api_secret(self, api_secret: str = "") -> str:
        return str(
            api_secret
            or self.config.get("api_and_integrations", "gas_api_secret", "")
            or ""
        ).strip()

    def _with_api_secret(self, payload: Dict[str, Any], api_secret: str = "") -> Dict[str, Any]:
        secret = self._resolve_api_secret(api_secret)
        if secret:
            payload["api_secret"] = secret
        return payload

    def test_connection(
        self,
        apps_script_url: str = "",
        sheet_url: str = "",
        timeout: float = 8.0,
        api_secret: str = "",
    ) -> Tuple[bool, str, Dict[str, Any]]:
        gas_url, sheet_id = self._resolve_urls(apps_script_url, sheet_url)
        if not gas_url:
            return False, "Google Apps Script 웹 앱 URL이 입력되지 않았습니다.", {}

        try:
            params = self._with_api_secret(
                {"action": "ping", "sheetId": sheet_id},
                api_secret=api_secret,
            )
            data = _http_get_json(
                gas_url,
                params,
                timeout=timeout,
            )
            if not data.get("ok"):
                err = str(data.get("error") or "Apps Script가 오류를 반환했습니다.")
                return False, err, data
            ss_name = str(data.get("spreadsheet_name") or "연결된 시트")
            counts = data.get("counts") or {}
            msg = (
                f"연결 성공: {ss_name} "
                f"(일정 {counts.get('schedules', 0)}건 · 작업 {counts.get('tasks', 0)}건 · "
                f"알림 {counts.get('notifications', 0)}건)"
            )
            return True, msg, data
        except Exception as exc:
            return False, f"연결 테스트 실패: {exc}", {}

    def sync_from_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apps Script JSON 응답(또는 테스트 페이로드)을 로컬 SQLite와 동기화합니다.
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
            channel = str(raw_noti.get("channel", "google_sheets") or "google_sheets").strip()
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
            "server_time": str(payload.get("server_time", "")),
        }

    def sync_all(
        self,
        apps_script_url: str = "",
        sheet_url: str = "",
        timeout: float = 10.0,
        api_secret: str = "",
    ) -> Dict[str, Any]:
        gas_url, sheet_id = self._resolve_urls(apps_script_url, sheet_url)
        if not gas_url:
            return {
                "ok": False,
                "skipped": True,
                "error": "Google Apps Script 웹 앱 URL이 설정되지 않았습니다.",
                "pending_mobile_tasks": self.db.get_mobile_tasks(pending_only=True),
            }

        params = self._with_api_secret(
            {"action": "pull_all", "sheetId": sheet_id},
            api_secret=api_secret,
        )
        payload = _http_get_json(
            gas_url,
            params,
            timeout=timeout,
        )
        if not payload.get("ok"):
            return {
                "ok": False,
                "skipped": False,
                "error": str(payload.get("error") or "Google Sheets 동기화 응답 오류"),
                "pending_mobile_tasks": self.db.get_mobile_tasks(pending_only=True),
            }

        summary = self.sync_from_payload(payload)
        self.db.add_log(
            "INFO",
            "GoogleSheetsSync",
            (
                f"동기화 완료: 일정 +{summary['schedules_added']}, "
                f"할일 +{summary['todos_added']}, "
                f"모바일 예약 +{summary['mobile_tasks_added']}, "
                f"외부 알림 +{summary['notifications_added']}"
            ),
        )
        return summary

    def push_task_status(
        self,
        remote_id: str,
        exec_status: str,
        is_done: bool = False,
        apps_script_url: str = "",
        sheet_url: str = "",
        timeout: float = 8.0,
        memo: Optional[str] = None,
        api_secret: str = "",
    ) -> bool:
        gas_url, sheet_id = self._resolve_urls(apps_script_url, sheet_url)
        rid = str(remote_id or "").strip()
        if not gas_url or not rid:
            return False
        payload: Dict[str, Any] = self._with_api_secret(
            {
                "action": "update_task_status",
                "sheetId": sheet_id,
                "remote_id": rid,
                "exec_status": exec_status,
                "is_done": bool(is_done),
            },
            api_secret=api_secret,
        )
        if memo is not None:
            payload["memo"] = str(memo)
        try:
            res = _http_post_json(
                gas_url,
                payload,
                timeout=timeout,
            )
            return bool(res.get("ok", True))
        except Exception as exc:
            self.db.add_log("WARN", "GoogleSheetsSync", f"상태 Push 실패 ({rid}): {exc}")
            return False

    def fetch_sheet_rows(
        self,
        sheet_name: str,
        apps_script_url: str = "",
        sheet_url: str = "",
        timeout: float = 10.0,
        api_secret: str = "",
    ) -> List[Dict[str, Any]]:
        """지정한 시트(예: 가정통신문_회신, 시간표_변동 등)의 행 목록을 조회합니다."""
        gas_url, sheet_id = self._resolve_urls(apps_script_url, sheet_url)
        if not gas_url or not sheet_name:
            return []
        try:
            params = self._with_api_secret(
                {"action": "list_sheet", "sheet": sheet_name, "sheetId": sheet_id},
                api_secret=api_secret,
            )
            res = _http_get_json(
                gas_url,
                params,
                timeout=timeout,
            )
            if not res.get("ok"):
                return []
            rows = res.get("rows", [])
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            self.db.add_log("WARN", "GoogleSheetsSync", f"시트 조회 실패 ({sheet_name}): {exc}")
            return []

    def push_timetable_changes(
        self,
        rows: List[Dict[str, Any]],
        apps_script_url: str = "",
        sheet_url: str = "",
        timeout: float = 10.0,
        api_secret: str = "",
    ) -> bool:
        """컴시간 알리미 등에서 파싱한 시간표 변동 내역을 시간표_변동 시트로 Push합니다."""
        gas_url, sheet_id = self._resolve_urls(apps_script_url, sheet_url)
        if not gas_url or not rows:
            return False
        try:
            payload = self._with_api_secret(
                {
                    "action": "push_timetable_changes",
                    "sheetId": sheet_id,
                    "rows": rows,
                },
                api_secret=api_secret,
            )
            res = _http_post_json(
                gas_url,
                payload,
                timeout=timeout,
            )
            return bool(res.get("ok", True))
        except Exception as exc:
            self.db.add_log("WARN", "GoogleSheetsSync", f"시간표 변동 Push 실패: {exc}")
            return False

    def push_local_item(
        self,
        action: str,
        item_data: Dict[str, Any],
        apps_script_url: str = "",
        sheet_url: str = "",
        timeout: float = 8.0,
        api_secret: str = "",
    ) -> bool:
        gas_url, sheet_id = self._resolve_urls(apps_script_url, sheet_url)
        if not gas_url:
            return False
        try:
            payload = self._with_api_secret(
                {
                    "action": action,
                    "sheetId": sheet_id,
                    "data": item_data,
                },
                api_secret=api_secret,
            )
            res = _http_post_json(
                gas_url,
                payload,
                timeout=timeout,
            )
            return bool(res.get("ok", True))
        except Exception as exc:
            self.db.add_log("WARN", "GoogleSheetsSync", f"항목 Push 실패 ({action}): {exc}")
            return False


class GoogleSheetsSyncWorker(BaseWorker):
    """QThread 백그라운드에서 Google Sheets + Apps Script 동기화를 실행하는 워커."""

    def __init__(
        self,
        sync_service: Optional[GoogleSheetsSyncService] = None,
        apps_script_url: str = "",
        sheet_url: str = "",
        task_name: str = "Google Sheets 동기화",
    ):
        super().__init__(task_name=task_name)
        self.sync_service = sync_service or GoogleSheetsSyncService()
        self.apps_script_url = apps_script_url
        self.sheet_url = sheet_url

    def execute(self) -> Dict[str, Any]:
        self.emit_progress(20, "Google Apps Script 웹 앱에서 최신 데이터 조회 중...")
        result = self.sync_service.sync_all(
            apps_script_url=self.apps_script_url,
            sheet_url=self.sheet_url,
        )
        if result.get("ok"):
            self.emit_progress(100, "Google Sheets 동기화 완료")
        elif result.get("skipped"):
            self.emit_progress(100, "Apps Script URL 미설정 — 로컬 DB만 유지")
        else:
            self.emit_progress(100, str(result.get("error", "동기화 오류")))
        return result
