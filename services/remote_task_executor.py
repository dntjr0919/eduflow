"""
모바일 원격 작업 큐 폴링 및 실제 데스크톱 로직 실행기 (services/remote_task_executor.py).
- 교사 로그인 상태에서 주기적으로(예: 60초) `fetch_pending_remote_tasks()`를 조회하여
  예약 시각이 도래한 `pending` 작업을 순차 처리합니다.
- 시작 시 `exec_status='running'`, 완료 시 `exec_status='done'` + 결과 요약 `memo`,
  실패 시 `exec_status='error'` + 오류 사유를 기록합니다.
- 지원 `task_type`:
  - `comcigan_sync`: 컴시간 시간표 동기화 (`sync_comcigan_from_web` / `push_comcigan_changes`)
  - `schoolbell_sync`: 학교종이 최근 공지 수집 (`SchoolbellCrawlerService`, 저장된 게시판 URL 기준)
  - `file_sort`: 스마트 파일 자동 분류 (`BatchSortWorker` / `FileSorterService`)
  - `homeroom_report`: 현재 담임 학급 출결·가정통신문 현황 요약 보고서(`.xlsx` 또는 `.txt`) 생성 및 경로 기록
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.file_sorter_service import BatchSortWorker, FileSorterService
from services.firestore_sync_service import FirestoreSyncService
from services.mobile_integration_worker import (
    MobileRemoteTaskDispatcher,
    push_comcigan_changes,
    sync_comcigan_from_web,
)
from services.schoolbell_crawler_service import (
    SchoolbellCrawlerService,
    load_saved_schoolbell_board_url,
)

_LOGGER = logging.getLogger(__name__)


def parse_scheduled_datetime(raw_value: Any, base_now: Optional[datetime] = None) -> Optional[datetime]:
    """예약 시각 문자열(`YYYY-MM-DD HH:MM`, ISO8601, `HH:MM` 등)을 datetime으로 파싱합니다."""
    s = str(raw_value or "").strip()
    if not s:
        return None
    now = base_now or datetime.now()

    # ISO 형식 (Z 또는 +09:00 등 타임존 제거 후 로컬 naive 비교)
    iso_candidate = s.replace("T", " ").strip()
    if iso_candidate.endswith("Z"):
        iso_candidate = iso_candidate[:-1].strip()
    if "+" in iso_candidate[10:]:
        iso_candidate = iso_candidate.split("+", 1)[0].strip()

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(iso_candidate, fmt)
        except ValueError:
            continue

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t_val = datetime.strptime(iso_candidate, fmt).time()
            return datetime.combine(now.date(), t_val)
        except ValueError:
            continue

    return None


def is_task_due(task: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """원격 작업의 예약 시각(`scheduled_time` / `run_at`)이 도래했는지 판별합니다."""
    ref_now = now or datetime.now()
    raw_sched = task.get("scheduled_time") or task.get("run_at") or ""
    if not str(raw_sched or "").strip():
        return True
    parsed = parse_scheduled_datetime(raw_sched, base_now=ref_now)
    if parsed is None:
        return True
    return parsed <= ref_now


def _parse_action_payload(raw_payload: Any) -> Dict[str, Any]:
    if isinstance(raw_payload, dict):
        return dict(raw_payload)
    if isinstance(raw_payload, str) and raw_payload.strip():
        try:
            parsed = json.loads(raw_payload)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"raw": raw_payload.strip()}
    return {}


class RemoteTaskExecutor:
    """모바일 원격 작업 큐 폴링 및 4종 핵심 데스크톱 작업 실행기."""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
        sync_service: Optional[FirestoreSyncService] = None,
    ):
        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()
        self.sync_service = sync_service or FirestoreSyncService(db=self.db, config=self.config)

    def _teacher_meta(self) -> Dict[str, Any]:
        tp = self.config.get_section("teacher_profile") or {}
        return {
            "teacher_id": str(tp.get("teacher_id") or tp.get("login_id") or "").strip(),
            "teacher_name": str(tp.get("name") or tp.get("teacher_id") or "담임교사").strip(),
            "school_code": str(tp.get("school_code") or tp.get("neis_school_code") or "").strip(),
            "school_name": str(tp.get("school_name") or "").strip(),
            "grade": int(tp["grade"]) if str(tp.get("grade") or "").isdigit() else None,
            "class_no": int(tp["class_no"]) if str(tp.get("class_no") or "").isdigit() else None,
        }

    def poll_and_execute_due_tasks(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        대기 중인 원격 작업을 조회해 예약 시각이 도래한 `pending` 작업을 순차 실행합니다.
        """
        ref_now = now or datetime.now()
        meta = self._teacher_meta()
        pending = self.sync_service.fetch_pending_remote_tasks(
            teacher_id=meta["teacher_id"],
            school_code=meta["school_code"],
        )
        results: List[Dict[str, Any]] = []
        for task in pending:
            if not isinstance(task, dict):
                continue
            status = str(task.get("exec_status") or "pending").strip().lower()
            if status not in ("", "pending"):
                continue
            if not is_task_due(task, now=ref_now):
                continue
            results.append(self.execute_task(task))
        return results

    def execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """단일 원격 작업을 실행하고 상태(`running` -> `done` / `error`)를 Firestore에 보고합니다."""
        task_id = int(task.get("id") or 0)
        remote_id = str(task.get("remote_id") or "").strip()
        task_type = str(task.get("task_type") or "general").strip().lower()
        title = str(task.get("title") or task_type or "모바일 원격 작업").strip()
        payload = _parse_action_payload(task.get("action_payload"))

        if remote_id:
            self.sync_service.update_remote_task_status(
                remote_id=remote_id,
                exec_status="running",
                memo=f"실행 중: {title}",
                is_done=False,
            )

        try:
            if task_type == "comcigan_sync":
                summary, detail = self._run_comcigan_sync(payload, task)
            elif task_type == "schoolbell_sync":
                summary, detail = self._run_schoolbell_sync(payload, task)
            elif task_type == "file_sort":
                summary, detail = self._run_file_sort(payload, task)
            elif task_type == "homeroom_report":
                summary, detail = self._run_homeroom_report(payload, task)
            else:
                dispatcher = MobileRemoteTaskDispatcher(
                    db=self.db,
                    config=self.config,
                    sync_service=self.sync_service,
                )
                # dispatcher가 중복 상태 업데이트하지 않도록 remote_id를 비워 실행
                sub_task = dict(task)
                sub_task["remote_id"] = ""
                sub_task["id"] = 0
                res = dispatcher.dispatch_task(sub_task)
                summary = str(res.get("summary") or f"작업 완료 ({title})")
                detail = dict(res.get("detail") or {})

            if task_id > 0:
                try:
                    self.db.complete_mobile_task(task_id)
                except Exception:
                    pass
            if remote_id:
                self.sync_service.update_remote_task_status(
                    remote_id=remote_id,
                    exec_status="done",
                    memo=summary,
                    is_done=True,
                )
            self.db.add_log("INFO", "RemoteTaskExecutor", f"[{task_type}] {summary}")
            return {
                "ok": True,
                "remote_id": remote_id,
                "task_type": task_type,
                "exec_status": "done",
                "summary": summary,
                "detail": detail,
            }
        except Exception as exc:
            err_msg = f"실행 실패 ({task_type}): {exc}"
            if remote_id:
                self.sync_service.update_remote_task_status(
                    remote_id=remote_id,
                    exec_status="error",
                    memo=err_msg,
                    is_done=False,
                )
            self.db.add_log("ERROR", "RemoteTaskExecutor", f"[{remote_id or task_type}] {err_msg}")
            return {
                "ok": False,
                "remote_id": remote_id,
                "task_type": task_type,
                "exec_status": "error",
                "error": str(exc),
                "summary": err_msg,
            }

    def _run_comcigan_sync(
        self,
        payload: Dict[str, Any],
        task: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        meta = self._teacher_meta()
        api_sec = self.config.get_section("api_and_integrations") or {}

        if payload.get("raw_data") or payload.get("changes"):
            res = push_comcigan_changes(
                raw_data=payload.get("raw_data") or payload.get("changes"),
                sync_service=self.sync_service,
                target_date=str(payload.get("date") or ""),
                grade=str(payload.get("grade") or meta.get("grade") or ""),
                class_no=str(payload.get("class_no") or meta.get("class_no") or ""),
            )
            if not res.get("ok"):
                raise RuntimeError(str(res.get("error") or "컴시간 변동 반영 실패"))
            return f"컴시간 시간표 변동 {res.get('pushed', 0)}건 반영 완료", res

        res = sync_comcigan_from_web(
            url=str(payload.get("url") or api_sec.get("comcigan_url") or "").strip(),
            school_code=str(
                payload.get("school_code") or api_sec.get("comcigan_school_code") or ""
            ).strip(),
            target_date=str(payload.get("date") or "").strip(),
            teacher_name=str(
                payload.get("teacher_name")
                or api_sec.get("comcigan_teacher_name")
                or meta.get("teacher_name")
                or ""
            ).strip(),
            target_grade=str(payload.get("grade") or meta.get("grade") or "").strip(),
            target_class=str(payload.get("class_no") or meta.get("class_no") or "").strip(),
            headless=bool(payload.get("headless", True)),
            sync_service=self.sync_service,
        )
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error") or "컴시간 시간표 동기화 실패"))
        base_cnt = int(res.get("base_count") or 0)
        chg_cnt = int(res.get("change_count") or 0)
        return f"컴시간 시간표 동기화 완료 (기본 {base_cnt}건 · 변동 {chg_cnt}건)", res

    def _run_schoolbell_sync(
        self,
        payload: Dict[str, Any],
        task: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        meta = self._teacher_meta()
        board_url = str(
            payload.get("board_url")
            or payload.get("target_url")
            or load_saved_schoolbell_board_url()
            or ""
        ).strip()
        if not board_url:
            raise RuntimeError(
                "저장된 학교종이 게시판 URL이 없습니다. 데스크톱에서 학교종이 공지를 1회 이상 먼저 수집해 주세요."
            )

        max_items = int(payload.get("max_items") or 15)
        headless = bool(payload.get("headless", True))
        crawler = SchoolbellCrawlerService()
        res = crawler.fetch_recent_notices(
            max_items=max_items,
            login_timeout_sec=int(payload.get("login_timeout_sec") or 60),
            headless=headless,
            school_name=str(payload.get("school_name") or meta.get("school_name") or ""),
            target_url=board_url,
        )
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error") or "학교종이 공지 수집 실패"))

        notices = list(res.get("notices") or [])
        latest_title = str(notices[0].get("title") or "").strip() if notices else ""
        tail = f" (최근: {latest_title[:30]})" if latest_title else ""
        return f"학교종이 최근 공지 {len(notices)}건 수집 완료{tail}", res

    def _run_file_sort(
        self,
        payload: Dict[str, Any],
        task: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        fs_sec = self.config.get_section("file_sorter") or {}
        source_dirs = payload.get("source_dirs")
        if not source_dirs:
            if payload.get("source_dir"):
                source_dirs = [str(payload["source_dir"])]
            else:
                cfg_dirs = fs_sec.get("source_dirs") or []
                if isinstance(cfg_dirs, str):
                    source_dirs = [cfg_dirs] if cfg_dirs.strip() else []
                else:
                    source_dirs = [str(d).strip() for d in cfg_dirs if str(d).strip()]
        if not source_dirs:
            raise RuntimeError("스마트 파일 자동 분류 대상 폴더(source_dirs)가 설정되지 않았습니다.")

        target_base_dir = str(
            payload.get("target_dir")
            or payload.get("target_base_dir")
            or fs_sec.get("target_base_dir")
            or (Path.home() / "Documents" / "EduFlow")
        ).strip()

        worker = BatchSortWorker(
            sorter_service=FileSorterService(db=self.db),
            source_dirs=list(source_dirs),
            target_base_dir=target_base_dir,
            action_mode=str(payload.get("action_mode") or fs_sec.get("action_mode") or "move"),
            duplicate_policy=str(
                payload.get("duplicate_policy") or fs_sec.get("duplicate_policy") or "number"
            ),
            include_unmatched=bool(
                payload.get("include_unmatched", fs_sec.get("include_unmatched", False))
            ),
            unmatched_folder_name=str(
                payload.get("unmatched_folder_name")
                or fs_sec.get("unmatched_folder_name")
                or "99_기타_미분류"
            ),
            ignore_temp_files=bool(
                payload.get("ignore_temp_files", fs_sec.get("ignore_temp_files", True))
            ),
        )
        detail = worker.execute()
        processed = int(
            detail.get("processed")
            or detail.get("moved_count")
            or detail.get("success_count")
            or 0
        )
        return f"스마트 파일 자동 분류 완료 ({processed}건 처리, 출력: {target_base_dir})", detail

    def _run_homeroom_report(
        self,
        payload: Dict[str, Any],
        task: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        meta = self._teacher_meta()
        fs_sec = self.config.get_section("file_sorter") or {}
        school_code = str(payload.get("school_code") or meta["school_code"] or "").strip()
        school_name = str(payload.get("school_name") or meta["school_name"] or "소속학교").strip()
        grade = payload.get("grade") if payload.get("grade") is not None else meta["grade"]
        class_no = payload.get("class_no") if payload.get("class_no") is not None else meta["class_no"]
        g_int = int(grade) if grade is not None and str(grade).isdigit() else 1
        c_int = int(class_no) if class_no is not None and str(class_no).isdigit() else 1

        # 1) 명렬표 수집
        students = self.db.get_active_homeroom_students()
        if not students:
            try:
                students = self.sync_service.fetch_homeroom_students(
                    school_code=school_code,
                    school_name=school_name,
                    grade=g_int,
                    class_no=c_int,
                )
            except Exception:
                students = []

        # 2) 출결 현황 수집 (로컬 SQLite + 원격 Firestore 병합)
        hr_key = f"{school_name}_{g_int}학년_{c_int}반"
        attendance_rows: List[Dict[str, Any]] = []
        try:
            attendance_rows.extend(self.db.get_attendance_logs(hr_key) or [])
        except Exception:
            pass
        try:
            remote_att = self.sync_service.fetch_homeroom_attendance(
                grade=g_int,
                class_no=c_int,
                school_code=school_code,
                school_name=school_name,
            )
            if remote_att:
                seen_att = {
                    (str(r.get("student_id") or ""), str(r.get("period_text") or r.get("date") or ""), str(r.get("category") or r.get("attendance_type") or ""))
                    for r in attendance_rows
                }
                for r in remote_att:
                    key = (
                        str(r.get("student_id") or ""),
                        str(r.get("date") or r.get("period_text") or ""),
                        str(r.get("attendance_type") or r.get("category") or ""),
                    )
                    if key not in seen_att:
                        attendance_rows.append(r)
                        seen_att.add(key)
        except Exception:
            pass

        # 3) 가정통신문 현황 수집
        notices: List[Dict[str, Any]] = []
        try:
            all_notices = self.sync_service.fetch_collection_rows("가정통신문_목록") or []
            for n in all_notices:
                if not isinstance(n, dict):
                    continue
                n_sc = str(n.get("school_code") or "").strip()
                if school_code and n_sc and n_sc != school_code:
                    continue
                n_g = n.get("target_grade") if n.get("target_grade") is not None else n.get("grade")
                n_c = n.get("target_class") if n.get("target_class") is not None else n.get("class_no")
                if n_g is not None and str(n_g).isdigit() and int(n_g) != g_int:
                    continue
                if n_c is not None and str(n_c).isdigit() and int(n_c) not in (0, c_int):
                    continue
                notices.append(n)
        except Exception:
            notices = []

        out_dir = Path(
            str(
                payload.get("output_dir")
                or (Path(fs_sec.get("target_base_dir") or (Path.home() / "Documents" / "EduFlow")) / "Reports")
            )
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_school = "".join(ch for ch in school_name if ch.isalnum() or ch in ("_", "-")) or "학교"
        fmt = str(payload.get("format") or "xlsx").strip().lower()

        saved_path: Path
        if fmt == "txt":
            saved_path = out_dir / f"학급현황요약_{safe_school}_{g_int}학년{c_int}반_{ts}.txt"
            lines = [
                f"[담임 학급 출결·가정통신문 현황 요약 보고서]",
                f"학교/학급: {school_name} {g_int}학년 {c_int}반",
                f"생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"재적 인원: {len(students)}명 | 출결 기록: {len(attendance_rows)}건 | 가정통신문: {len(notices)}건",
                "",
                "--- 출결 현황 ---",
            ]
            for r in attendance_rows:
                lines.append(
                    f"- {r.get('student_id', '')} {r.get('student_name', '')} | "
                    f"{r.get('category') or r.get('attendance_type') or ''} | "
                    f"{r.get('period_text') or r.get('date') or ''} | "
                    f"{r.get('reason', '')}"
                )
            lines.append("")
            lines.append("--- 가정통신문 현황 ---")
            for n in notices:
                lines.append(
                    f"- {n.get('title', '')} ({n.get('category', '가정통신문')}) | 마감: {n.get('due_date', '-')} | "
                    f"게시: {'게시됨' if n.get('is_published', True) else '후보'}"
                )
            saved_path.write_text("\n".join(lines), encoding="utf-8")
        else:
            import openpyxl

            saved_path = out_dir / f"학급현황요약_{safe_school}_{g_int}학년{c_int}반_{ts}.xlsx"
            wb = openpyxl.Workbook()
            ws_sum = wb.active
            ws_sum.title = "요약"
            ws_sum.append(["항목", "내용"])
            ws_sum.append(["학교명", school_name])
            ws_sum.append(["학급", f"{g_int}학년 {c_int}반"])
            ws_sum.append(["생성일시", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            ws_sum.append(["재적 인원", len(students)])
            ws_sum.append(["출결 기록 건수", len(attendance_rows)])
            ws_sum.append(["가정통신문 건수", len(notices)])

            ws_att = wb.create_sheet("출결현황")
            ws_att.append(["학번", "이름", "구분", "사유", "기간/날짜", "증빙/확인 상태"])
            for r in attendance_rows:
                ws_att.append(
                    [
                        str(r.get("student_id") or ""),
                        str(r.get("student_name") or ""),
                        str(r.get("category") or r.get("attendance_type") or ""),
                        str(r.get("reason") or ""),
                        str(r.get("period_text") or r.get("date") or ""),
                        str(r.get("proof_status") or ("확인 완료" if r.get("teacher_confirmed") else "미확인")),
                    ]
                )

            ws_noti = wb.create_sheet("가정통신문현황")
            ws_noti.append(["제목", "분류", "마감일", "게시 여부", "회신 필요"])
            for n in notices:
                ws_noti.append(
                    [
                        str(n.get("title") or ""),
                        str(n.get("category") or "가정통신문"),
                        str(n.get("due_date") or ""),
                        "게시됨" if n.get("is_published", True) else "후보",
                        "예" if n.get("requires_reply") else "아니오",
                    ]
                )
            wb.save(saved_path)

        summary = (
            f"담임 학급 현황 보고서 생성 완료 "
            f"(학생 {len(students)}명 · 출결 {len(attendance_rows)}건 · 가정통신문 {len(notices)}건) "
            f"| 저장 경로: {saved_path}"
        )
        return summary, {
            "report_path": str(saved_path),
            "student_count": len(students),
            "attendance_count": len(attendance_rows),
            "notice_count": len(notices),
        }
