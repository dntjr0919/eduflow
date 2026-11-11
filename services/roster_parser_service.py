"""
나이스(NEIS) 엑셀 양식 파서 서비스 모듈 (services/roster_parser_service.py).
1. 학급담임 명렬표 (homeroom): 1행 헤더(학년, 반, 번호, 성명, 주민등록번호, 주소 등), 학급 자동 감지, 동적 컬럼 보존
2. 교과 출석부 (subject): 상단 메타데이터(교과명, 대상학급, 담당교사, 시간표), 연번(1~35) 정렬, 학번 자동 계산
3. 동아리 출석부 (club): 상단 메타데이터(부서명, 담당교사), No 정렬, 다학년 학번 자동 계산
"""

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl


def _clean_header_text(val: Any) -> str:
    """헤더 셀 내 개행문자(_x000D_, \\n) 및 중복 공백을 정리합니다."""
    if val is None:
        return ""
    text = str(val).replace("_x000D_", "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_homeroom_roster(file_path: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    나이스 학급담임 명렬표 엑셀 파일을 파싱합니다.
    - 1행 헤더: 학년, 반, 번호, 성명, 학생개인번호, 주민등록번호, 성별, 생년월일, 주소 등
    - 학급 정보: 데이터 행들의 학년/반 최빈값 계산 -> "X학년 Y반"
    - 학번: 학년 * 10000 + 반 * 100 + 번호 (예: 10701)
    - 반환: (group_info, student_list)
    """
    wb = openpyxl.load_workbook(file_path, data_only=True)
    ws = wb.active

    # 1. 1행 헤더 읽기
    headers: List[str] = []
    col_idx_map: Dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        raw_val = ws.cell(1, c).value
        clean_name = _clean_header_text(raw_val)
        if clean_name:
            headers.append(clean_name)
            # 매핑용 키 생성 (공백 제거)
            key = clean_name.replace(" ", "")
            col_idx_map[key] = c

    # 필수 컬럼 인덱스 찾기
    col_grade = col_idx_map.get("학년")
    col_class = col_idx_map.get("반")
    col_number = col_idx_map.get("번호")
    col_name = col_idx_map.get("성명") or col_idx_map.get("이름") or col_idx_map.get("성 명")
    col_memo = col_idx_map.get("비고")

    # 2. 2행부터 데이터 행 파싱
    students: List[Dict[str, Any]] = []
    grades_found: List[int] = []
    classes_found: List[int] = []

    for r in range(2, ws.max_row + 1):
        name_val = str(ws.cell(r, col_name).value or "").strip() if col_name else ""
        raw_grade = ws.cell(r, col_grade).value if col_grade else None
        raw_class = ws.cell(r, col_class).value if col_class else None
        raw_number = ws.cell(r, col_number).value if col_number else None

        # 이름도 없고 학년/반/번호도 모두 비어있으면 건너뜀
        if not name_val and raw_grade is None and raw_class is None and raw_number is None:
            # 혹시 이전학적정보에 '1학년7반19번' 등이 있는 특수 행인지 확인
            continue

        try:
            g = int(float(raw_grade)) if raw_grade is not None and str(raw_grade).strip().isdigit() else None
        except Exception:
            g = None
        try:
            c = int(float(raw_class)) if raw_class is not None and str(raw_class).strip().isdigit() else None
        except Exception:
            c = None
        try:
            n = int(float(raw_number)) if raw_number is not None and str(raw_number).strip().isdigit() else None
        except Exception:
            n = None

        if g is not None:
            grades_found.append(g)
        if c is not None:
            classes_found.append(c)

        # 행 전체 컬럼 데이터 추출
        row_extra: Dict[str, Any] = {}
        for c_idx, h_name in enumerate(headers, start=1):
            h_key = h_name.replace(" ", "")
            if h_key not in ("학년", "반", "번호", "성명", "이름"):
                val = ws.cell(r, c_idx).value
                if val is not None:
                    row_extra[h_name] = str(val).strip()

        memo_val = str(ws.cell(r, col_memo).value or "").strip() if col_memo else ""

        students.append({
            "seq_no": len(students) + 1,
            "raw_grade": g,
            "raw_class": c,
            "raw_number": n,
            "name": name_val,
            "extra_data": row_extra,
            "memo": memo_val,
        })

    # 3. 학급명 결정 ("X학년 Y반")
    mode_grade = Counter(grades_found).most_common(1)[0][0] if grades_found else None
    mode_class = Counter(classes_found).most_common(1)[0][0] if classes_found else None

    # 만약 데이터 행에서 학년/반을 못 찾았다면 전체 셀 및 파일명에서 탐색
    if mode_grade is None or mode_class is None:
        pattern = re.compile(r"(\d+)\s*학년\s*(\d+)\s*반")
        # 시트 전체 텍스트 검색
        found_in_sheet = False
        for r in range(1, min(ws.max_row + 1, 60)):
            for col in range(1, min(ws.max_column + 1, 20)):
                cell_str = str(ws.cell(r, col).value or "")
                m = pattern.search(cell_str)
                if m:
                    mode_grade = int(m.group(1))
                    mode_class = int(m.group(2))
                    found_in_sheet = True
                    break
            if found_in_sheet:
                break

    if mode_grade is None or mode_class is None:
        # 파일명에서 검색
        fname = os.path.basename(file_path)
        m = re.search(r"(\d+)\s*학년\s*(\d+)\s*반", fname)
        if m:
            mode_grade = int(m.group(1))
            mode_class = int(m.group(2))
        else:
            mode_grade = mode_grade or 1
            mode_class = mode_class or 1

    group_name = f"{mode_grade}학년 {mode_class}반"

    # 4. 학생별 학번 확정
    final_students: List[Dict[str, Any]] = []
    for idx, s in enumerate(students, start=1):
        g = s["raw_grade"] if s["raw_grade"] is not None else mode_grade
        c = s["raw_class"] if s["raw_class"] is not None else mode_class
        n = s["raw_number"] if s["raw_number"] is not None else idx
        sid = f"{g}{c:02d}{n:02d}"

        final_students.append({
            "seq_no": idx,
            "student_id": sid,
            "grade": g,
            "class_no": c,
            "number": n,
            "name": s["name"],
            "extra_data": s["extra_data"],
            "memo": s["memo"],
        })

    final_students.sort(key=lambda x: (x["grade"], x["class_no"], x["number"]))

    group_info: Dict[str, Any] = {
        "group_type": "homeroom",
        "group_name": group_name,
        "extra_info": "",
        "columns_json": json.dumps(headers, ensure_ascii=False),
    }

    return group_info, final_students


def parse_subject_roster(file_path: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    나이스 교과시간별출석부 엑셀 파일을 파싱합니다.
    - 6행: '교과 : 과학탐구실험2 1학년  1-6', '담당교사 : 유우석'
    - 7행: '[목 4]'
    - 9행: 테이블 헤더 (연번, 학년, 학과, 반, 번호, 성 명)
    - 10행부터 데이터 읽기 (빈 행 및 페이지 '1 / 2' 등 필터링)
    - 학번: int(학년) * 10000 + int(반) * 100 + int(번호)
    - 정렬: 연번(seq_no) 오름차순
    """
    wb = openpyxl.load_workbook(file_path, data_only=True)
    ws = wb.active

    subject_raw = ""
    teacher_name = ""
    timetable = ""

    # 1. 상단 메타데이터 추출 (1~9행 탐색)
    for r in range(1, 10):
        for c in range(1, ws.max_column + 1):
            val = ws.cell(r, c).value
            if not val:
                continue
            val_str = str(val).strip()

            # 교과명 추출
            m_sub = re.search(r"교과\s*:\s*([^\n\r]+)", val_str)
            if m_sub:
                full_sub = m_sub.group(1).strip()
                if "담당교사" in full_sub:
                    parts = re.split(r"담당교사\s*:\s*", full_sub)
                    full_sub = parts[0].strip()
                    if len(parts) > 1 and not teacher_name:
                        teacher_name = parts[1].strip()
                subject_raw = full_sub

            # 담당교사 추출
            m_tea = re.search(r"담당교사\s*:\s*([^\s\n\r]+)", val_str)
            if m_tea and not teacher_name:
                teacher_name = m_tea.group(1).strip()

            # 시간표 추출: [목 4]
            m_time = re.search(r"\[([^\]]+)\]", val_str)
            if m_time and not timetable:
                timetable = f"[{m_time.group(1).strip()}]"

    # 교과명 정제: '과학탐구실험2 1학년  1-6' -> '과학탐구실험2 1-6'
    if subject_raw:
        # '1학년  1-6' 같은 부분에서 학년 텍스트 정리
        group_name = re.sub(r"\s*\d+학년\s*", " ", subject_raw).strip()
        group_name = re.sub(r"\s+", " ", group_name)
    else:
        group_name = "교과 강좌"

    extra_info = timetable.strip()

    # 2. 헤더 행 탐색 (연번, 학년, 반, 번호, 성명)
    col_map: Dict[str, int] = {}
    header_row = 9  # 기본값
    table_headers: List[str] = ["연번", "학번", "학년", "학과", "반", "번호", "성명"]

    for r in range(1, 15):
        temp_map: Dict[str, int] = {}
        for c in range(1, ws.max_column + 1):
            cell_txt = str(ws.cell(r, c).value or "").strip().replace(" ", "")
            if cell_txt in ("연번", "No", "NO", "no"):
                temp_map["연번"] = c
            elif cell_txt == "학년":
                temp_map["학년"] = c
            elif cell_txt == "학과":
                temp_map["학과"] = c
            elif cell_txt == "반":
                temp_map["반"] = c
            elif cell_txt == "번호":
                temp_map["번호"] = c
            elif cell_txt in ("성명", "성 명", "이름"):
                temp_map["성명"] = c

        if len(temp_map) >= 4 and "연번" in temp_map and "성명" in temp_map:
            col_map = temp_map
            header_row = r
            break

    # 3. 데이터 행 추출
    students: List[Dict[str, Any]] = []
    seen_seq = set()

    for r in range(header_row + 1, ws.max_row + 1):
        seq_cell = ws.cell(r, col_map.get("연번", 2)).value if "연번" in col_map else None
        name_cell = ws.cell(r, col_map.get("성명", 9)).value if "성명" in col_map else None

        if seq_cell is None or name_cell is None:
            continue

        seq_str = str(seq_cell).strip()
        name_str = str(name_cell).strip()

        # 헤더 반복행 및 페이지 푸터 ('1 / 2', '천안오성고' 등) 필터링
        if not name_str or seq_str in ("연번", "No", "NO") or "성명" in name_str or "/" in seq_str:
            continue

        try:
            seq_no = int(float(seq_str))
            grade = int(float(ws.cell(r, col_map.get("학년", 4)).value or 1))
            class_no = int(float(ws.cell(r, col_map.get("반", 7)).value or 1))
            num = int(float(ws.cell(r, col_map.get("번호", 8)).value or 1))
        except (ValueError, TypeError):
            continue

        if seq_no in seen_seq:
            continue
        seen_seq.add(seq_no)

        sid = f"{grade}{class_no:02d}{num:02d}"
        dept = str(ws.cell(r, col_map.get("학과", 5)).value or "").strip() if "학과" in col_map else ""

        students.append({
            "seq_no": seq_no,
            "student_id": sid,
            "grade": grade,
            "class_no": class_no,
            "number": num,
            "name": name_str,
            "extra_data": {"학과": dept} if dept else {},
            "memo": "",
        })

    # 연번 정수형 기준 정렬
    students.sort(key=lambda x: x["seq_no"])

    group_info: Dict[str, Any] = {
        "group_type": "subject",
        "group_name": group_name,
        "extra_info": extra_info,
        "columns_json": json.dumps(table_headers, ensure_ascii=False),
    }

    return group_info, students


def parse_club_roster(file_path: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    나이스 동아리활동 출석부 엑셀 파일을 파싱합니다.
    - 5행: '부서명: {동아리명}', '담당교사 : {교사명}'
    - 6행: 테이블 헤더 (No, 학년, 학과, 반, 번호, 성 명, 날짜...)
    - 7행부터 데이터 읽기 (중복 헤더 행 제외)
    - 학번: int(학년) * 10000 + int(반) * 100 + int(번호)
    - 정렬: No(seq_no) 오름차순
    """
    wb = openpyxl.load_workbook(file_path, data_only=True)
    ws = wb.active

    club_name = ""
    teacher_name = ""

    # 1. 상단 메타데이터 추출 (1~6행 탐색)
    for r in range(1, 8):
        for c in range(1, ws.max_column + 1):
            val = ws.cell(r, c).value
            if not val:
                continue
            val_str = str(val).strip()

            # '부서명: 케미 리터러시' 또는 셀 분리 형태('부서명: ' -> 다음 셀 '케미 리터러시')
            m_club = re.search(r"부서명\s*:\s*(.+)", val_str)
            if m_club:
                cand = m_club.group(1).strip()
                if cand and not club_name:
                    club_name = cand
            elif "부서명" in val_str and not club_name:
                for c_next in range(c + 1, ws.max_column + 1):
                    nxt = ws.cell(r, c_next).value
                    if nxt and str(nxt).strip() and "담당교사" not in str(nxt):
                        club_name = str(nxt).strip()
                        break

            # 담당교사
            m_tea = re.search(r"담당교사\s*:\s*([^\s\n\r]+)", val_str)
            if m_tea and not teacher_name:
                teacher_name = m_tea.group(1).strip()

    if not club_name:
        club_name = "동아리 부서"

    extra_info = f"담당: {teacher_name}" if teacher_name else ""

    # 2. 헤더 행 탐색 (No, 학년, 반, 번호, 성명)
    col_map: Dict[str, int] = {}
    header_row = 6
    table_headers: List[str] = ["No", "학번", "학년", "학과", "반", "번호", "성명"]

    for r in range(1, 10):
        temp_map: Dict[str, int] = {}
        for c in range(1, ws.max_column + 1):
            cell_txt = str(ws.cell(r, c).value or "").strip().replace(" ", "")
            if cell_txt.lower() in ("no", "연번", "번호") and "no" not in temp_map:
                temp_map["no"] = c
            elif cell_txt == "학년":
                temp_map["학년"] = c
            elif cell_txt == "학과":
                temp_map["학과"] = c
            elif cell_txt == "반":
                temp_map["반"] = c
            elif cell_txt == "번호":
                temp_map["번호"] = c
            elif cell_txt in ("성명", "이름"):
                temp_map["성명"] = c

        if len(temp_map) >= 4 and "no" in temp_map and "성명" in temp_map:
            col_map = temp_map
            header_row = r
            break

    # 3. 데이터 행 추출 및 중복 No/학생 제거
    students_by_no: Dict[int, Dict[str, Any]] = {}

    for r in range(header_row + 1, ws.max_row + 1):
        no_cell = ws.cell(r, col_map.get("no", 2)).value if "no" in col_map else None
        name_cell = ws.cell(r, col_map.get("성명", 9)).value if "성명" in col_map else None

        if no_cell is None or name_cell is None:
            continue

        no_str = str(no_cell).strip()
        name_str = str(name_cell).strip()

        # 헤더 반복 행 필터링
        if not name_str or no_str.lower() in ("no", "연번", "번호") or "성명" in name_str:
            continue

        try:
            no_int = int(float(no_str))
            grade = int(float(ws.cell(r, col_map.get("학년", 3)).value or 1))
            class_no = int(float(ws.cell(r, col_map.get("반", 7)).value or 1))
            num = int(float(ws.cell(r, col_map.get("번호", 8)).value or 1))
        except (ValueError, TypeError):
            continue

        sid = f"{grade}{class_no:02d}{num:02d}"
        dept = str(ws.cell(r, col_map.get("학과", 5)).value or "").strip() if "학과" in col_map else ""

        if no_int not in students_by_no:
            students_by_no[no_int] = {
                "seq_no": no_int,
                "student_id": sid,
                "grade": grade,
                "class_no": class_no,
                "number": num,
                "name": name_str,
                "extra_data": {"학과": dept} if dept else {},
                "memo": "",
            }

    students = sorted(students_by_no.values(), key=lambda x: x["seq_no"])

    group_info: Dict[str, Any] = {
        "group_type": "club",
        "group_name": club_name,
        "extra_info": extra_info,
        "columns_json": json.dumps(table_headers, ensure_ascii=False),
    }

    return group_info, students
