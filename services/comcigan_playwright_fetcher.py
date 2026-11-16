"""
Playwright 기반 컴시간 알리미(교사용/학생용 웹 페이지) 자동 파서 모듈.
- ComciganPlaywrightFetcher: Playwright Chromium으로 컴시간 URL에 접속하여
  시간표 테이블(table/tr/td)과 학급/교사 드롭다운(select)을 순회·파싱하고,
  Google Sheets '시간표_기본'(base_rows) 및 '시간표_변동'(change_rows) 스키마로 정규화합니다.
- 파싱/접속 실패 시 로그 디렉터리에 comcigan_debug.html 및 comcigan_debug.png를 저장합니다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.timetable_manual_service import normalize_teacher_name
from utils.path_helper import get_data_path

DAY_NAMES = ("월", "화", "수", "목", "금", "토")
DAY_TO_OFFSET = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5}
CHANGE_STYLE_HINTS = ("#ff", "red", "rgb(255", "#fde", "#fee", "#fef", "#fff3", "yellow", "orange")
CHANGE_CLASS_HINTS = ("change", "changed", "modify", "modified", "diff", "변동", "변경", "보강")


class _CellNode:
    """HTML <td>/<th> 셀의 텍스트, 스타일, 취소선(<del>/<s>) 정보를 보관합니다."""

    def __init__(self, attrs: Dict[str, str], is_header: bool = False) -> None:
        self.attrs = attrs
        self.is_header = is_header
        self.text_parts: List[str] = []
        self.deleted_parts: List[str] = []
        self.in_deleted = False

    def append_text(self, text: str) -> None:
        if self.in_deleted:
            self.deleted_parts.append(text)
        else:
            self.text_parts.append(text)

    def full_text(self) -> str:
        raw = "".join(self.text_parts)
        return re.sub(r"[ \t]+", " ", raw).strip()

    def deleted_text(self) -> str:
        raw = "".join(self.deleted_parts)
        return re.sub(r"[ \t]+", " ", raw).strip()

    def has_change_hint(self) -> bool:
        if self.deleted_parts:
            return True
        cls = self.attrs.get("class", "").lower()
        style = self.attrs.get("style", "").lower()
        data_chg = self.attrs.get("data-changed", "").lower()
        if data_chg in ("1", "true", "yes"):
            return True
        if self.attrs.get("data-original"):
            return True
        if any(h in cls for h in CHANGE_CLASS_HINTS):
            return True
        if any(h in style for h in CHANGE_STYLE_HINTS):
            return True
        return False


class _TimetableHTMLParser(HTMLParser):
    """HTML 내 모든 <table>을 행/셀 구조로 추출하는 경량 stdlib 파서."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[Dict[str, Any]] = []
        self._cur_table: Optional[Dict[str, Any]] = None
        self._cur_row: Optional[List[_CellNode]] = None
        self._cur_cell: Optional[_CellNode] = None
        self._in_caption = False
        self._caption_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_map = {str(k).lower(): str(v or "") for k, v in attrs}
        if t == "table":
            self._cur_table = {"attrs": attr_map, "caption": "", "rows": []}
        elif t == "caption" and self._cur_table is not None:
            self._in_caption = True
            self._caption_parts = []
        elif t == "tr" and self._cur_table is not None:
            self._cur_row = []
        elif t in ("td", "th") and self._cur_row is not None:
            self._cur_cell = _CellNode(attr_map, is_header=(t == "th"))
        elif t == "br" and self._cur_cell is not None:
            self._cur_cell.append_text("\n")
        elif t in ("del", "s", "strike") and self._cur_cell is not None:
            self._cur_cell.in_deleted = True

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("del", "s", "strike") and self._cur_cell is not None:
            self._cur_cell.in_deleted = False
        elif t in ("td", "th") and self._cur_cell is not None and self._cur_row is not None:
            self._cur_row.append(self._cur_cell)
            self._cur_cell = None
        elif t == "tr" and self._cur_row is not None and self._cur_table is not None:
            if self._cur_row:
                self._cur_table["rows"].append(self._cur_row)
            self._cur_row = None
        elif t == "caption" and self._in_caption:
            self._in_caption = False
            if self._cur_table is not None:
                self._cur_table["caption"] = "".join(self._caption_parts).strip()
        elif t == "table" and self._cur_table is not None:
            if self._cur_table["rows"]:
                self.tables.append(self._cur_table)
            self._cur_table = None

    def handle_data(self, data: str) -> None:
        if self._in_caption:
            self._caption_parts.append(data)
        elif self._cur_cell is not None:
            self._cur_cell.append_text(data)


def _norm_num(val: str) -> str:
    """'05반', '1학년', '01' 등에서 선행 0을 제거한 순수 숫자 문자열을 반환합니다."""
    digits = re.sub(r"\D+", "", str(val or "").strip())
    return str(int(digits)) if digits else ""


def _extract_grade_class(text: str, default_grade: str = "1", default_class: str = "1") -> Tuple[str, str]:
    """문자열('1학년 3반', '2-5', '105', '3반' 등)에서 학년/반 숫자를 추출합니다."""
    s = str(text or "").strip()
    m = re.search(r"(\d+)\s*(?:학년|[-/])\s*(\d+)\s*반?", s)
    if m:
        return str(int(m.group(1))), str(int(m.group(2)))
    m_3 = re.match(r"^(\d)(\d{2})\s*반?$", s)
    if m_3:
        return str(int(m_3.group(1))), str(int(m_3.group(2)))
    m_cls = re.search(r"(\d+)\s*반", s)
    if m_cls:
        dg = _norm_num(default_grade) or default_grade
        return dg, str(int(m_cls.group(1)))
    return default_grade, default_class


def _normalize_day_header(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or ""))
    for d in DAY_NAMES:
        if (
            compact == d
            or compact.startswith(d + "(")
            or compact.startswith(d + "요일")
            or re.match(rf"^{d}(?:\d|/|\.|\-|$)", compact)
        ):
            return d
    return ""


def _resolve_day_date(base_date_str: str, day_name: str) -> str:
    """기준 날짜(target_date)가 속한 주의 월~토 요일별 실제 날짜(YYYY-MM-DD)를 계산합니다."""
    try:
        base_dt = datetime.strptime(str(base_date_str or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        base_dt = datetime.now().date()
    if day_name not in DAY_TO_OFFSET:
        return base_dt.strftime("%Y-%m-%d")
    monday = base_dt - timedelta(days=base_dt.weekday())
    return (monday + timedelta(days=DAY_TO_OFFSET[day_name])).strftime("%Y-%m-%d")


def _select_week_option_for_date(
    date_select: Any,
    date_options: List[Tuple[str, str]],
    target_date_str: str,
    target_frame: Any,
) -> None:
    """일자 <select>에서 target_date가 속한 주차 옵션을 찾아 선택합니다."""
    if date_select is None or not date_options:
        return
    try:
        target_dt = datetime.strptime(str(target_date_str or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        target_dt = datetime.now().date()
    monday_dt = target_dt - timedelta(days=target_dt.weekday())

    chosen_val = ""
    for val, txt in date_options:
        # 예: "2026-11-16 ~ 2026-11-20", "26.11.16 ~ 26.11.20", "11/16 ~ 11/20"
        full_dates = re.findall(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", txt)
        parsed_dates: List[date] = []
        for y, m, d in full_dates:
            try:
                parsed_dates.append(date(int(y), int(m), int(d)))
            except ValueError:
                pass
        if not parsed_dates:
            short_dates = re.findall(r"(\d{1,2})[./](\d{1,2})", txt)
            for m, d in short_dates:
                try:
                    parsed_dates.append(date(target_dt.year, int(m), int(d)))
                except ValueError:
                    pass
        if len(parsed_dates) >= 2 and parsed_dates[0] <= target_dt <= parsed_dates[1]:
            chosen_val = val
            break
        if any(dt in (target_dt, monday_dt) for dt in parsed_dates):
            chosen_val = val
            break

    if not chosen_val:
        try:
            cur_val = date_select.input_value()
            if cur_val and cur_val in [v for v, _ in date_options]:
                return
        except Exception:
            pass
        chosen_val = date_options[0][0]

    try:
        date_select.select_option(chosen_val)
        target_frame.wait_for_timeout(400)
    except Exception:
        pass


def _parse_subject_teacher(text: str) -> Tuple[str, str]:
    """셀 문자열('수학Ⅰ\\n홍길동' 또는 '수학Ⅰ (홍길동)')에서 과목과 교사명을 분리합니다."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return "", ""
    if len(lines) >= 2:
        subj = lines[0]
        teacher = re.sub(r"[()]", "", lines[1]).strip()
        return subj, teacher
    single = lines[0]
    m = re.match(r"^([^\s(]+(?:\s+[^\s(]+)?)\s*\(([^)]+)\)$", single)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return single, ""


def _parse_teacher_cell(text: str, default_teacher: str = "") -> Tuple[str, str, str, str]:
    """교사 개인 시간표 셀('1-3\\n수학Ⅰ', '1학년 3반 수학Ⅰ', '103\\n수학Ⅰ' 등)에서 (grade, class_no, subject, teacher)를 추출합니다."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    joined = " ".join(lines)
    grade = ""
    class_no = ""
    subj = joined

    m = re.match(r"^\s*(\d+)\s*(?:학년|[-/])\s*(\d+)\s*반?\s*(.*)$", joined)
    if m:
        grade = m.group(1)
        class_no = m.group(2)
        subj = m.group(3).strip()
    elif lines:
        m_num = re.match(r"^(\d)(\d{2})$", lines[0])
        if m_num:
            grade = m_num.group(1)
            class_no = str(int(m_num.group(2)))
            subj = " ".join(lines[1:]).strip()
        elif len(lines) >= 2:
            g2, c2 = _extract_grade_class(lines[0], default_grade="", default_class="")
            if g2 and c2:
                grade, class_no = g2, c2
                subj = " ".join(lines[1:]).strip()

    return grade, class_no, subj.strip(), default_teacher


class ComciganPlaywrightFetcher:
    """Playwright Chromium을 사용해 컴시간 웹 시간표를 추출하고 정규화하는 파서."""

    def __init__(
        self,
        url: str = "",
        school_code: str = "",
        comcigan_school_code: str = "",
        school_name: str = "",
        target_date: str = "",
        teacher_name: str = "",
        target_grade: str = "",
        target_class: str = "",
        scope: str = "homeroom_and_teacher",
        headless: bool = True,
        timeout_ms: int = 20000,
        log_dir: Optional[Path | str] = None,
    ) -> None:
        self.url = str(url or "").strip()
        self.school_code = str(school_code or "").strip()
        self.comcigan_school_code = str(comcigan_school_code or "").strip()
        self.school_name = str(school_name or "").strip()
        self.target_date = str(target_date or datetime.now().strftime("%Y-%m-%d")).strip()
        self.teacher_name = normalize_teacher_name(teacher_name)
        self.target_grade = str(target_grade or "").strip()
        self.target_class = str(target_class or "").strip()
        self.scope = str(scope or "homeroom_and_teacher").strip()
        self.headless = bool(headless)
        self.timeout_ms = int(timeout_ms)

        if log_dir is not None:
            self.log_dir = Path(log_dir)
            self.log_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.log_dir = Path(get_data_path("logs/comcigan_debug.html", ensure_parent=True)).parent
        self.debug_html_path = self.log_dir / "comcigan_debug.html"
        self.debug_png_path = self.log_dir / "comcigan_debug.png"

    @classmethod
    def parse_html(
        cls,
        html_content: str,
        grade_hint: str = "1",
        class_hint: str = "1",
        target_date: str = "",
        teacher_name: str = "",
        mode: str = "class",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """HTML 문자열에서 시간표_기본(base_rows)과 시간표_변동(change_rows)을 파싱합니다."""
        date_str = str(target_date or datetime.now().strftime("%Y-%m-%d")).strip()
        parser = _TimetableHTMLParser()
        parser.feed(html_content or "")
        parser.close()

        base_rows: List[Dict[str, Any]] = []
        change_rows: List[Dict[str, Any]] = []

        for tbl in parser.tables:
            rows = tbl["rows"]
            if len(rows) < 2:
                continue

            # 헤더 행 탐색 (요일명이 2개 이상 포함된 첫 행)
            header_idx = -1
            day_cols: Dict[int, str] = {}
            for r_idx, row in enumerate(rows[:3]):
                found: Dict[int, str] = {}
                for c_idx, cell in enumerate(row):
                    day = _normalize_day_header(cell.full_text())
                    if day:
                        found[c_idx] = day
                if len(found) >= 2:
                    header_idx = r_idx
                    day_cols = found
                    break

            if header_idx < 0:
                continue
                     
            if mode == "teacher":
                caption_grade, caption_class = "", ""
            else:
                caption_grade, caption_class = _extract_grade_class(
                    tbl.get("caption", ""), default_grade=grade_hint, default_class=class_hint
                )

            for row in rows[header_idx + 1 :]:
                if not row:
                    continue
                period_raw = row[0].full_text()
                m_per = re.search(r"(\d+)", period_raw)
                if not m_per:
                    continue
                period = m_per.group(1)

                for c_idx, day_name in day_cols.items():
                    if c_idx >= len(row):
                        continue
                    cell = row[c_idx]
                    raw_text = cell.full_text()
                    deleted_txt = cell.deleted_text()
                    is_changed = cell.has_change_hint()

                    # 교사 시간표(mode="teacher")의 빈 셀(원래 공강)은 변동으로 기록하지 않음
                    if mode == "teacher" and not raw_text and not deleted_txt:
                        continue

                    # 텍스트가 비어 있어도 class="변경" 등 변동 힌트가 있으면 수업 취소/공강으로 처리
                    if not raw_text and not deleted_txt and not is_changed:
                        continue

                    arrow_match = re.search(
                        r"([^\s\-→>]+)\s*(?:->|→)\s*([^\n(]+)(?:\(([^)]+)\))?",
                        raw_text,
                    )

                    if not raw_text and not deleted_txt and is_changed:
                        row_grade, row_class = caption_grade, caption_class
                        orig_subj = ""
                        orig_teacher = teacher_name if mode == "teacher" else ""
                        chg_subj = "수업없음(휴업/공강)"
                        chg_teacher = ""
                    elif deleted_txt and not raw_text:
                        is_changed = True
                        if mode == "teacher":
                            row_grade, row_class, orig_subj, orig_teacher = _parse_teacher_cell(
                                deleted_txt, teacher_name
                            )
                        else:
                            orig_subj, orig_teacher = _parse_subject_teacher(deleted_txt)
                            row_grade, row_class = caption_grade, caption_class
                        chg_subj = "수업없음(휴업/공강)"
                        chg_teacher = ""
                    elif arrow_match:
                        is_changed = True
                        orig_subj = arrow_match.group(1).strip()
                        chg_subj = arrow_match.group(2).strip()
                        chg_teacher = (arrow_match.group(3) or "").strip()
                        if mode == "teacher":
                            row_grade, row_class, _, _ = _parse_teacher_cell(raw_text, teacher_name)
                            orig_teacher = teacher_name
                        else:
                            row_grade, row_class = caption_grade, caption_class
                            orig_teacher = ""
                    elif deleted_txt and raw_text:
                        is_changed = True
                        if mode == "teacher":
                            del_grade, del_class, orig_subj, orig_teacher = _parse_teacher_cell(
                                deleted_txt, teacher_name
                            )
                            row_grade, row_class, chg_subj, chg_teacher = _parse_teacher_cell(
                                raw_text, teacher_name
                            )
                            row_grade = row_grade or del_grade
                            row_class = row_class or del_class
                        else:
                            orig_subj, orig_teacher = _parse_subject_teacher(deleted_txt)
                            chg_subj, chg_teacher = _parse_subject_teacher(raw_text)
                            row_grade, row_class = caption_grade, caption_class
                    else:
                        if mode == "teacher":
                            row_grade, row_class, chg_subj, chg_teacher = _parse_teacher_cell(raw_text, teacher_name)
                        else:
                            chg_subj, chg_teacher = _parse_subject_teacher(raw_text)
                            row_grade, row_class = caption_grade, caption_class
                        orig_subj = str(cell.attrs.get("data-original", "") or "").strip()
                        orig_teacher = ""
                        if "[변경]" in chg_subj or "(변경)" in chg_subj:
                            is_changed = True
                            chg_subj = chg_subj.replace("[변경]", "").replace("(변경)", "").strip()
                        if is_changed and mode == "teacher" and not orig_teacher:
                            orig_teacher = teacher_name

                    if not chg_subj:
                        continue

                    eff_grade = row_grade or caption_grade
                    eff_class = row_class or caption_class
                    eff_teacher = normalize_teacher_name(chg_teacher or teacher_name)
                    orig_teacher = normalize_teacher_name(orig_teacher)
                    entry_type = "teacher" if mode == "teacher" else "class"
                    row_date = _resolve_day_date(date_str, day_name)
                    week_start = _resolve_day_date(date_str, "월")

                    remote_base_id = (
                        f"tb_t_{eff_teacher}_{day_name}_{period}"
                        if entry_type == "teacher"
                        else f"tb_c_{eff_grade}_{eff_class}_{day_name}_{period}"
                    )

                    # 변동 셀(class="변경")은 기본 시간표(시간표_기본)를 덮어쓰지 않음
                    if not is_changed:
                        base_rows.append(
                            {
                                "remote_id": remote_base_id,
                                "entry_type": entry_type,
                                "week_start": week_start,
                                "date": row_date,
                                "grade": eff_grade,
                                "class_no": eff_class,
                                "day_of_week": day_name,
                                "period": period,
                                "subject": chg_subj,
                                "teacher_name": eff_teacher,
                                "teacher": eff_teacher,
                                "room": "",
                                "source": "comcigan",
                            }
                        )

                    if is_changed:
                        row_date = _resolve_day_date(date_str, day_name)
                        if mode == "teacher" and (not eff_grade or not eff_class):
                            chg_remote_id = f"tc_t_{row_date.replace('-', '')}_{eff_teacher}_{period}"
                        else:
                            chg_remote_id = f"tc_{row_date.replace('-', '')}_{eff_grade}_{eff_class}_{period}"
                        change_rows.append(
                            {
                                "remote_id": chg_remote_id,
                                "date": row_date,
                                "target_date": row_date,
                                "grade": eff_grade,
                                "class_no": eff_class,
                                "period": period,
                                "original_subject": orig_subj or "(기존수업)",
                                "changed_subject": chg_subj,
                                "original_teacher": orig_teacher,
                                "changed_teacher": eff_teacher,
                                "reason": "컴시간 웹 자동 감지",
                                "source": "comcigan",
                            }
                        )

        return base_rows, change_rows

    def _save_debug_snapshot(self, page: Any) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            if page is not None:
                chunks: List[str] = [page.content() or ""]
                try:
                    for idx, frm in enumerate(page.frames):
                        if frm == page.main_frame:
                            continue
                        try:
                            f_html = frm.content() or ""
                            chunks.append(f"\n<!-- ===== FRAME #{idx}: {frm.url} ===== -->\n{f_html}")
                        except Exception:
                            pass
                except Exception:
                    pass
                self.debug_html_path.write_text("\n".join(chunks), encoding="utf-8")
                page.screenshot(path=str(self.debug_png_path), full_page=True)
        except Exception:
            pass

    def _resolve_target_frame(self, page: Any) -> Any:
        """<frameset>/<frame> 또는 <iframe> 내부의 실제 시간표 프레임을 찾아 반환합니다."""
        deadline_ms = min(self.timeout_ms, 8000)
        waited = 0
        step = 300
        while waited <= deadline_ms:
            for frm in page.frames:
                if frm == page.main_frame:
                    continue
                try:
                    if frm.locator("input, select, table").count() > 0:
                        return frm
                except Exception:
                    continue
            if page.locator("frame, iframe").count() == 0:
                break
            page.wait_for_timeout(step)
            waited += step

        for frm in page.frames:
            if frm != page.main_frame and frm.url and frm.url != "about:blank":
                return frm
        return page.main_frame

    def _handle_school_search_if_needed(self, target_frame: Any) -> None:
        if not self.school_code:
            return
        try:
            search_input = target_frame.locator(
                "input[type='text'], input:not([type]), input[name*='sc'], input[id*='sc'], input[placeholder*='학교']"
            ).first
            if search_input.count() > 0 and search_input.is_visible():
                search_input.fill(self.school_code)
                btn = target_frame.locator(
                    "button:has-text('설정'), input[value*='설정'], "
                    "button:has-text('검색'), input[value*='검색'], "
                    "button:has-text('확인'), input[value*='확인'], "
                    "input[type='button'], input[type='submit'], button"
                ).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                else:
                    search_input.press("Enter")
                target_frame.wait_for_timeout(1200)
                match_opt = target_frame.locator(
                    f"a:has-text('{self.school_code}'), li:has-text('{self.school_code}')"
                ).first
                if match_opt.count() > 0 and match_opt.is_visible():
                    match_opt.click()
                    target_frame.wait_for_timeout(800)
        except Exception:
            pass

    def fetch(
        self,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """Playwright로 컴시간 페이지에 접속해 시간표를 파싱합니다."""
        if not self.url:
            raise ValueError("컴시간 접속 URL이 비어 있습니다.")

        def _report(pct: int, msg: str) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(max(0, min(100, int(pct))), msg)
                except Exception:
                    pass

        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Playwright 패키지가 설치되어 있지 않습니다. 'pip install playwright && playwright install chromium'을 실행해 주세요."
            ) from exc

        playwright_obj = None
        browser = None
        context = None
        page = None
        base_rows: List[Dict[str, Any]] = []
        change_rows: List[Dict[str, Any]] = []

        try:
            _report(5, "컴시간 웹 페이지 접속 중...")
            playwright_obj = sync_playwright().start()
            browser = playwright_obj.chromium.launch(headless=self.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            page.on("dialog", lambda d: d.accept())

            page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            target_frame = self._resolve_target_frame(page)
            self._handle_school_search_if_needed(target_frame)

            # 학교코드 설정/검색 후 프레임이 리로드될 수 있으므로 재확인
            target_frame = self._resolve_target_frame(page)
            target_frame.wait_for_selector("table tr td", timeout=self.timeout_ms)

            # 일자/학급/교사 select 드롭다운 분류
            selects = target_frame.locator("select")
            date_select = None
            date_options: List[Tuple[str, str]] = []
            class_select = None
            class_options: List[Tuple[str, str]] = []
            teacher_select = None
            teacher_options: List[Tuple[str, str]] = []

            for idx in range(selects.count()):
                sel = selects.nth(idx)
                if not sel.is_visible():
                    continue
                opts = sel.locator("option")
                valid_opts: List[Tuple[str, str]] = []
                matched_class: List[Tuple[str, str]] = []
                matched_date: List[Tuple[str, str]] = []
                for o_idx in range(opts.count()):
                    opt = opts.nth(o_idx)
                    txt = (opt.inner_text() or "").strip()
                    raw_val = opt.get_attribute("value")
                    val = txt if raw_val is None else str(raw_val)
                    if not txt or txt.startswith("-") or "선택" in txt:
                        continue
                    valid_opts.append((val, txt))
                    if re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}|~", txt):
                        matched_date.append((val, txt))
                    elif re.search(r"\d+\s*(?:학년|[-/])\s*\d+|^\d{3}\s*반?$", txt):
                        matched_class.append((val, txt))

                if matched_date and date_select is None:
                    date_select = sel
                    date_options = matched_date
                elif matched_class and class_select is None:
                    class_select = sel
                    class_options = matched_class
                elif valid_opts and teacher_select is None:
                    teacher_select = sel
                    teacher_options = valid_opts

            _select_week_option_for_date(
                date_select=date_select,
                date_options=date_options,
                target_date_str=self.target_date,
                target_frame=target_frame,
            )

            seen_base: Dict[str, Dict[str, Any]] = {}
            seen_chg: Dict[str, Dict[str, Any]] = {}
            class_base_keys: set[str] = set()
            teacher_base_keys: set[str] = set()

            # 1) 학급 시간표 추출 (담임반 지정 시 해당 반 우선, 전체 학급 모드면 전체 순회)
            if class_select is not None and class_options:
                target_class_opts = class_options
                norm_tg = _norm_num(self.target_grade)
                norm_tc = _norm_num(self.target_class)
                if self.scope != "all_classes_and_teacher" and norm_tg and norm_tc:
                    filtered = [
                        (v, t)
                        for v, t in class_options
                        if _extract_grade_class(t, "", "") == (norm_tg, norm_tc)
                    ]
                    if filtered:
                        target_class_opts = filtered
                    else:
                        target_class_opts = class_options[:1]
                elif self.scope != "all_classes_and_teacher":
                    target_class_opts = class_options[:1]

                total_cls = len(target_class_opts[:60])
                for idx, (val, txt) in enumerate(target_class_opts[:60], start=1):
                    pct = 10 + int(55 * idx / max(1, total_cls))
                    _report(pct, f"학급 시간표 파싱 중 ({idx}/{total_cls}: {txt})")
                    class_select.select_option(val)
                    target_frame.wait_for_timeout(350)
                    g, c = _extract_grade_class(txt)
                    b_list, c_list = self.parse_html(
                        target_frame.content(),
                        grade_hint=g,
                        class_hint=c,
                        target_date=self.target_date,
                        teacher_name="",
                        mode="class",
                    )
                    for row in b_list:
                        seen_base[row["remote_id"]] = row
                        class_base_keys.add(row["remote_id"])
                    for row in c_list:
                        seen_chg[row["remote_id"]] = row
            else:
                _report(35, f"학급 시간표 파싱 중 ({self.target_grade or '1'}학년 {self.target_class or '1'}반)")
                b_list, c_list = self.parse_html(
                    target_frame.content(),
                    grade_hint=self.target_grade or "1",
                    class_hint=self.target_class or "1",
                    target_date=self.target_date,
                    teacher_name="",
                    mode="class",
                )
                for row in b_list:
                    seen_base[row["remote_id"]] = row
                    class_base_keys.add(row["remote_id"])
                for row in c_list:
                    seen_chg[row["remote_id"]] = row

            # 2) 교사 개인 시간표 추출 (comcigan_teacher_name 설정 시)
            if self.teacher_name and teacher_select is not None and teacher_options:
                exact = [(v, t) for v, t in teacher_options if t == self.teacher_name]
                partial = [(v, t) for v, t in teacher_options if self.teacher_name in t]
                chosen = exact or partial
                if chosen:
                    _report(70, f"교사 시간표 파싱 중 ({self.teacher_name})")
                    teacher_select.select_option(chosen[0][0])
                    target_frame.wait_for_timeout(450)
                    b_t_list, c_t_list = self.parse_html(
                        target_frame.content(),
                        grade_hint=self.target_grade or "1",
                        class_hint=self.target_class or "1",
                        target_date=self.target_date,
                        teacher_name=self.teacher_name,
                        mode="teacher",
                    )
                    for row in b_t_list:
                        if row["remote_id"] not in seen_base:
                            seen_base[row["remote_id"]] = row
                            teacher_base_keys.add(row["remote_id"])
                    for row in c_t_list:
                        if row["remote_id"] not in seen_chg:
                            seen_chg[row["remote_id"]] = row

            base_rows = list(seen_base.values())
            change_rows = list(seen_chg.values())
            _report(75, f"시간표 파싱 완료 (기본 {len(base_rows)}건, 변동 {len(change_rows)}건)")

            if not base_rows and not change_rows:
                self._save_debug_snapshot(page)
                raise RuntimeError(
                    f"시간표 테이블을 파싱하지 못했습니다. 디버그 파일: {self.debug_html_path}"
                )

            return {
                "ok": True,
                "url": self.url,
                "school_code": self.school_code,
                "target_date": self.target_date,
                "base_rows": base_rows,
                "change_rows": change_rows,
                "base_count": len(base_rows),
                "class_base_count": len(class_base_keys),
                "teacher_base_count": len(teacher_base_keys),
                "change_count": len(change_rows),
            }
        except Exception as exc:
            self._save_debug_snapshot(page)
            msg = str(exc)
            if str(self.debug_html_path) not in msg:
                msg = f"{msg} (디버그: {self.debug_html_path})"
            raise RuntimeError(msg) from exc
        finally:
            for obj in (page, context, browser):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
            try:
                if playwright_obj is not None:
                    playwright_obj.stop()
            except Exception:
                pass
