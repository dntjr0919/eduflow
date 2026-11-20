"""
학생 얼굴 사진 일괄 매칭 · 경량 리사이즈 · 동기화 서비스 및 다이얼로그 (ui/dialogs/student_face_photo_dialog.py).
- 파일명에서 학번(예: 10501) 또는 학생 이름(예: 김유민)을 추출해 현재 담임 학급 명렬표와 자동 매칭
- 미리보기 표(파일명 ↔ 매칭된 학생)에서 교사가 확인·수정 가능
- [업로드 및 동기화] 클릭 시 Pillow(PIL)로 최대 400px 경량 JPEG(약 20KB 이하)로 리사이즈한 뒤
  Data URL(`data:image/jpeg;base64,...`) 형태로 Firestore `학생_안면데이터` 컬렉션에 직접 일괄 저장
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListView,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.base_worker import FunctionWorker, TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.firestore_sync_service import FirestoreSyncService
from ui.components.file_drop_helper import enable_file_drop
from ui.utils import COMBOBOX_STYLE, install_no_scroll_wheel_filter

SUPPORTED_IMAGE_EXTENSIONS: Tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp")
DEFAULT_MAX_PHOTO_PX = 400
DEFAULT_JPEG_QUALITY = 80
TARGET_MAX_JPEG_BYTES = 20 * 1024


def apply_qss_role(widget: QWidget, role: str = "", obj_name: str = "") -> None:
    """위젯에 objectName 및 role 속성을 부여하고 style().polish()를 즉시 호출합니다."""
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    if obj_name:
        widget.setObjectName(obj_name)
    if role:
        widget.setProperty("role", role)
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


def build_student_photo_drive_folder(
    school_name: str = "",
    grade: Optional[int] = None,
    class_no: Optional[int] = None,
    school_code: str = "",
) -> str:
    """
    Google Drive 업로드 폴더 경로를 생성합니다:
    `EduFlow_Student_Photos/{school_name}_{grade}학년{class_no}반`
    """
    sn = str(school_name or school_code or "학교").strip() or "학교"
    g = int(grade) if grade is not None and str(grade).lstrip("-").isdigit() else 1
    c = int(class_no) if class_no is not None and str(class_no).lstrip("-").isdigit() else 1
    return f"EduFlow_Student_Photos/{sn}_{g}학년{c}반"


def resize_image_to_jpeg_bytes(
    image_path: str | Path,
    max_px: int = DEFAULT_MAX_PHOTO_PX,
    quality: int = DEFAULT_JPEG_QUALITY,
    target_max_bytes: int = TARGET_MAX_JPEG_BYTES,
) -> bytes:
    """
    Pillow(PIL)를 사용해 이미지를 EXIF 회전 보정 후 긴 변 기준 최대 `max_px`(기본 400px) 이하의
    경량 RGB JPEG 바이트(기본 약 20KB 이하)로 리사이즈합니다.
    """
    from PIL import Image, ImageOps

    p = Path(image_path)
    with Image.open(p) as img:
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            rgb_img = bg
        elif img.mode != "RGB":
            rgb_img = img.convert("RGB")
        else:
            rgb_img = img.copy()

        w, h = rgb_img.size
        limit = max(64, int(max_px or DEFAULT_MAX_PHOTO_PX))
        if max(w, h) > limit:
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
            rgb_img.thumbnail((limit, limit), resample=resample)

        cur_quality = int(quality or DEFAULT_JPEG_QUALITY)
        out_bytes = b""
        while True:
            buf = io.BytesIO()
            rgb_img.save(buf, format="JPEG", quality=cur_quality, optimize=True)
            out_bytes = buf.getvalue()
            if not target_max_bytes or len(out_bytes) <= target_max_bytes or cur_quality <= 50:
                break
            cur_quality = max(50, cur_quality - 10)
        return out_bytes


def _normalize_roster_students(
    roster: Sequence[Any],
    default_grade: Optional[int] = None,
    default_class_no: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    명렬표 입력(dict 목록 또는 (student_id, name) 튜플 목록)을
    표준 학생 dict 목록 `[{student_id, name, grade, class_no, number}]`으로 정규화합니다.
    """
    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(roster or [], start=1):
        if isinstance(item, dict):
            sid = re.sub(r"^(\d+)\.0+$", r"\1", str(item.get("student_id") or "").strip())
            name = str(item.get("name") or item.get("student_name") or "").strip()
            g_raw = item.get("grade") if item.get("grade") is not None else default_grade
            c_raw = item.get("class_no") if item.get("class_no") is not None else default_class_no
            n_raw = item.get("number") if item.get("number") is not None else item.get("student_no")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            sid = re.sub(r"^(\d+)\.0+$", r"\1", str(item[0] or "").strip())
            name = str(item[1] or "").strip()
            g_raw = default_grade
            c_raw = default_class_no
            n_raw = None
        else:
            continue

        if not sid and not name:
            continue

        grade = int(g_raw) if g_raw is not None and str(g_raw).isdigit() else None
        class_no = int(c_raw) if c_raw is not None and str(c_raw).isdigit() else None
        number = int(n_raw) if n_raw is not None and str(n_raw).isdigit() else None

        if len(sid) == 5 and sid.isdigit():
            if grade is None:
                grade = int(sid[0])
            if class_no is None:
                class_no = int(sid[1:3])
            if number is None:
                number = int(sid[3:5])

        if number is None:
            number = idx

        normalized.append(
            {
                "student_id": sid,
                "name": name,
                "grade": grade,
                "class_no": class_no,
                "number": number,
            }
        )
    return normalized


def match_filename_to_student(
    filename: str | Path,
    roster: Sequence[Any],
    default_grade: Optional[int] = None,
    default_class_no: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    이미지 파일명(확장자 제외 stem)에서 학번(예: `10501`) 또는 학생 이름(예: `김유민`)을 추출하여
    담임 학급 명렬표 학생과 자동 매칭합니다.

    반환값: `(매칭된 학생 dict 또는 None, 매칭 사유 문자열)`
    매칭 우선순위:
    1. 학번 정확 일치 (파일명 전체 또는 파일명 내 숫자 토큰이 명렬표 `student_id`와 일치)
    2. 학번+이름 복합 파일명 (예: `10501_김유민.jpg`, `10501 김유민.png`, `01_김유민.jpg`)
    3. 학생 이름 정확 일치 (파일명 stem 또는 구분자 분리 토큰이 이름과 일치)
    4. 학생 이름 부분 포함 일치 (파일명 stem 안에 명렬표 학생 이름이 유일하게 포함된 경우)
    """
    students = _normalize_roster_students(
        roster,
        default_grade=default_grade,
        default_class_no=default_class_no,
    )
    if not students:
        return None, "명렬표 없음"

    stem = Path(str(filename or "")).stem.strip()
    if not stem:
        return None, "미매칭"

    by_sid: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for stu in students:
        sid = str(stu.get("student_id") or "").strip()
        name = str(stu.get("name") or "").strip()
        if sid:
            by_sid[sid] = stu
        if name:
            by_name.setdefault(name, []).append(stu)

    stem_compact = re.sub(r"\s+", "", stem)

    # 1순위: 파일명 전체가 학번과 일치하거나, 파일명 안의 숫자 블록이 학번과 정확히 일치
    if stem_compact in by_sid:
        return by_sid[stem_compact], "학번 일치"

    digit_tokens = re.findall(r"\d+", stem)
    for d_tok in digit_tokens:
        if d_tok in by_sid:
            return by_sid[d_tok], "학번 추출"

    # 2순위: 파일명 전체(공백 제거)가 학생 이름과 정확히 일치
    if stem_compact in by_name and len(by_name[stem_compact]) == 1:
        return by_name[stem_compact][0], "이름 일치"

    # 구분자(_, -, 공백, ., 괄호 등)로 분리한 토큰 및 한글 블록에서 이름 매칭
    split_tokens = [
        t.strip()
        for t in re.split(r"[\s_\-\.\(\)\[\]0-9]+", stem)
        if t and t.strip()
    ]
    for tok in split_tokens:
        if tok in by_name:
            candidates = by_name[tok]
            if len(candidates) == 1:
                return candidates[0], "이름 추출"
            # 동명이인이 있는 경우 번호 토큰으로 보조 판별
            for d_tok in digit_tokens:
                try:
                    num_val = int(d_tok)
                except ValueError:
                    continue
                for cand in candidates:
                    if cand.get("number") == num_val:
                        return cand, "번호+이름 추출"
            return candidates[0], "이름 추출"

    # 3순위: 파일명에 학생 이름이 부분 문자열로 포함된 경우 (긴 이름 우선)
    substring_matches: List[Dict[str, Any]] = []
    sorted_names = sorted(by_name.keys(), key=len, reverse=True)
    for name in sorted_names:
        if len(name) >= 2 and name in stem_compact:
            substring_matches.extend(by_name[name])

    if len(substring_matches) == 1:
        return substring_matches[0], "이름 포함"
    elif len(substring_matches) > 1:
        for d_tok in digit_tokens:
            try:
                num_val = int(d_tok)
            except ValueError:
                continue
            for cand in substring_matches:
                if cand.get("number") == num_val:
                    return cand, "번호+이름 포함"
        return substring_matches[0], "이름 포함"

    return None, "미매칭"


def match_photo_files_to_roster(
    file_paths: Sequence[str | Path],
    roster: Sequence[Any],
    default_grade: Optional[int] = None,
    default_class_no: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    다중 이미지 파일 경로 목록을 받아 각 파일별로 명렬표 학생 자동 매칭 결과를 반환합니다.
    반환 항목:
    `{file_path, file_name, matched_student_id, matched_student_name, match_reason, student}`
    """
    results: List[Dict[str, Any]] = []
    seen_paths = set()
    for raw_path in file_paths or []:
        p = Path(str(raw_path).strip())
        if p.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            continue
        norm_key = str(p.resolve()) if p.exists() else str(p)
        if norm_key in seen_paths:
            continue
        seen_paths.add(norm_key)

        matched_stu, reason = match_filename_to_student(
            p.name,
            roster,
            default_grade=default_grade,
            default_class_no=default_class_no,
        )
        results.append(
            {
                "file_path": str(p),
                "file_name": p.name,
                "matched_student_id": str(matched_stu.get("student_id") or "") if matched_stu else "",
                "matched_student_name": str(matched_stu.get("name") or "") if matched_stu else "",
                "match_reason": reason,
                "student": matched_stu,
            }
        )
    return results


def collect_image_files_from_folder(folder_path: str | Path) -> List[str]:
    """지정한 폴더 내 지원 이미지(`.jpg`, `.jpeg`, `.png`, `.webp`) 파일 경로를 정렬해 반환합니다."""
    folder = Path(str(folder_path).strip())
    if not folder.exists() or not folder.is_dir():
        return []
    files = [
        str(p)
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower())
        if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    ]
    return files


class StudentFacePhotoBatchDialog(QDialog):
    """학생 얼굴 사진 일괄 매칭 & 경량 JPEG Data URL · Firestore `학생_안면데이터` 직접 동기화 다이얼로그."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        roster: Sequence[Any] = (),
        school_code: str = "",
        school_name: str = "",
        grade: Optional[int] = None,
        class_no: Optional[int] = None,
        db: Optional[DatabaseManager] = None,
        config: Optional[ConfigManager] = None,
        task_runner: Optional[TaskRunner] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("학생 얼굴 사진 일괄 등록 (자동 매칭 · Firestore 직접 저장)")
        self.resize(900, 620)
        self.setMinimumSize(780, 520)
        apply_qss_role(self, obj_name="StudentFacePhotoBatchModal")

        self.db = db or DatabaseManager()
        self.config = config or ConfigManager()
        self.task_runner = task_runner or TaskRunner(self)

        tp = self.config.get_section("teacher_profile") or {}
        self.school_code = str(
            school_code or tp.get("school_code") or tp.get("neis_school_code") or ""
        ).strip()
        self.school_name = str(school_name or tp.get("school_name") or "").strip()
        self.grade = (
            int(grade)
            if grade is not None and str(grade).isdigit()
            else (int(tp["grade"]) if str(tp.get("grade") or "").isdigit() else 1)
        )
        self.class_no = (
            int(class_no)
            if class_no is not None and str(class_no).isdigit()
            else (int(tp["class_no"]) if str(tp.get("class_no") or "").isdigit() else 1)
        )

        self.students: List[Dict[str, Any]] = _normalize_roster_students(
            roster,
            default_grade=self.grade,
            default_class_no=self.class_no,
        )
        self._student_by_id: Dict[str, Dict[str, Any]] = {
            str(s["student_id"]): s for s in self.students if s.get("student_id")
        }
        self._row_files: List[str] = []
        self._uploading: bool = False

        self._init_ui()
        self._update_summary_badge()

    def _init_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(12)

        # 1. 상단 안내 및 대상 학급 표시 카드
        info_frame = QFrame()
        apply_qss_role(info_frame, role="subCard", obj_name="FacePhotoHeaderCard")
        info_lay = QVBoxLayout(info_frame)
        info_lay.setContentsMargins(14, 12, 14, 12)
        info_lay.setSpacing(6)

        lbl_title = QLabel(
            f"대상 학급: {self.school_name or self.school_code or '소속학교'} "
            f"{self.grade}학년 {self.class_no}반 (명렬표 {len(self.students)}명)"
        )
        lbl_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #0F172A;")

        lbl_guide = QLabel(
            "폴더 또는 다중 이미지(.jpg, .jpeg, .png, .webp)를 선택(또는 아래 표에 드래그 앤 드롭)하면 파일명에서 "
            "학번(예: 10501) 또는 이름(예: 김유민)을 추출해 자동 매칭합니다.\n"
            "동기화 시 최대 400px 경량 JPEG(약 20KB 이하) Data URL로 변환하여 "
            "Firestore `학생_안면데이터` 컬렉션에 직접 저장합니다."
        )
        apply_qss_role(lbl_guide, role="mutedText")
        lbl_guide.setWordWrap(True)

        info_lay.addWidget(lbl_title)
        info_lay.addWidget(lbl_guide)
        lay.addWidget(info_frame)

        # 2. 파일/폴더 선택 버튼 바
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.btn_pick_folder = QPushButton("폴더 선택...")
        apply_qss_role(self.btn_pick_folder, role="secondary", obj_name="BtnPickPhotoFolder")
        self.btn_pick_folder.setMinimumHeight(34)
        self.btn_pick_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick_folder.clicked.connect(self._on_pick_folder)

        self.btn_pick_files = QPushButton("이미지 다중 선택...")
        apply_qss_role(self.btn_pick_files, role="primary", obj_name="BtnPickPhotoFiles")
        self.btn_pick_files.setMinimumHeight(34)
        self.btn_pick_files.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick_files.clicked.connect(self._on_pick_files)

        self.btn_clear_rows = QPushButton("목록 비우기")
        apply_qss_role(self.btn_clear_rows, role="secondary", obj_name="BtnClearPhotoRows")
        self.btn_clear_rows.setMinimumHeight(34)
        self.btn_clear_rows.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_rows.clicked.connect(self._on_clear_rows)

        self.lbl_match_summary = QLabel("선택된 사진: 0건 (매칭 완료: 0건)")
        apply_qss_role(self.lbl_match_summary, role="badgeNeutral", obj_name="LblPhotoMatchSummary")

        toolbar.addWidget(self.btn_pick_folder)
        toolbar.addWidget(self.btn_pick_files)
        toolbar.addWidget(self.btn_clear_rows)
        toolbar.addStretch(1)
        toolbar.addWidget(self.lbl_match_summary)
        lay.addLayout(toolbar)

        # 3. 미리보기 & 매칭 수정 테이블
        self.table = QTableWidget(0, 5)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.verticalHeader().setVisible(False)
        self.table.setIconSize(QSize(40, 40))
        self.table.setHorizontalHeaderLabels(
            ["선택", "미리보기 · 파일명", "매칭 방식", "매칭된 학생 (클릭하여 변경 가능)", "업로드 상태"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setMinimumHeight(34)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 52)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        enable_file_drop(
            self.table,
            allowed_exts=list(SUPPORTED_IMAGE_EXTENSIONS),
            allow_dirs=True,
            allow_files=True,
            multiple=True,
            on_files_dropped=self._on_paths_dropped,
            context_label="학생 얼굴 사진 폴더 또는 이미지",
        )
        lay.addWidget(self.table, stretch=1)

        # 4. 진행률 표시바
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        lay.addWidget(self.progress_bar)

        # 5. 하단 액션 버튼 바
        btm = QHBoxLayout()
        btm.setSpacing(8)

        self.lbl_status = QLabel("사진 폴더 또는 이미지 파일을 추가해 주세요.")
        apply_qss_role(self.lbl_status, role="mutedText")

        self.btn_close = QPushButton("닫기")
        apply_qss_role(self.btn_close, role="secondary", obj_name="BtnCloseFacePhotoModal")
        self.btn_close.setMinimumHeight(38)
        self.btn_close.clicked.connect(self.reject)

        self.btn_upload_sync = QPushButton("업로드 및 동기화")
        apply_qss_role(self.btn_upload_sync, role="primary", obj_name="BtnUploadSyncFacePhotos")
        self.btn_upload_sync.setMinimumHeight(38)
        self.btn_upload_sync.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_upload_sync.clicked.connect(self._on_upload_and_sync)

        btm.addWidget(self.lbl_status, stretch=1)
        btm.addWidget(self.btn_close)
        btm.addWidget(self.btn_upload_sync)
        lay.addLayout(btm)

    def _on_pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "학생 얼굴 사진 폴더 선택")
        if not folder:
            return
        files = collect_image_files_from_folder(folder)
        if not files:
            QMessageBox.information(
                self,
                "이미지 없음",
                "선택한 폴더에 지원되는 이미지(.jpg, .jpeg, .png, .webp) 파일이 없습니다.",
            )
            return
        self.add_image_files(files)

    def _on_pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "학생 얼굴 사진 다중 선택",
            "",
            "Images (*.jpg *.jpeg *.png *.webp);;All Files (*.*)",
        )
        if files:
            self.add_image_files(files)

    def _on_paths_dropped(self, paths: List[str]) -> None:
        collected: List[str] = []
        for raw in paths or []:
            p = Path(raw)
            if p.is_dir():
                collected.extend(collect_image_files_from_folder(p))
            elif p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
                collected.append(str(p))
        if collected:
            self.add_image_files(collected)

    def _on_clear_rows(self) -> None:
        if self._uploading:
            return
        self.table.setRowCount(0)
        self._row_files.clear()
        self._update_summary_badge()
        self.lbl_status.setText("목록을 비웠습니다.")

    def add_image_files(self, file_paths: Sequence[str | Path]) -> int:
        """이미지 파일 목록을 테이블에 추가하고 자동 매칭합니다."""
        existing = {str(Path(fp).resolve()) if Path(fp).exists() else str(fp) for fp in self._row_files}
        new_paths: List[str] = []
        for raw in file_paths or []:
            p = Path(str(raw).strip())
            if p.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
                continue
            key = str(p.resolve()) if p.exists() else str(p)
            if key not in existing:
                existing.add(key)
                new_paths.append(str(p))

        if not new_paths:
            return 0

        matches = match_photo_files_to_roster(
            new_paths,
            self.students,
            default_grade=self.grade,
            default_class_no=self.class_no,
        )

        for item in matches:
            self._append_match_row(item)

        self._update_summary_badge()
        self.lbl_status.setText(f"{len(matches)}건의 사진이 추가되었습니다. 매칭된 학생을 확인 후 [업로드 및 동기화]를 클릭하세요.")
        return len(matches)

    def _build_student_combo(self, selected_sid: str) -> QComboBox:
        combo = QComboBox()
        combo.setMinimumHeight(34)
        if not isinstance(combo.view(), QListView):
            combo.setView(QListView())
        combo.setStyleSheet(COMBOBOX_STYLE)
        install_no_scroll_wheel_filter(combo)

        combo.addItem("-- 미선택 (업로드 제외) --", "")
        for stu in self.students:
            sid = str(stu.get("student_id") or "").strip()
            name = str(stu.get("name") or "").strip()
            num = stu.get("number")
            num_prefix = f"[{int(num):02d}번] " if isinstance(num, int) else ""
            combo.addItem(f"{num_prefix}{sid} {name}", sid)

        if selected_sid:
            idx = combo.findData(selected_sid)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(lambda _i: self._on_student_combo_changed())
        return combo

    def _append_match_row(self, match_item: Dict[str, Any]) -> None:
        row_idx = self.table.rowCount()
        self.table.insertRow(row_idx)

        file_path = str(match_item["file_path"])
        file_name = str(match_item["file_name"])
        matched_sid = str(match_item.get("matched_student_id") or "")
        reason = str(match_item.get("match_reason") or "미매칭")
        self._row_files.append(file_path)

        # 0) 선택 체크박스
        chk = QCheckBox()
        chk.setChecked(bool(matched_sid))
        chk.stateChanged.connect(lambda _s: self._update_summary_badge())
        chk_wrap = QWidget()
        chk_lay = QHBoxLayout(chk_wrap)
        chk_lay.setContentsMargins(0, 0, 0, 0)
        chk_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chk_lay.addWidget(chk)
        self.table.setCellWidget(row_idx, 0, chk_wrap)

        # 1) 썸네일 아이콘 + 파일명
        it_file = QTableWidgetItem(file_name)
        it_file.setData(Qt.ItemDataRole.UserRole, file_path)
        it_file.setToolTip(file_path)
        pix = QPixmap(file_path)
        if not pix.isNull():
            thumb = pix.scaled(
                40,
                40,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            it_file.setIcon(QIcon(thumb))
        self.table.setItem(row_idx, 1, it_file)

        # 2) 매칭 방식
        it_reason = QTableWidgetItem(reason)
        it_reason.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row_idx, 2, it_reason)

        # 3) 매칭 학생 선택 콤보박스
        combo = self._build_student_combo(matched_sid)
        self.table.setCellWidget(row_idx, 3, combo)

        # 4) 업로드 상태
        it_status = QTableWidgetItem("대기" if matched_sid else "학생 미지정")
        it_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row_idx, 4, it_status)

    def _get_row_checkbox(self, row: int) -> Optional[QCheckBox]:
        wrap = self.table.cellWidget(row, 0)
        if wrap is None:
            return None
        return wrap.findChild(QCheckBox)

    def _get_row_combo(self, row: int) -> Optional[QComboBox]:
        w = self.table.cellWidget(row, 3)
        return w if isinstance(w, QComboBox) else None

    def _on_student_combo_changed(self) -> None:
        for r in range(self.table.rowCount()):
            combo = self._get_row_combo(r)
            chk = self._get_row_checkbox(r)
            it_status = self.table.item(r, 4)
            if combo is None:
                continue
            sid = str(combo.currentData() or "").strip()
            if chk is not None:
                chk.blockSignals(True)
                chk.setChecked(bool(sid))
                chk.blockSignals(False)
            if it_status is not None and it_status.text() in ("대기", "학생 미지정"):
                it_status.setText("대기" if sid else "학생 미지정")
        self._update_summary_badge()

    def _update_summary_badge(self) -> None:
        total = self.table.rowCount()
        ready = 0
        for r in range(total):
            chk = self._get_row_checkbox(r)
            combo = self._get_row_combo(r)
            sid = str(combo.currentData() or "").strip() if combo else ""
            if chk and chk.isChecked() and sid:
                ready += 1
        self.lbl_match_summary.setText(f"선택된 사진: {total}건 (업로드 대상: {ready}건)")
        apply_qss_role(
            self.lbl_match_summary,
            role="badgeSuccess" if ready > 0 else "badgeNeutral",
            obj_name="LblPhotoMatchSummary",
        )

    def get_selected_upload_targets(self) -> List[Dict[str, Any]]:
        """현재 체크되어 있고 학생이 매칭된 업로드 대상 목록을 반환합니다."""
        targets: List[Dict[str, Any]] = []
        for r in range(self.table.rowCount()):
            chk = self._get_row_checkbox(r)
            combo = self._get_row_combo(r)
            it_file = self.table.item(r, 1)
            if not chk or not chk.isChecked() or not combo or not it_file:
                continue
            sid = str(combo.currentData() or "").strip()
            if not sid:
                continue
            stu = self._student_by_id.get(sid) or {
                "student_id": sid,
                "name": combo.currentText().split()[-1] if combo.currentText() else "",
                "grade": self.grade,
                "class_no": self.class_no,
            }
            targets.append(
                {
                    "row": r,
                    "file_path": str(it_file.data(Qt.ItemDataRole.UserRole) or ""),
                    "file_name": it_file.text(),
                    "student_id": sid,
                    "student_name": str(stu.get("name") or "").strip(),
                    "grade": stu.get("grade") if stu.get("grade") is not None else self.grade,
                    "class_no": stu.get("class_no") if stu.get("class_no") is not None else self.class_no,
                }
            )
        return targets

    def _set_uploading_ui_state(self, uploading: bool) -> None:
        self._uploading = uploading
        self.btn_pick_folder.setEnabled(not uploading)
        self.btn_pick_files.setEnabled(not uploading)
        self.btn_clear_rows.setEnabled(not uploading)
        self.btn_upload_sync.setEnabled(not uploading)
        self.btn_upload_sync.setText("업로드 및 동기화 중..." if uploading else "업로드 및 동기화")
        self.progress_bar.setVisible(uploading)

    def _on_upload_and_sync(self) -> None:
        if self._uploading:
            return
        targets = self.get_selected_upload_targets()
        if not targets:
            QMessageBox.warning(
                self,
                "업로드 대상 없음",
                "업로드할 사진을 선택하고 매칭된 학생을 지정해 주세요.",
            )
            return

        self._set_uploading_ui_state(True)
        self.progress_bar.setValue(0)
        self.lbl_status.setText(
            f"총 {len(targets)}명의 사진을 400px 경량 JPEG Data URL로 변환하여 `학생_안면데이터`에 동기화 중..."
        )

        def _work(worker: FunctionWorker) -> Dict[str, Any]:
            svc = FirestoreSyncService(db=self.db, config=self.config)
            prepared_docs: List[Dict[str, Any]] = []
            row_outcomes: List[Tuple[int, bool, str]] = []
            total = len(targets)

            for idx, item in enumerate(targets, start=1):
                r_idx = int(item["row"])
                sid = str(item["student_id"])
                sname = str(item["student_name"])
                fp = str(item["file_path"])
                pct = int((idx - 1) / max(1, total) * 80)
                worker.emit_progress(pct, f"[{idx}/{total}] {sid} {sname} 400px JPEG 변환 중...")

                try:
                    jpeg_bytes = resize_image_to_jpeg_bytes(fp, max_px=DEFAULT_MAX_PHOTO_PX)
                    b64_data = base64.b64encode(jpeg_bytes).decode("ascii")
                    photo_data_url = f"data:image/jpeg;base64,{b64_data}"
                    photo_hash = hashlib.sha256(jpeg_bytes).hexdigest().lower()

                    doc = svc.normalize_student_face_photo_doc(
                        {
                            "school_code": self.school_code,
                            "school_name": self.school_name,
                            "grade": item.get("grade") or self.grade,
                            "class_no": item.get("class_no") or self.class_no,
                            "student_id": sid,
                            "student_name": sname,
                            "photo_data_url": photo_data_url,
                            "photo_hash": photo_hash,
                        }
                    )
                    prepared_docs.append(doc)
                    row_outcomes.append((r_idx, True, f"변환 완료 ({len(jpeg_bytes) // 1024 or 1}KB)"))
                except Exception as exc:
                    row_outcomes.append((r_idx, False, f"오류: {exc}"))

            batch_ok = False
            if prepared_docs:
                worker.emit_progress(85, f"학생_안면데이터 컬렉션 일괄 동기화 중 ({len(prepared_docs)}건)...")
                batch_ok = svc.upsert_student_face_photos_batch(
                    prepared_docs,
                    school_code=self.school_code,
                    school_name=self.school_name,
                    grade=self.grade,
                    class_no=self.class_no,
                )

            worker.emit_progress(100, "학생 얼굴 사진 변환 및 동기화 완료")
            return {
                "uploaded_count": len(prepared_docs),
                "total_count": total,
                "batch_ok": batch_ok,
                "row_outcomes": row_outcomes,
            }

        def _on_progress(pct: int, msg: str) -> None:
            self.progress_bar.setValue(max(0, min(100, int(pct))))
            if msg:
                self.lbl_status.setText(msg)

        def _on_result(res: Any) -> None:
            data = res if isinstance(res, dict) else {}
            outcomes: List[Tuple[int, bool, str]] = list(data.get("row_outcomes") or [])
            batch_ok = bool(data.get("batch_ok"))
            uploaded_cnt = int(data.get("uploaded_count") or 0)
            total_cnt = int(data.get("total_count") or 0)

            for r_idx, ok, status_txt in outcomes:
                it_status = self.table.item(r_idx, 4)
                if it_status is not None:
                    if ok and batch_ok:
                        it_status.setText("동기화 완료")
                    elif ok and not batch_ok:
                        it_status.setText("DB 저장 실패")
                    else:
                        it_status.setText(status_txt)

            if uploaded_cnt > 0 and batch_ok:
                self.lbl_status.setText(
                    f"총 {uploaded_cnt}/{total_cnt}명 400px JPEG 변환 및 `학생_안면데이터` 저장 완료"
                )
                QMessageBox.information(
                    self,
                    "학생 얼굴 사진 등록 완료",
                    f"총 {uploaded_cnt}명의 학생 사진이 400px 경량 JPEG Data URL로 최적화되어\n"
                    "Firestore `학생_안면데이터` 컬렉션에 직접 동기화되었습니다.",
                )
            elif uploaded_cnt > 0 and not batch_ok:
                self.lbl_status.setText("이미지 변환은 완료되었으나 Firestore `학생_안면데이터` 저장에 실패했습니다.")
                QMessageBox.warning(
                    self,
                    "동기화 실패",
                    f"{uploaded_cnt}건의 이미지 변환은 완료되었으나\n"
                    "`학생_안면데이터` 컬렉션 일괄 저장에 실패했습니다. 네트워크 및 Firebase/Vercel API 연결을 확인해 주세요.",
                )
            else:
                self.lbl_status.setText("이미지 처리에 실패했습니다. 상태 열의 오류 메시지를 확인해 주세요.")
                QMessageBox.warning(
                    self,
                    "처리 실패",
                    "학생 사진 변환에 실패했습니다. 이미지 파일이 손상되지 않았는지 확인해 주세요.",
                )

        def _on_error(err: str) -> None:
            first_line = str(err or "").splitlines()[0] if err else "알 수 없는 오류"
            self.lbl_status.setText(f"오류 발생: {first_line}")
            QMessageBox.critical(self, "학생 사진 업로드 오류", str(err))

        worker = FunctionWorker("학생 얼굴 사진 일괄 업로드", _work)
        worker.signals.progress.connect(_on_progress)
        self.task_runner.start_worker(
            worker,
            task_key="student_face_photos_batch_upload",
            on_result=_on_result,
            on_error=_on_error,
            on_finished=lambda: self._set_uploading_ui_state(False),
        )
