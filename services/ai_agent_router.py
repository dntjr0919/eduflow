from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.database import DatabaseManager
from services.excel_merger_service import merge_excel_assignments
from services.file_sorter_service import BatchSortWorker, FileSorterService
from services.llm_client import extract_json_from_llm_response
from services.multi_doc_extractor_service import extract_raw_text_preview
from services.record_generator_service import extract_text_from_xlsx

MAX_PW_ATTACHMENTS = 10
MAX_EXTRACTED_CHARS_PER_FILE = 4000

TEXT_READABLE_EXTS = {
    ".txt",
    ".md",
    ".py",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".js",
    ".ts",
    ".css",
    ".sql",
}

PLAYWRIGHT_SUPPORTED_EXTS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".pdf",
    ".docx",
    ".xlsx",
    ".xls",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
}

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "query_calendar_todos": {
        "kind": "read",
        "label": "캘린더/할일 조회",
        "description": "로컬 DB에서 일정 및 할일 목록을 조회합니다.",
        "args": {
            "start_date": "조회 시작일 (YYYY-MM-DD, 선택)",
            "end_date": "조회 종료일 (YYYY-MM-DD, 선택)",
            "include_done": "완료된 할일 포함 여부 (bool, 기본 true)",
            "keyword": "제목/메모 검색어 (선택)",
        },
    },
    "query_class_roster": {
        "kind": "read",
        "label": "학급 명렬표/출결 조회",
        "description": "활성 학급 명렬표와 출결 기록을 조회합니다.",
        "args": {
            "homeroom_key": "담임반 키 (선택)",
            "keyword": "학생 이름/학번 검색어 (선택)",
        },
    },
    "scan_folder_files": {
        "kind": "read",
        "label": "폴더 파일 목록 스캔",
        "description": "지정된 폴더 경로의 하위 파일/폴더 트리를 스캔합니다.",
        "args": {
            "folder_path": "스캔할 폴더 절대 경로 (필수)",
            "max_items": "최대 항목 수 (기본 50)",
        },
    },
    "get_session_history": {
        "kind": "read",
        "label": "세션 대화 문맥 조회",
        "description": "현재 대화 세션의 이전 메시지 기록을 로컬 DB에서 조회합니다.",
        "args": {
            "limit": "조회할 최근 메시지 수 (기본 20)",
            "keyword": "메시지 내용 검색어 (선택)",
        },
    },
    "add_calendar_todo": {
        "kind": "action",
        "label": "일정/할일 추가",
        "description": "새 일정 또는 할일을 로컬 DB에 등록합니다.",
        "args": {
            "item_type": "'schedule' 또는 'todo'",
            "title": "제목 (필수)",
            "date": "날짜 YYYY-MM-DD (선택)",
            "start_time": "시작 시각 HH:MM (선택)",
            "priority": "할일 우선순위 high|normal|low (선택)",
            "category": "일정 분류 (선택)",
            "memo": "메모 (선택)",
        },
    },
    "sort_files_auto": {
        "kind": "action",
        "label": "파일 자동 분류",
        "description": "분류 규칙에 따라 폴더 내 파일을 자동으로 이동/복사 정리합니다.",
        "args": {
            "source_dir": "원본 폴더 경로 (필수)",
            "target_base_dir": "분류 대상 기준 폴더 경로 (선택, 미지정 시 source_dir)",
            "action_mode": "'move' 또는 'copy' (기본 'move')",
        },
    },
    "merge_excel_assignments": {
        "kind": "action",
        "label": "다중 엑셀 병합",
        "description": "여러 과제 엑셀 파일을 학번 기준으로 가로 병합합니다.",
        "args": {
            "excel_paths": "병합할 엑셀 파일 경로 리스트 (필수)",
            "output_path": "출력 엑셀 경로 (선택)",
        },
    },
    "process_hwpx_or_ocr": {
        "kind": "action",
        "label": "HWPX/OCR 작업 전환",
        "description": "HWPX AI 에이전트 또는 AI OCR 타겟으로 전환해 문서를 처리합니다.",
        "args": {
            "target_id": "'hwpx_agent' 또는 'ocr'",
            "instruction": "처리 지시사항 (선택)",
        },
    },
}


def build_agent_system_prompt() -> str:
    """3모드(API/Playwright/수동) 공통 에이전트 도구 스키마 및 JSON 액션 규격 시스템 프롬프트를 생성합니다."""
    lines = [
        "당신은 교직 실무 어시스턴트 EduFlow 에이전트입니다.",
        "일반 질문에는 자연어로 답하고, 아래 EduFlow 내부 도구를 사용해야 할 때는 반드시 단일 JSON 블록으로 응답하세요.",
        "현재 세션의 이전 대화 맥락이 필요하다고 판단되면 {\"tool\": \"get_session_history\", \"args\": {\"limit\": 20}} JSON을 출력하세요.",
        "",
        "[도구 호출 JSON 규격]",
        '```json',
        '{"tool": "도구명", "args": {...}, "reply": "사용자에게 보여줄 안내 문구"}',
        '```',
        "",
        "[사용 가능한 도구 목록]",
    ]
    for name, meta in TOOL_REGISTRY.items():
        kind_lbl = "조회형(즉시 자동 실행)" if meta["kind"] == "read" else "실행형(사용자 승인 후 실행)"
        args_desc = ", ".join(f"{k}: {v}" for k, v in (meta.get("args") or {}).items())
        lines.append(f"- {name} [{kind_lbl}]: {meta['description']} | args=({args_desc})")
    return "\n".join(lines)


def parse_agent_response(raw_text: str) -> Dict[str, Any]:
    """LLM 응답 텍스트에서 도구 호출 JSON({tool, args, reply})을 파싱합니다."""
    txt = str(raw_text or "").strip()
    if not txt:
        return {
            "is_tool_call": False,
            "tool_name": "",
            "tool_kind": "",
            "args": {},
            "reply": "",
            "raw_text": txt,
        }

    parsed: Dict[str, Any] = {}
    try:
        cand = extract_json_from_llm_response(txt)
        if isinstance(cand, dict):
            parsed = cand
    except Exception:
        parsed = {}

    if not parsed:
        # fallback: 직접 중괄호 JSON 블록 탐지
        match = re.search(r"\{[\s\S]*\"tool\"[\s\S]*\}", txt)
        if match:
            try:
                cand = json.loads(match.group(0))
                if isinstance(cand, dict):
                    parsed = cand
            except Exception:
                parsed = {}

    tool_name = str(parsed.get("tool", "") or "").strip()
    if tool_name and tool_name in TOOL_REGISTRY:
        meta = TOOL_REGISTRY[tool_name]
        args = parsed.get("args")
        if not isinstance(args, dict):
            args = {}
        reply = str(parsed.get("reply", "") or "").strip()
        return {
            "is_tool_call": True,
            "tool_name": tool_name,
            "tool_kind": str(meta["kind"]),
            "tool_label": str(meta["label"]),
            "args": args,
            "reply": reply,
            "raw_text": txt,
        }

    return {
        "is_tool_call": False,
        "tool_name": "",
        "tool_kind": "",
        "tool_label": "",
        "args": {},
        "reply": txt,
        "raw_text": txt,
    }


def summarize_tool_args(tool_name: str, args: Dict[str, Any]) -> str:
    """확인 카드에 표시할 간결한 인자 요약을 반환합니다."""
    if not args:
        return "(인자 없음)"
    parts: List[str] = []
    for k, v in args.items():
        if isinstance(v, list):
            val_str = f"{len(v)}개 항목"
        else:
            val_str = str(v)
        if len(val_str) > 48:
            val_str = val_str[:45] + "..."
        parts.append(f"{k}={val_str}")
    return ", ".join(parts)


def extract_file_text_for_prompt(file_path: Path | str, max_chars: int = MAX_EXTRACTED_CHARS_PER_FILE) -> str:
    """API / 웹 미지원 첨부파일을 위해 파일 텍스트를 추출합니다."""
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return "(파일을 찾을 수 없습니다.)"
    ext = p.suffix.lower()
    try:
        if ext in TEXT_READABLE_EXTS:
            for enc in ("utf-8", "cp949", "euc-kr"):
                try:
                    txt = p.read_text(encoding=enc).strip()
                    return txt[:max_chars] if txt else "(빈 파일)"
                except UnicodeDecodeError:
                    continue
            return "(텍스트 인코딩 판별 실패)"
        if ext in (".xlsx", ".xls"):
            txt = extract_text_from_xlsx(p).strip()
            return txt[:max_chars] if txt else "(빈 시트)"
        if ext in (".docx", ".hwpx", ".pdf", ".hwp"):
            return extract_raw_text_preview(p, max_chars=max_chars)
    except Exception as exc:
        return f"(텍스트 추출 오류: {exc})"
    return f"(바이너리/미지원 형식: {ext or '확장자 없음'})"


def prepare_attachments_for_mode(
    attachments: List[Dict[str, Any]],
    mode_idx: int,
    max_pw_files: int = MAX_PW_ATTACHMENTS,
) -> Dict[str, Any]:
    """
    모드(0=API, 1=수동, 2=Playwright)에 맞춰 첨부 파일/폴더 컨텍스트를 조립합니다.
    - 폴더: 모든 모드에서 경로 + 하위 파일 목록 메타데이터만 프롬프트에 포함
    - API/수동: 텍스트/문서 파일은 텍스트를 추출해 프롬프트에 포함
    - Playwright: 웹 지원 확장자는 max_pw_files 상한까지 pw_attach_paths에 담고,
      미지원 확장자(.hwp 등) 또는 상한 초과 파일은 텍스트 추출 후 프롬프트에 병합
    """
    blocks: List[str] = []
    pw_attach_paths: List[str] = []

    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "file"))
        name = str(item.get("name", "") or "")
        path_str = str(item.get("path", "") or "").strip()
        if not path_str:
            continue

        if item_type == "dir":
            files = item.get("files") or []
            file_count = int(item.get("file_count", len(files)))
            truncated = bool(item.get("truncated", False))
            tree_lines = "\n".join(f"  - {f}" for f in files) if files else "  (하위 파일 없음)"
            trunc_note = "\n  ... (일부 파일 생략됨)" if truncated else ""
            blocks.append(
                f"[첨부 폴더] {name}\n"
                f"- folder_path: {path_str}\n"
                f"- 하위 파일 ({file_count}개):\n{tree_lines}{trunc_note}"
            )
            continue

        p = Path(path_str)
        ext = p.suffix.lower()
        if mode_idx == 2:
            if ext in PLAYWRIGHT_SUPPORTED_EXTS and len(pw_attach_paths) < max_pw_files and p.is_file():
                pw_attach_paths.append(str(p.resolve()))
                blocks.append(f"[웹 첨부 파일] {name or p.name} ({path_str})")
            else:
                extracted = extract_file_text_for_prompt(p)
                blocks.append(
                    f"[첨부 파일 텍스트 추출] {name or p.name} ({path_str})\n"
                    f"```\n{extracted}\n```"
                )
        else:
            extracted = extract_file_text_for_prompt(p)
            blocks.append(
                f"[첨부 파일] {name or p.name} ({path_str})\n"
                f"```\n{extracted}\n```"
            )

    return {
        "prompt_context": "\n\n".join(blocks).strip(),
        "pw_attach_paths": pw_attach_paths,
    }


def execute_read_only_tool(
    tool_name: str,
    args: Dict[str, Any],
    db: Optional[DatabaseManager] = None,
    session_id: Optional[int] = None,
) -> Dict[str, Any]:
    """조회형(read-only) 도구를 즉시 실행하고 구조화된 결과를 반환합니다."""
    db_mgr = db or DatabaseManager()
    if tool_name == "query_calendar_todos":
        start_date = str(args.get("start_date", "") or "").strip() or None
        end_date = str(args.get("end_date", "") or "").strip() or None
        include_done = bool(args.get("include_done", True))
        keyword = str(args.get("keyword", "") or "").strip().lower()

        schedules = db_mgr.get_schedules(start_date=start_date, end_date=end_date)
        todos = db_mgr.get_todos(include_done=include_done)
        if keyword:
            schedules = [
                s for s in schedules
                if keyword in str(s.get("title", "")).lower() or keyword in str(s.get("memo", "")).lower()
            ]
            todos = [
                t for t in todos
                if keyword in str(t.get("title", "")).lower() or keyword in str(t.get("memo", "")).lower()
            ]
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": f"일정 {len(schedules)}건, 할일 {len(todos)}건 조회됨",
            "data": {"schedules": schedules[:30], "todos": todos[:30]},
        }

    if tool_name == "query_class_roster":
        keyword = str(args.get("keyword", "") or "").strip().lower()
        homeroom_key = str(args.get("homeroom_key", "") or "").strip()
        students = db_mgr.get_students()
        if keyword:
            students = [
                s for s in students
                if keyword in str(s.get("name", "")).lower()
                or keyword in str(s.get("student_number", "")).lower()
            ]
        attendance = db_mgr.get_attendance_logs(homeroom_key) if homeroom_key else []
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": f"학생 {len(students)}명, 출결 기록 {len(attendance)}건 조회됨",
            "data": {"students": students[:50], "attendance": attendance[:30]},
        }

    if tool_name == "scan_folder_files":
        folder_str = str(args.get("folder_path", "") or "").strip()
        max_items = int(args.get("max_items", 50) or 50)
        folder = Path(folder_str)
        if not folder_str or not folder.is_dir():
            return {
                "ok": False,
                "tool_name": tool_name,
                "summary": f"폴더를 찾을 수 없습니다: {folder_str}",
                "data": {"files": []},
            }
        items: List[str] = []
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                items.append(str(p.relative_to(folder)).replace("\\", "/"))
                if len(items) >= max_items:
                    break
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": f"폴더 '{folder.name}'에서 {len(items)}개 파일 스캔됨",
            "data": {"folder_path": str(folder.resolve()), "files": items},
        }

    if tool_name == "get_session_history":
        sid = session_id if session_id is not None else args.get("session_id")
        if sid is None:
            return {
                "ok": False,
                "tool_name": tool_name,
                "summary": "활성 세션 ID가 없습니다.",
                "data": {"messages": []},
            }
        limit = max(1, min(100, int(args.get("limit", 20) or 20)))
        keyword = str(args.get("keyword", "") or "").strip().lower()
        messages = db_mgr.get_ai_chat_messages(int(sid))
        if keyword:
            messages = [m for m in messages if keyword in str(m.get("content", "")).lower()]
        recent = messages[-limit:]
        compact = [
            {
                "role": m.get("role", ""),
                "content": str(m.get("content", "") or "")[:600],
                "created_at": m.get("created_at", ""),
            }
            for m in recent
        ]
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": f"현재 세션의 대화 {len(compact)}건 조회됨",
            "data": {"session_id": int(sid), "messages": compact},
        }

    return {
        "ok": False,
        "tool_name": tool_name,
        "summary": f"알 수 없는 조회형 도구: {tool_name}",
        "data": {},
    }


def execute_action_tool(
    tool_name: str,
    args: Dict[str, Any],
    db: Optional[DatabaseManager] = None,
) -> Dict[str, Any]:
    """실행/변경형(action) 도구를 사용자 승인 후 실행합니다."""
    db_mgr = db or DatabaseManager()
    if tool_name == "add_calendar_todo":
        item_type = str(args.get("item_type", "todo") or "todo").strip().lower()
        title = str(args.get("title", "") or "").strip() or "제목 없음"
        date_val = str(args.get("date", "") or args.get("start_date", "") or "").strip()
        memo = str(args.get("memo", "") or "").strip()
        if item_type == "schedule":
            row_id = db_mgr.add_schedule(
                title=title,
                category=str(args.get("category", "학사일정") or "학사일정"),
                start_date=date_val,
                end_date=str(args.get("end_date", "") or date_val),
                start_time=str(args.get("start_time", "") or ""),
                memo=memo,
            )
            return {
                "ok": True,
                "tool_name": tool_name,
                "summary": f"일정 등록 완료 (ID={row_id}): {title}",
                "data": {"id": row_id, "item_type": "schedule", "title": title},
            }
        row_id = db_mgr.add_todo(
            title=title,
            due_date=date_val,
            priority=str(args.get("priority", "normal") or "normal"),
            memo=memo,
        )
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": f"할일 등록 완료 (ID={row_id}): {title}",
            "data": {"id": row_id, "item_type": "todo", "title": title},
        }

    if tool_name == "sort_files_auto":
        src = str(args.get("source_dir", "") or "").strip()
        tgt = str(args.get("target_base_dir", "") or src).strip()
        action_mode = str(args.get("action_mode", "move") or "move").strip()
        if not src or not Path(src).is_dir():
            return {
                "ok": False,
                "tool_name": tool_name,
                "summary": f"원본 폴더가 존재하지 않습니다: {src}",
                "data": {},
            }
        service = FileSorterService(db_mgr)
        worker = BatchSortWorker(
            sorter_service=service,
            source_dirs=[src],
            target_base_dir=tgt,
            action_mode=action_mode,
        )
        res = worker.execute()
        processed = int(res.get("processed", 0))
        skipped = int(res.get("skipped", 0))
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": f"파일 자동 분류 완료: 처리 {processed}건, 건너뜀 {skipped}건",
            "data": res,
        }

    if tool_name == "merge_excel_assignments":
        paths = args.get("excel_paths") or []
        out_path = args.get("output_path") or None
        res = merge_excel_assignments(excel_paths=list(paths), output_path=out_path)
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": f"다중 엑셀 병합 완료: {res.get('output_path', '')}",
            "data": res,
        }

    if tool_name == "process_hwpx_or_ocr":
        tid = str(args.get("target_id", "hwpx_agent") or "hwpx_agent").strip()
        instruction = str(args.get("instruction", "") or "").strip()
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": f"대상 타겟 전환 준비 완료: {tid}",
            "data": {"target_id": tid, "instruction": instruction},
        }

    return {
        "ok": False,
        "tool_name": tool_name,
        "summary": f"알 수 없는 실행형 도구: {tool_name}",
        "data": {},
    }
