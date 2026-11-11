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
