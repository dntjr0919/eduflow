"""
HWPX AI 문서 에이전트용 대용량 참고 문서 세션 캐시 · 통합 PDF 파서 · 2단계 AI 오케스트레이션 서비스.
- pypdf 하드 의존성 오류를 제거하고 PyMuPDF(fitz) / pypdfium2 / pypdf 선택적 엔진을 통합 지원합니다.
- 모든 1차 파싱 결과와 AI OCR 결과는 현재 앱 세션 동안만 유지되며 앱 종료 시 자동 삭제됩니다.
"""

from __future__ import annotations

import atexit
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.llm_client import extract_json_from_llm_response
from services.multi_doc_extractor_service import PDF_MIN_CHARS_PER_PAGE

MAX_EXTRA_CHUNK_ROUNDS = 3
DEFAULT_REF_CHUNK_CHARS = 8000
SCANNED_TOC_MAX_PAGES = 10
SCANNED_OCR_BATCH_PAGES = 5


def inspect_pdf_document(path: Path) -> Dict[str, Any]:
    """
    PyMuPDF(fitz) -> pypdfium2 -> pypdf 순으로 PDF 텍스트 레이어와 페이지 수를 판별합니다.
    어떤 PDF 라이브러리도 설치되지 않은 경우에도 예외를 던지지 않고 스캔 PDF(AI OCR 대상)로 분류합니다.
    """
    page_texts: List[str] = []
    engine = "none"
    page_count = 0

    # 1순위: PyMuPDF (fitz)
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(path))
        try:
            page_count = len(doc)
            page_texts = [(page.get_text("text") or "").strip() for page in doc]
            engine = "fitz"
        finally:
            doc.close()
    except ImportError:
        pass
    except Exception:
        page_texts = []

    # 2순위: pypdfium2
    if engine == "none":
        try:
            import pypdfium2 as pdfium  # type: ignore

            pdf = pdfium.PdfDocument(str(path))
            try:
                page_count = len(pdf)
                extracted: List[str] = []
                for idx in range(page_count):
                    page = pdf[idx]
                    textpage = page.get_textpage()
                    try:
                        extracted.append((textpage.get_text_range() or "").strip())
                    finally:
                        textpage.close()
                        page.close()
                page_texts = extracted
                engine = "pypdfium2"
            finally:
                pdf.close()
        except ImportError:
            pass
        except Exception:
            page_texts = []

    # 3순위: pypdf (설치된 환경에서만 선택적 사용)
    if engine == "none":
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(path))
            page_count = len(reader.pages)
            page_texts = [(p.extract_text() or "").strip() for p in reader.pages]
            engine = "pypdf"
        except ImportError:
            pass
        except Exception:
            page_texts = []

    total_chars = sum(len(t) for t in page_texts)
    avg_chars = total_chars / max(1, page_count or len(page_texts) or 1)
    is_scanned = (engine == "none") or (avg_chars < PDF_MIN_CHARS_PER_PAGE) or (total_chars == 0)

    return {
        "engine": engine,
        "page_count": max(page_count, len(page_texts)),
        "page_texts": page_texts,
        "avg_chars": avg_chars,
        "is_scanned": is_scanned,
    }


def _summarize_chunk_title(text: str, fallback: str) -> str:
    for ln in (text or "").splitlines():
        s = re.sub(r"\s+", " ", ln).strip()
        if not s or s.startswith("---") or s.startswith("==="):
            continue
        return s[:60]
    return fallback


class HwpxRefSessionCache:
    """현재 앱 세션 동안만 유지되는 참고 문서 인덱스/청크/AI OCR 임시 캐시."""

    def __init__(self) -> None:
        self._docs: Dict[str, Dict[str, Any]] = {}
        self._temp_dir: Optional[Path] = None

    def get_temp_dir(self) -> Path:
        if self._temp_dir is None or not self._temp_dir.exists():
            self._temp_dir = Path(tempfile.mkdtemp(prefix="eduflow_hwpx_ref_"))
        return self._temp_dir

    def clear(self) -> None:
        self._docs.clear()
        if self._temp_dir is not None:
            try:
                if self._temp_dir.exists():
                    shutil.rmtree(self._temp_dir, ignore_errors=True)
            finally:
                self._temp_dir = None

    def _file_cache_key(self, p: Path) -> str:
        st = p.stat()
        return f"{p.resolve()}::{st.st_mtime_ns}::{st.st_size}"

    def get_or_index_document(
        self,
        file_path: Path | str,
        max_chars_per_chunk: int = DEFAULT_REF_CHUNK_CHARS,
    ) -> Dict[str, Any]:
        p = Path(str(file_path or "").strip())
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"참고 문서를 찾을 수 없습니다: {p}")

        key = self._file_cache_key(p)
        if key in self._docs:
            return self._docs[key]

        doc_num = len(self._docs) + 1
        doc_id = f"doc{doc_num}"
        ext = p.suffix.lower()
        chunks: List[Dict[str, Any]] = []
        page_count = 0
        doc_kind = ext.lstrip(".") or "text"

        if ext == ".pdf":
            pdf_info = inspect_pdf_document(p)
            page_count = int(pdf_info["page_count"] or 0)
            if pdf_info["is_scanned"]:
                doc_kind = "scanned_pdf"
                total_pages = max(1, page_count)
                for p_num in range(1, total_pages + 1):
                    chunks.append(
                        {
                            "chunk_id": f"{doc_id}_p{p_num}",
                            "doc_id": doc_id,
                            "file_path": str(p),
                            "file_name": p.name,
                            "page_num": p_num,
                            "page_range": [p_num, p_num],
                            "title": f"{p.name} {p_num}쪽 (스캔 이미지 · OCR 대기)",
                            "char_count": 0,
                            "status": "needs_ocr",
                            "content": "",
                        }
                    )
            else:
                doc_kind = "text_pdf"
                page_texts = pdf_info["page_texts"]
                buf_pages: List[int] = []
                buf_texts: List[str] = []
                buf_len = 0
                c_idx = 0

                def _flush_pdf_buf() -> None:
                    nonlocal c_idx, buf_pages, buf_texts, buf_len
                    if not buf_texts:
                        return
                    c_idx += 1
                    merged = "\n\n".join(buf_texts).strip()
                    p_start, p_end = buf_pages[0], buf_pages[-1]
                    p_label = f"{p_start}쪽" if p_start == p_end else f"{p_start}~{p_end}쪽"
                    chunks.append(
                        {
                            "chunk_id": f"{doc_id}_p{p_start}" if p_start == p_end else f"{doc_id}_p{p_start}_{p_end}",
                            "doc_id": doc_id,
                            "file_path": str(p),
                            "file_name": p.name,
                            "page_num": p_start,
                            "page_range": [p_start, p_end],
                            "title": f"{p_label} — {_summarize_chunk_title(merged, p.name)}",
                            "char_count": len(merged),
                            "status": "cached",
                            "content": merged,
                        }
                    )
                    buf_pages = []
                    buf_texts = []
                    buf_len = 0

                for p_num, p_txt in enumerate(page_texts, start=1):
                    clean_p = (p_txt or "").strip()
                    if not clean_p:
                        continue
                    tagged = f"[페이지 {p_num}]\n{clean_p}"
                    if buf_texts and buf_len + len(tagged) > max_chars_per_chunk:
                        _flush_pdf_buf()
                    buf_pages.append(p_num)
                    buf_texts.append(tagged)
                    buf_len += len(tagged) + 2
                _flush_pdf_buf()
        else:
            from services.hwpx_agent_service import (
                _extract_reference_text_from_file,
                _split_text_into_chunks,
            )

            raw_text = _extract_reference_text_from_file(p)
            raw_chunks = _split_text_into_chunks(raw_text, max_chars_per_chunk=max_chars_per_chunk)
            for idx, chunk_text in enumerate(raw_chunks, start=1):
                chunks.append(
                    {
                        "chunk_id": f"{doc_id}_c{idx}",
                        "doc_id": doc_id,
                        "file_path": str(p),
                        "file_name": p.name,
                        "page_num": None,
                        "page_range": None,
                        "title": _summarize_chunk_title(chunk_text, f"{p.name} 파트 {idx}"),
                        "char_count": len(chunk_text),
                        "status": "cached",
                        "content": chunk_text,
                    }
                )

        record = {
            "doc_id": doc_id,
            "file_path": str(p),
            "file_name": p.name,
            "ext": ext,
            "doc_kind": doc_kind,
            "page_count": page_count,
            "chunks": chunks,
        }
        self._docs[key] = record
        return record

    def store_ocr_page_result(self, file_path: Path | str, page_num: int, text: str) -> bool:
        p = Path(str(file_path or "").strip())
        if not p.exists():
            return False
        key = self._file_cache_key(p)
        doc = self._docs.get(key)
        if not doc:
            return False
        clean_text = (text or "").strip()
        for ch in doc.get("chunks", []):
            if ch.get("page_num") == int(page_num):
                tagged = f"[페이지 {page_num}]\n{clean_text}" if clean_text else f"[페이지 {page_num}] (텍스트 없음)"
                ch["content"] = tagged
                ch["char_count"] = len(tagged)
                ch["status"] = "cached"
                ch["title"] = f"{page_num}쪽 — {_summarize_chunk_title(clean_text, p.name)}"
                return True
        return False

    def render_pdf_pages_for_ocr(
        self,
        file_path: Path | str,
        page_numbers: List[int],
    ) -> List[Tuple[int, Optional[Path]]]:
        """지정된 1-based 페이지들을 세션 임시 폴더에 PNG로 렌더링합니다(fitz/pypdfium2 미설치 시 None 반환)."""
        p = Path(str(file_path or "").strip())
        out: List[Tuple[int, Optional[Path]]] = []
        if not p.exists() or not page_numbers:
            return out

        tmp_dir = self.get_temp_dir()
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", p.stem)

        try:
            import fitz  # type: ignore

            doc = fitz.open(str(p))
            try:
                for p_num in page_numbers:
                    idx = int(p_num) - 1
                    if 0 <= idx < len(doc):
                        img_path = tmp_dir / f"{safe_stem}_p{p_num}.png"
                        if not img_path.exists():
                            pix = doc[idx].get_pixmap(dpi=150)
                            pix.save(str(img_path))
                        out.append((int(p_num), img_path))
                return out
            finally:
                doc.close()
        except ImportError:
            pass
        except Exception:
            pass

        try:
            import pypdfium2 as pdfium  # type: ignore

            pdf = pdfium.PdfDocument(str(p))
            try:
                for p_num in page_numbers:
                    idx = int(p_num) - 1
                    if 0 <= idx < len(pdf):
                        img_path = tmp_dir / f"{safe_stem}_p{p_num}.png"
                        if not img_path.exists():
                            page = pdf[idx]
                            try:
                                bmp = page.render(scale=2.0)
                                pil_img = bmp.to_pil()
                                pil_img.save(str(img_path))
                            finally:
                                page.close()
                        out.append((int(p_num), img_path))
                return out
            finally:
                pdf.close()
        except Exception:
            pass

        return [(int(p_num), None) for p_num in page_numbers]


_SESSION_CACHE = HwpxRefSessionCache()


def get_hwpx_ref_session_cache() -> HwpxRefSessionCache:
    return _SESSION_CACHE


def clear_hwpx_ref_session_cache() -> None:
    _SESSION_CACHE.clear()


atexit.register(clear_hwpx_ref_session_cache)


def build_reference_index_summary(
    file_paths: List[Path | str],
    max_chars_per_chunk: int = DEFAULT_REF_CHUNK_CHARS,
) -> Dict[str, Any]:
    """참고 파일들을 세션 캐시에 1차 인덱싱하고 AI 1단계 판단용 경량 인덱스 요약을 생성합니다."""
    cache = get_hwpx_ref_session_cache()
    docs: List[Dict[str, Any]] = []
    all_chunks: List[Dict[str, Any]] = []
    lines: List[str] = []
    scanned_toc_targets: List[Dict[str, Any]] = []

    for raw in file_paths or []:
        if not raw:
            continue
        doc = cache.get_or_index_document(raw, max_chars_per_chunk=max_chars_per_chunk)
        doc_chunks = doc.get("chunks", [])
        needs_ocr_cnt = sum(1 for c in doc_chunks if c.get("status") == "needs_ocr")
        docs.append(
            {
                "doc_id": doc["doc_id"],
                "file_path": doc["file_path"],
                "file_name": doc["file_name"],
                "doc_kind": doc["doc_kind"],
                "page_count": doc["page_count"],
                "chunk_count": len(doc_chunks),
                "needs_ocr_count": needs_ocr_cnt,
            }
        )
        all_chunks.extend(doc_chunks)

        if doc["doc_kind"] == "scanned_pdf" and needs_ocr_cnt > 0:
            toc_pages = [
                int(c["page_num"])
                for c in doc_chunks
                if c.get("status") == "needs_ocr" and int(c.get("page_num") or 0) <= SCANNED_TOC_MAX_PAGES
            ]
            if toc_pages:
                scanned_toc_targets.append(
                    {
                        "file_path": doc["file_path"],
                        "file_name": doc["file_name"],
                        "pages": toc_pages,
                    }
                )

        page_info = f", 총 {doc['page_count']}쪽" if doc.get("page_count") else ""
        lines.append(f"## [{doc['doc_id']}] {doc['file_name']} ({doc['doc_kind']}{page_info}, 청크 {len(doc_chunks)}개)")
        for ch in doc_chunks[:120]:
            st_label = "OCR대기" if ch.get("status") == "needs_ocr" else f"{ch.get('char_count', 0):,}자"
            lines.append(f"- `{ch['chunk_id']}` | {ch['title']} | {st_label}")
        if len(doc_chunks) > 120:
            lines.append(f"- ... 외 {len(doc_chunks) - 120}개 청크/페이지 (page_ranges로 범위 지정 가능)")

    return {
        "docs": docs,
        "chunks": all_chunks,
        "index_text": "\n".join(lines).strip(),
        "has_scanned_pdf": any(d["doc_kind"] == "scanned_pdf" for d in docs),
        "scanned_toc_targets": scanned_toc_targets,
    }


def _expand_page_ranges(raw_ranges: Any) -> List[int]:
    pages: List[int] = []
    if not isinstance(raw_ranges, list):
        return pages
    for item in raw_ranges:
        if isinstance(item, int):
            if item > 0:
                pages.append(item)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                s, e = int(item[0]), int(item[1])
                if s > e:
                    s, e = e, s
                pages.extend(range(max(1, s), e + 1))
            except Exception:
                continue
        elif isinstance(item, str):
            m = re.match(r"^\s*(\d+)\s*[-~]\s*(\d+)\s*$", item)
            if m:
                s, e = int(m.group(1)), int(m.group(2))
                if s > e:
                    s, e = e, s
                pages.extend(range(max(1, s), e + 1))
            elif item.strip().isdigit():
                pages.append(int(item.strip()))
    return pages


def resolve_selected_chunks(
    file_paths: List[Path | str],
    chunk_ids: Optional[List[str]] = None,
    page_ranges: Optional[List[Any]] = None,
    max_chars_per_chunk: int = DEFAULT_REF_CHUNK_CHARS,
) -> List[Dict[str, Any]]:
    """AI가 선택한 chunk_id 또는 page_ranges에 해당하는 세션 캐시 청크 목록을 반환합니다."""
    summary = build_reference_index_summary(file_paths, max_chars_per_chunk=max_chars_per_chunk)
    all_chunks = summary["chunks"]
    if not all_chunks:
        return []

    wanted_ids = {str(cid).strip() for cid in (chunk_ids or []) if str(cid).strip()}
    wanted_pages = set(_expand_page_ranges(page_ranges or []))

    selected: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ch in all_chunks:
        cid = str(ch.get("chunk_id", ""))
        matched = cid in wanted_ids
        if not matched and wanted_pages:
            pr = ch.get("page_range")
            if isinstance(pr, (list, tuple)) and len(pr) >= 2:
                if any(p in wanted_pages for p in range(int(pr[0]), int(pr[1]) + 1)):
                    matched = True
            elif ch.get("page_num") in wanted_pages:
                matched = True
        if matched and cid not in seen_ids:
            seen_ids.add(cid)
            selected.append(ch)

    if not selected:
        selected = all_chunks[:1]
    return selected


_KO_STOPWORDS = {
    "참고", "문서", "기반", "바탕", "내용", "작성", "생성", "만들어", "만들기",
    "해줘", "해주세요", "부탁", "양식", "문항", "문제", "전체", "부분", "위주",
}


def fallback_select_chunks_by_keywords(
    file_paths: List[Path | str],
    user_instruction: str,
    *,
    max_chunks: int = 3,
    max_chars_per_chunk: int = DEFAULT_REF_CHUNK_CHARS,
) -> List[Dict[str, Any]]:
    """
    1단계 AI 응답이 JSON 형식이 아닐 때 사용자 요청의 키워드와 세션 캐시 인덱스(제목/요약/본문)를
    로컬 매칭해 관련 청크를 자동 선별합니다.
    """
    summary = build_reference_index_summary(file_paths, max_chars_per_chunk=max_chars_per_chunk)
    all_chunks = summary.get("chunks") or []
    if not all_chunks:
        return []

    raw_tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", str(user_instruction or ""))
    keywords = [t.lower() for t in raw_tokens if t.lower() not in _KO_STOPWORDS]

    if not keywords:
        return all_chunks[: max(1, min(2, len(all_chunks)))]

    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    for idx, ch in enumerate(all_chunks):
        title_l = str(ch.get("title") or "").lower()
        body_l = str(ch.get("content") or "").lower()
        score = 0
        for kw in keywords:
            if kw in title_l:
                score += 3
            if kw in body_l:
                score += 1
        scored.append((score, -idx, ch))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    positive = [item[2] for item in scored if item[0] > 0][:max_chunks]
    if positive:
        return positive
    return all_chunks[: max(1, min(2, len(all_chunks)))]


def build_hwpx_stage1_selection_prompt(
    user_instruction: str,
    index_text: str,
    dom_map: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """1단계: 참고 문서 인덱스만 보고 필요 범위를 선별하거나 사용자에게 역질문하는 프롬프트를 생성합니다."""
    system_prompt = (
        "인사말·설명 문장 없이 반드시 유효한 ```json ... ``` 코드블록 1개만 출력하세요.\n"
        "당신은 교직 실무용 대용량 참고 문서 분석 및 HWPX 작업 계획 AI입니다.\n"
        "참고 문서 전체를 한 번에 읽지 않고, 먼저 [참고 문서 인덱스]와 [교사 요청]을 검토하여 다음 두 가지 중 하나를 JSON으로 응답하세요.\n\n"
        "1) 요청이 모호하거나 범위·조건(단원, 쪽수, 문항 수, 유형 등)이 불명확한 경우:\n"
        '```json\n{"status": "needs_clarification", "question": "사용자에게 물어볼 구체적 질문", "options": ["선택지1", "선택지2"]}\n```\n\n'
        "2) 요청이 명확하여 작업에 필요한 참고 문서 구간을 특정할 수 있는 경우:\n"
        '```json\n{"status": "ready", "chunk_ids": ["doc1_p1", "doc1_p2"], "page_ranges": [[1, 3]], "reason": "선별 이유"}\n```\n\n'
        "규칙:\n"
        "- 필요한 최소 범위의 chunk_ids 또는 page_ranges만 선택하세요.\n"
        "- 인사말·설명 문장 없이 반드시 유효한 ```json ... ``` 코드블록 1개만 출력하세요."
    )
    dom_note = ""
    if isinstance(dom_map, dict) and dom_map.get("blocks"):
        dom_note = f"\n\n### [현재 열린 HWPX 문서 블록 수]\n총 {len(dom_map.get('blocks', []))}개 블록"
    user_prompt = (
        f"### [교사 요청]\n{(user_instruction or '').strip() or '참고 문서를 바탕으로 HWPX 양식을 작성해 주세요.'}"
        f"{dom_note}\n\n"
        f"### [참고 문서 인덱스]\n{index_text or '(인덱스 없음)'}\n\n"
        "인사말·설명 문장 없이 반드시 유효한 ```json ... ``` 코드블록 1개만 출력하세요."
    )
    return system_prompt, user_prompt


def build_hwpx_scanned_ocr_prompt(
    file_name: str,
    page_numbers: List[int],
    *,
    is_toc: bool = False,
) -> Tuple[str, str]:
    """스캔 PDF 페이지 배치를 AI OCR로 판독하기 위한 프롬프트를 생성합니다."""
    p_desc = ", ".join(f"{p}쪽" for p in page_numbers)
    purpose = "목차·단원 구조 파악" if is_toc else "본문·표·문항 텍스트 정밀 추출"
    system_prompt = (
        "당신은 한국어 교육 자료(교과서·공문·시험지) 스캔 PDF 정밀 OCR AI입니다.\n"
        "첨부된 페이지 이미지(또는 PDF)에서 텍스트, 표, 수식을 빠짐없이 판독하여 JSON으로 응답하세요.\n"
        '```json\n{"pages": [{"page": 1, "text": "판독된 전체 텍스트"}]}\n```'
    )
    user_prompt = (
        f"파일명: {file_name}\n"
        f"대상 페이지: {p_desc}\n"
        f"목적: {purpose}\n"
        "각 페이지의 내용을 누락 없이 판독해 `pages` 배열로 출력하세요."
    )
    return system_prompt, user_prompt


def parse_stage1_selection_response(raw_or_dict: Any) -> Dict[str, Any]:
    """1단계 AI 응답(needs_clarification / ready / direct_operations)을 정규화합니다."""
    if isinstance(raw_or_dict, dict):
        data = dict(raw_or_dict)
    else:
        data = extract_json_from_llm_response(str(raw_or_dict or ""))

    if not isinstance(data, dict):
        raise ValueError("1단계 AI 응답이 JSON 객체가 아닙니다.")

    status = str(data.get("status") or "").strip().lower()
    if status == "needs_clarification":
        opts = data.get("options")
        clean_opts = [str(o).strip() for o in opts if str(o).strip()] if isinstance(opts, list) else []
        return {
            "status": "needs_clarification",
            "question": str(data.get("question") or "작업 범위와 조건을 좀 더 구체적으로 알려주세요.").strip(),
            "options": clean_opts,
        }

    if isinstance(data.get("operations"), list) and status != "ready":
        return {"status": "direct_operations", "parsed_json": data}

    chunk_ids = [str(x).strip() for x in (data.get("chunk_ids") or []) if str(x).strip()]
    page_ranges = data.get("page_ranges") if isinstance(data.get("page_ranges"), list) else []
    return {
        "status": "ready",
        "chunk_ids": chunk_ids,
        "page_ranges": page_ranges,
        "reason": str(data.get("reason") or "").strip(),
    }


def parse_stage2_response_status(parsed_json: Any) -> Dict[str, Any]:
    """2단계 작업 응답에서 역질문(needs_clarification) 또는 추가 청크 요청(need_more_chunks) 여부를 판별합니다."""
    if not isinstance(parsed_json, dict):
        return {"status": "ok", "parsed_json": parsed_json}

    status = str(parsed_json.get("status") or "").strip().lower()
    if status == "needs_clarification":
        opts = parsed_json.get("options")
        clean_opts = [str(o).strip() for o in opts if str(o).strip()] if isinstance(opts, list) else []
        return {
            "status": "needs_clarification",
            "question": str(parsed_json.get("question") or "추가 확인이 필요합니다.").strip(),
            "options": clean_opts,
        }
    if status == "need_more_chunks":
        chunk_ids = [str(x).strip() for x in (parsed_json.get("chunk_ids") or []) if str(x).strip()]
        pages = parsed_json.get("pages") if isinstance(parsed_json.get("pages"), list) else []
        return {
            "status": "need_more_chunks",
            "chunk_ids": chunk_ids,
            "pages": pages,
            "reason": str(parsed_json.get("reason") or "").strip(),
        }
    return {"status": "ok", "parsed_json": parsed_json}
