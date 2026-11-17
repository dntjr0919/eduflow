import builtins
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
from PyQt6.QtWidgets import QApplication

from core.base_worker import TaskRunner
from core.config_manager import ConfigManager
from core.database import DatabaseManager
from services.hwpx_agent_service import create_minimal_hwpx_document, extract_hwpx_reference_chunks
from services.hwpx_ref_session_service import (
    MAX_EXTRA_CHUNK_ROUNDS,
    build_reference_index_summary,
    clear_hwpx_ref_session_cache,
    get_hwpx_ref_session_cache,
    inspect_pdf_document,
    parse_stage1_selection_response,
    parse_stage2_response_status,
    resolve_selected_chunks,
)
from ui.components.ai_execution_dock import AiExecutionDock, _DockApiHwpxAgentWorker


class TestHwpxRefSessionPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        clear_hwpx_ref_session_cache()

    def test_1_pdf_detection_without_pypdf(self) -> None:
        """pypdf가 없는 환경에서도 fitz 또는 fallback으로 텍스트 PDF / 스캔 PDF가 오류 없이 판별되는지 검증."""
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")

            real_import = builtins.__import__

            def _block_pypdf(name, globals=None, locals=None, fromlist=(), level=0):
                if name in ("pypdf", "PyPDF2"):
                    raise ImportError("blocked pypdf")
                return real_import(name, globals, locals, fromlist, level)

            # A) fitz/pypdfium2/pypdf 모두 없을 때도 pypdf 오류 없이 scanned_pdf로 안전 분류
            with patch("builtins.__import__", side_effect=_block_pypdf):
                info_none = inspect_pdf_document(pdf_path)
                self.assertTrue(info_none["is_scanned"])
                chunks_none = extract_hwpx_reference_chunks([pdf_path])
                self.assertEqual(len(chunks_none), 1)
                self.assertEqual(chunks_none[0]["status"], "needs_ocr")

            # B) fitz(PyMuPDF)로 텍스트 PDF vs 스캔 PDF 판별
            class _FakeFitzDoc:
                def __init__(self, pages):
                    self._pages = pages

                def __len__(self):
                    return len(self._pages)

                def __iter__(self):
                    return iter(self._pages)

                def close(self):
                    pass

            fake_text_pages = [
                SimpleNamespace(get_text=lambda _mode="text": "1단원 물질의 규칙성과 결합 " + ("가" * 80)),
                SimpleNamespace(get_text=lambda _mode="text": "2단원 자연의 구성 물질 " + ("나" * 80)),
            ]
            fake_fitz_text = SimpleNamespace(open=lambda _p: _FakeFitzDoc(fake_text_pages))
            with patch("builtins.__import__", side_effect=_block_pypdf), patch.dict(
                sys.modules, {"fitz": fake_fitz_text}
            ):
                info_text = inspect_pdf_document(pdf_path)
                self.assertFalse(info_text["is_scanned"])
                self.assertEqual(info_text["engine"], "fitz")
                self.assertEqual(info_text["page_count"], 2)

            fake_scan_pages = [
                SimpleNamespace(get_text=lambda _mode="text": "1"),
                SimpleNamespace(get_text=lambda _mode="text": ""),
            ]
            fake_fitz_scan = SimpleNamespace(open=lambda _p: _FakeFitzDoc(fake_scan_pages))
            with patch("builtins.__import__", side_effect=_block_pypdf), patch.dict(
                sys.modules, {"fitz": fake_fitz_scan}
            ):
                info_scan = inspect_pdf_document(pdf_path)
                self.assertTrue(info_scan["is_scanned"])
                self.assertEqual(info_scan["page_count"], 2)

    def test_2_session_cache_cleanup_on_exit(self) -> None:
        """세션 임시 캐시 폴더 및 인메모리 캐시가 clear_hwpx_ref_session_cache() 호출 시 완전 삭제되는지 검증."""
        cache = get_hwpx_ref_session_cache()
        tmp_dir = cache.get_temp_dir()
        marker = tmp_dir / "temp_ocr_page.png"
        marker.write_bytes(b"png")
        self.assertTrue(tmp_dir.exists())

        with tempfile.TemporaryDirectory() as td:
            txt_p = Path(td) / "ref.txt"
            txt_p.write_text("세션 캐시 테스트 본문", encoding="utf-8")
            summary = build_reference_index_summary([txt_p])
            self.assertEqual(len(summary["docs"]), 1)

        clear_hwpx_ref_session_cache()
        self.assertFalse(tmp_dir.exists())
        self.assertEqual(len(cache._docs), 0)

    def test_3_all_formats_indexing_and_stage1_stage2_clarification_loop(self) -> None:
        """HWP/HWPX/DOCX/XLSX/텍스트PDF/스캔PDF 인덱싱 및 needs_clarification / need_more_chunks 루프 검증."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            hwpx_p = tmp / "ref.hwpx"
            create_minimal_hwpx_document(title="HWPX 제목").save(hwpx_p)
            hwp_p = tmp / "ref.hwp"
            hwp_p.write_bytes(b"dummy hwp")
            docx_p = tmp / "ref.docx"
            with zipfile.ZipFile(docx_p, "w") as zf:
                zf.writestr(
                    "word/document.xml",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>DOCX 참고 문단</w:t></w:r></w:p></w:body></w:document>",
                )
            xlsx_p = tmp / "ref.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "문항표"
            ws.append(["번호", "성취기준"])
            ws.append(["1", "[10통과01-01]"])
            wb.save(str(xlsx_p))
            wb.close()

            txt_pdf = tmp / "textbook.pdf"
            txt_pdf.write_bytes(b"%PDF-1.4")

            with patch(
                "services.multi_doc_extractor_service._extract_hwp_text_olefile",
                return_value="HWP 로컬 추출 본문입니다.",
            ), patch(
                "services.hwpx_ref_session_service.inspect_pdf_document",
                return_value={
                    "engine": "fitz",
                    "page_count": 3,
                    "page_texts": [
                        "1쪽 목차 " + ("가" * 70),
                        "2쪽 1단원 " + ("나" * 70),
                        "3쪽 2단원 " + ("다" * 70),
                    ],
                    "avg_chars": 75.0,
                    "is_scanned": False,
                },
            ):
                summary = build_reference_index_summary(
                    [hwpx_p, hwp_p, docx_p, xlsx_p, txt_pdf], max_chars_per_chunk=50
                )
                self.assertEqual(len(summary["docs"]), 5)
                self.assertIn("[시트: 문항표]", summary["index_text"])
                self.assertIn("textbook.pdf", summary["index_text"])

                # 1) 모호한 요청 -> needs_clarification
                mock_client_clarify = MagicMock()
                mock_client_clarify.generate.return_value = json.dumps(
                    {
                        "status": "needs_clarification",
                        "question": "어느 단원과 몇 문항을 출제할까요?",
                        "options": ["1단원 5문항", "2단원 5문항"],
                    },
                    ensure_ascii=False,
                )
                worker_c = _DockApiHwpxAgentWorker(
                    dom_map=None,
                    instruction="시험 문제 만들어줘",
                    is_create_mode=True,
                    reference_files=[str(txt_pdf)],
                )
                with patch("ui.components.ai_execution_dock.MultiLlmClient", return_value=mock_client_clarify):
                    res_c = worker_c.execute()
                self.assertIn("clarification", res_c)
                self.assertEqual(res_c["clarification"]["status"], "needs_clarification")

                # 2) 명확한 요청 -> Stage1 선별 -> Stage2 need_more_chunks -> 추가 청크 전달 후 완료
                calls = {"n": 0}

                def _seq_generate(**_kwargs):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return json.dumps({"status": "ready", "page_ranges": [[1, 1]], "reason": "1쪽 선별"})
                    if calls["n"] == 2:
                        return json.dumps({"status": "need_more_chunks", "pages": [2], "reason": "2쪽 추가 필요"})
                    return json.dumps(
                        {
                            "summary": "문항 생성 완료",
                            "operations": [{"op": "append_paragraph", "text": "1번 문항"}],
                        }
                    )

                mock_client_seq = MagicMock()
                mock_client_seq.generate.side_effect = _seq_generate
                worker_s = _DockApiHwpxAgentWorker(
                    dom_map=None,
                    instruction="1쪽과 필요한 단원으로 1문항 출제해줘",
                    is_create_mode=True,
                    reference_files=[str(txt_pdf)],
                )
                with patch("ui.components.ai_execution_dock.MultiLlmClient", return_value=mock_client_seq):
                    res_s = worker_s.execute()
                self.assertEqual(res_s["completed_parts"], 2)
                self.assertEqual(calls["n"], 3)

    def test_4_three_modes_prompt_and_batch_dispatch(self) -> None:
        """API / 수동 복붙 / Playwright 자동 3가지 모드에서 대용량·스캔 PDF 1단계/2단계 분기가 깨지지 않는지 검증."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = DatabaseManager(tmp / "test.db")
            dock = AiExecutionDock(db=db, task_runner=TaskRunner())

            scan_pdf = tmp / "scanned_book.pdf"
            scan_pdf.write_bytes(b"%PDF-1.4")

            with patch(
                "services.hwpx_ref_session_service.inspect_pdf_document",
                return_value={
                    "engine": "fitz",
                    "page_count": 12,
                    "page_texts": [""] * 12,
                    "avg_chars": 0.0,
                    "is_scanned": True,
                },
            ):
                ctx = {
                    "target_id": "hwpx_agent",
                    "kind": "hwpx_agent",
                    "is_create_mode": True,
                    "reference_files": [str(scan_pdf)],
                    "dom_map": None,
                }
                dock.register_target(
                    target_id="hwpx_agent",
                    label="HWPX",
                    context_getter=lambda: ctx,
                    on_result=lambda _p: None,
                )
                dock.set_target("hwpx_agent", force=True)
                dock.txt_hwpx_instruction.setPlainText("중간고사 시험지 만들어줘")

                # 1) 수동 복붙: 1단계 인덱스 프롬프트 생성 -> needs_clarification 처리 확인
                manual_p1 = dock._build_current_manual_prompt()
                self.assertIn("참고 문서 인덱스", manual_p1)
                self.assertIn("scanned_book.pdf", manual_p1)

                dock.txt_manual_response.setPlainText(
                    json.dumps(
                        {
                            "status": "needs_clarification",
                            "question": "어느 단원 범위를 출제할까요?",
                            "options": ["1단원", "2단원"],
                        },
                        ensure_ascii=False,
                    )
                )
                dock._on_manual_apply_and_next()
                self.assertIsNotNone(dock._hwpx_pending_clarification)

                # 사용자가 채팅창에 답변 입력 -> 기존 지시사항과 병합
                dock.txt_chat_input.setPlainText("1단원 3~5쪽에서 2문항")
                dock._hwpx_pending_clarification["original_instruction"] = "중간고사 시험지 만들어줘"
                with patch.object(dock, "_on_api_start_clicked"):
                    dock.set_mode_index(0)
                    dock._on_chat_send_clicked()
                self.assertIn("1단원 3~5쪽에서 2문항", dock.txt_hwpx_instruction.toPlainText())
                self.assertIsNone(dock._hwpx_pending_clarification)

                # 2) 수동 복붙 1단계 ready 응답 -> 선택된 청크만 2단계 배치로 전환
                dock.set_mode_index(1)
                dock.txt_manual_response.setPlainText(
                    json.dumps({"status": "ready", "page_ranges": [[3, 4]], "reason": "1단원"})
                )
                dock._on_manual_apply_and_next()
                self.assertEqual(len(dock._hwpx_selected_chunk_ids), 2)
                self.assertEqual(dock.combo_manual_batch.count(), 2)
                manual_p2 = dock._build_current_manual_prompt()
                self.assertIn("Part 1 / 2", manual_p2)

                # 3) Playwright 1단계 ready -> result에서는 플래그만 세우고 _on_pw_finished 이후 2단계 자동 실행 확인
                dock._hwpx_selected_chunk_ids = []
                dock._pw_hwpx_stage1_pending = True
                dock._pw_next_stage_pending = False
                with patch.object(dock, "_on_pw_start_clicked") as mock_pw_start:
                    dock._on_pw_finished_result(
                        "hwpx_agent",
                        {"raw_response": json.dumps({"status": "ready", "page_ranges": [[3, 5]]})},
                    )
                    self.assertFalse(dock._pw_hwpx_stage1_pending)
                    self.assertTrue(dock._pw_next_stage_pending)
                    self.assertEqual(mock_pw_start.call_count, 0)
                    dock._on_pw_finished()
                    self._app.processEvents()
                    self.assertFalse(dock._pw_next_stage_pending)
                    self.assertEqual(len(dock._hwpx_selected_chunk_ids), 3)
                    self.assertEqual(mock_pw_start.call_count, 1)

                # 4) Playwright 1단계 일반 텍스트(비-JSON) 응답 -> 로컬 키워드 폴백 후 _on_pw_finished 이후 2단계 자동 실행 확인
                dock._hwpx_selected_chunk_ids = []
                dock._pw_hwpx_stage1_pending = True
                dock._pw_next_stage_pending = False
                with patch.object(dock, "_on_pw_start_clicked") as mock_pw_start:
                    dock._on_pw_finished_result(
                        "hwpx_agent",
                        {"raw_response": "네, 3쪽부터 5쪽 내용을 참고하여 시험지를 작성해 드리겠습니다."},
                    )
                    self.assertFalse(dock._pw_hwpx_stage1_pending)
                    self.assertTrue(dock._pw_next_stage_pending)
                    dock._on_pw_finished()
                    self._app.processEvents()
                    self.assertFalse(dock._pw_next_stage_pending)
                    self.assertGreaterEqual(len(dock._hwpx_selected_chunk_ids), 1)
                    self.assertEqual(mock_pw_start.call_count, 1)

                # 5) 1단계·2단계 프롬프트 JSON 출력 강제 및 자기완결형 문구 포함 확인
                self.assertIn("인사말·설명 문장 없이 반드시 유효한 ```json ... ``` 코드블록 1개만 출력하세요", manual_p1)
                self.assertIn("인사말·설명 문장 없이 반드시 유효한 ```json ... ``` 코드블록 1개만 출력하세요", manual_p2)
                self.assertIn("이전 대화 맥락에 의존하지 말고", manual_p2)

    def test_5_qthread_consecutive_restart_no_crash(self) -> None:
        """동일 task_key로 QThread 워커를 연속 재시작(1단계 -> 2단계)해도 파괴 크래시가 발생하지 않는지 검증."""
        import time
        from core.base_worker import FunctionWorker

        runner = TaskRunner()
        results = []
        finished_count = {"n": 0}

        def _slow_job(w, val):
            for _ in range(5):
                if w.is_stop_requested():
                    return f"stopped_{val}"
                time.sleep(0.02)
            return f"done_{val}"

        runner.start_worker(
            FunctionWorker("job1", _slow_job, 1),
            task_key="same_key",
            on_result=lambda r: results.append(r),
            on_finished=lambda: finished_count.__setitem__("n", finished_count["n"] + 1),
        )
        # 이전 QThread가 아직 실행 중일 때 동일 key로 즉시 2번째 워커 시작 -> _retired_tasks로 안전 보관
        runner.start_worker(
            FunctionWorker("job2", _slow_job, 2),
            task_key="same_key",
            on_result=lambda r: results.append(r),
            on_finished=lambda: finished_count.__setitem__("n", finished_count["n"] + 1),
        )

        deadline = time.time() + 3.0
        while time.time() < deadline and (runner.is_running("same_key") or runner._retired_tasks):
            self._app.processEvents()
            time.sleep(0.02)
        self._app.processEvents()

        runner.stop_all(wait_ms=1000)
        self.assertEqual(len(runner._retired_tasks), 0)
        self.assertIn("done_2", results)


if __name__ == "__main__":
    unittest.main()
