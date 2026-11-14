"""
EduFlow 공통 파일/폴더 드래그앤 드롭 헬퍼 (ui/components/file_drop_helper.py)

모든 위젯(QLineEdit, QListWidget, QTableWidget, QTreeWidget, QFrame, QPushButton, QTextBrowser 등)에
공통으로 부착하여 로컬 파일/폴더 드래그앤 드롭, 확장자/디렉토리 검증, 파란 점선(#2563EB) 시각적 피드백,
허용되지 않는 형식 안내를 일관되게 처리합니다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple

from PyQt6.QtCore import QEvent, QMimeData, QObject
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QMessageBox,
    QPushButton,
    QWidget,
)


def normalize_allowed_exts(allowed_exts: Optional[Sequence[str]]) -> Set[str]:
    """허용 확장자 목록을 소문자 '.ext' 형태의 집합으로 정규화합니다."""
    if not allowed_exts:
        return set()
    normalized: Set[str] = set()
    for ext in allowed_exts:
        clean = str(ext or "").strip().lower()
        if not clean:
            continue
        if not clean.startswith("."):
            clean = f".{clean}"
        normalized.add(clean)
    return normalized


def _normalize_local_path(raw_path: str) -> str:
    cleaned = (raw_path or "").strip()
    if not cleaned:
        return ""
    try:
        return str(Path(cleaned).resolve())
    except OSError:
        return os.path.normpath(cleaned)


def extract_local_paths_from_mime(mime_data: Optional[QMimeData]) -> List[str]:
    """QMimeData에서 로컬 파일/폴더 경로 목록을 추출합니다."""
    if mime_data is None or not mime_data.hasUrls():
        return []
    paths: List[str] = []
    seen: Set[str] = set()
    for url in mime_data.urls():
        if not url.isLocalFile():
            continue
        raw = url.toLocalFile()
        if not raw:
            continue
        # 가상 테스트 경로가 끝 슬래시로 디렉토리를 표현할 수 있으므로 정규화 전 플래그 유지
        trailing_sep = raw.endswith("/") or raw.endswith("\\")
        norm = _normalize_local_path(raw)
        if not norm:
            continue
        if trailing_sep and not os.path.exists(norm) and not norm.endswith(os.sep):
            norm = f"{norm}{os.sep}"
        if norm not in seen:
            seen.add(norm)
            paths.append(norm)
    return paths


def _is_directory_path(path_str: str) -> bool:
    if os.path.isdir(path_str):
        return True
    if os.path.isfile(path_str):
        return False
    # 실제 디스크에 없는 가상 테스트 경로 판별: 끝 슬래시 여부로 디렉토리 판단
    return path_str.endswith("/") or path_str.endswith("\\")


def classify_dropped_paths(
    paths: Iterable[str],
    *,
    allowed_exts: Set[str],
    allow_dirs: bool,
    allow_files: bool,
) -> Tuple[List[str], List[str]]:
    """드롭된 경로 목록을 (유효 경로 목록, 거부된 경로 목록)으로 분류합니다."""
    valid: List[str] = []
    invalid: List[str] = []
    for p in paths:
        is_dir = _is_directory_path(p)
        clean_p = p.rstrip("\\/") if is_dir and len(p) > 3 else p
        if is_dir:
            if allow_dirs:
                valid.append(clean_p)
            else:
                invalid.append(clean_p)
            continue

        if not allow_files:
            invalid.append(clean_p)
            continue

        ext = Path(clean_p).suffix.lower()
        if not allowed_exts or ext in allowed_exts:
            valid.append(clean_p)
        else:
            invalid.append(clean_p)
    return valid, invalid


def _describe_allowed_targets(
    allowed_exts: Set[str], allow_dirs: bool, allow_files: bool
) -> str:
    parts: List[str] = []
    if allow_dirs:
        parts.append("폴더")
    if allow_files:
        if allowed_exts:
            parts.append(", ".join(sorted(allowed_exts)))
        else:
            parts.append("모든 파일")
    return " / ".join(parts) if parts else "지원 형식 없음"


def _set_main_status_message(widget: QWidget, message: str) -> None:
    win = widget.window() if widget is not None else None
    if win is None:
        return
    status_lbl = getattr(win, "lbl_single_line_log", None)
    if status_lbl is not None and hasattr(status_lbl, "setText"):
        status_lbl.setText(message)
    status_bar_fn = getattr(win, "statusBar", None)
    if callable(status_bar_fn):
        sb = status_bar_fn()
        if sb is not None and hasattr(sb, "showMessage"):
            sb.showMessage(message, 6000)


class _FileDropEventFilter(QObject):
    """위젯(및 스크롤 뷰포트)에 드래그앤 드롭 검증과 시각 피드백을 제공하는 이벤트 필터."""

    _ORIG_STYLE_PROP = "_eduflow_orig_drop_stylesheet"

    def __init__(
        self,
        widget: QWidget,
        *,
        allowed_exts: Set[str],
        allow_dirs: bool,
        allow_files: bool,
        multiple: bool,
        on_files_dropped: Optional[Callable[[List[str]], None]],
        on_invalid_drop: Optional[Callable[[List[str]], None]],
        context_label: str,
        highlight_target: Optional[QWidget],
    ) -> None:
        super().__init__(widget)
        self.widget = widget
        self.highlight_target = highlight_target or widget
        self.allowed_exts = allowed_exts
        self.allow_dirs = allow_dirs
        self.allow_files = allow_files
        self.multiple = multiple
        self.on_files_dropped = on_files_dropped
        self.on_invalid_drop = on_invalid_drop
        self.context_label = context_label or "파일"
        self._drag_active: bool = False
        self.last_accepted_paths: List[str] = []
        self.last_rejected_paths: List[str] = []
        self.last_warning_message: str = ""

    def _apply_highlight(self, active: bool) -> None:
        target = self.highlight_target
        if target is None:
            return
        if active == self._drag_active:
            return
        self._drag_active = active

        if active:
            if target.property(self._ORIG_STYLE_PROP) is None:
                target.setProperty(self._ORIG_STYLE_PROP, target.styleSheet() or "")
            orig_style = str(target.property(self._ORIG_STYLE_PROP) or "")
            obj_name = target.objectName()
            cls_name = target.metaObject().className()
            role = str(target.property("role") or "")
            sel_parts = [
                f"{cls_name}[fileDragActive=\"true\"]",
                '*[fileDragActive="true"]',
            ]
            if role:
                sel_parts.append(f'{cls_name}[role="{role}"][fileDragActive="true"]')
                sel_parts.append(f'*[role="{role}"][fileDragActive="true"]')
            if obj_name:
                sel_parts.append(f'#{obj_name}[fileDragActive="true"]')
                sel_parts.append(f'{cls_name}#{obj_name}[fileDragActive="true"]')
            selector = ", ".join(sel_parts)
            if isinstance(target, QPushButton):
                drag_css = (
                    f"{selector} {{ border: 2px dashed #2563EB; "
                    f"background-color: #EFF6FF; color: #1D4ED8; }}"
                )
            elif cls_name in ("QFrame", "HoverZoomCard") or target.__class__.__name__ == "HoverZoomCard":
                drag_css = f"{selector} {{ border: 2px dashed #2563EB; }}"
            else:
                drag_css = (
                    f"{selector} {{ border: 2px dashed #2563EB; background-color: #EFF6FF; }}"
                )
            target.setProperty("fileDragActive", "true")
            merged = f"{orig_style}\n{drag_css}" if orig_style else drag_css
            target.setStyleSheet(merged)
        else:
            orig_style = target.property(self._ORIG_STYLE_PROP)
            target.setProperty("fileDragActive", "false")
            if orig_style is not None:
                target.setStyleSheet(str(orig_style))
                target.setProperty(self._ORIG_STYLE_PROP, None)

        st = target.style()
        if st is not None:
            st.unpolish(target)
            st.polish(target)
        target.update()

    def _notify_invalid(self, invalid_paths: List[str]) -> None:
        if not invalid_paths:
            return
        names = [Path(p).name or p for p in invalid_paths]
        preview = ", ".join(names[:3])
        if len(names) > 3:
            preview = f"{preview} 외 {len(names) - 3}건"
        allowed_desc = _describe_allowed_targets(
            self.allowed_exts, self.allow_dirs, self.allow_files
        )
        msg = (
            f"[{self.context_label}] 허용되지 않는 형식 {len(invalid_paths)}건이 제외되었습니다: "
            f"{preview}\n허용 형식: {allowed_desc}"
        )
        self.last_warning_message = msg
        _set_main_status_message(self.widget, msg.replace("\n", " | "))
        if self.on_invalid_drop is not None:
            self.on_invalid_drop(list(invalid_paths))
            return
        QMessageBox.warning(self.widget, "지원하지 않는 파일 형식", msg)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        etype = event.type()

        if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            mime = event.mimeData() if hasattr(event, "mimeData") else None
            paths = extract_local_paths_from_mime(mime)
            if not paths:
                # 내부 리스트 항목 이동(InternalMove) 등 비-URL 드래그는 기본 동작 유지
                return False
            valid, _invalid = classify_dropped_paths(
                paths,
                allowed_exts=self.allowed_exts,
                allow_dirs=self.allow_dirs,
                allow_files=self.allow_files,
            )
            if valid:
                event.acceptProposedAction()
                self._apply_highlight(True)
            else:
                event.ignore()
                self._apply_highlight(False)
            return True

        if etype == QEvent.Type.DragLeave:
            if self._drag_active:
                self._apply_highlight(False)
                return True
            return False

        if etype == QEvent.Type.Drop:
            self._apply_highlight(False)
            mime = event.mimeData() if hasattr(event, "mimeData") else None
            paths = extract_local_paths_from_mime(mime)
            if not paths:
                return False

            valid, invalid = classify_dropped_paths(
                paths,
                allowed_exts=self.allowed_exts,
                allow_dirs=self.allow_dirs,
                allow_files=self.allow_files,
            )
            self.last_accepted_paths = list(valid)
            self.last_rejected_paths = list(invalid)

            if invalid:
                self._notify_invalid(invalid)

            if valid:
                delivered = valid if self.multiple else valid[:1]
                event.acceptProposedAction()
                if self.on_files_dropped is not None:
                    self.on_files_dropped(delivered)
            else:
                event.ignore()
            return True

        if etype == QEvent.Type.Hide and self._drag_active:
            self._apply_highlight(False)

        return False


def enable_file_drop(
    widget: QWidget,
    *,
    allowed_exts: Optional[Sequence[str]] = None,
    allow_dirs: bool = False,
    allow_files: bool = True,
    multiple: bool = True,
    on_files_dropped: Optional[Callable[[List[str]], None]] = None,
    on_invalid_drop: Optional[Callable[[List[str]], None]] = None,
    context_label: str = "파일",
    highlight_target: Optional[QWidget] = None,
) -> _FileDropEventFilter:
    """
    지정한 위젯에 로컬 파일/폴더 드래그앤 드롭 지원과 파란 점선(#2563EB) 시각 피드백을 활성화합니다.
    QAbstractScrollArea(QListWidget, QTableWidget, QTreeWidget, QTextBrowser 등)의 경우
    viewport()에도 동일 필터를 자동 설치해 외부 파일 드롭을 가로챕니다.
    """
    normalized_exts = normalize_allowed_exts(allowed_exts)
    filt = _FileDropEventFilter(
        widget,
        allowed_exts=normalized_exts,
        allow_dirs=bool(allow_dirs),
        allow_files=bool(allow_files),
        multiple=bool(multiple),
        on_files_dropped=on_files_dropped,
        on_invalid_drop=on_invalid_drop,
        context_label=context_label,
        highlight_target=highlight_target,
    )

    widget.setAcceptDrops(True)
    widget.installEventFilter(filt)

    if isinstance(widget, QAbstractScrollArea) or hasattr(widget, "viewport"):
        vp = widget.viewport() if callable(getattr(widget, "viewport", None)) else None
        if isinstance(vp, QWidget):
            vp.setAcceptDrops(True)
            vp.installEventFilter(filt)

    if isinstance(widget, QAbstractItemView):
        # InternalMove가 설정된 리스트는 내부 정렬을 보존하고, 그 외 뷰는 외부 드롭 전용 설정 허용
        if widget.dragDropMode() == QAbstractItemView.DragDropMode.NoDragDrop:
            widget.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    # 기존 필터 참조 목록을 위젯에 유지해 GC 수거 방지
    refs: List[_FileDropEventFilter] = getattr(widget, "_eduflow_drop_filters", [])
    refs.append(filt)
    setattr(widget, "_eduflow_drop_filters", refs)
    setattr(widget, "_eduflow_file_drop_filter", filt)
    return filt
