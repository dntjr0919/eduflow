"""
스마트 파일 자동 분류 엔진 및 실시간 감시 워커 모듈 (services/file_sorter_service.py).
"""

import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.base_worker import BaseWorker
from core.database import DatabaseManager

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore


TEMP_EXTENSIONS = {".crdownload", ".part", ".tmp", ".download", ".partial", ".opdownload"}
SYSTEM_IGNORE_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}


class FileSorterService:
    """파일 분류 규칙 매칭, 안전한 파일 이동/복사 및 실행 취소(Undo) 비즈니스 로직."""

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()

    @staticmethod
    def is_temporary_or_system_file(file_path: Path) -> bool:
        name_lower = file_path.name.lower()
        if name_lower in SYSTEM_IGNORE_FILES:
            return True
        if name_lower.startswith("~$") or name_lower.startswith(".~"):
            return True
        if file_path.suffix.lower() in TEMP_EXTENSIONS:
            return True
        return False

    @staticmethod
    def is_file_ready(file_path: Path) -> bool:
        try:
            if not file_path.exists() or not file_path.is_file():
                return False
            with open(file_path, "rb"):
                pass
            return True
        except OSError:
            return False

    @staticmethod
    def resolve_unique_target_path(target_dir: Path, file_name: str, policy: str = "number") -> Path:
        candidate = target_dir / file_name
        if not candidate.exists():
            return candidate

        stem = Path(file_name).stem
        suffix = Path(file_name).suffix

        if policy == "date":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dated_candidate = target_dir / f"{stem}_{ts}{suffix}"
            if not dated_candidate.exists():
                return dated_candidate

        counter = 1
        while True:
            numbered = target_dir / f"{stem}_({counter}){suffix}"
            if not numbered.exists():
                return numbered
            counter += 1

    def match_rule(
        self, file_path: Path, active_rules: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        stem_lower = file_path.stem.lower()
        ext_lower = file_path.suffix.lower()

        for rule in active_rules:
            match_type = rule.get("match_type", "keyword").lower()
            raw_keywords = rule.get("keywords", "")
            raw_extensions = rule.get("extensions", "")

            keywords = [k.strip().lower() for k in raw_keywords.split(",") if k.strip()]
            extensions = [
                (e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}")
                for e in raw_extensions.split(",")
                if e.strip()
            ]

            kw_matched = any(kw in stem_lower for kw in keywords) if keywords else False
            ext_matched = (ext_lower in extensions) if extensions else False

            if match_type == "keyword" and kw_matched:
                return rule
            elif match_type == "extension" and ext_matched:
                return rule
            elif match_type == "both" and kw_matched and ext_matched:
                return rule

        return None

    def process_single_file(
        self,
        file_path: Path,
        target_base_dir: Path,
        active_rules: List[Dict[str, Any]],
        action_mode: str = "move",
        duplicate_policy: str = "number",
        include_unmatched: bool = False,
        unmatched_folder_name: str = "99_기타_미분류",
        ignore_temp_files: bool = True,
        batch_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not file_path.exists() or not file_path.is_file():
            return None

        if ignore_temp_files and self.is_temporary_or_system_file(file_path):
            return None

        try:
            file_resolved = file_path.resolve()
            target_resolved = target_base_dir.resolve()
            if target_resolved in file_resolved.parents:
                return None
        except Exception:
            pass

        if not self.is_file_ready(file_path):
            return None

        matched_rule = self.match_rule(file_path, active_rules)
        if matched_rule is not None:
            category_folder = matched_rule["category_folder"].strip()
            rule_name = matched_rule["rule_name"].strip()
        elif include_unmatched:
            category_folder = unmatched_folder_name.strip() or "99_기타_미분류"
            rule_name = "미분류 기타 파일"
        else:
            return None

        dest_folder = target_base_dir / category_folder
        dest_folder.mkdir(parents=True, exist_ok=True)

        dest_path = self.resolve_unique_target_path(
            dest_folder, file_path.name, policy=duplicate_policy
        )

        orig_str = str(file_path.resolve())
        dest_str = str(dest_path.resolve())

        if action_mode == "copy":
            shutil.copy2(orig_str, dest_str)
        else:
            shutil.move(orig_str, dest_str)

        effective_batch_id = batch_id or datetime.now().strftime("BATCH_%Y%m%d_%H%M%S")
        history_id = self.db.add_file_history(
            batch_id=effective_batch_id,
            file_name=file_path.name,
            original_path=orig_str,
            target_path=dest_str,
            category_folder=category_folder,
            rule_name=rule_name,
            action_type=action_mode,
        )

        return {
            "history_id": history_id,
            "batch_id": effective_batch_id,
            "file_name": file_path.name,
            "original_path": orig_str,
            "target_path": dest_str,
            "category_folder": category_folder,
            "rule_name": rule_name,
            "action_type": action_mode,
        }

    def undo_history_item(self, history_row: Dict[str, Any]) -> Tuple[bool, str]:
        if history_row.get("is_undone", 0) == 1:
            return False, "이미 실행 취소된 항목입니다."

        history_id = int(history_row["id"])
        orig_path = Path(history_row["original_path"])
        target_path = Path(history_row["target_path"])
        action_type = history_row.get("action_type", "move")

        if not target_path.exists():
            return False, f"이동된 파일이 존재하지 않습니다: {target_path.name}"

        try:
            orig_path.parent.mkdir(parents=True, exist_ok=True)
            if action_type == "copy":
                if orig_path.exists():
                    target_path.unlink()
                else:
                    shutil.move(str(target_path), str(orig_path))
            else:
                restore_target = orig_path
                if restore_target.exists():
                    restore_target = self.resolve_unique_target_path(
                        orig_path.parent, orig_path.name, policy="number"
                    )
                shutil.move(str(target_path), str(restore_target))

            self.db.mark_history_undone(history_id)
            return True, f"복구 완료: {target_path.name} -> {orig_path.parent}"
        except Exception as exc:
            return False, f"실행 취소 실패 ({target_path.name}): {exc}"


class BatchSortWorker(BaseWorker):
    def __init__(
        self,
        sorter_service: FileSorterService,
        source_dirs: List[str],
        target_base_dir: str,
        action_mode: str = "move",
        duplicate_policy: str = "number",
        include_unmatched: bool = False,
        unmatched_folder_name: str = "99_기타_미분류",
        ignore_temp_files: bool = True,
    ):
        super().__init__(task_name="스마트 파일 일괄 분류")
        self.service = sorter_service
        self.source_dirs = source_dirs
        self.target_base_dir = Path(target_base_dir)
        self.action_mode = action_mode
        self.duplicate_policy = duplicate_policy
        self.include_unmatched = include_unmatched
        self.unmatched_folder_name = unmatched_folder_name
        self.ignore_temp_files = ignore_temp_files

    def execute(self) -> Dict[str, Any]:
        active_rules = self.service.db.get_sorting_rules(active_only=True)
        if not active_rules and not self.include_unmatched:
            self.emit_log("WARN", "활성화된 분류 규칙이 없습니다.")
            return {"processed": 0, "skipped": 0, "items": []}

        candidate_files: List[Path] = []
        for src in self.source_dirs:
            src_path = Path(src)
            if not src_path.exists() or not src_path.is_dir():
                self.emit_log("WARN", f"원본 폴더가 존재하지 않습니다: {src}")
                continue
            for item in src_path.iterdir():
                if item.is_file():
                    candidate_files.append(item)

        total = len(candidate_files)
        if total == 0:
            self.emit_progress(100, "분류할 파일이 없습니다.")
            self.emit_log("INFO", "선택된 원본 폴더에 파일이 없습니다.")
            return {"processed": 0, "skipped": 0, "items": []}

        batch_id = datetime.now().strftime("BATCH_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
        processed_items: List[Dict[str, Any]] = []
        skipped_count = 0

        self.emit_log("INFO", f"총 {total}개 파일 대상 자동 분류를 시작합니다.")
        for idx, file_path in enumerate(candidate_files, start=1):
            if self.is_stop_requested():
                self.emit_log("WARN", "사용자 요청으로 일괄 분류가 중단되었습니다.")
                break

            pct = int((idx / total) * 100)
            self.emit_progress(pct, f"분류 검사 중 ({idx}/{total}): {file_path.name}")

            try:
                res = self.service.process_single_file(
                    file_path=file_path,
                    target_base_dir=self.target_base_dir,
                    active_rules=active_rules,
                    action_mode=self.action_mode,
                    duplicate_policy=self.duplicate_policy,
                    include_unmatched=self.include_unmatched,
                    unmatched_folder_name=self.unmatched_folder_name,
                    ignore_temp_files=self.ignore_temp_files,
                    batch_id=batch_id,
                )
                if res:
                    processed_items.append(res)
                    self.emit_log(
                        "SUCCESS",
                        f"[{res['category_folder']}] {res['file_name']} ({res['rule_name']})",
                    )
                else:
                    skipped_count += 1
            except Exception as exc:
                skipped_count += 1
                self.emit_log("ERROR", f"파일 처리 실패 ({file_path.name}): {exc}")

        self.emit_progress(
            100, f"분류 완료: {len(processed_items)}건 처리 / {skipped_count}건 건너뜀"
        )
        return {
            "batch_id": batch_id,
            "processed": len(processed_items),
            "skipped": skipped_count,
            "items": processed_items,
        }


class _WatchdogHandler(FileSystemEventHandler):
    def __init__(self, trigger_callback):
        super().__init__()
        self._trigger_callback = trigger_callback

    def on_created(self, event):
        if not getattr(event, "is_directory", False):
            self._trigger_callback(event.src_path)

    def on_moved(self, event):
        if not getattr(event, "is_directory", False):
            self._trigger_callback(event.dest_path)


class RealtimeFolderWatchWorker(BaseWorker):
    def __init__(
        self,
        sorter_service: FileSorterService,
        source_dirs: List[str],
        target_base_dir: str,
        action_mode: str = "move",
        duplicate_policy: str = "number",
        include_unmatched: bool = False,
        unmatched_folder_name: str = "99_기타_미분류",
        ignore_temp_files: bool = True,
        poll_interval_sec: float = 3.0,
        on_file_sorted_signal=None,
    ):
        super().__init__(task_name="실시간 폴더 감시")
        self.service = sorter_service
        self.source_dirs = source_dirs
        self.target_base_dir = Path(target_base_dir)
        self.action_mode = action_mode
        self.duplicate_policy = duplicate_policy
        self.include_unmatched = include_unmatched
        self.unmatched_folder_name = unmatched_folder_name
        self.ignore_temp_files = ignore_temp_files
        self.poll_interval_sec = max(1.0, float(poll_interval_sec))
        self.on_file_sorted_signal = on_file_sorted_signal
        self._pending_paths: List[str] = []

    def _enqueue_path(self, path_str: str) -> None:
        if path_str not in self._pending_paths:
            self._pending_paths.append(path_str)

    def execute(self) -> None:
        observer = None
        valid_dirs: List[Path] = []
        for d in self.source_dirs:
            p = Path(d)
            if p.exists() and p.is_dir():
                valid_dirs.append(p)

        if not valid_dirs:
            raise RuntimeError("감시할 유효한 원본 폴더가 없습니다. 폴더 경로를 확인해 주세요.")

        if HAS_WATCHDOG and Observer is not None:
            try:
                observer = Observer()
                handler = _WatchdogHandler(self._enqueue_path)
                for vd in valid_dirs:
                    observer.schedule(handler, str(vd), recursive=False)
                observer.start()
                self.emit_log(
                    "INFO",
                    f"Watchdog 실시간 파일 이벤트 감시 활성화 완료 ({len(valid_dirs)}개 폴더)",
                )
            except Exception as exc:
                self.emit_log("WARN", f"Watchdog 초기화 실패, 폴링 모드로 전환합니다: {exc}")
                observer = None

        self.emit_progress(0, "실시간 자동 감시 동작 중...")

        try:
            while not self.is_stop_requested():
                active_rules = self.service.db.get_sorting_rules(active_only=True)
                batch_id = datetime.now().strftime("WATCH_%Y%m%d_%H%M%S")

                scan_targets: List[Path] = []
                while self._pending_paths:
                    raw_p = self._pending_paths.pop(0)
                    scan_targets.append(Path(raw_p))

                for vd in valid_dirs:
                    if vd.exists():
                        for item in vd.iterdir():
                            if item.is_file() and item not in scan_targets:
                                scan_targets.append(item)

                for file_path in scan_targets:
                    if self.is_stop_requested():
                        break
                    try:
                        res = self.service.process_single_file(
                            file_path=file_path,
                            target_base_dir=self.target_base_dir,
                            active_rules=active_rules,
                            action_mode=self.action_mode,
                            duplicate_policy=self.duplicate_policy,
                            include_unmatched=self.include_unmatched,
                            unmatched_folder_name=self.unmatched_folder_name,
                            ignore_temp_files=self.ignore_temp_files,
                            batch_id=batch_id,
                        )
                        if res:
                            self.emit_log(
                                "SUCCESS",
                                f"[실시간 분류] {res['file_name']} -> {res['category_folder']}",
                            )
                            if self.on_file_sorted_signal is not None:
                                self.on_file_sorted_signal.emit(res)
                    except Exception as exc:
                        self.emit_log("ERROR", f"실시간 분류 오류 ({file_path.name}): {exc}")

                steps = max(1, int(self.poll_interval_sec / 0.25))
                for _ in range(steps):
                    if self.is_stop_requested():
                        break
                    time.sleep(0.25)
        finally:
            if observer is not None:
                try:
                    observer.stop()
                    observer.join(timeout=2.0)
                except Exception:
                    pass
            self.emit_progress(100, "실시간 자동 감시 대기 중 (OFF)")
