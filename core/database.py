"""
로컬 SQLite 데이터베이스(data/workstation.db) 관리 모듈.
- 파일 자동 분류 규칙(sorting_rules)
- 파일 이동/복사 이력 및 실행 취소(file_history)
- 학급 학생 명렬표 마스터 데이터(students)
- 시스템 작업 로그(app_logs)
"""

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from utils.path_helper import get_data_path


DEFAULT_SORTING_RULES: List[Tuple[int, str, str, str, str, str, int]] = [
    # (priority, rule_name, match_type, keywords, extensions, category_folder, is_active)
    (1, "공문 및 대외협조", "keyword", "공문,시행,협조,접수,발송", "", "01_공문서_시행문", 1),
    (2, "학교/부서 계획서", "keyword", "계획서,운영계획,기본계획,방안,품의", "", "02_업무계획_품의", 1),
    (3, "출결 및 체험학습", "keyword", "출결,결석계,체험학습,교외체험,질병결석", "", "03_출결_체험학습", 1),
    (4, "성적 및 평가자료", "keyword", "성적,지필,수행평가,정답,채점,문항,일람표", "", "04_성적_평가관리", 1),
    (5, "가정통신문 및 안내장", "keyword", "가정통신문,통신문,안내장,학부모안내,동의서", "", "05_가정통신문", 1),
    (6, "한글/워드 문서", "extension", "", ".hwp,.hwpx,.doc,.docx,.txt,.rtf", "10_일반문서(HWP_Word)", 1),
    (7, "엑셀/스프레드시트", "extension", "", ".xlsx,.xls,.csv", "11_스프레드시트(Excel_CSV)", 1),
    (8, "프레젠테이션/수업교재", "extension", "", ".pptx,.ppt,.show", "12_수업발표자료(PPT)", 1),
    (9, "PDF 문서", "extension", "", ".pdf", "13_PDF문서", 1),
    (10, "이미지 및 스캔본", "extension", "", ".jpg,.jpeg,.png,.gif,.bmp,.webp,.tif,.tiff", "14_이미지_스캔자료", 1),
    (11, "압축 파일", "extension", "", ".zip,.7z,.rar,.alz,.egg,.tar,.gz", "15_압축파일", 1),
]

DEFAULT_SUBJECT_EVAL_COLUMNS: List[Dict[str, str]] = [
    {"id": "exam_1", "name": "1차 지필"},
    {"id": "exam_2", "name": "2차 지필"},
    {"id": "perf_1", "name": "수행평가"},
]


class DatabaseManager:
    """스레드 안전한 로컬 SQLite 데이터베이스 매니저 (싱글톤)."""

    _instance = None
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, db_rel_path: str = "data/workstation.db"):
        if getattr(self, "_initialized", False):
            return
        self.db_path: str = get_data_path(db_rel_path, ensure_parent=True)
        self._init_schema()
        self._seed_default_rules()
        self._initialized = True

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sorting_rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        priority INTEGER NOT NULL DEFAULT 10,
                        rule_name TEXT NOT NULL,
                        match_type TEXT NOT NULL DEFAULT 'keyword',
                        keywords TEXT NOT NULL DEFAULT '',
                        extensions TEXT NOT NULL DEFAULT '',
                        category_folder TEXT NOT NULL,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS file_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        batch_id TEXT NOT NULL,
                        file_name TEXT NOT NULL,
                        original_path TEXT NOT NULL,
                        target_path TEXT NOT NULL,
                        category_folder TEXT NOT NULL,
                        rule_name TEXT NOT NULL,
                        action_type TEXT NOT NULL DEFAULT 'move',
                        is_undone INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS students (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_id TEXT NOT NULL UNIQUE,
                        grade INTEGER NOT NULL DEFAULT 1,
                        class_no INTEGER NOT NULL DEFAULT 1,
                        number INTEGER NOT NULL DEFAULT 1,
                        name TEXT NOT NULL,
                        contact TEXT NOT NULL DEFAULT '',
                        parent_contact TEXT NOT NULL DEFAULT '',
                        memo TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        level TEXT NOT NULL,
                        source TEXT NOT NULL,
                        message TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS roster_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_type TEXT NOT NULL,
                        group_name TEXT NOT NULL,
                        extra_info TEXT NOT NULL DEFAULT '',
                        columns_json TEXT NOT NULL DEFAULT '[]',
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS roster_students (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id INTEGER NOT NULL,
                        seq_no INTEGER NOT NULL DEFAULT 0,
                        student_id TEXT NOT NULL,
                        grade INTEGER NOT NULL DEFAULT 1,
                        class_no INTEGER NOT NULL DEFAULT 1,
                        number INTEGER NOT NULL DEFAULT 1,
                        name TEXT NOT NULL,
                        extra_data TEXT NOT NULL DEFAULT '{}',
                        memo TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(group_id) REFERENCES roster_groups(id) ON DELETE CASCADE
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_roster_groups_type ON roster_groups(group_type, is_active)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_roster_students_group ON roster_students(group_id, seq_no, student_id)"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subject_eval_configs (
                        subject_key TEXT PRIMARY KEY,
                        columns_json TEXT NOT NULL DEFAULT '[]',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subject_student_grades (
                        subject_key TEXT NOT NULL,
                        student_id TEXT NOT NULL,
                        scores_json TEXT NOT NULL DEFAULT '{}',
                        memo TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (subject_key, student_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS duty_rotation_state (
                        homeroom_key TEXT PRIMARY KEY,
                        cleaning_next_num INTEGER NOT NULL DEFAULT 1,
                        bulletin_next_num INTEGER NOT NULL DEFAULT 1,
                        excluded_json TEXT NOT NULL DEFAULT '[]',
                        cleaning_reassign_json TEXT NOT NULL DEFAULT '[]',
                        bulletin_reassign_json TEXT NOT NULL DEFAULT '[]',
                        last_period_text TEXT NOT NULL DEFAULT '',
                        selected_dates_json TEXT NOT NULL DEFAULT '[]',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute("PRAGMA table_info(duty_rotation_state)")
                duty_cols = {str(r["name"]) for r in cur.fetchall()}
                if "selected_dates_json" not in duty_cols:
                    cur.execute(
                        "ALTER TABLE duty_rotation_state ADD COLUMN selected_dates_json TEXT NOT NULL DEFAULT '[]'"
                    )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subject_material_configs (
                        subject_key TEXT PRIMARY KEY,
                        root_folder TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subject_material_progress (
                        subject_key TEXT NOT NULL,
                        rel_path TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT '예정',
                        memo TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (subject_key, rel_path)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS classroom_attendance_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        homeroom_key TEXT NOT NULL,
                        student_id TEXT NOT NULL,
                        student_name TEXT NOT NULL,
                        category TEXT NOT NULL DEFAULT '질병결석',
                        reason TEXT NOT NULL DEFAULT '',
                        period_text TEXT NOT NULL DEFAULT '',
                        selected_dates_json TEXT NOT NULL DEFAULT '[]',
                        proof_status TEXT NOT NULL DEFAULT '증빙 대기',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_classroom_attendance_hr ON classroom_attendance_logs(homeroom_key, id DESC)"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS classroom_seating_state (
                        homeroom_key TEXT PRIMARY KEY,
                        seating_json TEXT NOT NULL DEFAULT '[]',
                        front_care_json TEXT NOT NULL DEFAULT '[]',
                        use_front_care INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS classroom_observation_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        homeroom_key TEXT NOT NULL,
                        student_id TEXT NOT NULL,
                        student_name TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_classroom_obs_hr ON classroom_observation_logs(homeroom_key, id DESC)"
                )
                conn.commit()
            finally:
                conn.close()

    def _seed_default_rules(self) -> None:
        """최초 실행 시 분류 규칙이 비어 있으면 교무/행정 기본 규칙을 자동 생성합니다."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) as cnt FROM sorting_rules")
                row = cur.fetchone()
                if row and row["cnt"] == 0:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    for priority, name, m_type, kw, ext, folder, active in DEFAULT_SORTING_RULES:
                        cur.execute(
                            """
                            INSERT INTO sorting_rules
                            (priority, rule_name, match_type, keywords, extensions, category_folder, is_active, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (priority, name, m_type, kw, ext, folder, active, now_str),
                        )
                    conn.commit()
            finally:
                conn.close()

    def get_sorting_rules(self, active_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                if active_only:
                    cur.execute(
                        "SELECT * FROM sorting_rules WHERE is_active = 1 ORDER BY priority ASC, id ASC"
                    )
                else:
                    cur.execute("SELECT * FROM sorting_rules ORDER BY priority ASC, id ASC")
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def add_sorting_rule(
        self,
        priority: int,
        rule_name: str,
        match_type: str,
        keywords: str,
        extensions: str,
        category_folder: str,
        is_active: bool = True,
    ) -> int:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO sorting_rules
                    (priority, rule_name, match_type, keywords, extensions, category_folder, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        priority,
                        rule_name.strip(),
                        match_type.strip(),
                        keywords.strip(),
                        extensions.strip(),
                        category_folder.strip(),
                        1 if is_active else 0,
                        now_str,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def update_sorting_rule(
        self,
        rule_id: int,
        priority: int,
        rule_name: str,
        match_type: str,
        keywords: str,
        extensions: str,
        category_folder: str,
        is_active: bool = True,
    ) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE sorting_rules
                    SET priority = ?,
                        rule_name = ?,
                        match_type = ?,
                        keywords = ?,
                        extensions = ?,
                        category_folder = ?,
                        is_active = ?
                    WHERE id = ?
                    """,
                    (
                        priority,
                        rule_name.strip(),
                        match_type.strip(),
                        keywords.strip(),
                        extensions.strip(),
                        category_folder.strip(),
                        1 if is_active else 0,
                        rule_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_sorting_rule(self, rule_id: int) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM sorting_rules WHERE id = ?", (rule_id,))
                conn.commit()
            finally:
                conn.close()

    def reset_default_sorting_rules(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM sorting_rules")
                conn.commit()
            finally:
                conn.close()
            self._seed_default_rules()

    def add_file_history(
        self,
        batch_id: str,
        file_name: str,
        original_path: str,
        target_path: str,
        category_folder: str,
        rule_name: str,
        action_type: str = "move",
    ) -> int:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO file_history
                    (batch_id, file_name, original_path, target_path, category_folder, rule_name, action_type, is_undone, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        batch_id,
                        file_name,
                        original_path,
                        target_path,
                        category_folder,
                        rule_name,
                        action_type,
                        now_str,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def get_file_history(self, limit: int = 200, include_undone: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                if include_undone:
                    cur.execute(
                        "SELECT * FROM file_history ORDER BY id DESC LIMIT ?", (limit,)
                    )
                else:
                    cur.execute(
                        "SELECT * FROM file_history WHERE is_undone = 0 ORDER BY id DESC LIMIT ?",
                        (limit,),
                    )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def mark_history_undone(self, history_id: int) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE file_history SET is_undone = 1 WHERE id = ?", (history_id,)
                )
                conn.commit()
            finally:
                conn.close()

    def clear_file_history(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM file_history")
                conn.commit()
            finally:
                conn.close()

    def create_roster_group(
        self,
        group_type: str,
        group_name: str,
        extra_info: str = "",
        columns_json: str = "[]",
        is_active: int = 1,
    ) -> int:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if group_type == "homeroom" and is_active == 1:
                    cur.execute(
                        "UPDATE roster_groups SET is_active = 0 WHERE group_type = 'homeroom'"
                    )
                col_str = (
                    columns_json
                    if isinstance(columns_json, str)
                    else json.dumps(columns_json, ensure_ascii=False)
                )
                cur.execute(
                    """
                    INSERT INTO roster_groups (group_type, group_name, extra_info, columns_json, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_type.strip(),
                        group_name.strip(),
                        extra_info.strip(),
                        col_str,
                        int(is_active),
                        now_str,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def get_roster_groups(self, group_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                if group_type:
                    cur.execute(
                        "SELECT * FROM roster_groups WHERE group_type = ? ORDER BY id DESC",
                        (group_type,),
                    )
                else:
                    cur.execute("SELECT * FROM roster_groups ORDER BY id DESC")
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def get_roster_group(self, group_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT * FROM roster_groups WHERE id = ?", (group_id,))
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def delete_roster_group(self, group_id: int) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM roster_students WHERE group_id = ?", (group_id,))
                cur.execute("DELETE FROM roster_groups WHERE id = ?", (group_id,))
                conn.commit()
            finally:
                conn.close()

    def clear_roster_groups(self, group_type: Optional[str] = None) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                if group_type:
                    cur.execute(
                        "DELETE FROM roster_students WHERE group_id IN (SELECT id FROM roster_groups WHERE group_type = ?)",
                        (group_type,),
                    )
                    cur.execute("DELETE FROM roster_groups WHERE group_type = ?", (group_type,))
                else:
                    cur.execute("DELETE FROM roster_students")
                    cur.execute("DELETE FROM roster_groups")
                conn.commit()
            finally:
                conn.close()

    def add_roster_students(self, group_id: int, students_list: List[Dict[str, Any]]) -> int:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                rows_to_insert = []
                for s in students_list:
                    extra = s.get("extra_data", {})
                    extra_str = (
                        json.dumps(extra, ensure_ascii=False)
                        if isinstance(extra, dict)
                        else str(extra or "{}")
                    )
                    rows_to_insert.append(
                        (
                            group_id,
                            int(s.get("seq_no", 0)),
                            str(s.get("student_id", "")).strip(),
                            int(s.get("grade", 1)),
                            int(s.get("class_no", 1)),
                            int(s.get("number", 1)),
                            str(s.get("name", "")).strip(),
                            extra_str,
                            str(s.get("memo", "")).strip(),
                            now_str,
                        )
                    )
                cur.executemany(
                    """
                    INSERT INTO roster_students
                    (group_id, seq_no, student_id, grade, class_no, number, name, extra_data, memo, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
                conn.commit()
                return len(rows_to_insert)
            finally:
                conn.close()

    def get_roster_students(self, group_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM roster_students WHERE group_id = ? ORDER BY seq_no ASC, student_id ASC",
                    (group_id,),
                )
                results = []
                for r in cur.fetchall():
                    item = dict(r)
                    extra_raw = item.get("extra_data", "{}")
                    try:
                        extra_dict = (
                            json.loads(extra_raw)
                            if isinstance(extra_raw, str)
                            else dict(extra_raw or {})
                        )
                    except Exception:
                        extra_dict = {}
                    item["extra_data_dict"] = extra_dict
                    if "contact" not in item:
                        item["contact"] = (
                            extra_dict.get("연락처")
                            or extra_dict.get("학생연락처")
                            or extra_dict.get("contact")
                            or ""
                        )
                    if "parent_contact" not in item:
                        item["parent_contact"] = (
                            extra_dict.get("학부모연락처")
                            or extra_dict.get("parent_contact")
                            or ""
                        )
                    results.append(item)
                return results
            finally:
                conn.close()

    def get_active_homeroom_group(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM roster_groups WHERE group_type = 'homeroom' AND is_active = 1 ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_active_homeroom_students(self) -> List[Dict[str, Any]]:
        hr_group = self.get_active_homeroom_group()
        if hr_group:
            return self.get_roster_students(hr_group["id"])
        return []

    def get_students(self) -> List[Dict[str, Any]]:
        with self._lock:
            # 1. 활성화된 학급담임 명렬표 우선 반환
            hr_students = self.get_active_homeroom_students()
            if hr_students:
                return hr_students

            # 2. 담임 명렬표가 없으면 첫 번째 교과 명렬표 반환
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM roster_groups WHERE group_type = 'subject' ORDER BY id ASC LIMIT 1"
                )
                sub_row = cur.fetchone()
                if sub_row:
                    sub_students = self.get_roster_students(sub_row["id"])
                    if sub_students:
                        return sub_students

                # 3. 하위 호환성을 위해 기존 students 테이블 조회
                cur.execute(
                    "SELECT * FROM students ORDER BY grade ASC, class_no ASC, number ASC, student_id ASC"
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def upsert_student(
        self,
        student_id: str,
        grade: int,
        class_no: int,
        number: int,
        name: str,
        contact: str = "",
        parent_contact: str = "",
        memo: str = "",
    ) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO students (student_id, grade, class_no, number, name, contact, parent_contact, memo, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(student_id) DO UPDATE SET
                        grade = excluded.grade,
                        class_no = excluded.class_no,
                        number = excluded.number,
                        name = excluded.name,
                        contact = excluded.contact,
                        parent_contact = excluded.parent_contact,
                        memo = excluded.memo,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(student_id).strip(),
                        int(grade),
                        int(class_no),
                        int(number),
                        str(name).strip(),
                        str(contact).strip(),
                        str(parent_contact).strip(),
                        str(memo).strip(),
                        now_str,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_student(self, student_id: str) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM students WHERE student_id = ?", (student_id,))
                conn.commit()
            finally:
                conn.close()

    def clear_students(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM students")
                conn.commit()
            finally:
                conn.close()

    def add_log(self, level: str, source: str, message: str) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "INSERT INTO app_logs (level, source, message, created_at) VALUES (?, ?, ?, ?)",
                    (level.upper(), source, message, now_str),
                )
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()

    def get_recent_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT * FROM app_logs ORDER BY id DESC LIMIT ?", (limit,))
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    # =========================================================================
    # 교과별 평가 항목 구성 및 학생 성적·세특 비고 영구 저장
    # =========================================================================
    def get_subject_eval_columns(self, subject_key: str) -> List[Dict[str, str]]:
        key = (subject_key or "").strip() or "__default__"
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT columns_json FROM subject_eval_configs WHERE subject_key = ?",
                    (key,),
                )
                row = cur.fetchone()
                if row is None and key != "__default__":
                    cur.execute(
                        "SELECT columns_json FROM subject_eval_configs WHERE subject_key = ?",
                        ("__default__",),
                    )
                    row = cur.fetchone()
                if row and row["columns_json"]:
                    try:
                        parsed = json.loads(row["columns_json"])
                        cleaned: List[Dict[str, str]] = []
                        if isinstance(parsed, list):
                            for idx, item in enumerate(parsed, start=1):
                                if isinstance(item, dict):
                                    col_id = str(item.get("id", "")).strip() or f"col_{idx}"
                                    col_name = str(item.get("name", "")).strip()
                                    if col_name:
                                        cleaned.append({"id": col_id, "name": col_name})
                        if cleaned:
                            return cleaned
                    except Exception:
                        pass
                return [dict(c) for c in DEFAULT_SUBJECT_EVAL_COLUMNS]
            finally:
                conn.close()

    def save_subject_eval_columns(
        self, subject_key: str, columns: List[Dict[str, str]]
    ) -> None:
        key = (subject_key or "").strip() or "__default__"
        cleaned: List[Dict[str, str]] = []
        used_ids = set()
        for idx, item in enumerate(columns, start=1):
            col_name = str(item.get("name", "")).strip()
            if not col_name:
                continue
            col_id = str(item.get("id", "")).strip() or f"col_{idx}"
            while col_id in used_ids:
                col_id = f"{col_id}_{idx}"
            used_ids.add(col_id)
            cleaned.append({"id": col_id, "name": col_name})
        if not cleaned:
            cleaned = [dict(c) for c in DEFAULT_SUBJECT_EVAL_COLUMNS]

        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO subject_eval_configs (subject_key, columns_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(subject_key) DO UPDATE SET
                        columns_json = excluded.columns_json,
                        updated_at = excluded.updated_at
                    """,
                    (key, json.dumps(cleaned, ensure_ascii=False), now_str),
                )
                conn.commit()
            finally:
                conn.close()

    def get_subject_grades_map(self, subject_key: str) -> Dict[str, Dict[str, Any]]:
        key = (subject_key or "").strip() or "__default__"
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT student_id, scores_json, memo FROM subject_student_grades WHERE subject_key = ?",
                    (key,),
                )
                result: Dict[str, Dict[str, Any]] = {}
                for r in cur.fetchall():
                    sid = str(r["student_id"]).strip()
                    try:
                        scores = json.loads(r["scores_json"] or "{}")
                        if not isinstance(scores, dict):
                            scores = {}
                    except Exception:
                        scores = {}
                    result[sid] = {
                        "scores": {str(k): str(v) for k, v in scores.items()},
                        "memo": str(r["memo"] or ""),
                    }
                return result
            finally:
                conn.close()

    def upsert_subject_student_grade(
        self,
        subject_key: str,
        student_id: str,
        scores: Dict[str, str],
        memo: str,
    ) -> None:
        key = (subject_key or "").strip() or "__default__"
        sid = str(student_id or "").strip()
        if not sid:
            return
        clean_scores = {str(k): str(v).strip() for k, v in (scores or {}).items()}
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO subject_student_grades (subject_key, student_id, scores_json, memo, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(subject_key, student_id) DO UPDATE SET
                        scores_json = excluded.scores_json,
                        memo = excluded.memo,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        sid,
                        json.dumps(clean_scores, ensure_ascii=False),
                        str(memo or "").strip(),
                        now_str,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    # =========================================================================
    # 학급 청소·게시판 당번 번호순 순환 포인터 및 예외 명단 영구 저장
    # =========================================================================
    def get_duty_rotation_state(self, homeroom_key: str) -> Dict[str, Any]:
        key = (homeroom_key or "").strip() or "__sample__"
        default_state: Dict[str, Any] = {
            "cleaning_next_num": 1,
            "bulletin_next_num": 1,
            "excluded_ids": [],
            "cleaning_reassign_ids": [],
            "bulletin_reassign_ids": [],
            "last_period_text": "",
            "selected_dates": [],
        }
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM duty_rotation_state WHERE homeroom_key = ?",
                    (key,),
                )
                row = cur.fetchone()
                if not row:
                    return default_state
                def _parse_str_list(raw: Any) -> List[str]:
                    try:
                        arr = json.loads(raw or "[]")
                        if isinstance(arr, list):
                            return [str(x).strip() for x in arr if str(x).strip()]
                    except Exception:
                        pass
                    return []
                row_keys = set(row.keys())
                return {
                    "cleaning_next_num": max(1, int(row["cleaning_next_num"] or 1)),
                    "bulletin_next_num": max(1, int(row["bulletin_next_num"] or 1)),
                    "excluded_ids": _parse_str_list(row["excluded_json"]),
                    "cleaning_reassign_ids": _parse_str_list(row["cleaning_reassign_json"]),
                    "bulletin_reassign_ids": _parse_str_list(row["bulletin_reassign_json"]),
                    "last_period_text": str(row["last_period_text"] or ""),
                    "selected_dates": _parse_str_list(
                        row["selected_dates_json"] if "selected_dates_json" in row_keys else "[]"
                    ),
                }
            finally:
                conn.close()

    def save_duty_rotation_state(
        self,
        homeroom_key: str,
        *,
        cleaning_next_num: int,
        bulletin_next_num: int,
        excluded_ids: List[str],
        cleaning_reassign_ids: List[str],
        bulletin_reassign_ids: List[str],
        last_period_text: str = "",
        selected_dates: Optional[List[str]] = None,
    ) -> None:
        key = (homeroom_key or "").strip() or "__sample__"
        def _dedup(items: List[str]) -> List[str]:
            seen = set()
            out: List[str] = []
            for raw in items or []:
                s = str(raw).strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
            return out

        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO duty_rotation_state (
                        homeroom_key, cleaning_next_num, bulletin_next_num,
                        excluded_json, cleaning_reassign_json, bulletin_reassign_json,
                        last_period_text, selected_dates_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(homeroom_key) DO UPDATE SET
                        cleaning_next_num = excluded.cleaning_next_num,
                        bulletin_next_num = excluded.bulletin_next_num,
                        excluded_json = excluded.excluded_json,
                        cleaning_reassign_json = excluded.cleaning_reassign_json,
                        bulletin_reassign_json = excluded.bulletin_reassign_json,
                        last_period_text = excluded.last_period_text,
                        selected_dates_json = excluded.selected_dates_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        max(1, int(cleaning_next_num)),
                        max(1, int(bulletin_next_num)),
                        json.dumps(_dedup(excluded_ids), ensure_ascii=False),
                        json.dumps(_dedup(cleaning_reassign_ids), ensure_ascii=False),
                        json.dumps(_dedup(bulletin_reassign_ids), ensure_ascii=False),
                        str(last_period_text or "").strip(),
                        json.dumps(_dedup(selected_dates or []), ensure_ascii=False),
                        now_str,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    # =========================================================================
    # 교과별 로컬 수업 자료 폴더 경로 및 학반별 단원·파일 진도 상태 영구 저장
    # =========================================================================
    def get_subject_material_folder(self, subject_key: str) -> str:
        key = (subject_key or "").strip() or "__default__"
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT root_folder FROM subject_material_configs WHERE subject_key = ?",
                    (key,),
                )
                row = cur.fetchone()
                if row and str(row["root_folder"] or "").strip():
                    return str(row["root_folder"]).strip()
                if "|" in key:
                    group_prefix = key.split("|", 1)[0].strip()
                    if group_prefix:
                        cur.execute(
                            """
                            SELECT root_folder FROM subject_material_configs
                            WHERE subject_key LIKE ? AND root_folder != ''
                            ORDER BY updated_at DESC LIMIT 1
                            """,
                            (f"{group_prefix}|%",),
                        )
                        shared_row = cur.fetchone()
                        if shared_row and str(shared_row["root_folder"] or "").strip():
                            return str(shared_row["root_folder"]).strip()
                return ""
            finally:
                conn.close()

    def save_subject_material_folder(self, subject_key: str, root_folder: str) -> None:
        key = (subject_key or "").strip() or "__default__"
        folder_str = str(root_folder or "").strip()
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO subject_material_configs (subject_key, root_folder, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(subject_key) DO UPDATE SET
                        root_folder = excluded.root_folder,
                        updated_at = excluded.updated_at
                    """,
                    (key, folder_str, now_str),
                )
                conn.commit()
            finally:
                conn.close()

    def get_subject_material_progress_map(self, subject_key: str) -> Dict[str, Dict[str, str]]:
        key = (subject_key or "").strip() or "__default__"
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT rel_path, status, memo, updated_at
                    FROM subject_material_progress
                    WHERE subject_key = ?
                    """,
                    (key,),
                )
                out: Dict[str, Dict[str, str]] = {}
                for r in cur.fetchall():
                    rel = str(r["rel_path"] or "").strip()
                    if rel:
                        out[rel] = {
                            "status": str(r["status"] or "예정").strip() or "예정",
                            "memo": str(r["memo"] or "").strip(),
                            "updated_at": str(r["updated_at"] or "").strip(),
                        }
                return out
            finally:
                conn.close()

    def upsert_subject_material_progress(
        self,
        subject_key: str,
        rel_path: str,
        status: str,
        memo: str,
    ) -> None:
        key = (subject_key or "").strip() or "__default__"
        rel = str(rel_path or "").strip()
        if not rel:
            return
        st_clean = str(status or "예정").strip() or "예정"
        memo_clean = str(memo or "").strip()
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO subject_material_progress (subject_key, rel_path, status, memo, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(subject_key, rel_path) DO UPDATE SET
                        status = excluded.status,
                        memo = excluded.memo,
                        updated_at = excluded.updated_at
                    """,
                    (key, rel, st_clean, memo_clean, now_str),
                )
                conn.commit()
            finally:
                conn.close()

    # =========================================================================
    # 학급 경영 도구함 (출결 대장 · 좌석 배치 · 학생 관찰/상담 메모)
    # =========================================================================
    def get_attendance_logs(self, homeroom_key: str) -> List[Dict[str, Any]]:
        key = (homeroom_key or "").strip() or "__sample__"
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, homeroom_key, student_id, student_name, category,
                           reason, period_text, selected_dates_json, proof_status, updated_at
                    FROM classroom_attendance_logs
                    WHERE homeroom_key = ?
                    ORDER BY id DESC
                    """,
                    (key,),
                )
                rows: List[Dict[str, Any]] = []
                for r in cur.fetchall():
                    try:
                        sel_dates = json.loads(r["selected_dates_json"] or "[]")
                        if not isinstance(sel_dates, list):
                            sel_dates = []
                    except Exception:
                        sel_dates = []
                    rows.append(
                        {
                            "id": int(r["id"]),
                            "homeroom_key": str(r["homeroom_key"]),
                            "student_id": str(r["student_id"] or ""),
                            "student_name": str(r["student_name"] or ""),
                            "category": str(r["category"] or "질병결석"),
                            "reason": str(r["reason"] or ""),
                            "period_text": str(r["period_text"] or ""),
                            "selected_dates": [str(x).strip() for x in sel_dates if str(x).strip()],
                            "proof_status": str(r["proof_status"] or "증빙 대기"),
                            "updated_at": str(r["updated_at"] or ""),
                        }
                    )
                return rows
            finally:
                conn.close()

    def add_attendance_log(
        self,
        homeroom_key: str,
        *,
        student_id: str,
        student_name: str,
        category: str,
        reason: str,
        period_text: str,
        selected_dates: Optional[List[str]] = None,
        proof_status: str = "증빙 대기",
    ) -> int:
        key = (homeroom_key or "").strip() or "__sample__"
        dates_clean = [str(x).strip() for x in (selected_dates or []) if str(x).strip()]
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO classroom_attendance_logs
                    (homeroom_key, student_id, student_name, category, reason, period_text, selected_dates_json, proof_status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        str(student_id or "").strip(),
                        str(student_name or "").strip(),
                        str(category or "질병결석").strip() or "질병결석",
                        str(reason or "").strip(),
                        str(period_text or "").strip(),
                        json.dumps(dates_clean, ensure_ascii=False),
                        str(proof_status or "증빙 대기").strip() or "증빙 대기",
                        now_str,
                    ),
                )
                new_id = int(cur.lastrowid or 0)
                conn.commit()
                return new_id
            finally:
                conn.close()

    def update_attendance_log(
        self,
        log_id: int,
        *,
        student_id: str,
        student_name: str,
        category: str,
        reason: str,
        period_text: str,
        selected_dates: Optional[List[str]] = None,
        proof_status: str = "증빙 대기",
    ) -> None:
        dates_clean = [str(x).strip() for x in (selected_dates or []) if str(x).strip()]
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    UPDATE classroom_attendance_logs
                    SET student_id = ?,
                        student_name = ?,
                        category = ?,
                        reason = ?,
                        period_text = ?,
                        selected_dates_json = ?,
                        proof_status = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(student_id or "").strip(),
                        str(student_name or "").strip(),
                        str(category or "질병결석").strip() or "질병결석",
                        str(reason or "").strip(),
                        str(period_text or "").strip(),
                        json.dumps(dates_clean, ensure_ascii=False),
                        str(proof_status or "증빙 대기").strip() or "증빙 대기",
                        now_str,
                        int(log_id),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_attendance_log(self, log_id: int) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM classroom_attendance_logs WHERE id = ?", (int(log_id),))
                conn.commit()
            finally:
                conn.close()

    def get_seating_state(self, homeroom_key: str) -> Dict[str, Any]:
        key = (homeroom_key or "").strip() or "__sample__"
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT seating_json, front_care_json, use_front_care
                    FROM classroom_seating_state
                    WHERE homeroom_key = ?
                    """,
                    (key,),
                )
                row = cur.fetchone()
                if not row:
                    return {
                        "seating": [],
                        "front_care_ids": [],
                        "use_front_care": True,
                    }
                try:
                    seating = json.loads(row["seating_json"] or "[]")
                    if not isinstance(seating, list):
                        seating = []
                except Exception:
                    seating = []
                try:
                    care_ids = json.loads(row["front_care_json"] or "[]")
                    if not isinstance(care_ids, list):
                        care_ids = []
                except Exception:
                    care_ids = []
                return {
                    "seating": seating,
                    "front_care_ids": [str(x).strip() for x in care_ids if str(x).strip()],
                    "use_front_care": bool(int(row["use_front_care"] or 0)),
                }
            finally:
                conn.close()

    def save_seating_state(
        self,
        homeroom_key: str,
        *,
        seating: List[Dict[str, str]],
        front_care_ids: List[str],
        use_front_care: bool = True,
    ) -> None:
        key = (homeroom_key or "").strip() or "__sample__"
        care_clean = [str(x).strip() for x in (front_care_ids or []) if str(x).strip()]
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO classroom_seating_state
                    (homeroom_key, seating_json, front_care_json, use_front_care, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(homeroom_key) DO UPDATE SET
                        seating_json = excluded.seating_json,
                        front_care_json = excluded.front_care_json,
                        use_front_care = excluded.use_front_care,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        json.dumps(seating or [], ensure_ascii=False),
                        json.dumps(care_clean, ensure_ascii=False),
                        1 if use_front_care else 0,
                        now_str,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_observation_logs(self, homeroom_key: str) -> List[Dict[str, Any]]:
        key = (homeroom_key or "").strip() or "__sample__"
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, homeroom_key, student_id, student_name, content, created_at, updated_at
                    FROM classroom_observation_logs
                    WHERE homeroom_key = ?
                    ORDER BY id DESC
                    """,
                    (key,),
                )
                return [
                    {
                        "id": int(r["id"]),
                        "homeroom_key": str(r["homeroom_key"]),
                        "student_id": str(r["student_id"] or ""),
                        "student_name": str(r["student_name"] or ""),
                        "content": str(r["content"] or ""),
                        "created_at": str(r["created_at"] or ""),
                        "updated_at": str(r["updated_at"] or ""),
                    }
                    for r in cur.fetchall()
                ]
            finally:
                conn.close()

    def add_observation_log(
        self,
        homeroom_key: str,
        *,
        student_id: str,
        student_name: str,
        content: str,
    ) -> int:
        key = (homeroom_key or "").strip() or "__sample__"
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO classroom_observation_logs
                    (homeroom_key, student_id, student_name, content, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        str(student_id or "").strip(),
                        str(student_name or "").strip(),
                        str(content or "").strip(),
                        now_str,
                        now_str,
                    ),
                )
                new_id = int(cur.lastrowid or 0)
                conn.commit()
                return new_id
            finally:
                conn.close()

    def update_observation_log(self, log_id: int, content: str) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    UPDATE classroom_observation_logs
                    SET content = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(content or "").strip(), now_str, int(log_id)),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_observation_log(self, log_id: int) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM classroom_observation_logs WHERE id = ?", (int(log_id),))
                conn.commit()
            finally:
                conn.close()
