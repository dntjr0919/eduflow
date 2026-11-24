import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from core.database import DatabaseManager
from ui.components.ai_execution_dock import AiExecutionDock


class _CaptureRunner:
    def __init__(self) -> None:
        self.started = []

    def is_running(self, _key: str) -> bool:
        return False

    def start_worker(
        self,
        *,
        worker,
        task_key: str = "",
        on_result=None,
        on_error=None,
        on_finished=None,
        **_kw,
    ) -> None:
        self.started.append((task_key, worker, on_result, on_error, on_finished))

    def stop_task(self, *_a, **_kw) -> None:
        pass


class TestAiDockPipelineRegression(unittest.TestCase):
    """7개 타깃의 공통 계약(동기화 -> 3모드 실행 -> on_result) 및 핵심 예외 경로 회귀 테스트."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db = DatabaseManager(str(self.tmp_path / "regression.db"))
        self.runner = _CaptureRunner()
        self.dock = AiExecutionDock(task_runner=self.runner, db=self.db)
        self.dispatched = []

        wb_patch = patch("ui.components.ai_execution_dock.webbrowser.open", return_value=True)
        self.mock_web_open = wb_patch.start()
        self.addCleanup(wb_patch.stop)

    def tearDown(self) -> None:
        try:
            self.dock.shutdown_workers()
            self.dock.close()
            self.dock.deleteLater()
            self.app.processEvents()
        finally:
            self.tmp_dir.cleanup()

    def _register_std_target(self, target_id: str, ctx: dict) -> None:
        self.dock.register_target(
            target_id=target_id,
            label=target_id,
            context_getter=lambda c=ctx: dict(c),
            on_result=lambda payload, tid=target_id: self.dispatched.append((tid, payload)),
        )

    def test_1_general_chat_three_modes_and_agent_response(self) -> None:
        self.dock.set_target("general_chat", force=True)
        self.assertEqual(self.dock.active_target_id(), "general_chat")

        # API 모드
        self.dock._on_mode_segment_clicked(0)
        self.dock.txt_chat_input.setPlainText("오늘 할일 알려줘")
        with patch(
            "ui.components.ai_execution_dock.get_active_ai_info",
            return_value={"has_key": True, "provider": "gemini", "model": "gemini-2.5-flash"},
        ):
            self.dock._on_chat_send_clicked()
        self.assertEqual(len(self.runner.started), 1)
        self.assertEqual(self.runner.started[0][0], self.dock.TASK_DOCK_API)

        # 수동 모드
        self.dock._on_mode_segment_clicked(1)
        self.dock.txt_chat_input.setPlainText("공문 초안 작성")
        self.dock._on_chat_send_clicked()
        self.assertTrue(self.dock._manual_waiting_collect)
        self.dock.txt_chat_input.setPlainText("초안입니다.")
        self.dock._on_chat_send_clicked()
        self.assertFalse(self.dock._manual_waiting_collect)

        # Playwright 모드
        self.dock._on_mode_segment_clicked(2)
        self.dock.txt_chat_input.setPlainText("웹 검색 질문")
        self.dock._on_chat_send_clicked()
        self.assertEqual(len(self.runner.started), 2)
        self.assertEqual(self.runner.started[1][0], self.dock.TASK_DOCK_PLAYWRIGHT)

    def test_2_class_notice_digest_three_modes(self) -> None:
        self._register_std_target(
            "class_notice_digest",
            {
                "target_id": "class_notice_digest",
                "kind": "class_notice_digest",
                "task_label": "카톡·학교종이 AI 공지 요약",
                "system_prompt": "SYS_NOTICE",
                "user_prompt": "내일 소풍 준비물 안내",
                "source_label": "카카오톡 대화",
                "source_type": "kakao",
                "digest_action": "kakao_digest",
                "review_card_id": "",
            },
        )
        self.dock.set_target("class_notice_digest", force=True)

        # API
        self.dock._on_mode_segment_clicked(0)
        with patch(
            "ui.components.ai_execution_dock.get_active_ai_info",
            return_value={"has_key": True, "provider": "gemini", "model": "gemini-2.5-flash"},
        ):
            self.dock._on_chat_send_clicked()
        self.assertEqual(len(self.runner.started), 1)
        if callable(self.runner.started[0][4]):
            self.runner.started[0][4]()

        # 수동 복붙 + 응답 회수
        self.dock._on_mode_segment_clicked(1)
        prompt = self.dock._build_current_manual_prompt()
        self.assertIn("SYS_NOTICE", prompt)
        self.assertIn("내일 소풍 준비물 안내", prompt)
        self.dock._on_chat_send_clicked()
        self.assertTrue(self.dock._manual_waiting_collect)
        self.dock.txt_chat_input.setPlainText(
            '[{"title": "소풍 안내", "summary": "도시락 지참", "due_date": "2026-11-25", "category": "행사"}]'
        )
        self.dock._on_chat_send_clicked()
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0][0], "class_notice_digest")
        self.assertEqual(self.dispatched[0][1]["items"][0]["title"], "소풍 안내")

        # Playwright
        self.dock._on_mode_segment_clicked(2)
        self.dock._on_chat_send_clicked()
        self.assertEqual(len(self.runner.started), 2)
        self.assertEqual(self.runner.started[1][0], self.dock.TASK_DOCK_PLAYWRIGHT)

    def test_3_calendar_todo_direct_json_and_query_branch(self) -> None:
        self._register_std_target(
            "calendar_todo",
            {
                "target_id": "calendar_todo",
                "kind": "calendar_todo",
                "task_label": "일정·할일 자동 추출",
                "schedule_context": "[등록된 일정]\n- 2026-11-25 10:00 | [업무] 교무회의",
            },
        )
        self.dock.set_target("calendar_todo", force=True)

        # JSON 직접 입력 시 AI 호출 스킵 -> 즉시 on_result
        self.dock._on_mode_segment_clicked(0)
        self.dock.txt_chat_input.setPlainText(
            '[{"item_type": "schedule", "title": "학부모 총회", "start_date": "2026-11-26"}]'
        )
        self.dock._on_chat_send_clicked()
        self.assertEqual(len(self.runner.started), 0)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0][1]["mode"], "direct_json")

        # 수동 모드에서 조회성 질문 일반 텍스트 응답 -> JSON 파싱 에러 없이 is_query_answer 전달
        self.dock._on_mode_segment_clicked(1)
        self.dock.txt_chat_input.setPlainText("이번 주 일정 요약해줘")
        self.dock._on_chat_send_clicked()
        self.assertIn("교무회의", self.dock._last_manual_prompt)
        self.dock.txt_chat_input.setPlainText("내일 오전 10시에 교무회의가 예정되어 있습니다.")
        self.dock._on_chat_send_clicked()
        self.assertEqual(len(self.dispatched), 2)
        self.assertTrue(self.dispatched[1][1].get("is_query_answer"))

    def test_4_notice_ocr_vs_ocr_prompt_and_attachments(self) -> None:
        sample_pdf = self.tmp_path / "notice.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4\n")

        self._register_std_target(
            "notice_ocr",
            {
                "target_id": "notice_ocr",
                "kind": "ocr",
                "task_label": "가정통신문 PDF OCR",
                "ocr_files": [str(sample_pdf)],
                "attachment_paths": [str(sample_pdf)],
                "ocr_prompt": "CUSTOM_NOTICE_JSON_PROMPT",
                "ocr_current_text": "",
            },
        )
        self.dock.set_target("notice_ocr", force=True)
        notice_prompt = self.dock._build_current_manual_prompt()
        self.assertEqual(notice_prompt, "CUSTOM_NOTICE_JSON_PROMPT")

        # notice_ocr 수동 응답 적용 시 parsed_json 또는 raw_response 전달
        self.dock._on_mode_segment_clicked(1)
        with patch.object(self.dock, "_open_attachment_in_explorer"):
            self.dock._on_chat_send_clicked()
        self.dock.txt_chat_input.setPlainText('{"title": "가정통신문", "due_date": "2026-11-30"}')
        self.dock._on_chat_send_clicked()
        self.assertEqual(self.dispatched[-1][0], "notice_ocr")
        self.assertEqual(self.dispatched[-1][1]["parsed_json"]["title"], "가정통신문")

        # 일반 ocr 타깃은 build_structured_prompt(ocr_correction)를 사용해야 함
        self._register_std_target(
            "ocr",
            {
                "target_id": "ocr",
                "kind": "ocr",
                "task_label": "2. AI OCR 및 텍스트 보정",
                "ocr_files": [],
                "attachment_paths": [],
                "ocr_prompt": "오탈자만 교정",
                "ocr_current_text": "원본 문장입니다.",
            },
        )
        self.dock.set_target("ocr", force=True)
        ocr_prompt = self.dock._build_current_manual_prompt()
        self.assertIn("System 지시문", ocr_prompt)
        self.assertIn("원본 문장입니다.", ocr_prompt)

    def test_5_agg_step2_manual_batch_navigation_and_execution(self) -> None:
        import pandas as pd

        xlsx_path = self.tmp_path / "students.xlsx"
        df = pd.DataFrame(
            [
                {"학번": f"101{i:02d}", "이름": f"학생{i}", "활동내용": "성실히 탐구함 " * 10, "생성된_과세특": ""}
                for i in range(1, 6)
            ]
        )
        df.to_excel(xlsx_path, index=False)

        self._register_std_target(
            "agg_step2",
            {
                "target_id": "agg_step2",
                "kind": "excel_batch",
                "excel_path": str(xlsx_path),
                "docx_dir": "",
                "schema_path": "",
                "prompt_file": "",
                "reference_dir": "",
                "max_bytes": 1500,
                "attachment_paths": [],
            },
        )
        self.dock.set_target("agg_step2", force=True)
        self.dock._on_mode_segment_clicked(1)
        self.dock.spin_manual_students.setValue(2)
        self.dock._rebuild_manual_batches(copy_first=False, quiet=True)
        self.assertGreaterEqual(self.dock.combo_manual_batch.count(), 2)

        # 배치 이동 확인
        self.dock.combo_manual_batch.setCurrentIndex(1)
        self.assertEqual(self.dock.combo_manual_batch.currentIndex(), 1)

        # API / Playwright 시작 확인
        self.dock._on_mode_segment_clicked(0)
        with patch(
            "ui.components.ai_execution_dock.get_active_ai_info",
            return_value={"has_key": True, "provider": "gemini", "model": "gemini-2.5-flash"},
        ):
            self.dock._on_api_start_clicked()
        self.assertEqual(len(self.runner.started), 1)
        self.assertEqual(self.runner.started[0][0], self.dock.TASK_DOCK_API)

        self.dock._on_mode_segment_clicked(2)
        self.dock._on_pw_start_clicked()
        self.assertEqual(len(self.runner.started), 2)
        self.assertEqual(self.runner.started[1][0], self.dock.TASK_DOCK_PLAYWRIGHT)

    def test_6_hwpx_agent_clarification_and_code_mode_manual(self) -> None:
        self._register_std_target(
            "hwpx_agent",
            {
                "target_id": "hwpx_agent",
                "kind": "hwpx_agent",
                "task_label": "HWPX 문서 편집",
                "hwpx_path": "sample.hwpx",
                "dom_map": {"file_name": "sample.hwpx", "block_count": 1, "blocks": [{"id": "p_0", "type": "paragraph", "text": "제목"}]},
                "is_create_mode": False,
                "reference_files": [],
                "attachment_paths": [],
            },
        )
        self.dock.set_target("hwpx_agent", force=True)

        # 1) needs_clarification 후 2턴째 지시사항 결합
        self.dock.txt_hwpx_instruction.setPlainText("시험문항 추가해줘")
        handled = self.dock._handle_hwpx_clarification_or_more_chunks(
            "hwpx_agent",
            {"status": "needs_clarification", "question": "몇 번 문항 뒤에 추가할까요?"},
            mode="api",
        )
        self.assertTrue(handled)
        self.assertIsNotNone(self.dock._hwpx_pending_clarification)

        self.dock._on_mode_segment_clicked(0)
        self.dock.txt_chat_input.setPlainText("3번 문항 뒤에 추가해줘")
        with patch(
            "ui.components.ai_execution_dock.get_active_ai_info",
            return_value={"has_key": True, "provider": "gemini", "model": "gemini-2.5-flash"},
        ):
            self.dock._on_chat_send_clicked()
        self.assertIn("시험문항 추가해줘", self.dock.txt_hwpx_instruction.toPlainText())
        self.assertIn("3번 문항 뒤에 추가해줘", self.dock.txt_hwpx_instruction.toPlainText())

        # 2) 수동 복붙 code_mode (def edit_hwpx) 감지 및 전달
        self.dock._on_mode_segment_clicked(1)
        self.dock.txt_manual_response.setPlainText(
            "```python\ndef edit_hwpx(doc):\n    return doc\n```"
        )
        self.dock._on_manual_apply_and_next()
        self.assertEqual(self.dispatched[-1][0], "hwpx_agent")
        self.assertEqual(self.dispatched[-1][1].get("exec_kind"), "code_mode")


if __name__ == "__main__":
    unittest.main()
