import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.hwpx_agent_service import (
    apply_hwpx_operations,
    build_hwpx_agent_prompt,
    create_minimal_hwpx_document,
)
from services.llm_client import extract_json_from_llm_response
from services.web_gemini_auto_worker import WebGeminiAutoWorker


class TestWebLlmJsonAndCleanup(unittest.TestCase):
    def test_extract_json_with_chemical_formulas_and_ui_noise(self) -> None:
        noisy_reply = (
            "화학식 H_{2}O 및 아세트산 rm {CH}_{3}COOH(it {aq}) 수식을 반영한 결과입니다.\n"
            "json\n"
            "복사\n"
            "Copy code\n"
            '{"summary": "화학식 수식 치환", "operations": [{"op": "replace_text", '
            '"target_id": "p_0", "old_text": "H2O", "new_text": "$$\\\\frac{H_{2}O}{CO_{2}}$$"}]}\n'
            "추가 설명: {aq}는 수용액 상태를 뜻합니다."
        )
        parsed = extract_json_from_llm_response(noisy_reply)
        self.assertEqual(parsed["summary"], "화학식 수식 치환")
        self.assertEqual(len(parsed["operations"]), 1)
        self.assertEqual(parsed["operations"][0]["op"], "replace_text")

    def test_is_incomplete_json_response(self) -> None:
        worker = WebGeminiAutoWorker.__new__(WebGeminiAutoWorker)

        # 미완성 JSON (마커 없음, 마커 있음, 문자열 내부 중괄호 포함)
        self.assertTrue(
            worker._is_incomplete_json_response('{"summary": "진행중", "operations": [{"op": "replace_text"')
        )
        self.assertTrue(
            worker._is_incomplete_json_response('```json\n{"summary": "진행중", "operations": [')
        )
        self.assertTrue(
            worker._is_incomplete_json_response('{"summary": "열린 문자열...')
        )

        # 완성된 JSON (문자열 내부에 불균형 중괄호/대괄호가 있어도 외부 괄호가 닫히면 완성)
        self.assertFalse(
            worker._is_incomplete_json_response('{"summary": "문자열 내 { [ 포함", "operations": []}')
        )
        self.assertFalse(
            worker._is_incomplete_json_response('```json\n{"summary": "완료", "operations": []}\n```')
        )
        self.assertFalse(
            worker._is_incomplete_json_response("일반 안내 텍스트입니다.")
        )

    def test_cleanup_playwright_resources_does_not_block(self) -> None:
        call_order = []
        thread_ids = []
        caller_tid = threading.get_ident()

        class _FakePage:
            def close(self) -> None:
                call_order.append("page")
                thread_ids.append(threading.get_ident())

        class _FakeContext:
            def close(self) -> None:
                call_order.append("context")
                thread_ids.append(threading.get_ident())

        class _FakeBrowser:
            def close(self) -> None:
                call_order.append("browser")
                thread_ids.append(threading.get_ident())

        class _FakePlaywright:
            def stop(self) -> None:
                call_order.append("playwright")
                thread_ids.append(threading.get_ident())

        with patch("services.web_gemini_auto_worker.time.sleep") as mock_sleep:
            WebGeminiAutoWorker._cleanup_playwright_resources(
                page=_FakePage(),
                context=_FakeContext(),
                browser=_FakeBrowser(),
                playwright_obj=_FakePlaywright(),
            )

        self.assertEqual(call_order, ["page", "context", "browser", "playwright"])
        self.assertEqual(thread_ids, [caller_tid, caller_tid, caller_tid, caller_tid])
        mock_sleep.assert_called_once_with(0.5)

    def test_hwpx_large_dom_and_whitespace_flexible_replace(self) -> None:
        blocks = [{"id": f"p_{i}", "type": "paragraph", "text": f"{i+1}. 문항 본문 테스트 내용입니다."} for i in range(120)]
        dom_map = {"file_name": "exam.hwpx", "block_count": len(blocks), "blocks": blocks}
        _, user_prompt = build_hwpx_agent_prompt(dom_map, "전체 문항 점검")
        self.assertIn("p_119", user_prompt)
        self.assertNotIn("...(이하 생략)", user_prompt)

        doc = create_minimal_hwpx_document()
        apply_hwpx_operations(
            doc,
            [
                {
                    "op": "set_paragraph_text",
                    "target_id": "p_0",
                    "new_text": "다음   화합물\nH₂O의 성질을   설명하시오.",
                }
            ],
        )
        touched = apply_hwpx_operations(
            doc,
            [
                {
                    "op": "replace_text",
                    "target_id": "p_0",
                    "old_text": "다음 화합물 H2O의 성질을 설명하시오.",
                    "new_text": "물(H_{2}O)의 성질을 설명하시오.",
                }
            ],
        )
        self.assertEqual(touched, ["p_0"])

    def test_launch_browser_context_builtin_chromium_persistent(self) -> None:
        recorded_calls = []

        class _FakeContext:
            def __init__(self) -> None:
                self.init_scripts = []

            def add_init_script(self, script: str) -> None:
                self.init_scripts.append(script)

        class _FallbackChromium:
            def launch_persistent_context(self, **kwargs):
                recorded_calls.append(kwargs)
                if kwargs.get("channel") == "chrome":
                    raise RuntimeError("Executable doesn't exist at C:\\Program Files\\Google\\Chrome\\chrome.exe")
                return _FakeContext()

        class _FallbackPlaywright:
            chromium = _FallbackChromium()

        worker = WebGeminiAutoWorker.__new__(WebGeminiAutoWorker)
        worker.keep_login_session = True
        worker.headless = False
        worker.emit_log = lambda *_args, **_kwargs: None

        with patch.object(WebGeminiAutoWorker, "_find_system_browser_exe", return_value=None):
            browser, ctx, tmp = worker._launch_browser_context(_FallbackPlaywright(), "data/playwright_web_llm_profile")
        self.assertIsNone(browser)
        self.assertIsNone(tmp)
        self.assertEqual(len(recorded_calls), 2)
        self.assertEqual(recorded_calls[0].get("channel"), "chrome")
        fallback_call = recorded_calls[1]
        self.assertNotIn("channel", fallback_call)
        self.assertEqual(fallback_call["user_data_dir"], "data/playwright_web_llm_profile")
        self.assertIs(fallback_call["headless"], False)
        self.assertEqual(fallback_call["permissions"], ["clipboard-read", "clipboard-write"])
        self.assertEqual(fallback_call["ignore_default_args"], ["--enable-automation"])
        self.assertEqual(fallback_call["args"], ["--disable-blink-features=AutomationControlled"])
        self.assertTrue(any("navigator, 'webdriver'" in s for s in ctx.init_scripts))

        # 프로필 잠금 오류 시 임시 폴더 우회 대신 잔류 프로세스/잠금 정리 후 동일 고정 user_data_dir로 재시도 검증
        lock_calls = []
        kill_calls = []
        clean_calls = []

        class _LockOnceChromium:
            def launch_persistent_context(self, **kwargs):
                lock_calls.append(kwargs)
                if len(lock_calls) == 1:
                    raise RuntimeError("Target page, context or browser has been closed: ProcessSingleton")
                return _FakeContext()

        class _LockOncePlaywright:
            chromium = _LockOnceChromium()

        with (
            patch.object(WebGeminiAutoWorker, "_find_system_browser_exe", return_value=None),
            patch.object(WebGeminiAutoWorker, "_kill_stale_chromium_for_profile", side_effect=lambda p: kill_calls.append(p)),
            patch.object(WebGeminiAutoWorker, "_clean_stale_profile_locks", side_effect=lambda p: clean_calls.append(p)),
            patch("services.web_gemini_auto_worker.time.sleep") as mock_sleep,
        ):
            browser2, ctx2, tmp2 = worker._launch_browser_context(
                _LockOncePlaywright(), "data/playwright_web_llm_profile"
            )

        self.assertIsNone(browser2)
        self.assertIsNone(tmp2)
        self.assertEqual(len(lock_calls), 2)
        self.assertEqual(lock_calls[0]["user_data_dir"], "data/playwright_web_llm_profile")
        self.assertEqual(lock_calls[1]["user_data_dir"], "data/playwright_web_llm_profile")
        self.assertEqual(lock_calls[1].get("channel"), "chrome")
        self.assertEqual(kill_calls, ["data/playwright_web_llm_profile"])
        self.assertEqual(clean_calls, ["data/playwright_web_llm_profile"])
        mock_sleep.assert_called_once_with(0.5)
        self.assertTrue(any("navigator, 'webdriver'" in s for s in ctx2.init_scripts))

    def test_launch_browser_context_prefers_cdp_for_system_browser(self) -> None:
        popen_calls = []
        cdp_urls = []
        granted = []

        class _FakeProc:
            def __init__(self) -> None:
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                self.returncode = 0

        fake_proc = _FakeProc()

        class _FakeCdpContext:
            def __init__(self) -> None:
                self.init_scripts = []

            def grant_permissions(self, perms) -> None:
                granted.append(list(perms))

            def add_init_script(self, script: str) -> None:
                self.init_scripts.append(script)

        class _FakeCdpBrowser:
            def __init__(self) -> None:
                self.contexts = [_FakeCdpContext()]

        class _FakeCdpChromium:
            def connect_over_cdp(self, url: str):
                cdp_urls.append(url)
                return _FakeCdpBrowser()

            def launch_persistent_context(self, **kwargs):
                raise AssertionError("CDP 성공 시 launch_persistent_context가 호출되면 안 됩니다.")

        class _FakePlaywright:
            chromium = _FakeCdpChromium()

        worker = WebGeminiAutoWorker.__new__(WebGeminiAutoWorker)
        worker.keep_login_session = True
        worker.headless = False
        worker.emit_log = lambda *_args, **_kwargs: None

        def _fake_popen(cmd, **_kwargs):
            popen_calls.append(cmd)
            return fake_proc

        profile_rel = "data/playwright_web_llm_profile"
        expected_abs = str(Path(profile_rel).resolve())

        with (
            patch.object(WebGeminiAutoWorker, "_find_system_browser_exe", return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            patch.object(WebGeminiAutoWorker, "_find_free_port", return_value=9333),
            patch.object(WebGeminiAutoWorker, "_wait_for_cdp_endpoint", return_value="http://127.0.0.1:9333") as mock_wait,
            patch("services.web_gemini_auto_worker.subprocess.Popen", side_effect=_fake_popen),
        ):
            browser, ctx, tmp = worker._launch_browser_context(_FakePlaywright(), profile_rel)

        self.assertIsNotNone(browser)
        self.assertIsNone(tmp)
        self.assertIs(worker._cdp_browser_process, fake_proc)
        self.assertEqual(cdp_urls, ["http://127.0.0.1:9333"])
        self.assertEqual(granted, [["clipboard-read", "clipboard-write"]])
        self.assertEqual(ctx.init_scripts, [])
        mock_wait.assert_called_once_with(9333, timeout_sec=10.0, proc=fake_proc)

        self.assertEqual(len(popen_calls), 1)
        cmd = popen_calls[0]
        self.assertEqual(cmd[0], r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.assertIn("--remote-debugging-port=9333", cmd)
        self.assertIn(f"--user-data-dir={expected_abs}", cmd)
        self.assertIn("--no-first-run", cmd)
        self.assertIn("--no-default-browser-check", cmd)
        self.assertTrue(any(arg.startswith("--disable-features=ChromeWhatsNewUI") for arg in cmd))
        self.assertNotIn("--disable-blink-features=AutomationControlled", cmd)

    def test_bypass_programmatic_auth_if_needed(self) -> None:
        logs = []
        goto_calls = []

        class _FakePage:
            def __init__(self, url: str) -> None:
                self.url = url

            def goto(self, url: str, **kwargs) -> None:
                goto_calls.append((url, kwargs))

        worker = WebGeminiAutoWorker.__new__(WebGeminiAutoWorker)
        worker.emit_log = lambda level, msg: logs.append((level, msg))

        normal_page = _FakePage("https://gemini.google.com/app")
        self.assertFalse(worker._bypass_programmatic_auth_if_needed(normal_page))
        self.assertEqual(goto_calls, [])

        auth_page = _FakePage("https://accounts.google.com/v3/signin/programmatic_auth?hl=ko")
        self.assertTrue(worker._bypass_programmatic_auth_if_needed(auth_page))
        self.assertEqual(len(goto_calls), 1)
        self.assertEqual(goto_calls[0][0], "https://accounts.google.com")
        self.assertTrue(any(level == "WARN" and "programmatic_auth" in msg for level, msg in logs))

    def test_cleanup_playwright_resources_graceful_cdp_shutdown(self) -> None:
        events = []

        class _FakeCdpSession:
            def send(self, method: str) -> None:
                events.append(f"cdp:{method}")

        class _FakeBrowser:
            def new_browser_cdp_session(self):
                return _FakeCdpSession()

            def close(self) -> None:
                events.append("browser.close")

        class _FakePlaywright:
            def stop(self) -> None:
                events.append("playwright.stop")

        class _FakeProc:
            def __init__(self) -> None:
                self._done = False

            def poll(self):
                return 0 if self._done else None

            def wait(self, timeout=None):
                events.append(f"proc.wait:{timeout}")
                self._done = True
                return 0

            def terminate(self) -> None:
                events.append("proc.terminate")

        with patch("services.web_gemini_auto_worker.time.sleep"):
            WebGeminiAutoWorker._cleanup_playwright_resources(
                browser=_FakeBrowser(),
                playwright_obj=_FakePlaywright(),
                browser_process=_FakeProc(),
                timeout_sec=3.0,
            )

        self.assertEqual(
            events,
            ["cdp:Browser.close", "browser.close", "playwright.stop", "proc.wait:3.0"],
        )

    def test_hwp_security_popup_watcher_lifecycle(self) -> None:
        from services.hwpx_agent_service import _hwp_security_popup_watcher

        with _hwp_security_popup_watcher(interval_sec=0.05):
            time.sleep(0.02)

    def test_convert_via_hwp_com_watcher_readonly_and_lock_handling(self) -> None:
        import os
        import stat
        import tempfile
        import types
        from contextlib import contextmanager
        from services.hwpx_agent_service import (
            _HWP_FILE_LOCKED_MSG,
            _convert_via_hwp_com,
        )

        watcher_state = {"active": False, "events": []}

        @contextmanager
        def _fake_watcher(interval_sec: float = 0.15):
            watcher_state["active"] = True
            try:
                yield
            finally:
                watcher_state["active"] = False

        class _FakeWindow:
            Visible = True

        class _FakeWindows:
            def Item(self, _idx: int):
                return _FakeWindow()

        class _FakeHFileOpenSave:
            def __init__(self) -> None:
                self.HSet = object()
                self.filename = ""
                self.Format = ""

        class _FakeHParameterSet:
            def __init__(self) -> None:
                self.HFileOpenSave = _FakeHFileOpenSave()

        class _FakeHAction:
            def __init__(self, param_set: _FakeHParameterSet) -> None:
                self._ps = param_set

            def GetDefault(self, _name: str, _hset) -> None:
                return None

            def Execute(self, name: str, _hset) -> None:
                watcher_state["events"].append((name, watcher_state["active"]))
                target = Path(self._ps.HFileOpenSave.filename)
                target.write_bytes(b"NEW_HWP_DATA")

        class _FakeHwp:
            def __init__(self) -> None:
                self.XHwpWindows = _FakeWindows()
                self.HParameterSet = _FakeHParameterSet()
                self.HAction = _FakeHAction(self.HParameterSet)

            def SetMessageBoxMode(self, _mode: int) -> None:
                return None

            def RegisterModule(self, _a: str, _b: str) -> None:
                return None

            def Open(self, _src: str, _fmt: str, _opts: str) -> None:
                watcher_state["events"].append(("Open", watcher_state["active"]))

            def SaveAs(self, _dst: str, _fmt: str, _opts: str) -> bool:
                watcher_state["events"].append(("SaveAs", watcher_state["active"]))
                # FileSaveAs_S 폴백 구간까지 워처가 유지되는지 함께 검증
                return False

            def Clear(self, _opt: int) -> None:
                return None

            def Quit(self) -> None:
                return None

        fake_pythoncom = types.SimpleNamespace(
            CoInitialize=lambda: None,
            CoUninitialize=lambda: None,
        )
        fake_win32_client = types.SimpleNamespace(DispatchEx=lambda _progid: _FakeHwp())
        fake_win32com = types.SimpleNamespace(client=fake_win32_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src = root / "sample.hwpx"
            dst = root / "target.hwp"
            src.write_bytes(b"SRC_HWPX")
            dst.write_bytes(b"OLD_HWP_READONLY")
            os.chmod(dst, stat.S_IREAD)

            with (
                patch.dict(
                    sys.modules,
                    {
                        "pythoncom": fake_pythoncom,
                        "win32com": fake_win32com,
                        "win32com.client": fake_win32_client,
                    },
                ),
                patch(
                    "services.hwpx_agent_service._hwp_security_popup_watcher",
                    side_effect=_fake_watcher,
                ),
            ):
                out_path = _convert_via_hwp_com(src, dst, "HWPX", "HWP")

            self.assertEqual(out_path, dst.resolve())
            self.assertEqual(dst.read_bytes(), b"NEW_HWP_DATA")
            self.assertTrue(bool(dst.stat().st_mode & stat.S_IWRITE))
            self.assertEqual(
                watcher_state["events"],
                [("Open", True), ("SaveAs", True), ("FileSaveAs_S", True)],
            )
            self.assertEqual(list(root.glob(".~eduflow_tmp_*")), [])

            # 다른 한글 프로세스 잠금 시 명확한 안내 예외 발생 및 임시 파일 정리 검증
            with (
                patch.dict(
                    sys.modules,
                    {
                        "pythoncom": fake_pythoncom,
                        "win32com": fake_win32com,
                        "win32com.client": fake_win32_client,
                    },
                ),
                patch(
                    "services.hwpx_agent_service._hwp_security_popup_watcher",
                    side_effect=_fake_watcher,
                ),
                patch(
                    "services.hwpx_agent_service.os.replace",
                    side_effect=PermissionError("locked by another process"),
                ),
            ):
                with self.assertRaises(PermissionError) as ctx:
                    _convert_via_hwp_com(src, dst, "HWPX", "HWP")

            self.assertIn(_HWP_FILE_LOCKED_MSG, str(ctx.exception))
            self.assertEqual(list(root.glob(".~eduflow_tmp_*")), [])

    def test_no_ws_chem_replace(self) -> None:
        from services.hwpx_agent_service import _replace_with_normalized_match

        updated, ok = _replace_with_normalized_match(
            "수용액 CH₃COOH (aq)의 농도",
            "CH3COOH(aq)",
            "$$rm {CH}_{3}COOH(it {aq})$$",
        )
        self.assertTrue(ok)
        self.assertEqual(updated, "수용액 $$rm {CH}_{3}COOH(it {aq})$$의 농도")


if __name__ == "__main__":
    unittest.main()
