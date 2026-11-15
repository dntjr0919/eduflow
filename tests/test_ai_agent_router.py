import tempfile
import unittest
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QFrame

from core.base_worker import TaskRunner
from core.database import DatabaseManager
from services.ai_agent_router import (
    MAX_PW_ATTACHMENTS,
    TOOL_REGISTRY,
    build_agent_system_prompt,
    execute_action_tool,
    execute_read_only_tool,
    parse_agent_response,
    prepare_attachments_for_mode,
)
from ui.components.ai_execution_dock import AiExecutionDock, build_attachment_entry


class TestAiAgentRouterAndDockPhase2(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_agent.db"
        self.db = DatabaseManager(str(self.db_path))
        self._tracked_docks = []

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

    def test_tool_registry_and_system_prompt_schema(self) -> None:
        self.assertEqual(TOOL_REGISTRY["query_calendar_todos"]["kind"], "read")
        self.assertEqual(TOOL_REGISTRY["query_class_roster"]["kind"], "read")
        self.assertEqual(TOOL_REGISTRY["scan_folder_files"]["kind"], "read")
        self.assertEqual(TOOL_REGISTRY["add_calendar_todo"]["kind"], "action")
        self.assertEqual(TOOL_REGISTRY["sort_files_auto"]["kind"], "action")
        self.assertEqual(TOOL_REGISTRY["merge_excel_assignments"]["kind"], "action")

        sys_p = build_agent_system_prompt()
        self.assertIn('"tool"', sys_p)
        self.assertIn("query_calendar_todos", sys_p)
        self.assertIn("add_calendar_todo", sys_p)
        self.assertIn("sort_files_auto", sys_p)

    def test_parse_agent_response_read_action_and_plain_text(self) -> None:
        fenced = (
            "확인해 보겠습니다.\n"
            "```json\n"
            '{"tool": "query_calendar_todos", "args": {"keyword": "회의"}, "reply": "일정을 조회합니다."}\n'
            "```"
        )
        p1 = parse_agent_response(fenced)
        self.assertTrue(p1["is_tool_call"])
        self.assertEqual(p1["tool_name"], "query_calendar_todos")
        self.assertEqual(p1["tool_kind"], "read")
        self.assertEqual(p1["args"]["keyword"], "회의")
        self.assertEqual(p1["reply"], "일정을 조회합니다.")

        raw_json = '{"tool": "add_calendar_todo", "args": {"item_type": "todo", "title": "출결 마감"}, "reply": "할일을 추가할까요?"}'
        p2 = parse_agent_response(raw_json)
        self.assertTrue(p2["is_tool_call"])
        self.assertEqual(p2["tool_name"], "add_calendar_todo")
        self.assertEqual(p2["tool_kind"], "action")

        plain = parse_agent_response("안녕하세요! 일반 답변입니다.")
        self.assertFalse(plain["is_tool_call"])
        self.assertEqual(plain["reply"], "안녕하세요! 일반 답변입니다.")

    def test_read_only_tools_execution(self) -> None:
        self.db.add_schedule(title="교무회의", start_date="2026-11-16")
        self.db.add_todo(title="세특 초안 작성", due_date="2026-11-20")

        res_cal = execute_read_only_tool(
            "query_calendar_todos",
            {"keyword": "교무"},
            db=self.db,
        )
        self.assertTrue(res_cal["ok"])
        self.assertEqual(len(res_cal["data"]["schedules"]), 1)
        self.assertEqual(res_cal["data"]["schedules"][0]["title"], "교무회의")

        sample_dir = Path(self.tmp_dir.name) / "docs"
        sample_dir.mkdir()
        (sample_dir / "a.txt").write_text("hello", encoding="utf-8")
        (sample_dir / "b.csv").write_text("x,y\n1,2", encoding="utf-8")

        res_scan = execute_read_only_tool(
            "scan_folder_files",
            {"folder_path": str(sample_dir)},
            db=self.db,
        )
        self.assertTrue(res_scan["ok"])
        self.assertEqual(res_scan["data"]["files"], ["a.txt", "b.csv"])

    def test_dock_read_only_auto_exec_and_action_approval_ux_and_db(self) -> None:
        self.db.add_schedule(title="학부모 상담 주간", start_date="2026-11-18")

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        self._tracked_docks.append(dock)
        dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
        dock.set_target("general_chat", force=True)
        dock.show()
        self.app.processEvents()

        # 1) 조회형 도구 호출 -> 즉시 로컬 실행 + DB 저장
        read_json = (
            '{"tool": "query_calendar_todos", "args": {"keyword": "학부모"}, "reply": "일정을 조회했습니다."}'
        )
        dock._handle_agent_response(
            read_json,
            exec_mode="manual",
            model_name="ChatGPT Web",
            allow_followup=False,
        )
        self.app.processEvents()

        msgs = self.db.get_ai_chat_messages(dock._active_session_id)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["tool_name"], "query_calendar_todos")
        self.assertIn("학부모 상담 주간", msgs[0]["content"])

        # 2) 실행/변경형 도구 호출 -> 승인 카드 표시, 승인 전에는 DB 미변경
        action_json = (
            '{"tool": "add_calendar_todo", '
            '"args": {"item_type": "todo", "title": "1학년 출결 확인", "date": "2026-11-19"}, '
            '"reply": "아래 할일을 추가할까요?"}'
        )
        dock._handle_agent_response(
            action_json,
            exec_mode="api",
            model_name="Gemini 2.5 Flash",
            allow_followup=False,
        )
        self.app.processEvents()

        self.assertEqual(len(self.db.get_todos()), 0)
        approval_cards = dock.chat_timeline_widget.findChildren(QFrame, "AgentActionApprovalCard")
        self.assertEqual(len(approval_cards), 1)
        card = approval_cards[0]
        self.assertIn("add_calendar_todo", card.lbl_summary.text())

        # [실행 승인] 클릭 -> 실제 DB 등록 & 결과 말풍선 DB 저장
        card.btn_approve.click()
        self.app.processEvents()

        todos = self.db.get_todos()
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["title"], "1학년 출결 확인")
        msgs_after = self.db.get_ai_chat_messages(dock._active_session_id)
        self.assertEqual(msgs_after[-1]["tool_name"], "add_calendar_todo")
        self.assertIn("할일 등록 완료", msgs_after[-1]["content"])

        # 3) [취소] 버튼 클릭 시 DB 미변경 확인
        cancel_json = (
            '{"tool": "add_calendar_todo", '
            '"args": {"item_type": "todo", "title": "취소될 할일"}, '
            '"reply": "추가할까요?"}'
        )
        dock._handle_agent_response(
            cancel_json,
            exec_mode="api",
            model_name="Gemini 2.5 Flash",
            allow_followup=False,
        )
        self.app.processEvents()
        cards2 = [
            c for c in dock.chat_timeline_widget.findChildren(QFrame, "AgentActionApprovalCard")
            if c.isVisible()
        ]
        self.assertEqual(len(cards2), 1)
        cards2[0].btn_cancel.click()
        self.app.processEvents()
        self.assertEqual(len(self.db.get_todos()), 1)

    def test_attachments_mode_handling_api_vs_playwright(self) -> None:
        base = Path(self.tmp_dir.name)
        txt_file = base / "note.txt"
        txt_file.write_text("교무실 전달사항 본문", encoding="utf-8")

        hwp_file = base / "legacy.hwp"
        hwp_file.write_bytes(b"dummy hwp binary")

        folder = base / "work_dir"
        folder.mkdir()
        (folder / "plan.md").write_text("# 계획", encoding="utf-8")

        # 다수 파일 생성 (Playwright 상한 초과 테스트용)
        extra_pdfs = []
        for idx in range(MAX_PW_ATTACHMENTS + 2):
            f = base / f"doc_{idx}.txt"
            f.write_text(f"문서 내용 {idx}", encoding="utf-8")
            extra_pdfs.append(build_attachment_entry(str(f)))

        # API 모드: 텍스트 추출본 포함, pw_attach_paths는 비어 있음
        api_atts = [build_attachment_entry(str(txt_file)), build_attachment_entry(str(folder))]
        api_prep = prepare_attachments_for_mode(api_atts, mode_idx=0)
        self.assertEqual(api_prep["pw_attach_paths"], [])
        self.assertIn("교무실 전달사항 본문", api_prep["prompt_context"])
        self.assertIn("folder_path:", api_prep["prompt_context"])
        self.assertIn("plan.md", api_prep["prompt_context"])

        # Playwright 모드: 지원 파일은 최대 MAX_PW_ATTACHMENTS개까지 첨부, .hwp 및 초과 파일은 텍스트 병합
        pw_atts = extra_pdfs + [build_attachment_entry(str(hwp_file)), build_attachment_entry(str(folder))]
        pw_prep = prepare_attachments_for_mode(pw_atts, mode_idx=2)
        self.assertEqual(len(pw_prep["pw_attach_paths"]), MAX_PW_ATTACHMENTS)
        self.assertIn("legacy.hwp", pw_prep["prompt_context"])
        self.assertIn(f"문서 내용 {MAX_PW_ATTACHMENTS}", pw_prep["prompt_context"])
        self.assertIn("plan.md", pw_prep["prompt_context"])

    def test_get_session_history_auto_followup_and_loop_guard(self) -> None:
        from unittest.mock import MagicMock, patch

        self.assertIn("get_session_history", TOOL_REGISTRY)
        self.assertIn("get_session_history", build_agent_system_prompt())

        dock = AiExecutionDock(task_runner=TaskRunner(), db=self.db)
        self._tracked_docks.append(dock)
        dock.setFixedWidth(AiExecutionDock.EXPANDED_WIDTH)
        dock.set_target("general_chat", force=True)
        dock.show()
        self.app.processEvents()

        dock._append_chat_message("user", "어제 교무회의 안건이 뭐였지?", exec_mode="api")
        dock._append_chat_message("assistant", "생활지도 및 평가 계획이었습니다.", exec_mode="api")

        hist_res = execute_read_only_tool(
            "get_session_history",
            {"limit": 20},
            db=self.db,
            session_id=dock._active_session_id,
        )
        self.assertTrue(hist_res["ok"])
        self.assertEqual(len(hist_res["data"]["messages"]), 2)

        # API 모드: get_session_history 반환 시 자동 재요청(allow_followup=False) 1회 호출
        tool_call_json = '{"tool": "get_session_history", "args": {"limit": 20}}'
        with patch.object(dock, "_execute_general_chat_api") as mock_api:
            dock._handle_agent_response(
                tool_call_json,
                exec_mode="api",
                model_name="Gemini 2.5 Flash",
                allow_followup=True,
            )
            mock_api.assert_called_once()
            _, kwargs = mock_api.call_args
            self.assertFalse(kwargs["allow_followup"])
            self.assertEqual(kwargs["tool_name_override"], "get_session_history")
            self.assertIn("생활지도 및 평가 계획", mock_api.call_args[0][0])

            # 1턴당 1회 제한: allow_followup=False면 재호출하지 않음
            mock_api.reset_mock()
            dock._handle_agent_response(
                tool_call_json,
                exec_mode="api",
                model_name="Gemini 2.5 Flash",
                allow_followup=False,
            )
            mock_api.assert_not_called()

        # Playwright 모드: get_session_history 반환 시 자동 재요청(allow_followup=False) 1회 호출
        with patch.object(dock, "_execute_general_chat_playwright") as mock_pw:
            dock._handle_agent_response(
                tool_call_json,
                exec_mode="playwright",
                model_name="Gemini Web",
                allow_followup=True,
            )
            mock_pw.assert_called_once()
            self.assertFalse(mock_pw.call_args.kwargs["allow_followup"])
            self.assertIn("생활지도 및 평가 계획", mock_pw.call_args[0][0])

    def test_google_sheets_sync_browser_headers_and_403_masking(self) -> None:
        from unittest.mock import MagicMock, patch
        from services.google_sheets_sync_service import _http_get_json, _mask_sensitive_url

        masked = _mask_sensitive_url("https://script.google.com/macros/s/abc/exec?action=ping&api_secret=my_pw_123")
        self.assertIn("api_secret=***", masked)
        self.assertNotIn("my_pw_123", masked)

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"ok": True}
        with patch("requests.get", return_value=fake_resp) as mock_get:
            res = _http_get_json("  https://script.google.com/macros/s/abc/exec  ", {"action": "ping"})
            self.assertTrue(res["ok"])
            called_url = mock_get.call_args[0][0]
            self.assertTrue(called_url.startswith("https://script.google.com/"))
            self.assertIn("User-Agent", mock_get.call_args.kwargs["headers"])

    def test_external_integration_action_tools(self) -> None:
        from unittest.mock import MagicMock, patch

        self.assertEqual(TOOL_REGISTRY["send_external_notification"]["kind"], "action")
        self.assertEqual(TOOL_REGISTRY["push_comcigan_timetable"]["kind"], "action")

        fake_notifier = MagicMock()
        fake_notifier.notify.return_value = ["데스크톱 토스트 발송 완료", "웹훅(Webhook) 알림 전송 완료"]
        with patch("services.ai_agent_router.NotificationService", return_value=fake_notifier):
            res_noti = execute_action_tool(
                "send_external_notification",
                {"title": "긴급 공지", "message": "3교시 수업 장소 변경"},
                db=self.db,
                enabled_tools={"send_external_notification": True},
            )
            self.assertTrue(res_noti["ok"])
            fake_notifier.notify.assert_called_once_with(
                "긴급 공지", "3교시 수업 장소 변경", force_external=True
            )

        with patch(
            "services.ai_agent_router.push_comcigan_changes",
            return_value={"ok": True, "pushed": 1, "rows": [{"period": "2"}]},
        ) as mock_push:
            res_push = execute_action_tool(
                "push_comcigan_timetable",
                {
                    "raw_data": "1-3 2교시 수학Ⅰ -> 영어 (한가인)",
                    "target_date": "2026-11-16",
                    "grade": "1",
                    "class_no": "3",
                },
                db=self.db,
                enabled_tools={"push_comcigan_timetable": True},
            )
            self.assertTrue(res_push["ok"])
            self.assertIn("1건", res_push["summary"])
            mock_push.assert_called_once_with(
                raw_data="1-3 2교시 수학Ⅰ -> 영어 (한가인)",
                target_date="2026-11-16",
                grade="1",
                class_no="3",
            )

    def test_enabled_agent_tools_filtering_and_custom_prompt_suffix(self) -> None:
        from unittest.mock import MagicMock, patch

        # 1) 명시적 인자로 도구 필터링 + 추가 지침 반영
        filtered = build_agent_system_prompt(
            enabled_tools={
                "query_calendar_todos": True,
                "send_external_notification": False,
                "push_comcigan_timetable": False,
            },
            extra_prompt="교무실 보고체로 간결하게 작성하세요.",
        )
        self.assertIn("query_calendar_todos", filtered)
        self.assertNotIn("send_external_notification", filtered)
        self.assertNotIn("push_comcigan_timetable", filtered)
        self.assertIn("[사용자 추가 지침]", filtered)
        self.assertIn("교무실 보고체로 간결하게 작성하세요.", filtered)

        # 2) 인자 생략 시 ConfigManager의 api_and_integrations 설정 자동 반영
        fake_cfg = MagicMock()
        fake_cfg.get_section.return_value = {
            "enabled_agent_tools": {
                "query_calendar_todos": False,
                "send_external_notification": True,
            },
            "custom_agent_prompt_suffix": "일정 변경 시 사유를 꼭 확인하세요.",
        }
        with patch("services.ai_agent_router.ConfigManager", return_value=fake_cfg):
            from_cfg = build_agent_system_prompt()
            self.assertNotIn("query_calendar_todos", from_cfg)
            self.assertIn("send_external_notification", from_cfg)
            self.assertIn("[사용자 추가 지침]", from_cfg)
            self.assertIn("일정 변경 시 사유를 꼭 확인하세요.", from_cfg)

            # 비활성화된 도구는 parse_agent_response 및 실행 단계에서 모두 차단됨
            blocked_parse = parse_agent_response(
                '{"tool": "query_calendar_todos", "args": {}, "reply": "조회합니다."}'
            )
            self.assertFalse(blocked_parse["is_tool_call"])
            self.assertIn("비활성화된 도구", blocked_parse["reply"])

            blocked_read = execute_read_only_tool("query_calendar_todos", {}, db=self.db)
            self.assertFalse(blocked_read["ok"])
            self.assertIn("비활성화된 도구", blocked_read["summary"])

        blocked_action = execute_action_tool(
            "send_external_notification",
            {"title": "알림", "message": "테스트"},
            db=self.db,
            enabled_tools={"send_external_notification": False},
        )
        self.assertFalse(blocked_action["ok"])
        self.assertIn("비활성화된 도구", blocked_action["summary"])

    def test_comcigan_html_parsing_and_auto_sync_tool(self) -> None:
        from unittest.mock import MagicMock, patch
        from services.comcigan_playwright_fetcher import ComciganPlaywrightFetcher
        from services.mobile_integration_worker import sync_comcigan_from_web

        sample_html = """
        <html><body>
          <table>
            <caption>1학년 3반 시간표</caption>
            <tr>
              <th>교시</th><th>월(11/16)</th><th>화(11/17)</th><th>수(11/18)</th>
            </tr>
            <tr>
              <td>1교시</td>
              <td>국어<br>김철수</td>
              <td class="changed" style="background-color:#fee2e2;"><del>수학Ⅰ</del> 영어 (한가인)</td>
              <td>과학 (이영희)</td>
            </tr>
            <tr>
              <td>2교시</td>
              <td>사회<br>박민수</td>
              <td>음악 -> 미술 (최예술)</td>
              <td>체육<br>정건강</td>
            </tr>
          </table>
        </body></html>
        """
        base_rows, change_rows = ComciganPlaywrightFetcher.parse_html(
            sample_html,
            target_date="2026-11-16",
        )
        self.assertEqual(len(base_rows), 6)
        self.assertEqual(base_rows[0]["grade"], "1")
        self.assertEqual(base_rows[0]["class_no"], "3")
        self.assertEqual(base_rows[0]["day_of_week"], "월")
        self.assertEqual(base_rows[0]["period"], "1")
        self.assertEqual(base_rows[0]["subject"], "국어")
        self.assertEqual(base_rows[0]["teacher_name"], "김철수")

        self.assertEqual(len(change_rows), 2)
        self.assertEqual(change_rows[0]["original_subject"], "수학Ⅰ")
        self.assertEqual(change_rows[0]["changed_subject"], "영어")
        self.assertEqual(change_rows[0]["changed_teacher"], "한가인")
        self.assertEqual(change_rows[1]["original_subject"], "음악")
        self.assertEqual(change_rows[1]["changed_subject"], "미술")
        self.assertEqual(change_rows[1]["changed_teacher"], "최예술")

        fake_fetcher = MagicMock()
        fake_fetcher.fetch.return_value = {
            "ok": True,
            "base_rows": base_rows,
            "change_rows": change_rows,
        }
        fake_sync = MagicMock()
        fake_sync.upsert_timetable_base.return_value = True
        fake_sync.push_timetable_changes.return_value = True

        sync_res = sync_comcigan_from_web(
            url="http://comci.net:4082/st",
            school_code="OO고",
            target_date="2026-11-16",
            sync_service=fake_sync,
            fetcher=fake_fetcher,
        )
        self.assertTrue(sync_res["ok"])
        self.assertEqual(sync_res["base_count"], 6)
        self.assertEqual(sync_res["change_count"], 2)
        fake_sync.upsert_timetable_base.assert_called_once_with(base_rows)
        fake_sync.push_timetable_changes.assert_called_once_with(change_rows)

        self.assertEqual(TOOL_REGISTRY["sync_comcigan_auto"]["kind"], "action")
        with patch(
            "services.ai_agent_router.sync_comcigan_from_web",
            return_value={"ok": True, "base_count": 6, "change_count": 2},
        ) as mock_auto:
            tool_res = execute_action_tool(
                "sync_comcigan_auto",
                {"url": "http://comci.net:4082/st", "school_code": "OO고", "target_date": "2026-11-16"},
                db=self.db,
                enabled_tools={"sync_comcigan_auto": True},
            )
            self.assertTrue(tool_res["ok"])
            self.assertIn("기본 6건, 변동 2건", tool_res["summary"])
            mock_auto.assert_called_once()

    def test_comcigan_teacher_personal_timetable_parsing_and_merge(self) -> None:
        from services.comcigan_playwright_fetcher import ComciganPlaywrightFetcher

        teacher_html = """
        <html><body>
          <table>
            <caption>홍길동 선생님 시간표</caption>
            <tr>
              <th>교시</th><th>월</th><th>화</th><th>수</th>
            </tr>
            <tr>
              <td>1</td>
              <td>2-1<br>수학Ⅰ</td>
              <td></td>
              <td>1학년 3반<br>수학Ⅰ</td>
            </tr>
            <tr>
              <td>2</td>
              <td></td>
              <td class="changed"><del>2-4 수학Ⅰ</del> 2-4 자습</td>
              <td>3-2 수학Ⅱ</td>
            </tr>
          </table>
        </body></html>
        """
        t_base, t_chg = ComciganPlaywrightFetcher.parse_html(
            teacher_html,
            target_date="2026-11-16",
            teacher_name="홍길동",
            mode="teacher",
        )
        self.assertEqual(len(t_base), 4)
        self.assertEqual(t_base[0]["grade"], "2")
        self.assertEqual(t_base[0]["class_no"], "1")
        self.assertEqual(t_base[0]["subject"], "수학Ⅰ")
        self.assertEqual(t_base[0]["teacher_name"], "홍길동")

        self.assertEqual(t_base[1]["grade"], "1")
        self.assertEqual(t_base[1]["class_no"], "3")
        self.assertEqual(t_base[3]["grade"], "3")
        self.assertEqual(t_base[3]["class_no"], "2")
        self.assertEqual(t_base[3]["subject"], "수학Ⅱ")

        self.assertEqual(len(t_chg), 1)
        self.assertEqual(t_chg[0]["grade"], "2")
        self.assertEqual(t_chg[0]["class_no"], "4")
        self.assertEqual(t_chg[0]["original_subject"], "수학Ⅰ")
        self.assertEqual(t_chg[0]["changed_subject"], "자습")
        self.assertEqual(t_chg[0]["changed_teacher"], "홍길동")

    def test_comcigan_weekday_date_mapping_and_unchanged_skip(self) -> None:
        from unittest.mock import MagicMock
        from services.comcigan_playwright_fetcher import ComciganPlaywrightFetcher
        from services.mobile_integration_worker import sync_comcigan_from_web

        week_html = """
        <html><body>
          <table>
            <caption>1학년 2반 시간표</caption>
            <tr>
              <th>교시</th>
              <th>월(11/16)</th>
              <th>화 (11/17)</th>
              <th>수요일</th>
              <th>목<br>(11/19)</th>
              <th>금 11/20</th>
            </tr>
            <tr>
              <td>1교시</td>
              <td class="changed"><del>국어</del> 영어</td>
              <td class="changed"><del>수학</del> 과학</td>
              <td class="changed"><del>사회</del> 역사</td>
              <td class="changed"><del>음악</del> 미술</td>
              <td class="changed"><del>체육</del> 진로</td>
            </tr>
          </table>
        </body></html>
        """
        base_rows, change_rows = ComciganPlaywrightFetcher.parse_html(
            week_html,
            target_date="2026-11-18",
        )
        self.assertEqual([r["day_of_week"] for r in base_rows], ["월", "화", "수", "목", "금"])
        self.assertEqual(
            [r["date"] for r in change_rows],
            ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"],
        )

        fake_fetcher = MagicMock()
        fake_fetcher.fetch.return_value = {
            "ok": True,
            "base_rows": base_rows,
            "change_rows": change_rows,
        }
        fake_sync = MagicMock()
        fake_sync.upsert_timetable_base.return_value = True
        fake_sync.push_timetable_changes.return_value = True

        first = sync_comcigan_from_web(
            url="http://comci.net:4082/th",
            target_date="2026-11-18",
            sync_service=fake_sync,
            fetcher=fake_fetcher,
            prev_snapshot_hash="",
            skip_if_unchanged=True,
        )
        self.assertTrue(first["ok"])
        self.assertFalse(first["skipped"])
        self.assertEqual(fake_sync.upsert_timetable_base.call_count, 1)
        self.assertEqual(fake_sync.push_timetable_changes.call_count, 1)

        second = sync_comcigan_from_web(
            url="http://comci.net:4082/th",
            target_date="2026-11-18",
            sync_service=fake_sync,
            fetcher=fake_fetcher,
            prev_snapshot_hash=first["snapshot_hash"],
            skip_if_unchanged=True,
        )
        self.assertTrue(second["ok"])
        self.assertTrue(second["skipped"])
        self.assertEqual(fake_sync.upsert_timetable_base.call_count, 1)
        self.assertEqual(fake_sync.push_timetable_changes.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
