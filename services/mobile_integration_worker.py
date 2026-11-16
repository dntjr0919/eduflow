"""
모바일 원격 예약 작업 실행기, 컴시간 알리미 변동 파서, 가정통신문 PDF 회신 추출 및 서명 일괄 합성 모듈.
- MobileRemoteTaskDispatcher: 모바일 Tasks 큐 작업을 로컬 모듈(파일 분류, 엑셀 병합, WebGeminiAutoWorker 등)로 실행 후 exec_status='done' 보고
- parse_comcigan_payload / push_comcigan_changes: 컴시간 알리미 변동 내역 파싱 및 시간표_변동 시트 푸시
- build_notice_extract_prompt / extract_notice_reply_schema: 가정통신문 PDF Playwright 첨부 항목 추출
- compose_signed_notice_pdfs: Google Drive 서명 PNG 일괄 다운로드 및 PDF 좌표 합성/출력
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.comcigan_playwright_fetcher import ComciganPlaywrightFetcher
from services.excel_merger_service import merge_excel_assignments
from services.file_sorter_service import BatchSortWorker, FileSorterService
from services.firestore_sync_service import FirestoreSyncService
from services.web_gemini_auto_worker import WebGeminiAutoWorker

_LOGGER = logging.getLogger(__name__)


def parse_comcigan_payload(
    raw_data: Any,
    target_date: str = "",
    grade: str = "",
    class_no: str = "",
) -> List[Dict[str, Any]]:
    """
    컴시간 알리미 JSON/텍스트/리스트 데이터를 시간표_변동 시트 스키마 행 목록으로 정규화합니다.
    """
    date_str = str(target_date or datetime.now().strftime("%Y-%m-%d")).strip()
    rows: List[Dict[str, Any]] = []

    if isinstance(raw_data, str):
        stripped = raw_data.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                raw_data = json.loads(stripped)
            except json.JSONDecodeError:
                raw_data = stripped

    if isinstance(raw_data, dict):
        items = raw_data.get("changes") or raw_data.get("rows") or raw_data.get("data") or []
    elif isinstance(raw_data, list):
        items = raw_data
    elif isinstance(raw_data, str):
        # 예: "3교시 화학Ⅰ -> 자습 (박OO) [보강]" 또는 "1-3 2교시 수학->영어"
        items = []
        for line in raw_data.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(
                r"(?:(\d+)\s*[-학년반\s]+(\d+)\s*반?\s*)?(\d+)\s*교시\s*([^\s\-→>]+)\s*(?:->|→)\s*([^\s(]+)(?:\s*\(([^)]+)\))?",
                line,
            )
            if m:
                g = m.group(1) or grade or "1"
                c = m.group(2) or class_no or "1"
                period = int(m.group(3))
                orig_subj = m.group(4).strip()
                chg_subj = m.group(5).strip()
                chg_teacher = (m.group(6) or "").strip()
                items.append(
                    {
                        "grade": g,
                        "class_no": c,
                        "period": period,
                        "original_subject": orig_subj,
                        "changed_subject": chg_subj,
                        "changed_teacher": chg_teacher,
                        "reason": "컴시간 알리미 변동",
                    }
                )
    else:
        items = []

    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        g = str(item.get("grade") or grade or "1").strip()
        c = str(item.get("class_no") or item.get("class") or class_no or "1").strip()
        period = str(item.get("period") or "").strip()
        if not period:
            continue
        d_str = str(item.get("date") or date_str).strip()
        rid = str(item.get("remote_id") or f"tc_{d_str.replace('-', '')}_{g}_{c}_{period}_{idx}").strip()
        rows.append(
            {
                "remote_id": rid,
                "date": d_str,
                "grade": g,
                "class_no": c,
                "period": period,
                "original_subject": str(item.get("original_subject") or item.get("orig_subject") or "").strip(),
                "changed_subject": str(item.get("changed_subject") or item.get("subject") or "").strip(),
                "original_teacher": str(item.get("original_teacher") or "").strip(),
                "changed_teacher": str(item.get("changed_teacher") or item.get("teacher") or "").strip(),
                "reason": str(item.get("reason") or "컴시간 알리미 자동 반영").strip(),
                "source": "comcigan",
            }
        )
    return rows


def push_comcigan_changes(
    raw_data: Any,
    sync_service: Optional[FirestoreSyncService] = None,
    target_date: str = "",
    grade: str = "",
    class_no: str = "",
) -> Dict[str, Any]:
    """컴시간 변동 내역을 파싱해 시간표_변동 시트로 Push합니다."""
    svc = sync_service or FirestoreSyncService()
    rows = parse_comcigan_payload(raw_data, target_date=target_date, grade=grade, class_no=class_no)
    if not rows:
        return {"ok": False, "pushed": 0, "error": "파싱된 시간표 변동 항목이 없습니다."}
    ok = svc.push_timetable_changes(rows)
    return {"ok": ok, "pushed": len(rows) if ok else 0, "rows": rows}


def compute_comcigan_snapshot_hash(
    base_rows: List[Dict[str, Any]],
    change_rows: List[Dict[str, Any]],
) -> str:
    """파싱된 기본/변동 시간표 스냅샷의 결정적 SHA-256 해시를 계산합니다."""
    payload = json.dumps(
        {"base_rows": base_rows, "change_rows": change_rows},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sync_comcigan_from_web(
    url: str = "",
    school_code: str = "",
    target_date: str = "",
    teacher_name: str = "",
    target_grade: str = "",
    target_class: str = "",
    scope: str = "homeroom_and_teacher",
    headless: bool = True,
    sync_service: Optional[FirestoreSyncService] = None,
    fetcher: Optional[ComciganPlaywrightFetcher] = None,
    prev_snapshot_hash: str = "",
    skip_if_unchanged: bool = False,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """Playwright로 컴시간 웹 시간표를 파싱한 뒤 시간표_기본 및 시간표_변동 시트로 반영합니다."""
    def _report(pct: int, msg: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(max(0, min(100, int(pct))), msg)
            except Exception:
                pass

    cfg_mgr = ConfigManager()
    cfg_sec = cfg_mgr.get_section("api_and_integrations") or {}
    tp_sec = cfg_mgr.get_section("teacher_profile") or {}
    eff_url = str(url or cfg_sec.get("comcigan_url", "") or "").strip()
    eff_code = str(
        school_code
        or tp_sec.get("neis_school_code")
        or cfg_sec.get("comcigan_school_code", "")
        or ""
    ).strip()
    eff_school_name = str(tp_sec.get("school_name") or "").strip()
    eff_teacher = str(
        teacher_name
        or cfg_sec.get("comcigan_teacher_name")
        or tp_sec.get("name")
        or ""
    ).strip()
    eff_date = str(target_date or datetime.now().strftime("%Y-%m-%d")).strip()

    if not eff_url and fetcher is None:
        return {
            "ok": False,
            "skipped": False,
            "changed": False,
            "snapshot_hash": "",
            "base_count": 0,
            "class_base_count": 0,
            "teacher_base_count": 0,
            "change_count": 0,
            "error": "컴시간 접속 URL이 설정되지 않았습니다.",
        }

    active_fetcher = fetcher or ComciganPlaywrightFetcher(
        url=eff_url,
        school_code=eff_code,
        target_date=eff_date,
        teacher_name=eff_teacher,
        target_grade=str(target_grade or "").strip(),
        target_class=str(target_class or "").strip(),
        scope=scope,
        headless=headless,
    )
    fetched = (
        active_fetcher.fetch(progress_callback=progress_callback)
        if progress_callback is not None
        else active_fetcher.fetch()
    )
    base_rows = list(fetched.get("base_rows") or [])
    change_rows = list(fetched.get("change_rows") or [])

    from services.timetable_manual_service import normalize_teacher_name

    for b_row in base_rows:
        if not isinstance(b_row, dict):
            continue
        if eff_code and not b_row.get("school_code"):
            b_row["school_code"] = eff_code
        if eff_school_name and not b_row.get("school_name"):
            b_row["school_name"] = eff_school_name
        if not b_row.get("source"):
            b_row["source"] = "comcigan"
        if b_row.get("teacher_name"):
            norm_t = normalize_teacher_name(b_row["teacher_name"])
            b_row["teacher_name"] = norm_t
            b_row["teacher"] = norm_t

    for c_row in change_rows:
        if not isinstance(c_row, dict):
            continue
        if eff_code and not c_row.get("school_code"):
            c_row["school_code"] = eff_code
        if eff_school_name and not c_row.get("school_name"):
            c_row["school_name"] = eff_school_name
        if not c_row.get("source") or c_row.get("source") == "comcigan_playwright":
            c_row["source"] = "comcigan"
        if c_row.get("original_teacher"):
            c_row["original_teacher"] = normalize_teacher_name(c_row["original_teacher"])
        if c_row.get("changed_teacher"):
            c_row["changed_teacher"] = normalize_teacher_name(c_row["changed_teacher"])

    cls_cnt = int(fetched.get("class_base_count", len(base_rows)) or 0)
    tch_cnt = int(fetched.get("teacher_base_count", 0) or 0)
    snap_hash = compute_comcigan_snapshot_hash(base_rows, change_rows)

    if skip_if_unchanged and prev_snapshot_hash and snap_hash == prev_snapshot_hash:
        return {
            "ok": True,
            "skipped": True,
            "changed": False,
            "snapshot_hash": snap_hash,
            "url": eff_url,
            "school_code": eff_code,
            "target_date": eff_date,
            "base_count": len(base_rows),
            "class_base_count": cls_cnt,
            "teacher_base_count": tch_cnt,
            "change_count": len(change_rows),
            "base_pushed": 0,
            "changes_pushed": 0,
            "error": "",
        }

    svc = sync_service or FirestoreSyncService()

    # 시간표_기본과 대조해 '(기존수업)' 대신 실제 원래 과목/교사명을 보완 (entry_type == "class" 우선)
    if change_rows:
        _report(78, "기존 기본 시간표 조회 및 변동 과목 매칭 중...")
        class_lookup: Dict[Tuple[str, str, str, str], Tuple[str, str]] = {}
        fallback_lookup: Dict[Tuple[str, str, str, str], Tuple[str, str]] = {}
        teacher_lookup: Dict[Tuple[str, str, str], Tuple[str, str]] = {}
        try:
            existing_base = svc.fetch_sheet_rows("시간표_기본") or []
        except Exception:
            existing_base = []
        for b in list(existing_base) + list(base_rows):
            if not isinstance(b, dict):
                continue
            g_val = str(b.get("grade") or "").strip()
            c_val = str(b.get("class_no") or "").strip()
            d_val = str(b.get("day_of_week") or "").strip()
            p_val = str(b.get("period") or "").strip()
            subj = str(b.get("subject") or "").strip()
            tchr = str(b.get("teacher_name") or b.get("teacher") or "").strip()
            if not subj or not d_val or not p_val:
                continue
            e_type = str(b.get("entry_type") or "").strip().lower()
            key = (g_val, c_val, d_val, p_val)
            if g_val and c_val:
                if e_type in ("", "class"):
                    class_lookup[key] = (subj, tchr)
                else:
                    fallback_lookup[key] = (subj, tchr)
            if tchr:
                t_key = (tchr, d_val, p_val)
                if e_type == "teacher" or t_key not in teacher_lookup:
                    teacher_lookup[t_key] = (subj, tchr)

        weekday_ko = ("월", "화", "수", "목", "금", "토", "일")
        for chg in change_rows:
            orig_s = str(chg.get("original_subject") or "").strip()
            if orig_s and orig_s != "(기존수업)":
                continue
            d_str = str(chg.get("date") or chg.get("target_date") or "").strip()
            try:
                day_name = weekday_ko[datetime.strptime(d_str, "%Y-%m-%d").weekday()]
            except ValueError:
                day_name = ""
            p_val = str(chg.get("period") or "").strip()
            key = (
                str(chg.get("grade") or "").strip(),
                str(chg.get("class_no") or "").strip(),
                day_name,
                p_val,
            )
            matched = class_lookup.get(key) or fallback_lookup.get(key)
            if not matched:
                chg_tchr = str(chg.get("original_teacher") or chg.get("changed_teacher") or "").strip()
                if chg_tchr and day_name and p_val:
                    matched = teacher_lookup.get((chg_tchr, day_name, p_val))
            if matched:
                chg["original_subject"] = matched[0]
                if not chg.get("original_teacher"):
                    chg["original_teacher"] = matched[1]

    if base_rows:
        _report(82, f"Google Sheets 기본 시간표 반영 중 ({len(base_rows)}건)...")
        if progress_callback is not None:
            base_ok = svc.upsert_timetable_base(
                base_rows,
                progress_callback=lambda b_idx, b_tot: _report(
                    82 + int(10 * b_idx / max(1, b_tot)),
                    f"Google Sheets 기본 시간표 반영 중 ({b_idx}/{b_tot} 배치)",
                ),
            )
        else:
            base_ok = svc.upsert_timetable_base(base_rows)
    else:
        base_ok = True

    if change_rows:
        _report(93, f"Google Sheets 시간표 변동 반영 중 ({len(change_rows)}건)...")
        if progress_callback is not None:
            chg_ok = svc.push_timetable_changes(
                change_rows,
                progress_callback=lambda b_idx, b_tot: _report(
                    93 + int(6 * b_idx / max(1, b_tot)),
                    f"Google Sheets 시간표 변동 반영 중 ({b_idx}/{b_tot} 배치)",
                ),
            )
        else:
            chg_ok = svc.push_timetable_changes(change_rows)
    else:
        chg_ok = True

    ok = base_ok and chg_ok
    if ok:
        _report(100, f"동기화 완료 (기본 {len(base_rows)}건, 변동 {len(change_rows)}건)")

    failed_steps: List[str] = []
    if not base_ok:
        failed_steps.append(f"시간표_기본 Upsert 실패 ({len(base_rows)}건)")
    if not chg_ok:
        failed_steps.append(f"시간표_변동 Push 실패 ({len(change_rows)}건)")
    error_msg = ", ".join(failed_steps)
    if error_msg:
        _LOGGER.warning("컴시간 Google Sheets 동기화 실패: %s", error_msg)

    return {
        "ok": ok,
        "skipped": False,
        "changed": True,
        "snapshot_hash": snap_hash,
        "url": eff_url,
        "school_code": eff_code,
        "target_date": eff_date,
        "base_count": len(base_rows),
        "class_base_count": cls_cnt,
        "teacher_base_count": tch_cnt,
        "change_count": len(change_rows),
        "base_pushed": len(base_rows) if base_ok else 0,
        "changes_pushed": len(change_rows) if chg_ok else 0,
        "error": error_msg,
    }


def build_notice_extract_prompt(pdf_path: Path | str) -> str:
    """가정통신문 PDF에서 회신서 항목과 전자서명 영역 정보를 추출하기 위한 프롬프트를 생성합니다."""
    name = Path(pdf_path).name
    return (
        f"첨부된 가정통신문 파일({name})을 분석해 학부모 회신서(동의서/신청서) 항목을 JSON으로 추출하세요.\n"
        "반드시 아래 키를 포함한 JSON 객체만 출력하세요:\n"
        "{\n"
        '  "title": "가정통신문 제목",\n'
        '  "due_date": "YYYY-MM-DD 또는 빈 문자열",\n'
        '  "requires_signature": true,\n'
        '  "reply_fields": [\n'
        '    {"key": "consent", "label": "참가 동의 여부", "type": "radio", "options": ["동의", "미동의"]}\n'
        "  ]\n"
        "}"
    )


def extract_notice_reply_schema(
    pdf_path: Path | str,
    provider: str = "gemini",
    headless: bool = False,
) -> Dict[str, Any]:
    """WebGeminiAutoWorker에 PDF를 첨부하여 회신서 항목 JSON을 추출합니다."""
    p = Path(pdf_path)
    if not p.exists():
        raise FileNotFoundError(f"가정통신문 PDF를 찾을 수 없습니다: {p}")
    prompt = build_notice_extract_prompt(p)
    worker = WebGeminiAutoWorker(
        provider=provider,
        headless=headless,
        single_prompt=prompt,
        attachment_paths=[str(p)],
        task_label=f"가정통신문 회신서 추출 ({p.name})",
    )
    res = worker.execute()
    return {"ok": True, "pdf_path": str(p), "worker_result": res}


def _download_signature_bytes(url_or_path: str, timeout: float = 10.0) -> Optional[bytes]:
    src = str(url_or_path or "").strip()
    if not src:
        return None
    local_p = Path(src)
    if local_p.exists() and local_p.is_file():
        return local_p.read_bytes()
    if src.startswith("http://") or src.startswith("https://"):
        req = urllib.request.Request(src, headers={"User-Agent": "EduFlow/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    return None


def compose_signed_notice_pdfs(
    replies: List[Dict[str, Any]],
    output_dir: Path | str,
    template_pdf_path: Optional[Path | str] = None,
    notice_title: str = "가정통신문 회신서",
    signature_rect: Tuple[int, int, int, int] = (360, 620, 180, 75),
) -> Dict[str, Any]:
    """
    가정통신문_회신 행 목록의 서명 PNG를 다운로드하여 학생별 서명 확인 PDF를 생성(또는 원본 PDF에 합성)합니다.
    """
    from PyQt6.QtCore import QRect, Qt
    from PyQt6.QtGui import QFont, QImage, QPainter, QPdfWriter, QPageSize

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_files: List[str] = []

    has_pypdf = False
    try:
        import pypdf  # type: ignore

        has_pypdf = True
    except ImportError:
        pypdf = None  # type: ignore

    tpl_path = Path(template_pdf_path) if template_pdf_path else None
    use_tpl_merge = bool(has_pypdf and tpl_path and tpl_path.exists())

    for idx, rep in enumerate(replies, start=1):
        stu_id = str(rep.get("student_id") or f"stu_{idx}").strip()
        stu_name = str(rep.get("student_name") or "").strip()
        parent_name = str(rep.get("parent_name") or "").strip()
        consent = str(rep.get("consent") or "동의").strip()
        signed_at = str(rep.get("signed_at") or "").strip()
        sig_src = str(rep.get("signature_drive_url") or rep.get("signature_local_path") or "").strip()

        overlay_pdf = out_dir / f"_tmp_overlay_{stu_id}.pdf"
        final_pdf = out_dir / f"signed_{stu_id}_{stu_name or 'student'}.pdf"

        target_for_paint = overlay_pdf if use_tpl_merge else final_pdf
        writer = QPdfWriter(str(target_for_paint))
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setResolution(150)

        painter = QPainter(writer)
        try:
            painter.setFont(QFont("Malgun Gothic", 11))
            if not use_tpl_merge:
                painter.drawText(80, 100, f"[{notice_title}]")
                painter.drawText(80, 160, f"학번: {stu_id}   학생명: {stu_name}")
                painter.drawText(80, 210, f"보호자: {parent_name}   회신: {consent}")
                painter.drawText(80, 260, f"서명일시: {signed_at}")
                painter.drawRect(75, 310, 500, 180)
                painter.drawText(90, 345, "전자서명 확인란:")
                sig_box = QRect(120, 360, 260, 110)
            else:
                x, y, w, h = signature_rect
                painter.drawText(x, max(40, y - 15), f"{stu_id} {stu_name} ({consent})")
                sig_box = QRect(x, y, w, h)

            sig_bytes = _download_signature_bytes(sig_src) if sig_src else None
            if sig_bytes:
                img = QImage()
                if img.loadFromData(sig_bytes):
                    scaled = img.scaled(
                        sig_box.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    painter.drawImage(sig_box.topLeft(), scaled)
        finally:
            painter.end()

        if use_tpl_merge and pypdf is not None and tpl_path is not None:
            base_reader = pypdf.PdfReader(str(tpl_path))
            over_reader = pypdf.PdfReader(str(overlay_pdf))
            pdf_writer = pypdf.PdfWriter()
            for p_idx, page in enumerate(base_reader.pages):
                if p_idx == len(base_reader.pages) - 1 and len(over_reader.pages) > 0:
                    page.merge_page(over_reader.pages[0])
                pdf_writer.add_page(page)
            with open(final_pdf, "wb") as fp:
                pdf_writer.write(fp)
            if overlay_pdf.exists():
                overlay_pdf.unlink()

        generated_files.append(str(final_pdf))

    return {
        "ok": True,
        "count": len(generated_files),
        "output_dir": str(out_dir),
        "files": generated_files,
    }


class MobileRemoteTaskDispatcher:
    """모바일에서 예약한 원격 작업(Tasks 시트)을 로컬 서비스로 자동 실행하고 완료 상태를 보고합니다."""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
        sync_service: Optional[FirestoreSyncService] = None,
    ):
        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()
        self.sync_service = sync_service or FirestoreSyncService(db=self.db, config=self.config)

    @staticmethod
    def _parse_payload(raw_payload: Any) -> Dict[str, Any]:
        if isinstance(raw_payload, dict):
            return raw_payload
        if isinstance(raw_payload, str) and raw_payload.strip():
            try:
                parsed = json.loads(raw_payload)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {"prompt": raw_payload.strip()}
        return {}

    def dispatch_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """단일 모바일 원격 작업을 task_type에 따라 실행하고 exec_status='done'을 보고합니다."""
        task_id = int(task.get("id") or 0)
        remote_id = str(task.get("remote_id") or "").strip()
        task_type = str(task.get("task_type") or "general").strip().lower()
        title = str(task.get("title") or "모바일 원격 작업").strip()
        payload = self._parse_payload(task.get("action_payload"))

        if remote_id:
            self.sync_service.push_task_status(remote_id, "running", is_done=False)

        summary = ""
        detail: Dict[str, Any] = {}

        if task_type == "file_sort":
            source_dirs = payload.get("source_dirs") or (
                [payload["source_dir"]] if payload.get("source_dir") else []
            )
            if not source_dirs:
                default_src = str(self.config.get("paths", "watch_dir", "") or "").strip()
                if default_src:
                    source_dirs = [default_src]
            target_dir = str(
                payload.get("target_dir")
                or self.config.get("paths", "output_dir", "")
                or Path.cwd() / "EduFlow_Output"
            )
            if not source_dirs:
                raise ValueError("file_sort 작업에 source_dirs가 지정되지 않았습니다.")
            worker = BatchSortWorker(
                sorter_service=FileSorterService(db=self.db),
                source_dirs=source_dirs,
                target_base_dir=target_dir,
                action_mode=str(payload.get("action_mode", "move")),
            )
            detail = worker.execute()
            summary = f"파일 분류 완료 ({detail.get('processed', 0)}건 처리)"

        elif task_type == "excel_merge":
            excel_paths = payload.get("excel_paths") or []
            if not excel_paths:
                raise ValueError("excel_merge 작업에 excel_paths가 지정되지 않았습니다.")
            detail = merge_excel_assignments(
                excel_paths=excel_paths,
                output_path=payload.get("output_path"),
            )
            summary = f"엑셀 병합 완료 ({detail.get('output_path', '')})"

        elif task_type in ("web_llm", "notice_extract"):
            prompt = str(payload.get("prompt") or task.get("target_info") or title).strip()
            attachments = payload.get("attachment_paths") or []
            if task_type == "notice_extract" and payload.get("pdf_path"):
                attachments = [str(payload["pdf_path"])]
                prompt = build_notice_extract_prompt(payload["pdf_path"])
            worker = WebGeminiAutoWorker(
                provider=str(payload.get("provider", "gemini")),
                headless=bool(payload.get("headless", False)),
                single_prompt=prompt,
                attachment_paths=attachments,
                task_label=title,
            )
            detail = worker.execute()
            summary = f"웹 LLM 작업 완료 ({title})"

        elif task_type == "comcigan_sync":
            detail = push_comcigan_changes(
                raw_data=payload.get("raw_data") or payload.get("changes") or "",
                sync_service=self.sync_service,
                target_date=str(payload.get("date", "")),
                grade=str(payload.get("grade", "")),
                class_no=str(payload.get("class_no", "")),
            )
            summary = f"컴시간 변동 {detail.get('pushed', 0)}건 푸시 완료"

        elif task_type == "signature_pdf":
            replies = payload.get("replies") or self.sync_service.fetch_sheet_rows("가정통신문_회신")
            notice_id = str(payload.get("notice_id", "")).strip()
            if notice_id:
                replies = [r for r in replies if str(r.get("notice_id", "")).strip() == notice_id]
            out_dir = payload.get("output_dir") or (Path.cwd() / "EduFlow_Output" / "Signed_Notices")
            detail = compose_signed_notice_pdfs(
                replies=replies,
                output_dir=out_dir,
                template_pdf_path=payload.get("template_pdf_path"),
                notice_title=str(payload.get("notice_title", title)),
            )
            summary = f"서명 PDF {detail.get('count', 0)}건 생성 완료"

        else:
            summary = f"작업 확인 및 완료 처리 ({title})"

        if task_id > 0:
            self.db.complete_mobile_task(task_id)
        if remote_id:
            self.sync_service.push_task_status(remote_id, "done", is_done=True, memo=summary)

        self.db.add_log("INFO", "MobileRemoteTask", summary)
        return {"ok": True, "remote_id": remote_id, "summary": summary, "detail": detail}
