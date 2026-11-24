import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QEnterEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication

from core.base_worker import TaskRunner
from core.database import DatabaseManager
from ui.components.ai_execution_dock import (
    AiExecutionDock,
    build_attachment_entry,
    format_attachments_for_prompt,
)


class TestAiChatDatabaseAndDock(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_chat.db"
        self.db = DatabaseManager(str(self.db_path))
        self._tracked_docks = []
        wb_patcher = patch("ui.components.ai_execution_dock.AiExecutionDock._open_external_web_url", return_value=True)
        self.mock_webbrowser_open = wb_patcher.start()
        self.addCleanup(wb_patcher.stop)

    def tearDown(self) -> None:
        for dock in self._tracked_docks:
            try:
                dock.close()
                dock.deleteLater()
            except Exception:
                pass
        self._tracked_docks.clear()
        self.app.processEvents()
        self.tmp_dir.cleanup()

    def test_db_chat_sessions_and_messages_crud(self) -> None:
        sid1 = self.db.create_ai_chat_session("첫 번째 대화", "general_chat")
        self.assertGreater(sid1, 0)
        sid2 = self.db.create_ai_chat_session("두 번째 대화", "ocr")
        self.assertGreater(sid2, sid1)

        sessions = self.db.get_ai_chat_sessions()
        self.assertEqual(len(sessions), 2)

        self.db.update_ai_chat_session(sid1, title="수정된 대화", target_mode="hwpx_agent")
        s1 = self.db.get_ai_chat_session(sid1)
        self.assertIsNotNone(s1)
        self.assertEqual(s1["title"], "수정된 대화")
        self.assertEqual(s1["target_mode"], "hwpx_agent")

        m1 = self.db.add_ai_chat_message(
            session_id=sid1,
            role="user",
            content="질문 1",
            exec_mode="api",
            tool_name="general_chat",
            attachments=[{"type": "file", "name": "a.txt", "path": "/tmp/a.txt"}],
        )
        m2 = self.db.add_ai_chat_message(
            session_id=sid1,
            role="assistant",
            content="답변 1",
            exec_mode="api",
            model_name="Gemini 2.5 Flash",
            tool_name="general_chat",
        )
        m3 = self.db.add_ai_chat_message(
            session_id=sid1,
            role="user",
            content="질문 2",
            exec_mode="manual",
        )
        msgs = self.db.get_ai_chat_messages(sid1)
        self.assertEqual([m["id"] for m in msgs], [m1, m2, m3])
        self.assertEqual(msgs[0]["attachments"][0]["name"], "a.txt")
        self.assertEqual(msgs[1]["model_name"], "Gemini 2.5 Flash")

        self.db.update_ai_chat_message(m1, content="수정된 질문 1")
        msgs_after_upd = self.db.get_ai_chat_messages(sid1)
        self.assertEqual(msgs_after_upd[0]["content"], "수정된 질문 1")

        deleted = self.db.delete_ai_chat_messages_from(sid1, m2, inclusive=True)
        self.assertEqual(deleted, 2)
        self.assertEqual(len(self.db.get_ai_chat_messages(sid1)), 1)

        self.db.delete_ai_chat_session(sid1)
        self.assertIsNone(self.db.get_ai_chat_session(sid1))
        self.assertEqual(self.db.get_ai_chat_messages(sid1), [])

    def test_dock_header_session_popover_and_general_chat_default(self) -> None:
        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        try:
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            dock.register_target(
                target_id="agg_step2",
                label="과제 파이프라인 > 2단계 세특 생성",
                context_getter=lambda: {"target_id": "agg_step2", "kind": "excel_batch"},
                on_result=lambda _r: None,
            )

            # 최상단 기본 옵션이 '범용 챗 (에이전트)'인지 확인
            self.assertEqual(dock.combo_target_selector.itemData(0), "general_chat")
            self.assertEqual(dock.combo_target_selector.itemText(0), "범용 챗 (에이전트)")
            self.assertEqual(dock.active_target_id(), "general_chat")

            # 세션 아이콘 버튼 존재 및 SVG 아이콘 적용 확인
            self.assertFalse(dock.btn_session_menu.icon().isNull())

            # 새 대화 생성 및 전환
            s1 = dock._active_session_id
            self.assertIsNotNone(s1)
            dock.txt_chat_input.setPlainText("첫 질문")
            dock._execute_general_chat_manual("첫 질문")
            dock._append_chat_message("user", "첫 질문", exec_mode="manual")
            dock._append_chat_message(
                "assistant", "첫 답변", exec_mode="manual", model_name="ChatGPT Web"
            )
            self.assertEqual(len(dock._message_cards), 2)

            s2 = dock.create_new_session("두 번째 세션")
            self.assertNotEqual(s1, s2)
            self.assertEqual(len(dock._message_cards), 0)

            dock.load_session(s1)
            self.assertEqual(dock._active_session_id, s1)
            self.assertEqual(len(dock._message_cards), 2)
        finally:
            dock.deleteLater()
            self.app.processEvents()

    def test_message_card_accordion_hover_and_edit_branch(self) -> None:
        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        try:
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            dock.show()
            self.app.processEvents()

            long_text = "이것은 아주 긴 텍스트입니다. " * 20
            u_id = dock._append_chat_message("user", long_text, exec_mode="manual")
            a_id = dock._append_chat_message(
                "assistant", long_text, exec_mode="api", model_name="Gemini 2.5 Flash"
            )
            self.assertEqual(len(dock._message_cards), 2)

            user_card, ai_card = dock._message_cards
            self.assertFalse(user_card.is_expanded())
            self.assertLess(len(user_card.lbl_content.text()), len(long_text))

            # 클릭 시 아코디언 펼침 / 재클릭 시 접힘
            click_evt = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(10, 10),
                QPointF(10, 10),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            user_card.mouseReleaseEvent(click_evt)
            self.assertTrue(user_card.is_expanded())
            self.assertEqual(user_card.lbl_content.text(), long_text)

            user_card.mouseReleaseEvent(click_evt)
            self.assertFalse(user_card.is_expanded())

            # 호버 시 즉시 표시되지 않고 1000ms 체류 후 액션 바 표시 및 이탈 시 숨김 확인
            self.assertTrue(ai_card.action_bar.isHidden())
            ai_card.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
            self.assertTrue(ai_card.action_bar.isHidden())
            ai_card._hover_expand_timer.stop()
            ai_card._on_hover_expand_timeout()
            self.assertFalse(ai_card.action_bar.isHidden())
            self.assertEqual(ai_card.lbl_model_badge.text(), "Gemini 2.5 Flash")
            with patch.object(ai_card, "_cursor_inside_card", return_value=False):
                ai_card.leaveEvent(QEvent(QEvent.Type.Leave))
                ai_card._hover_collapse_timer.stop()
                ai_card._on_hover_collapse_timeout()
            if ai_card._anim_group.state():
                ai_card._anim_group.stop()
                ai_card._on_content_anim_finished()
            self.assertTrue(ai_card.action_bar.isHidden())
            ai_card._hover_watch_timer.stop()

            # 내 질문 수정 버튼 -> 입력창 로드 -> 재전송 시 이후 대화 덮어쓰기(분기)
            user_card.btn_edit.click()
            self.assertEqual(dock._editing_message_id, u_id)
            self.assertEqual(dock.txt_chat_input.toPlainText(), long_text)
            self.assertFalse(dock.edit_branch_banner.isHidden())

            dock.txt_chat_input.setPlainText("수정된 새 질문")
            dock.set_mode_index(1)  # 수동 복붙 모드
            dock._on_chat_send_clicked()

            msgs = self.db.get_ai_chat_messages(dock._active_session_id)
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0]["content"], "수정된 새 질문")
            self.assertNotEqual(msgs[0]["id"], u_id)
            self.assertNotIn(a_id, [m["id"] for m in msgs])

            # 수동 복붙 모드에서 응답 붙여넣기 등록
            dock.txt_manual_response.setPlainText("수동으로 붙여넣은 AI 응답입니다.")
            dock._on_manual_register_chat_response()
            msgs2 = self.db.get_ai_chat_messages(dock._active_session_id)
            self.assertEqual(len(msgs2), 2)
            self.assertEqual(msgs2[1]["role"], "assistant")
            self.assertEqual(msgs2[1]["exec_mode"], "manual")
        finally:
            dock.close()
            dock.deleteLater()
            self.app.processEvents()

    def test_folder_and_file_attachments_metadata(self) -> None:
        folder_path = Path(self.tmp_dir.name) / "sample_folder"
        sub_dir = folder_path / "nested"
        sub_dir.mkdir(parents=True)
        f1 = folder_path / "readme.txt"
        f2 = sub_dir / "data.csv"
        f1.write_text("hello", encoding="utf-8")
        f2.write_text("a,b\n1,2", encoding="utf-8")

        single_file = Path(self.tmp_dir.name) / "single.md"
        single_file.write_text("# title", encoding="utf-8")

        dir_entry = build_attachment_entry(str(folder_path))
        self.assertIsNotNone(dir_entry)
        self.assertEqual(dir_entry["type"], "dir")
        self.assertEqual(dir_entry["file_count"], 2)
        self.assertIn("readme.txt", dir_entry["files"])
        self.assertIn("nested/data.csv", dir_entry["files"])

        file_entry = build_attachment_entry(str(single_file))
        self.assertEqual(file_entry["type"], "file")
        self.assertEqual(file_entry["name"], "single.md")

        prompt_ctx = format_attachments_for_prompt([dir_entry, file_entry])
        self.assertIn("[첨부 폴더] sample_folder", prompt_ctx)
        self.assertIn("readme.txt", prompt_ctx)
        self.assertIn("nested/data.csv", prompt_ctx)
        self.assertIn("[첨부 파일] single.md", prompt_ctx)

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        try:
            dock._on_chat_paths_dropped([str(folder_path), str(single_file)])
            self.assertEqual(len(dock._chat_attachments), 2)
            self.assertFalse(dock.attachment_chips_widget.isHidden())

            dock.set_mode_index(1)
            dock.txt_chat_input.setPlainText("폴더 구조 요약해줘")
            dock._on_chat_send_clicked()

            clip = QApplication.clipboard().text()
            self.assertIn("폴더 구조 요약해줘", clip)
            self.assertIn("[첨부 폴더] sample_folder", clip)
            self.assertIn("nested/data.csv", clip)
            self.assertEqual(len(dock._chat_attachments), 0)
            self.assertTrue(dock.attachment_chips_widget.isHidden())
        finally:
            dock.deleteLater()
            self.app.processEvents()

    def test_compact_layout_and_bottom_provider_controls(self) -> None:
        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        try:
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            dock.register_target(
                target_id="agg_step2",
                label="과제 파이프라인 > 2단계 세특 생성",
                context_getter=lambda: {"target_id": "agg_step2", "kind": "excel_batch"},
                on_result=lambda _r: None,
            )
            dock.set_target("general_chat", force=True)
            dock.show()
            self.app.processEvents()

            # 1) 상단 타겟 중복 박스 제거 확인
            self.assertFalse(hasattr(dock, "lbl_active_target_box"))

            # 2) 유휴 시 중지/회수/인증 버튼 숨김 & ⚙ 팝오버 버튼 확인
            self.assertTrue(dock.btn_api_stop.isHidden())
            self.assertTrue(dock.btn_pw_stop.isHidden())
            self.assertTrue(dock.btn_pw_collect_next.isHidden())
            self.assertTrue(dock.btn_pw_confirm_login.isHidden())
            self.assertFalse(dock.btn_mode_options.icon().isNull())

            # 범용 챗 API/Playwright 유휴 시 중간 body_scroll 숨김 -> 대화창 공간 확보
            dock.set_mode_index(0)
            self.app.processEvents()
            self.assertTrue(dock.body_scroll.isHidden())
            self.assertTrue(dock.combo_web_provider.isHidden())
            self.assertFalse(dock.lbl_chat_api_badge.isHidden())
            self.assertTrue(dock.edit_chat_custom_url.isHidden())

            dock.set_mode_index(2)
            self.app.processEvents()
            self.assertTrue(dock.body_scroll.isHidden())
            self.assertFalse(dock.combo_web_provider.isHidden())
            self.assertTrue(dock.lbl_chat_api_badge.isHidden())

            # 수동 복붙 모드에서 직접 URL 선택 시 1줄 URL 입력란 표시
            dock.set_mode_index(1)
            self.app.processEvents()
            self.assertFalse(dock.combo_web_provider.isHidden())
            idx_custom = dock.combo_web_provider.findData("custom")
            dock.combo_web_provider.setCurrentIndex(idx_custom)
            self.app.processEvents()
            self.assertFalse(dock.edit_chat_custom_url.isHidden())

            idx_gemini = dock.combo_web_provider.findData("gemini")
            dock.combo_web_provider.setCurrentIndex(idx_gemini)
            self.app.processEvents()
            self.assertTrue(dock.edit_chat_custom_url.isHidden())

            # 3) 하단 첨부 아이콘 버튼 텍스트 제거 & 툴팁 확인
            self.assertEqual(dock.btn_attach_file.text(), "")
            self.assertEqual(dock.btn_attach_folder.text(), "")
            self.assertTrue(bool(dock.btn_attach_file.toolTip()))
            self.assertTrue(bool(dock.btn_attach_folder.toolTip()))

            # 4) agg_step2 선택 시에도 중간 body_scroll은 숨겨지고 하단 버튼이 '작업 시작'으로 전환
            dock.set_target("agg_step2", force=True)
            self.app.processEvents()
            self.assertTrue(dock.body_scroll.isHidden())
            self.assertEqual(dock.btn_chat_send.text(), "작업 시작")
        finally:
            dock.close()
            dock.deleteLater()
            self.app.processEvents()

    def test_manual_send_to_collect_toggle_and_clipboard_guard(self) -> None:
        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        try:
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            dock.set_target("general_chat", force=True)
            dock.set_mode_index(1)  # 수동 복붙
            dock.show()
            self.app.processEvents()

            # 수동 복붙 모드 유휴 시 중간 붙여넣기 박스 제거 -> body_scroll 숨김
            self.assertTrue(dock.body_scroll.isHidden())
            self.assertEqual(dock.btn_chat_send.text(), "전송")
            self.assertTrue(dock.btn_chat_cancel_collect.isHidden())

            # 전송 클릭 -> 프롬프트 복사 + 버튼 '응답 회수' 전환 + 취소(×) 표시
            dock.txt_chat_input.setPlainText("수동 복붙 테스트 질문")
            dock.btn_chat_send.click()
            self.app.processEvents()

            self.assertTrue(dock._manual_waiting_collect)
            self.assertEqual(dock.btn_chat_send.text(), "응답 회수")
            self.assertFalse(dock.btn_chat_cancel_collect.isHidden())
            self.assertIn("수동 복붙 테스트 질문", QApplication.clipboard().text())

            # 클립보드가 직전 전송 프롬프트와 동일하면 안내 후 대기 유지
            dock.btn_chat_send.click()
            self.app.processEvents()
            self.assertTrue(dock._manual_waiting_collect)
            self.assertIn("직전 전송 프롬프트", dock.lbl_dock_progress_meta.text())

            # 새 응답을 클립보드에 넣고 응답 회수 클릭 -> AI 말풍선 등록 & 전송 버튼 복귀
            QApplication.clipboard().setText("웹 AI에서 생성한 응답입니다.")
            dock.btn_chat_send.click()
            self.app.processEvents()

            self.assertFalse(dock._manual_waiting_collect)
            self.assertEqual(dock.btn_chat_send.text(), "전송")
            self.assertTrue(dock.btn_chat_cancel_collect.isHidden())
            msgs = self.db.get_ai_chat_messages(dock._active_session_id)
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[1]["role"], "assistant")
            self.assertEqual(msgs[1]["content"], "웹 AI에서 생성한 응답입니다.")

            # 대기 중 취소(×) 버튼 동작 확인
            dock.txt_chat_input.setPlainText("두 번째 질문")
            dock.btn_chat_send.click()
            self.app.processEvents()
            self.assertTrue(dock._manual_waiting_collect)
            dock.btn_chat_cancel_collect.click()
            self.app.processEvents()
            self.assertFalse(dock._manual_waiting_collect)
            self.assertEqual(dock.btn_chat_send.text(), "전송")
            self.assertTrue(dock.btn_chat_cancel_collect.isHidden())
        finally:
            dock.close()
            dock.deleteLater()
            self.app.processEvents()

    def test_gear_popover_playwright_exec_mode_toggle(self) -> None:
        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        try:
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            dock.register_target(
                target_id="agg_step2",
                label="과제 파이프라인 > 2단계 세특 생성",
                context_getter=lambda: {"target_id": "agg_step2", "kind": "excel_batch"},
                on_result=lambda _r: None,
            )
            dock.set_target("general_chat", force=True)
            dock.set_mode_index(2)  # Playwright 자동
            dock.show()
            self.app.processEvents()

            # ⚙ 팝오버 내부에 combo_pw_exec_mode 존재 및 기본값(완전 자동)/반자동 전환 확인
            self.assertIs(dock.combo_pw_exec_mode.parentWidget(), dock._mode_options_popover)
            self.assertEqual(dock.combo_pw_exec_mode.currentData(), "auto")
            idx_semi = dock.combo_pw_exec_mode.findData("semi_auto")
            self.assertGreaterEqual(idx_semi, 0)
            dock.combo_pw_exec_mode.setCurrentIndex(idx_semi)
            self.app.processEvents()
            self.assertEqual(dock.combo_pw_exec_mode.currentData(), "semi_auto")

            # 유휴 Playwright 모드에서는 일반/전용 타겟 모두 중간 실행 박스 숨김
            self.assertTrue(dock.body_scroll.isHidden())
            dock.set_target("agg_step2", force=True)
            self.app.processEvents()
            self.assertTrue(dock.body_scroll.isHidden())
        finally:
            dock.close()
            dock.deleteLater()
            self.app.processEvents()

    def test_specialized_target_file_context_included_on_bottom_send(self) -> None:
        import pandas as pd

        excel_path = Path(self.tmp_dir.name) / "agg_students.xlsx"
        pd.DataFrame(
            [
                {
                    "학번": "10101",
                    "이름": "홍길동",
                    "원본내용": "실험 보고서 작성 우수",
                    "세특": "",
                }
            ]
        ).to_excel(excel_path, index=False)

        with (
            patch("ui.components.ai_execution_dock.QMessageBox") as mock_msgbox,
            patch.object(TaskRunner, "start_worker"),
        ):
            dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
            self._tracked_docks.append(dock)
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            sample_dom = {
                "block_count": 1,
                "blocks": [{"id": "p_0", "type": "paragraph", "text": "기존 HWPX 문서 제목"}],
            }
            dock.register_target(
                target_id="hwpx_agent",
                label="HWPX AI Agent",
                context_getter=lambda: {
                    "target_id": "hwpx_agent",
                    "kind": "hwpx_agent",
                    "dom_map": sample_dom,
                    "create_mode": False,
                    "reference_files": [],
                },
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="agg_step2",
                label="과제 파이프라인 > 2단계 세특 생성",
                context_getter=lambda: {
                    "target_id": "agg_step2",
                    "kind": "excel_batch",
                    "excel_path": str(excel_path),
                },
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="calendar_todo",
                label="캘린더 & 할일 (일정·할일 자동 추출)",
                context_getter=lambda: {"target_id": "calendar_todo", "kind": "calendar_todo"},
                on_result=lambda _r: None,
            )
            dock.show()
            self.app.processEvents()

            # 1) HWPX 타겟: 레거시 입력 박스 숨김 + placeholder 이동 + 열린 DOM 컨텍스트 통합
            dock.set_target("hwpx_agent", force=True)
            dock.set_mode_index(1)
            self.assertTrue(dock.hwpx_instruction_card.isHidden())
            self.assertIn("중화적정", dock.txt_chat_input.placeholderText())
            dock.txt_chat_input.setPlainText("표지 제목을 2026학년도 안내문으로 수정해줘")
            dock.btn_chat_send.click()
            self.app.processEvents()

            hwpx_clip = QApplication.clipboard().text()
            self.assertIn("기존 HWPX 문서 제목", hwpx_clip)
            self.assertIn("표지 제목을 2026학년도 안내문으로 수정해줘", hwpx_clip)
            self.assertEqual(dock.btn_chat_send.text(), "응답 회수")
            dock.btn_chat_cancel_collect.click()

            # 2) 캘린더 타겟: 레거시 입력 박스 숨김 + placeholder 이동 확인
            dock.set_target("calendar_todo", force=True)
            self.app.processEvents()
            self.assertTrue(dock.calendar_instruction_card.isHidden())
            self.assertIn("공문·안내문", dock.txt_chat_input.placeholderText())

            # 3) 과제 파이프라인 타겟: 열린 엑셀 학생 컨텍스트 + 하단 추가 지시/첨부 통합 확인
            extra_file = Path(self.tmp_dir.name) / "rubric.txt"
            extra_file.write_text("평가 기준", encoding="utf-8")
            dock.set_target("agg_step2", force=True)
            dock.set_mode_index(1)
            dock._on_chat_paths_dropped([str(extra_file)])
            dock.txt_chat_input.setPlainText("수행평가 점수도 함께 반영해줘")
            dock.btn_chat_send.click()
            self.app.processEvents()

            agg_clip = QApplication.clipboard().text()
            self.assertIn("홍길동", agg_clip)
            self.assertIn("수행평가 점수도 함께 반영해줘", agg_clip)
            self.assertIn("[첨부 파일] rubric.txt", agg_clip)
            mock_msgbox.information.assert_not_called()
            mock_msgbox.warning.assert_not_called()
            mock_msgbox.critical.assert_not_called()

    def test_manual_batch_bar_and_work_start_button_labels(self) -> None:
        import pandas as pd

        excel_path = Path(self.tmp_dir.name) / "students.xlsx"
        pd.DataFrame(
            [
                {"학번": "10101", "이름": "김하나", "원본내용": "내용1", "세특": ""},
                {"학번": "10102", "이름": "이둘", "원본내용": "내용2", "세특": ""},
                {"학번": "10103", "이름": "박셋", "원본내용": "내용3", "세특": ""},
            ]
        ).to_excel(excel_path, index=False)

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        try:
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            dock.register_target(
                target_id="agg_step2",
                label="과제 파이프라인 > 2단계 세특 생성",
                context_getter=lambda: {
                    "target_id": "agg_step2",
                    "kind": "excel_batch",
                    "excel_path": str(excel_path),
                },
                on_result=lambda _r: None,
            )
            dock.register_target(
                target_id="ocr",
                label="2. AI OCR 및 텍스트 보정",
                context_getter=lambda: {"target_id": "ocr", "kind": "ocr", "ocr_files": []},
                on_result=lambda _r: None,
            )
            dock.show()
            self.app.processEvents()

            # agg_step2 / ocr: '작업 시작' 라벨 & 전용 placeholder & 입력창 활성화
            dock.set_target("agg_step2", force=True)
            dock.spin_manual_students.setValue(1)  # 3명 -> 3개 분할 배치
            dock.set_mode_index(1)
            dock._rebuild_manual_batches(copy_first=False)
            self.app.processEvents()

            self.assertTrue(dock.txt_chat_input.isEnabled())
            self.assertIn("추가 지시 입력", dock.txt_chat_input.placeholderText())
            self.assertEqual(dock.btn_chat_send.text(), "작업 시작")
            self.assertFalse(dock.batch_bar_widget.isHidden())
            self.assertEqual(dock.combo_manual_batch.count(), 3)
            self.assertEqual(dock.lbl_batch_progress.text(), "1/3")

            dock.combo_manual_batch.setCurrentIndex(1)
            self.app.processEvents()
            self.assertEqual(dock.lbl_batch_progress.text(), "2/3")

            # 단건/OCR 타겟 전환 시 배치 바 숨김, '작업 시작' 유지
            dock.set_target("ocr", force=True)
            self.app.processEvents()
            self.assertEqual(dock.btn_chat_send.text(), "작업 시작")
            self.assertTrue(dock.batch_bar_widget.isHidden())

            # 범용 챗 복귀 시 '전송' 복귀
            dock.set_target("general_chat", force=True)
            self.app.processEvents()
            self.assertEqual(dock.btn_chat_send.text(), "전송")
            self.assertTrue(dock.batch_bar_widget.isHidden())
        finally:
            dock.close()
            dock.deleteLater()
            self.app.processEvents()

    def test_playwright_inline_status_cards_and_semi_auto_collect_toggle(self) -> None:
        class _DummyPwWorker:
            def __init__(self) -> None:
                self.login_confirmed = False
                self.collect_called = False
                self.stop_requested = False

            def confirm_login(self) -> None:
                self.login_confirmed = True

            def trigger_collect_and_next(self, _txt: str = "") -> None:
                self.collect_called = True

            def request_stop(self) -> None:
                self.stop_requested = True

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        try:
            dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
            dock.set_target("general_chat", force=True)
            dock.set_mode_index(2)
            dock.show()
            self.app.processEvents()

            dummy_worker = _DummyPwWorker()
            dock._active_pw_worker = dummy_worker
            dock._set_pw_running_state(True)
            self.app.processEvents()

            # 진행 중 인라인 카드 + 작업 중지 버튼 표시 확인
            self.assertIsNotNone(dock._pw_inline_card)
            self.assertFalse(dock._pw_inline_card.isHidden())
            self.assertFalse(dock._pw_inline_card.btn_stop.isHidden())
            self.assertTrue(dock._pw_inline_card.btn_confirm_login.isHidden())

            # 로그인/인증 대기 시 인라인 카드에 '인증/로그인 완료 · 작업 재개' 버튼 표시
            dock._on_pw_waiting_for_login("Gemini")
            self.app.processEvents()
            self.assertFalse(dock._pw_inline_card.btn_confirm_login.isHidden())
            dock._pw_inline_card.btn_confirm_login.click()
            self.app.processEvents()
            self.assertTrue(dummy_worker.login_confirmed)
            self.assertTrue(dock._pw_inline_card.btn_confirm_login.isHidden())

            # 반자동 응답 회수 대기 시 하단 버튼이 '응답 회수'로 전환 & 클릭 시 복귀
            dock._on_pw_waiting_for_collect(1, 1)
            self.app.processEvents()
            self.assertTrue(dock._pw_waiting_collect)
            self.assertEqual(dock.btn_chat_send.text(), "응답 회수")

            dock.btn_chat_send.click()
            self.app.processEvents()
            self.assertTrue(dummy_worker.collect_called)
            self.assertFalse(dock._pw_waiting_collect)
            self.assertEqual(dock.btn_chat_send.text(), "전송")

            # 인라인 작업 중지 버튼 클릭 확인
            dock._pw_inline_card.btn_stop.click()
            self.app.processEvents()
            self.assertTrue(dummy_worker.stop_requested)

            dock._on_pw_finished()
            self.app.processEvents()
            self.assertIsNone(dock._pw_inline_card)
        finally:
            dock.close()
            dock.deleteLater()
            self.app.processEvents()

    def test_session_popover_multiple_sessions_and_delete(self) -> None:
        from PyQt6.QtWidgets import QPushButton

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        self._tracked_docks.append(dock)
        dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
        dock.show()
        self.app.processEvents()

        # 초기 세션 포함 3개 세션 생성
        s1 = dock._active_session_id
        s2 = dock.create_new_session("두 번째 대화")
        s3 = dock.create_new_session("세 번째 대화")
        self.assertEqual(len(self.db.get_ai_chat_sessions()), 3)

        dock._on_session_menu_clicked()
        self.app.processEvents()

        # 팝오버에 3개 세션 항목 모두 표시 & 높이 클리핑 없이 확장 확인
        row_count = dock._session_popover.list_lay.count() - 1  # stretch 제외
        self.assertEqual(row_count, 3)
        self.assertGreater(dock._session_popover.height(), 140)

        # 첫 번째 항목(현재 활성 세션 s3)의 삭제(×) 버튼 visible/크기 및 클릭 삭제 확인
        first_row = dock._session_popover.list_lay.itemAt(0).widget()
        del_btns = [b for b in first_row.findChildren(QPushButton) if b.text() == "×"]
        self.assertEqual(len(del_btns), 1)
        self.assertTrue(del_btns[0].isVisible())
        self.assertEqual(del_btns[0].width(), 24)
        self.assertEqual(del_btns[0].height(), 24)
        del_btns[0].click()
        self.app.processEvents()

        remaining = self.db.get_ai_chat_sessions()
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(s3, [int(r["id"]) for r in remaining])
        self.assertIn(dock._active_session_id, [s1, s2])
        self.assertEqual(dock._session_popover.list_lay.count() - 1, 2)
        dock._session_popover.hide()

    def test_message_card_hover_dwell_pin_and_accordion_animation(self) -> None:
        from PyQt6.QtCore import QEasingCurve, QPropertyAnimation

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        self._tracked_docks.append(dock)
        dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
        dock.show()
        self.app.processEvents()

        long_text = "이것은 호버 체류 시간 및 아코디언 애니메이션 검증용 긴 메시지입니다. " * 25
        dock._append_chat_message("assistant", long_text, exec_mode="api", model_name="Gemini")
        card = dock._message_cards[0]
        self.app.processEvents()

        # 1) 초기 상태: 접힘, 타이머/애니메이션 설정 확인
        self.assertFalse(card.is_expanded())
        self.assertEqual(card._hover_expand_timer.interval(), 1000)
        self.assertEqual(card._hover_collapse_timer.interval(), 150)
        self.assertEqual(card._content_anim.easingCurve().type(), QEasingCurve.Type.OutCubic)

        # 2) 길이 비례 지속시간 클램핑 (220ms ~ 450ms) 확인
        self.assertEqual(card._compute_anim_duration_ms(10), 224)
        self.assertGreaterEqual(card._compute_anim_duration_ms(0), 220)
        self.assertEqual(card._compute_anim_duration_ms(5000), 450)

        # 3) enterEvent 직후 즉시 펼쳐지지 않고 1000ms 타이머 활성화 -> 타임아웃 시 펼침
        card.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        self.assertFalse(card.is_expanded())
        self.assertTrue(card._hover_expand_timer.isActive())
        card._hover_expand_timer.stop()
        card._on_hover_expand_timeout()
        self.assertTrue(card.is_expanded())
        self.assertFalse(card.is_pinned_expanded())

        # 애니메이션 완료 시 최대 높이 제한 해제(QWIDGETSIZE_MAX)
        if card._content_anim.state() == QPropertyAnimation.State.Running:
            card._content_anim.stop()
        card._on_content_anim_finished()
        self.assertEqual(card.lbl_content.maximumHeight(), card.QWIDGETSIZE_MAX)

        # 4) leaveEvent -> 150ms 타이머 후 다시 2줄로 접힘 (커서가 카드 밖일 때)
        with patch.object(card, "_cursor_inside_card", return_value=False):
            card.leaveEvent(QEvent(QEvent.Type.Leave))
            self.assertTrue(card._hover_collapse_timer.isActive())
            card._hover_collapse_timer.stop()
            card._on_hover_collapse_timeout()
            self.assertFalse(card.is_expanded())

        # 5) 직접 클릭해 펼친 카드는 호버 이탈 후에도 접히지 않고 고정 유지
        click_evt = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(10, 10),
            QPointF(10, 10),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        card.mouseReleaseEvent(click_evt)
        self.assertTrue(card.is_expanded())
        self.assertTrue(card.is_pinned_expanded())
        with patch.object(card, "_cursor_inside_card", return_value=False):
            card.leaveEvent(QEvent(QEvent.Type.Leave))
            self.assertFalse(card._hover_collapse_timer.isActive())
            card._on_hover_collapse_timeout()
            self.assertTrue(card.is_expanded())

        # 다시 클릭하면 접힘 및 고정 해제
        card.mouseReleaseEvent(click_evt)
        self.assertFalse(card.is_expanded())
        self.assertFalse(card.is_pinned_expanded())

    def test_playwright_exec_mode_visible_for_all_targets(self) -> None:
        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        self._tracked_docks.append(dock)
        dock.register_target(
            target_id="calendar_todo",
            label="캘린더 & 할일",
            context_getter=lambda: {"target_id": "calendar_todo", "kind": "calendar_todo"},
            on_result=lambda _r: None,
        )
        dock.register_target(
            target_id="hwpx_agent",
            label="HWPX 에이전트",
            context_getter=lambda: {"target_id": "hwpx_agent", "kind": "hwpx_agent"},
            on_result=lambda _r: None,
        )
        dock.set_mode_index(1)  # Playwright 모드
        for tid in ("general_chat", "calendar_todo", "hwpx_agent"):
            dock.set_target(tid, force=True)
            self.assertFalse(dock.combo_pw_exec_mode.isHidden(), f"{tid}에서 실행 방식 콤보가 숨겨짐")

    def test_message_card_watch_timer_collapses_when_cursor_leaves_without_leave_event(self) -> None:
        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        self._tracked_docks.append(dock)
        dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
        dock.show()
        self.app.processEvents()

        long_text = "빠른 마우스 이탈 감시 타이머 검증용 긴 메시지입니다. " * 20
        dock._append_chat_message("assistant", long_text, exec_mode="api", model_name="Gemini")
        card = dock._message_cards[0]
        self.app.processEvents()

        # 1) enterEvent 직후에는 action_bar도 숨김 유지, 카드도 접힘 유지
        card.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        self.assertTrue(card.action_bar.isHidden())
        self.assertFalse(card.is_expanded())
        self.assertTrue(card._hover_expand_timer.isActive())

        # 2) 1000ms 체류 완료 시 action_bar 표시 + 아코디언 펼침 + 감시 타이머(120ms) 시작
        card._hover_expand_timer.stop()
        card._on_hover_expand_timeout()
        self.assertFalse(card.action_bar.isHidden())
        self.assertTrue(card.is_expanded())
        self.assertTrue(card._hover_watch_timer.isActive())
        self.assertEqual(card._hover_watch_timer.interval(), 120)

        # 3) leaveEvent 없이 커서가 카드 밖으로 나간 경우 감시 타이머 틱에서 즉시 복귀 및 타이머 정지
        with patch.object(card, "_cursor_inside_card", return_value=False):
            card._on_hover_watch_tick()
        self.assertFalse(card.is_expanded())
        if card._anim_group.state():
            card._anim_group.stop()
            card._on_content_anim_finished()
        self.assertTrue(card.action_bar.isHidden())
        self.assertFalse(card._hover_watch_timer.isActive())

        # 4) 클릭으로 고정 펼친 카드는 감시 대상에서 제외되어 커서 이탈에도 유지
        click_evt = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(10, 10),
            QPointF(10, 10),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        card.mouseReleaseEvent(click_evt)
        self.assertTrue(card.is_expanded())
        self.assertTrue(card.is_pinned_expanded())
        self.assertFalse(card._hover_watch_timer.isActive())
        with patch.object(card, "_cursor_inside_card", return_value=False):
            card._on_hover_watch_tick()
        self.assertTrue(card.is_expanded())

    def test_user_and_assistant_cards_animate_action_bar_and_short_text_smoothly(self) -> None:
        from PyQt6.QtCore import QParallelAnimationGroup

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        self._tracked_docks.append(dock)
        dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
        dock.show()
        self.app.processEvents()

        short_user_text = "내일 2교시 과학실 실험 일정 추가해 줘."
        three_line_user_text = "1교시 수학\n2교시 국어\n3교시 체육 일정 확인"
        dock._append_chat_message("user", short_user_text, exec_mode="api")
        dock._append_chat_message("user", three_line_user_text, exec_mode="api")
        dock._append_chat_message("assistant", three_line_user_text, exec_mode="api", model_name="Gemini")

        short_card, user_card, ai_card = dock._message_cards
        self.app.processEvents()

        # 1) 1줄 사용자 카드도 펼칠 때 action_bar 높이(0 -> 자연 높이)가 애니메이션으로 전환됨
        self.assertEqual(short_card.action_bar.maximumHeight(), 0)
        short_card.set_expanded(True, animate=True, show_action_bar=True)
        self.assertEqual(short_card._anim_group.state(), QParallelAnimationGroup.State.Running)
        self.assertEqual(short_card._action_bar_anim.startValue(), 0)
        self.assertGreater(short_card._action_bar_anim.endValue(), 0)
        short_card._anim_group.stop()
        short_card._on_content_anim_finished()

        # 2) 3줄 사용자/AI 카드가 동일한 _anim_group 경로를 타며 접힘 애니메이션 중에는 전체 텍스트 유지
        for card in (user_card, ai_card):
            card.set_expanded(True, animate=True, show_action_bar=True)
            self.assertEqual(card._anim_group.state(), QParallelAnimationGroup.State.Running)
            avail_w = card.lbl_content.width() if card.lbl_content.width() > 40 else 246
            self.assertEqual(
                card._content_anim.endValue(),
                max(card._content_anim.startValue(), card.lbl_content.heightForWidth(avail_w)),
            )
            card._anim_group.stop()
            card._on_content_anim_finished()
            self.app.processEvents()

            card.set_expanded(False, animate=True, show_action_bar=False)
            self.assertEqual(card._anim_group.state(), QParallelAnimationGroup.State.Running)
            self.assertEqual(card.lbl_content.text(), three_line_user_text)
            self.assertEqual(card._action_bar_anim.endValue(), 0)
            card._anim_group.stop()
            card._on_content_anim_finished()
            self.assertTrue(card.action_bar.isHidden())
            self.assertEqual(card.action_bar.maximumHeight(), 0)

    def test_manual_send_opens_selected_web_provider_and_handles_custom_url(self) -> None:
        from ui.components.ai_execution_dock import PROVIDER_CONFIGS

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        self._tracked_docks.append(dock)
        dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
        dock.register_target(
            target_id="calendar_todo",
            label="캘린더 & 할일",
            context_getter=lambda: {"target_id": "calendar_todo", "kind": "calendar_todo"},
            on_result=lambda _r: None,
        )
        dock.set_mode_index(1)  # 수동 복붙 모드
        dock.show()
        self.app.processEvents()

        # 1) 범용 챗 + ChatGPT 선택 시 해당 URL 오픈 및 안내 문구/응답 회수 전환
        idx_gpt = dock.combo_web_provider.findData("chatgpt")
        self.assertGreaterEqual(idx_gpt, 0)
        dock.combo_web_provider.setCurrentIndex(idx_gpt)
        dock.txt_chat_input.setPlainText("오늘 학사 일정 정리해줘")
        dock.btn_chat_send.click()
        self.app.processEvents()

        self.mock_webbrowser_open.assert_called_once_with(PROVIDER_CONFIGS["chatgpt"]["url"])
        self.assertEqual(dock.btn_chat_send.text(), "응답 회수")
        self.assertIn("웹 브라우저 열기:", dock.lbl_dock_progress_meta.text())
        dock.btn_chat_cancel_collect.click()

        # 2) 특수 타겟(calendar_todo) + Claude 선택 시에도 Claude URL 자동 오픈
        self.mock_webbrowser_open.reset_mock()
        dock.set_target("calendar_todo", force=True)
        idx_claude = dock.combo_web_provider.findData("claude")
        self.assertGreaterEqual(idx_claude, 0)
        dock.combo_web_provider.setCurrentIndex(idx_claude)
        dock.txt_chat_input.setPlainText("다음 주 금요일 학부모 상담 일정 등록해줘")
        dock.btn_chat_send.click()
        self.app.processEvents()

        self.mock_webbrowser_open.assert_called_once_with(PROVIDER_CONFIGS["claude"]["url"])
        self.assertEqual(dock.btn_chat_send.text(), "응답 회수")
        dock.btn_chat_cancel_collect.click()

        # 3) 직접 URL 선택 + 빈 주소 -> 안내 메시지 표시 후 복사만 진행 (webbrowser.open 미호출)
        self.mock_webbrowser_open.reset_mock()
        idx_custom = dock.combo_web_provider.findData("custom")
        self.assertGreaterEqual(idx_custom, 0)
        dock.combo_web_provider.setCurrentIndex(idx_custom)
        dock.edit_chat_custom_url.setText("")
        dock.set_target("general_chat", force=True)
        dock.txt_chat_input.setPlainText("직접 URL 테스트")
        with patch("ui.components.ai_execution_dock.QMessageBox.information") as mock_info:
            dock.btn_chat_send.click()
            self.app.processEvents()
            mock_info.assert_called_once()
        self.mock_webbrowser_open.assert_not_called()
        self.assertEqual(dock.btn_chat_send.text(), "응답 회수")
        self.assertIn("직접 URL 테스트", QApplication.clipboard().text())
        dock.btn_chat_cancel_collect.click()

        # 4) 직접 URL 입력 후 전송 -> 정규화된 URL로 webbrowser.open 호출
        self.mock_webbrowser_open.reset_mock()
        dock.edit_chat_custom_url.setText("example.com/my-ai")
        dock.txt_chat_input.setPlainText("직접 URL 접속 테스트")
        dock.btn_chat_send.click()
        self.app.processEvents()
        self.mock_webbrowser_open.assert_called_once_with("https://example.com/my-ai")


if __name__ == "__main__":
    unittest.main(verbosity=2)
