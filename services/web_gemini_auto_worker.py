"""
Playwright 기반 멀티 웹 AI(Gemini, ChatGPT, Claude) 완전 자동화 워커 모듈 (services/web_gemini_auto_worker.py).
- BaseWorker를 상속하여 QThread 백그라운드에서 실행
- 1단계 배치 엔진(services/batch_prompt_service.py)을 재사용하여 미완료 배치를 무인으로 반복 처리
- 통합 영구 프로필(playwright_web_llm_profile)로 3개 웹 AI 서비스의 로그인 세션 유지
- 로그인 후 작업 / 비로그인 즉시 작업 선택 지원
"""

from pathlib import Path
import random
import time
from typing import Any, Dict, List, Optional

import pandas as pd
from PyQt6.QtCore import pyqtSignal

from core.base_worker import BaseWorker
from services.batch_prompt_service import (
    apply_batch_results_to_excel,
    build_student_batches,
    extract_batch_items_from_response,
    resolve_batch_output_path,
)
from services.llm_client import extract_json_from_llm_response
from services.record_generator_service import load_all_references, load_prompt_text
from utils.path_helper import get_data_path


PROVIDER_CONFIGS: Dict[str, Dict[str, str]] = {
    "gemini": {
        "name": "Google Gemini",
        "url": "https://gemini.google.com/app",
        "input": 'div[contenteditable="true"], rich-textarea div[contenteditable="true"]',
        "stop": 'button[aria-label*="중지"], button[aria-label*="Stop"]',
        "response": "message-content, model-response",
        "send": 'button[aria-label*="보내기"], button[aria-label*="전송"], button[aria-label*="Send"], button.send-button',
        "new_chat": 'a[href="/app"], button[aria-label*="새 채팅"], button[aria-label*="New chat"]',
    },
    "chatgpt": {
        "name": "ChatGPT",
        "url": "https://chatgpt.com/",
        "input": '#prompt-textarea, div[contenteditable="true"]',
        "stop": 'button[data-testid="stop-button"], button[aria-label*="중지"], button[aria-label*="Stop"]',
        "response": 'div[data-message-author-role="assistant"]',
        "send": 'button[data-testid="send-button"], button[aria-label*="프롬프트 보내기"], button[aria-label*="Send"]',
        "new_chat": 'a[href="/"], button[aria-label*="새 채팅"], button[aria-label*="New chat"]',
    },
    "claude": {
        "name": "Claude",
        "url": "https://claude.ai/new",
        "input": 'div[contenteditable="true"]',
        "stop": 'button[aria-label*="Stop"], button[aria-label*="중지"]',
        "response": "div[data-is-streaming], div.font-claude-message",
        "send": 'button[aria-label*="Send"], button[aria-label*="보내기"], button[aria-label*="전송"]',
        "new_chat": 'a[href="/new"], button[aria-label*="New chat"], button[aria-label*="새 채팅"]',
    },
}

# 기존 코드 호환용 상수
GEMINI_APP_URL = PROVIDER_CONFIGS["gemini"]["url"]
INPUT_SELECTORS = PROVIDER_CONFIGS["gemini"]["input"]
STOP_BTN_SELECTORS = PROVIDER_CONFIGS["gemini"]["stop"]
SEND_BTN_SELECTORS = PROVIDER_CONFIGS["gemini"]["send"]
NEW_CHAT_SELECTORS = PROVIDER_CONFIGS["gemini"]["new_chat"]
RESPONSE_SELECTORS = PROVIDER_CONFIGS["gemini"]["response"]


class WebGeminiAutoWorker(BaseWorker):
    """Playwright로 웹 AI(Gemini/ChatGPT/Claude)를 제어해 미완료 배치를 자동 처리하는 워커."""

    batch_completed = pyqtSignal(dict)

    def __init__(
        self,
        excel_path: Path | str,
        output_path: Optional[Path | str] = None,
        prompt_file: Optional[Path | str] = None,
        prompt_template: Optional[str] = None,
        reference_dir: Optional[Path | str] = None,
        reference_context: Optional[str] = None,
        max_students: int = 15,
        max_input_tokens: int = 15000,
        is_grading: Optional[bool] = None,
        max_bytes: int = 1500,
        headless: bool = False,
        provider: str = "gemini",
        require_login: bool = True,
    ):
        provider_key = str(provider or "gemini").strip().lower()
        if provider_key not in PROVIDER_CONFIGS:
            provider_key = "gemini"
        self.provider = provider_key
        self.require_login = bool(require_login)
        self.provider_config = PROVIDER_CONFIGS[self.provider]

        super().__init__(task_name=f"Playwright 웹 {self.provider_config['name']} 배치 자동화")
        self.excel_path = Path(excel_path)
        self.prompt_file = Path(prompt_file) if prompt_file else None
        self.reference_dir = Path(reference_dir) if reference_dir else None
        self.max_students = max(1, int(max_students))
        self.max_input_tokens = max(500, int(max_input_tokens))
        self.max_bytes = int(max_bytes)
        self.headless = bool(headless)

        if is_grading is None:
            self.is_grading = bool(
                self.prompt_file is not None and "채점" in self.prompt_file.name
            )
        else:
            self.is_grading = bool(is_grading)

        self.output_path = resolve_batch_output_path(
            self.excel_path, output_path, is_grading=self.is_grading
        )
        self._prompt_template = (
            prompt_template
            if prompt_template is not None
            else load_prompt_text(self.prompt_file)
        )
        if reference_context is not None:
            self._reference_context = reference_context
        elif self.reference_dir and self.reference_dir.exists():
            self._reference_context = load_all_references(self.reference_dir)
        else:
            self._reference_context = ""

    def _sleep_interruptible(self, seconds: float) -> bool:
        """중단 요청을 수시로 확인하며 대기합니다. 중단 요청 시 False를 반환합니다."""
        steps = max(1, int(seconds / 0.2))
        for _ in range(steps):
            if self.is_stop_requested():
                return False
            time.sleep(0.2)
        return not self.is_stop_requested()

    def _has_visible_locator(self, page: Any, selector: str) -> bool:
        """지정된 셀렉터에 해당하는 요소가 화면에 보이는지 안전하게 확인합니다."""
        try:
            loc = page.locator(selector).first
            return bool(loc.count() > 0 and loc.is_visible())
        except Exception:
            return False

    def _is_logged_in(self, page: Any, provider: Optional[str] = None) -> bool:
        """공급자별 DOM 및 URL 지표를 바탕으로 로그인 완료 여부를 판정합니다."""
        prov = str(provider or self.provider).strip().lower()
        curr_url = str(getattr(page, "url", "") or "").lower()

        if prov == "gemini":
            if "accounts.google.com" in curr_url:
                return False
            login_prompt = 'a[href*="accounts.google.com"], button:has-text("로그인"), button:has-text("Sign in")'
            if self._has_visible_locator(page, login_prompt):
                return False
            profile_sel = 'a[aria-label*="Google 계정"], a[aria-label*="Google Account"], img[alt*="프로필"]'
            return self._has_visible_locator(page, profile_sel)

        if prov == "chatgpt":
            login_prompt = 'button[data-testid="login-button"], button:has-text("로그인"), button:has-text("Log in")'
            if self._has_visible_locator(page, login_prompt):
                return False
            return self._has_visible_locator(page, '[data-testid="profile-button"]')

        if prov == "claude":
            if "/login" in curr_url:
                return False
            if self._has_visible_locator(page, 'input[type="email"]'):
                return False
            return self._has_visible_locator(page, PROVIDER_CONFIGS["claude"]["input"])

        cfg = PROVIDER_CONFIGS.get(prov, self.provider_config)
        return self._has_visible_locator(page, cfg["input"])

    def _wait_for_input_ready(self, page: Any, timeout_sec: float) -> Optional[Any]:
        """지정된 시간(초) 동안 선택된 웹 AI 프롬프트 입력창 활성화를 대기합니다."""
        input_selector = self.provider_config["input"]
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return None
            try:
                loc = page.locator(input_selector).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                pass
            time.sleep(0.5)
        return None

    def _ensure_logged_in(self, page: Any) -> Any:
        """대상 웹 AI 접속 후 로그인 모드 설정에 따라 입력창 활성화를 대기합니다."""
        provider_name = self.provider_config["name"]
        target_url = self.provider_config["url"]
        self.emit_log("INFO", f"[{provider_name}] 페이지로 이동합니다...")
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

        # 1) 비로그인 즉시 작업 모드
        if not self.require_login:
            input_loc = self._wait_for_input_ready(page, timeout_sec=15.0)
            if input_loc is not None:
                return input_loc
            if self.is_stop_requested():
                return None

            self.emit_log(
                "WARN",
                f"[{provider_name}] 비로그인 모드이나 채팅 입력창이 아직 활성화되지 않았습니다. "
                "사이트 정책상 로그인이 필요한 경우 브라우저 창에서 로그인을 완료해 주세요.",
            )
            self.emit_progress(5, f"[{provider_name}] 채팅 입력창 활성화 대기 중 (최대 300초)...")
            input_loc = self._wait_for_input_ready(page, timeout_sec=300.0)
            if input_loc is None and not self.is_stop_requested():
                raise TimeoutError(
                    f"300초 내에 [{provider_name}] 채팅 입력창 활성화가 완료되지 않았습니다."
                )
            return input_loc

        # 2) 로그인 후 작업 모드 (최대 15초 초기 확인 후 미로그인 시 300초 대기)
        init_deadline = time.monotonic() + 15.0
        while time.monotonic() < init_deadline:
            if self.is_stop_requested():
                return None
            if self._is_logged_in(page, self.provider):
                input_loc = self._wait_for_input_ready(page, timeout_sec=2.0)
                if input_loc is not None:
                    return input_loc
            time.sleep(0.5)

        if self.is_stop_requested():
            return None

        self.emit_log(
            "WARN",
            f"[{provider_name}] 브라우저 창에서 계정 로그인을 완료해 주세요. 로그인 후 자동으로 진행됩니다.",
        )
        self.emit_progress(5, f"[{provider_name}] 계정 로그인 대기 중 (최대 300초)...")

        login_deadline = time.monotonic() + 300.0
        while time.monotonic() < login_deadline:
            if self.is_stop_requested():
                return None
            if self._is_logged_in(page, self.provider):
                input_loc = self._wait_for_input_ready(page, timeout_sec=2.0)
                if input_loc is not None:
                    return input_loc
            time.sleep(0.5)

        if not self.is_stop_requested():
            raise TimeoutError(
                f"300초 내에 [{provider_name}] 계정 로그인 및 입력창 확인이 완료되지 않았습니다."
            )
        return None

    def _start_new_chat(self, page: Any) -> Any:
        """이전 대화 컨텍스트 오염을 막기 위해 새 채팅을 시작하고 입력창을 반환합니다."""
        target_url = self.provider_config["url"]
        new_chat_sel = self.provider_config["new_chat"]
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            btn = page.locator(new_chat_sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()

        input_loc = self._wait_for_input_ready(page, timeout_sec=20.0)
        if input_loc is None and not self.is_stop_requested():
            raise TimeoutError(f"[{self.provider_config['name']}] 새 채팅 입력창을 찾지 못했습니다.")
        return input_loc

    def _fill_and_submit_prompt(self, page: Any, input_loc: Any, prompt_text: str) -> None:
        """타이핑 지연 없이 프롬프트를 즉시 입력하고 전송합니다."""
        stop_sel = self.provider_config["stop"]
        send_sel = self.provider_config["send"]

        input_loc.click()
        filled = False
        try:
            input_loc.fill(prompt_text)
            filled = True
        except Exception:
            filled = False

        if not filled:
            try:
                input_loc.evaluate(
                    """(el, text) => {
                        el.focus();
                        if (el.isContentEditable) {
                            el.textContent = text;
                            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text }));
                        } else {
                            el.value = text;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    }""",
                    prompt_text,
                )
                filled = True
            except Exception as exc:
                raise RuntimeError(f"프롬프트 입력창에 텍스트를 삽입하지 못했습니다: {exc}") from exc

        time.sleep(0.3)
        try:
            input_loc.press("Enter")
        except Exception:
            pass

        # Enter로 전송되지 않은 경우 전송 버튼 클릭 폴백
        time.sleep(0.6)
        try:
            stop_visible = page.locator(stop_sel).first.is_visible()
        except Exception:
            stop_visible = False

        if not stop_visible:
            try:
                send_btn = page.locator(send_sel).first
                if send_btn.count() > 0 and send_btn.is_visible() and send_btn.is_enabled():
                    send_btn.click()
            except Exception:
                pass

    def _read_latest_response_text(self, page: Any, min_index: int = 0) -> str:
        """마지막 응답 요소의 전체 텍스트를 읽어옵니다."""
        resp_sel = self.provider_config["response"]
        try:
            responses = page.locator(resp_sel)
            count = responses.count()
            if count <= min_index:
                return ""
            return (responses.nth(count - 1).inner_text() or "").strip()
        except Exception:
            return ""

    def _wait_for_response_complete(
        self, page: Any, pre_response_count: int, timeout_sec: float = 180.0
    ) -> str:
        """
        생성 중지 버튼 소멸 및 마지막 응답 텍스트 길이 3초 연속 고정을 감지하여 응답 완료를 대기합니다.
        """
        stop_sel = self.provider_config["stop"]
        deadline = time.monotonic() + timeout_sec
        saw_stop_button = False
        last_text = ""
        last_change_ts = time.monotonic()

        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return ""

            stop_visible = False
            try:
                stop_loc = page.locator(stop_sel).first
                stop_visible = stop_loc.count() > 0 and stop_loc.is_visible()
            except Exception:
                stop_visible = False

            if stop_visible:
                saw_stop_button = True
                last_change_ts = time.monotonic()
            else:
                curr_text = self._read_latest_response_text(page, min_index=pre_response_count)
                if curr_text != last_text:
                    last_text = curr_text
                    last_change_ts = time.monotonic()
                elif curr_text and (time.monotonic() - last_change_ts) >= 3.0:
                    return curr_text

            # 중지 버튼이 나타났다가 사라진 상태에서도 3초 안정화 확인
            if saw_stop_button and not stop_visible:
                curr_text = self._read_latest_response_text(page, min_index=pre_response_count)
                if curr_text and curr_text == last_text and (time.monotonic() - last_change_ts) >= 3.0:
                    return curr_text

            time.sleep(0.5)

        if last_text:
            return last_text
        raise TimeoutError(
            f"[{self.provider_config['name']}] 응답 생성 대기 시간이 초과되었습니다 (180초)."
        )

    def execute(self) -> Dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            self.emit_log(
                "ERROR",
                "Playwright 패키지가 설치되어 있지 않습니다. 터미널에서 "
                "'pip install playwright && playwright install chromium'을 실행해 주세요.",
            )
            return {
                "completed_batches": 0,
                "total_batches": 0,
                "applied_students": 0,
                "missing_playwright": True,
                "output_path": str(self.output_path),
            }

        target_excel = self.output_path if self.output_path.exists() else self.excel_path
        df = pd.read_excel(target_excel)
        batches = build_student_batches(
            df=df,
            prompt_template=self._prompt_template,
            reference_context=self._reference_context,
            max_students=self.max_students,
            max_input_tokens=self.max_input_tokens,
            is_grading=self.is_grading,
            max_bytes=self.max_bytes,
        )

        total_batches = len(batches)
        if total_batches == 0:
            self.emit_progress(100, "처리할 미완료 학생이 없습니다.")
            self.emit_log("INFO", "모든 학생의 결과가 이미 작성되어 있어 자동 실행을 종료합니다.")
            return {
                "completed_batches": 0,
                "total_batches": 0,
                "applied_students": 0,
                "missing_playwright": False,
                "output_path": str(self.output_path),
            }

        profile_dir = get_data_path("playwright_web_llm_profile")
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        provider_name = self.provider_config["name"]
        mode_str = "로그인 후 작업" if self.require_login else "비로그인 즉시 작업"
        self.emit_log(
            "INFO",
            f"[{provider_name} · {mode_str}] 미완료 배치 {total_batches}개 자동 실행 시작 (영구 프로필: {profile_dir})",
        )

        playwright_obj = None
        context = None
        completed_batches = 0
        total_applied = 0
        failed_batches: List[int] = []
        last_saved_path = str(self.output_path)

        try:
            playwright_obj = sync_playwright().start()
            context = playwright_obj.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()

            self.emit_progress(2, f"[{provider_name}] 접속 및 세션 상태 확인 중...")
            self._ensure_logged_in(page)
            if self.is_stop_requested():
                self.emit_log("WARN", "사용자 요청으로 자동 실행을 중단했습니다.")
                return {
                    "completed_batches": completed_batches,
                    "total_batches": total_batches,
                    "applied_students": total_applied,
                    "missing_playwright": False,
                    "output_path": last_saved_path,
                }

            resp_sel = self.provider_config["response"]
            for b_idx, batch in enumerate(batches, start=1):
                if self.is_stop_requested():
                    self.emit_log("WARN", "사용자 요청으로 배치 루프를 중단했습니다.")
                    break

                students = batch.get("students", [])
                est_tokens = batch.get("estimated_tokens", 0)
                pct_base = int(((b_idx - 1) / max(1, total_batches)) * 100)
                self.emit_progress(
                    max(5, pct_base),
                    f"[{provider_name}] Batch {b_idx}/{total_batches} 실행 중 ({len(students)}명 | 예상 {est_tokens:,} 토큰)...",
                )

                batch_ok = False
                last_err: Optional[Exception] = None

                for attempt in range(1, 4):  # 최초 1회 + 최대 2회 재시도
                    if self.is_stop_requested():
                        break
                    try:
                        self.emit_log(
                            "INFO",
                            f"[{provider_name}] Batch {b_idx}/{total_batches} 처리 시도 ({attempt}/3) — 학생 {len(students)}명",
                        )
                        input_loc = self._start_new_chat(page)
                        if input_loc is None or self.is_stop_requested():
                            break

                        try:
                            pre_count = page.locator(resp_sel).count()
                        except Exception:
                            pre_count = 0

                        self._fill_and_submit_prompt(page, input_loc, batch["final_prompt"])
                        raw_reply = self._wait_for_response_complete(
                            page, pre_response_count=pre_count, timeout_sec=180.0
                        )
                        if self.is_stop_requested():
                            break

                        # JSON 추출 검증 (extract_json_from_llm_response 호출 포함)
                        extract_json_from_llm_response(raw_reply)
                        parsed_items = extract_batch_items_from_response(raw_reply)

                        save_res = apply_batch_results_to_excel(
                            excel_path=self.excel_path,
                            output_path=self.output_path,
                            parsed_items=parsed_items,
                            is_grading=self.is_grading,
                        )
                        applied = int(save_res.get("applied_count", 0))
                        last_saved_path = save_res.get("output_path", last_saved_path)
                        self.output_path = Path(last_saved_path)

                        completed_batches += 1
                        total_applied += applied
                        batch_ok = True

                        pct_done = int((b_idx / max(1, total_batches)) * 100)
                        self.emit_progress(
                            pct_done,
                            f"[{provider_name}] Batch {b_idx}/{total_batches} 저장 완료 ({applied}명 반영)",
                        )
                        self.emit_log(
                            "SUCCESS",
                            f"[{provider_name}] Batch {b_idx}/{total_batches} 완료: {applied}명 엑셀 저장 ({Path(last_saved_path).name})",
                        )
                        self.batch_completed.emit(
                            {
                                "provider": self.provider,
                                "batch_index": b_idx,
                                "total_batches": total_batches,
                                "applied_count": applied,
                                "unmatched_ids": save_res.get("unmatched_ids", []),
                                "output_path": last_saved_path,
                                "raw_response": raw_reply,
                                "parsed_items": parsed_items,
                            }
                        )
                        break
                    except Exception as exc:
                        last_err = exc
                        self.emit_log(
                            "WARN",
                            f"[{provider_name}] Batch {b_idx}/{total_batches} 시도 {attempt}/3 실패: {exc}",
                        )
                        if attempt < 3 and not self.is_stop_requested():
                            self._sleep_interruptible(3.0)

                if self.is_stop_requested():
                    break

                if not batch_ok:
                    failed_batches.append(b_idx)
                    self.emit_log(
                        "ERROR",
                        f"[{provider_name}] Batch {b_idx}/{total_batches} 최종 실패 (건너뜀): {last_err}",
                    )

                if b_idx < total_batches and not self.is_stop_requested():
                    wait_sec = random.uniform(5.0, 8.0)
                    self.emit_log(
                        "INFO",
                        f"다음 배치 진행 전 {wait_sec:.1f}초 대기 중...",
                    )
                    self._sleep_interruptible(wait_sec)

            if not self.is_stop_requested():
                self.emit_progress(
                    100,
                    f"[{provider_name}] 자동 실행 완료: {completed_batches}/{total_batches} 배치 ({total_applied}명 저장)",
                )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if playwright_obj is not None:
                try:
                    playwright_obj.stop()
                except Exception:
                    pass

        return {
            "provider": self.provider,
            "completed_batches": completed_batches,
            "total_batches": total_batches,
            "applied_students": total_applied,
            "failed_batches": failed_batches,
            "missing_playwright": False,
            "output_path": last_saved_path,
        }


WebLlmAutoWorker = WebGeminiAutoWorker
