"""
Playwright 기반 멀티 웹 AI(Gemini, ChatGPT, Claude, 커스텀 URL) 자동화/반자동화 워커 모듈 (services/web_gemini_auto_worker.py).
- BaseWorker를 상속하여 QThread 백그라운드에서 실행
- 1단계 배치 엔진(services/batch_prompt_service.py)을 재사용하여 미완료 배치를 처리
- 통합 영구 프로필(playwright_web_llm_profile)로 웹 AI 서비스의 로그인 세션 유지 지원
- 임의 URL 3단계 입력창 탐지 폴백 (도메인/캐시 매칭 -> 최대 면적 휴리스틱 -> 1회성 클릭 리스너 학습)
- 리치 에디터 호환 클립보드 + Control+V 우선 프롬프트 주입 (fill / insert_text 폴백)
- 완전 자동(auto) 및 반자동(semi_auto: 프롬프트 입력 후 수동 전송, 회수 버튼으로 다음 배치 진행) 지원
"""

import ctypes
from ctypes import wintypes
from pathlib import Path
import random
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import pandas as pd
from PyQt6.QtCore import pyqtSignal

from core.base_worker import BaseWorker
from core.config_manager import ConfigManager
from services.batch_prompt_service import (
    apply_batch_results_to_excel,
    build_student_batches,
    extract_batch_items_from_response,
    resolve_batch_output_path,
)
from services.llm_client import (
    extract_json_from_llm_response,
    format_parsed_json_to_readable_text,
)
from services.record_generator_service import load_all_references, load_prompt_text
from utils.path_helper import get_data_path


GENERIC_INPUT_SELECTORS = 'textarea, div[contenteditable="true"], [role="textbox"]'
GENERIC_RESPONSE_SELECTORS = (
    "message-content, model-response, "
    'div[data-message-author-role="assistant"], '
    "div.font-claude-message, "
    'article, .markdown, .prose, [class*="assistant"], [class*="response"]'
)

PROVIDER_CONFIGS: Dict[str, Dict[str, str]] = {
    "gemini": {
        "name": "Google Gemini",
        "url": "https://gemini.google.com/app",
        "input": 'div[contenteditable="true"], rich-textarea div[contenteditable="true"]',
        "stop": 'button[aria-label*="중지"], button[aria-label*="Stop"]',
        "response": "message-content, model-response",
        "send": 'button[aria-label*="보내기"], button[aria-label*="전송"], button[aria-label*="Send"], button.send-button',
        "new_chat": 'a[href="/app"], button[aria-label*="새 채팅"], button[aria-label*="New chat"]',
        "temp_chat": 'button[aria-label*="임시 채팅"], button[aria-label*="Temporary chat"], [data-test-id*="temporary-chat"]',
    },
    "chatgpt": {
        "name": "ChatGPT",
        "url": "https://chatgpt.com/",
        "temp_url": "https://chatgpt.com/?temporary-chat=true",
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
        "temp_chat": 'button[aria-label*="Temporary"], button[aria-label*="Incognito"], button[aria-label*="임시"]',
    },
    "custom": {
        "name": "Custom Web AI",
        "url": "",
        "input": GENERIC_INPUT_SELECTORS,
        "stop": 'button[aria-label*="Stop"], button[aria-label*="중지"], button[data-testid="stop-button"]',
        "response": GENERIC_RESPONSE_SELECTORS,
        "send": 'button[type="submit"], button[aria-label*="Send"], button[aria-label*="보내기"], button[aria-label*="전송"], button[data-testid="send-button"]',
        "new_chat": 'a[href="/"], button[aria-label*="새 채팅"], button[aria-label*="New chat"]',
        "temp_chat": "",
    },
}

DOMAIN_TO_PROVIDER: Dict[str, str] = {
    "gemini.google.com": "gemini",
    "chatgpt.com": "chatgpt",
    "chat.openai.com": "chatgpt",
    "claude.ai": "claude",
}

# 기존 코드 호환용 상수
GEMINI_APP_URL = PROVIDER_CONFIGS["gemini"]["url"]
INPUT_SELECTORS = PROVIDER_CONFIGS["gemini"]["input"]
STOP_BTN_SELECTORS = PROVIDER_CONFIGS["gemini"]["stop"]
SEND_BTN_SELECTORS = PROVIDER_CONFIGS["gemini"]["send"]
NEW_CHAT_SELECTORS = PROVIDER_CONFIGS["gemini"]["new_chat"]
RESPONSE_SELECTORS = PROVIDER_CONFIGS["gemini"]["response"]


def normalize_web_url(raw_url: str) -> str:
    """사용자 입력 URL을 http/https 스킴이 포함된 정규 URL로 변환합니다."""
    cleaned = str(raw_url or "").strip()
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    return cleaned


def extract_domain_from_url(url: str) -> str:
    """URL에서 소문자 netloc 도메인을 추출합니다."""
    if not url:
        return ""
    try:
        parsed = urlparse(normalize_web_url(url))
        return (parsed.netloc or "").strip().lower()
    except Exception:
        return ""


def set_clipboard_text_threadsafe(text: str) -> bool:
    """백그라운드 워커 스레드에서도 안전하게 시스템 클립보드에 UTF-16 텍스트를 복사합니다."""
    if sys.platform == "win32":
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        GMEM_ZEROINIT = 0x0040
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL

        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL

        data = str(text or "").encode("utf-16-le") + b"\x00\x00"
        for _ in range(5):
            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
                    if not h_mem:
                        return False
                    ptr = kernel32.GlobalLock(h_mem)
                    if not ptr:
                        kernel32.GlobalFree(h_mem)
                        return False
                    try:
                        ctypes.memmove(ptr, data, len(data))
                    finally:
                        kernel32.GlobalUnlock(h_mem)
                    if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                        kernel32.GlobalFree(h_mem)
                        return False
                    return True
                finally:
                    user32.CloseClipboard()
            time.sleep(0.05)
        return False

    try:
        from PyQt6.QtCore import QThread
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            app.clipboard().setText(str(text or ""))
            return True
    except Exception:
        pass
    return False


def get_clipboard_text_threadsafe() -> str:
    """백그라운드 워커 스레드에서도 안전하게 시스템 클립보드 텍스트를 읽습니다."""
    if sys.platform == "win32":
        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL

        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        for _ in range(5):
            if user32.OpenClipboard(None):
                try:
                    h_data = user32.GetClipboardData(CF_UNICODETEXT)
                    if not h_data:
                        return ""
                    ptr = kernel32.GlobalLock(h_data)
                    if not ptr:
                        return ""
                    try:
                        return ctypes.wstring_at(ptr) or ""
                    finally:
                        kernel32.GlobalUnlock(h_data)
                finally:
                    user32.CloseClipboard()
            time.sleep(0.05)
        return ""

    try:
        from PyQt6.QtCore import QThread
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            return app.clipboard().text() or ""
    except Exception:
        pass
    return ""


_CLICK_LISTENER_JS = r"""
() => {
  window.__eduflow_picked_selector = "";
  if (window.__eduflow_picker_installed) return;
  window.__eduflow_picker_installed = true;

  function cssEscape(str) {
    if (window.CSS && CSS.escape) return CSS.escape(str);
    return String(str).replace(/([ !"#$%&'()*+,./:;<=>?@[\\\]^`{|}~])/g, "\\$1");
  }

  function buildUniqueSelector(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) {
      const idSel = "#" + cssEscape(el.id);
      try {
        if (document.querySelectorAll(idSel).length === 1) return idSel;
      } catch (e) {}
    }
    const tag = el.tagName.toLowerCase();
    const attrs = ["data-testid", "data-test-id", "name", "aria-label", "placeholder", "role"];
    for (const attr of attrs) {
      const val = el.getAttribute(attr);
      if (val && val.length < 120) {
        const escapedVal = val.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
        const attrSel = `${tag}[${attr}="${escapedVal}"]`;
        try {
          if (document.querySelectorAll(attrSel).length === 1) return attrSel;
        } catch (e) {}
      }
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName.toLowerCase() !== "html") {
      const curTag = cur.tagName.toLowerCase();
      if (cur.id) {
        parts.unshift("#" + cssEscape(cur.id));
        const joined = parts.join(" > ");
        try {
          if (document.querySelectorAll(joined).length === 1) return joined;
        } catch (e) {}
      } else {
        let sibIdx = 1;
        let sib = cur.previousElementSibling;
        while (sib) {
          if (sib.tagName === cur.tagName) sibIdx++;
          sib = sib.previousElementSibling;
        }
        parts.unshift(`${curTag}:nth-of-type(${sibIdx})`);
        const joined = parts.join(" > ");
        try {
          if (document.querySelectorAll(joined).length === 1) return joined;
        } catch (e) {}
      }
      cur = cur.parentElement;
    }
    return parts.join(" > ");
  }

  document.addEventListener("click", function(ev) {
    let target = ev.target;
    if (target && target.closest) {
      const editable = target.closest(
        'textarea, input:not([type="hidden"]):not([type="button"]):not([type="submit"]), ' +
        '[contenteditable="true"], [contenteditable="plaintext-only"], [role="textbox"]'
      );
      if (editable) target = editable;
    }
    window.__eduflow_picked_selector = buildUniqueSelector(target) || "";
    window.__eduflow_picker_installed = false;
  }, { capture: true, once: true });
}
"""


class WebGeminiAutoWorker(BaseWorker):
    """Playwright로 웹 AI(Gemini/ChatGPT/Claude/Custom URL)를 제어해 배치를 자동/반자동 처리하는 워커."""

    batch_completed = pyqtSignal(dict)
    waiting_for_login = pyqtSignal(str)
    waiting_for_semi_auto_collect = pyqtSignal(int, int)
    waiting_for_element_click = pyqtSignal(str)

    def __init__(
        self,
        excel_path: Optional[Path | str] = None,
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
        use_temp_chat: bool = True,
        execution_mode: str = "auto",
        custom_url: str = "",
        keep_login_session: bool = True,
        single_prompt: Optional[str] = None,
    ):
        provider_key = str(provider or "gemini").strip().lower()
        if provider_key not in PROVIDER_CONFIGS:
            provider_key = "custom" if custom_url else "gemini"
        self.provider = provider_key
        self.require_login = bool(require_login)
        self.use_temp_chat = bool(use_temp_chat)
        self.keep_login_session = bool(keep_login_session)

        exec_mode = str(execution_mode or "auto").strip().lower()
        self.execution_mode = exec_mode if exec_mode in ("auto", "semi_auto") else "auto"
        self.custom_url = normalize_web_url(custom_url)

        self.provider_config = dict(PROVIDER_CONFIGS[self.provider])
        if self.custom_url:
            self.provider_config["url"] = self.custom_url
            domain = extract_domain_from_url(self.custom_url)
            if self.provider == "custom" and domain:
                self.provider_config["name"] = domain

        self._login_confirmed_event = threading.Event()
        self._collect_next_event = threading.Event()
        self._manual_override_response: str = ""

        mode_tag = "반자동" if self.execution_mode == "semi_auto" else "자동"
        super().__init__(task_name=f"Playwright 웹 {self.provider_config['name']} {mode_tag}")

        self.single_prompt = str(single_prompt).strip() if single_prompt else None
        self.excel_path = Path(excel_path) if excel_path and str(excel_path).strip() else None
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

        if self.excel_path is not None:
            self.output_path: Optional[Path] = resolve_batch_output_path(
                self.excel_path, output_path, is_grading=self.is_grading
            )
        else:
            self.output_path = Path(output_path) if output_path else None

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

    def request_stop(self) -> None:
        """중단 요청 시 대기 중인 모든 스레드 이벤트도 즉시 해제합니다."""
        super().request_stop()
        self._login_confirmed_event.set()
        self._collect_next_event.set()

    def confirm_login(self) -> None:
        """사용자가 브라우저에서 로그인을 완료했음을 워커에 즉시 알립니다."""
        self._login_confirmed_event.set()

    def trigger_collect_and_next(self, manual_response_text: str = "") -> None:
        """반자동 모드에서 사용자가 응답 회수 및 다음 배치 진행을 요청할 때 호출합니다."""
        self._manual_override_response = str(manual_response_text or "").strip()
        self._collect_next_event.set()

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

    def _get_cached_selector(self, domain: str) -> str:
        """config.json에 저장된 도메인별 커스텀 입력창 CSS 셀렉터를 조회합니다."""
        if not domain:
            return ""
        try:
            selectors = ConfigManager().get("api_and_integrations", "custom_web_selectors", {})
            if isinstance(selectors, dict):
                sel = selectors.get(domain) or selectors.get(domain.removeprefix("www.")) or ""
                return str(sel).strip()
        except Exception:
            pass
        return ""

    def _save_cached_selector(self, domain: str, selector: str) -> None:
        """학습된 도메인별 입력창 CSS 셀렉터를 config.json에 저장합니다."""
        if not domain or not selector:
            return
        try:
            cfg = ConfigManager()
            selectors = cfg.get("api_and_integrations", "custom_web_selectors", {})
            if not isinstance(selectors, dict):
                selectors = {}
            selectors = dict(selectors)
            selectors[domain] = selector
            cfg.set("api_and_integrations", "custom_web_selectors", selectors, auto_save=True)
        except Exception as exc:
            self.emit_log("WARN", f"커스텀 셀렉터 저장 중 경고 ({domain}): {exc}")

    def _match_provider_by_domain(self, domain: str) -> Optional[str]:
        """URL 도메인(netloc)으로 등록된 PROVIDER_CONFIGS 키를 찾습니다."""
        if not domain:
            return None
        clean = domain.split(":")[0].lower()
        if clean in DOMAIN_TO_PROVIDER:
            return DOMAIN_TO_PROVIDER[clean]
        for known_domain, prov_key in DOMAIN_TO_PROVIDER.items():
            if clean.endswith("." + known_domain):
                return prov_key
        return None

    def _detect_largest_visible_input(self, page: Any) -> Optional[Any]:
        """
        2단계 휴리스틱 탐지: 페이지 내 visible한 textarea, div[contenteditable='true'],
        [role='textbox'] 중 면적(width * height)이 가장 큰 입력창을 반환합니다.
        """
        try:
            candidates = page.locator(GENERIC_INPUT_SELECTORS)
            count = min(candidates.count(), 50)
        except Exception:
            return None

        best_loc = None
        best_area = 0.0
        for idx in range(count):
            try:
                cand = candidates.nth(idx)
                if not cand.is_visible():
                    continue
                box = cand.bounding_box()
                if not box:
                    continue
                w = float(box.get("width", 0.0) or 0.0)
                h = float(box.get("height", 0.0) or 0.0)
                if w < 20.0 or h < 15.0:
                    continue
                editable = cand.evaluate(
                    "el => !el.disabled && !el.readOnly && el.getAttribute('aria-disabled') !== 'true'"
                )
                if not editable:
                    continue
                area = w * h
                if area > best_area:
                    best_area = area
                    best_loc = cand
            except Exception:
                continue
        return best_loc

    def _wait_for_user_click_input(self, page: Any, domain: str) -> Optional[Any]:
        """
        3단계 폴백: 페이지에 1회성 클릭 리스너를 설치해 사용자가 브라우저에서 입력창을
        1회 클릭하면 고유 CSS 셀렉터를 추출해 도메인 캐시에 저장하고 Locator를 반환합니다.
        """
        target_label = domain or str(getattr(page, "url", "") or "커스텀 URL")
        self.emit_log(
            "WARN",
            f"[{target_label}] 입력창 자동 탐지에 실패했습니다. 열린 브라우저에서 프롬프트 입력창을 1회 클릭해 주세요.",
        )
        self.emit_progress(
            10,
            f"[{target_label}] 브라우저에서 프롬프트 입력창을 1회 클릭하세요...",
        )
        self.waiting_for_element_click.emit(target_label)

        try:
            page.evaluate(_CLICK_LISTENER_JS)
        except Exception as exc:
            self.emit_log("WARN", f"클릭 리스너 설치 실패: {exc}")
            return None

        while not self.is_stop_requested():
            try:
                installed = page.evaluate("() => Boolean(window.__eduflow_picker_installed)")
                picked = str(page.evaluate("() => window.__eduflow_picked_selector || ''") or "").strip()
            except Exception:
                picked = ""
                installed = True

            if picked:
                try:
                    loc = page.locator(picked).first
                    if loc.count() > 0:
                        curr_domain = extract_domain_from_url(str(getattr(page, "url", "") or "")) or domain
                        if curr_domain:
                            self._save_cached_selector(curr_domain, picked)
                            self.emit_log(
                                "SUCCESS",
                                f"[{curr_domain}] 입력창 셀렉터 학습 및 캐시 저장 완료: {picked}",
                            )
                        return loc
                except Exception:
                    pass
                # 추출된 셀렉터가 유효하지 않으면 리스너 재설치
                try:
                    page.evaluate(_CLICK_LISTENER_JS)
                except Exception:
                    pass
            elif not installed:
                try:
                    page.evaluate(_CLICK_LISTENER_JS)
                except Exception:
                    pass

            time.sleep(0.25)
        return None

    def _resolve_input_locator(
        self, page: Any, timeout_sec: float = 12.0, allow_click_fallback: bool = True
    ) -> Optional[Any]:
        """
        임의 URL 입력창 탐지 3단계 폴백:
        1) urlparse(url).netloc으로 PROVIDER_CONFIGS 도메인 또는 config.json 캐시 우선 매칭
        2) visible textarea / div[contenteditable='true'] / [role='textbox'] 중 최대 면적 휴리스틱 탐지
        3) 실패 시 1회성 클릭 리스너로 고유 CSS 셀렉터 추출 및 도메인 캐시 저장
        """
        curr_url = str(getattr(page, "url", "") or self.provider_config.get("url", "") or "")
        domain = extract_domain_from_url(curr_url) or extract_domain_from_url(self.custom_url)
        matched_provider = self._match_provider_by_domain(domain)

        # 1단계 + 2단계 폴링 (SPA 초기 렌더링 대기 포함)
        poll_budget = timeout_sec if matched_provider or self.provider != "custom" else min(timeout_sec, 6.0)
        deadline = time.monotonic() + max(1.0, poll_budget)

        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return None

            curr_url = str(getattr(page, "url", "") or curr_url)
            domain = extract_domain_from_url(curr_url) or domain
            matched_provider = self._match_provider_by_domain(domain) or (
                self.provider if self.provider in ("gemini", "chatgpt", "claude") and not self.custom_url else None
            )

            # 1-a) config.json 도메인별 커스텀 셀렉터 캐시 우선 매칭
            cached_sel = self._get_cached_selector(domain)
            if cached_sel:
                try:
                    loc = page.locator(cached_sel).first
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    pass

            # 1-b) 기등록 PROVIDER_CONFIGS 도메인 셀렉터 매칭
            if matched_provider and matched_provider in PROVIDER_CONFIGS:
                prov_sel = PROVIDER_CONFIGS[matched_provider]["input"]
                try:
                    loc = page.locator(prov_sel).first
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    pass

            # 2단계) 미등록 도메인 또는 기본 셀렉터 미검출 시 최대 면적 휴리스틱 탐지
            heuristic_loc = self._detect_largest_visible_input(page)
            if heuristic_loc is not None:
                return heuristic_loc

            time.sleep(0.4)

        if self.is_stop_requested() or not allow_click_fallback:
            return None

        # 3단계) 사용자 1회 클릭 리스너를 통한 셀렉터 추출 및 저장
        return self._wait_for_user_click_input(page, domain)

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

        return self._detect_largest_visible_input(page) is not None

    def _is_likely_login_page(self, page: Any) -> bool:
        """현재 페이지가 로그인 화면으로 리다이렉트되었는지 휴리스틱으로 확인합니다."""
        curr_url = str(getattr(page, "url", "") or "").lower()
        if any(tok in curr_url for tok in ("accounts.google.com", "/login", "/signin", "auth0", "oauth")):
            return True
        if self._has_visible_locator(page, 'input[type="password"]'):
            return True
        return False

    def _wait_for_input_ready(
        self, page: Any, timeout_sec: float, input_selector: Optional[str] = None
    ) -> Optional[Any]:
        """지정된 시간(초) 동안 선택된 웹 AI 프롬프트 입력창 활성화를 대기합니다."""
        if input_selector:
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
                time.sleep(0.4)
        return self._resolve_input_locator(page, timeout_sec=timeout_sec, allow_click_fallback=True)

    def _ensure_logged_in(self, page: Any) -> Any:
        """대상 웹 AI 접속 후 로그인 모드 설정에 따라 즉시 시작하거나 사용자 로그인 확인을 대기합니다."""
        provider_name = self.provider_config["name"]
        target_url = self.provider_config["url"]
        if not target_url:
            raise ValueError("접속할 웹 AI URL이 지정되지 않았습니다.")

        self.emit_log("INFO", f"[{provider_name}] 페이지로 이동합니다 ({target_url})...")
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

        # 1) 비로그인 / 즉시 시작 모드: 입력창 탐지 시도 -> 로그인 화면이거나 미발견 시 대기
        if not self.require_login:
            if not self._is_likely_login_page(page):
                # 커스텀 URL이 아닌 알려진 공급자는 최대 10초, 커스텀은 5초간 자동 탐지(클릭 폴백 전)
                wait_s = 5.0 if (self.provider == "custom" or self.custom_url) else 10.0
                input_loc = self._resolve_input_locator(
                    page, timeout_sec=wait_s, allow_click_fallback=False
                )
                if input_loc is not None:
                    return input_loc

            if self.is_stop_requested():
                return None

            # 로그인 화면이 아니고 커스텀 URL이면 클릭 학습 폴백 바로 허용 가능
            if not self._is_likely_login_page(page) and (self.provider == "custom" or self.custom_url):
                input_loc = self._wait_for_user_click_input(
                    page, extract_domain_from_url(str(getattr(page, "url", "") or target_url))
                )
                if input_loc is not None:
                    return input_loc
                if self.is_stop_requested():
                    return None

            self.emit_log(
                "WARN",
                f"[{provider_name}] 채팅 입력창이 보이지 않습니다. 브라우저에서 로그인을 마친 뒤 "
                "우측 AI 실행 패널의 '로그인 완료' 버튼을 눌러주세요.",
            )
            self.emit_progress(
                5,
                f"[{provider_name}] 로그인 확인 대기 중 ('로그인 완료' 버튼 클릭)...",
            )
            self.waiting_for_login.emit(provider_name)
            while not self._login_confirmed_event.is_set():
                if self.is_stop_requested():
                    return None
                time.sleep(0.2)
            self.emit_log("INFO", f"[{provider_name}] 로그인 확인 완료 — 작업을 시작합니다.")
            return True

        # 2) 명시적 로그인 후 작업 모드 (기존 BatchManualPromptDialog 호환)
        self.emit_log(
            "WARN",
            f"[{provider_name}] 브라우저에서 로그인을 마친 뒤 '로그인 완료' 버튼을 눌러주세요.",
        )
        self.emit_progress(
            5,
            f"[{provider_name}] 브라우저 로그인 대기 중 ('로그인 완료' 버튼 대기)...",
        )
        self.waiting_for_login.emit(provider_name)

        while not self._login_confirmed_event.is_set():
            if self.is_stop_requested():
                return None
            time.sleep(0.2)

        self.emit_log("INFO", f"[{provider_name}] 로그인 확인 완료 — 배치 작업을 시작합니다.")
        return True

    def _open_new_chat(
        self,
        page: Any,
        provider: Optional[str] = None,
        use_temp_chat: Optional[bool] = None,
        is_first_batch: bool = False,
    ) -> Any:
        """이전 대화 컨텍스트 오염을 막기 위해 새 채팅(또는 임시 채팅)을 열고 입력창을 반환합니다."""
        prov = str(provider or self.provider).strip().lower()
        temp_chat = self.use_temp_chat if use_temp_chat is None else bool(use_temp_chat)
        cfg = PROVIDER_CONFIGS.get(prov, self.provider_config)
        provider_name = self.provider_config["name"]
        target_url = self.provider_config.get("url") or cfg.get("url", "")
        new_chat_sel = cfg.get("new_chat", "")

        # 첫 배치에서 커스텀 URL이 이미 열려 있고 입력창이 보이면 재이동 없이 바로 사용
        if is_first_batch and (prov == "custom" or self.custom_url):
            existing = self._resolve_input_locator(page, timeout_sec=3.0, allow_click_fallback=False)
            if existing is not None:
                return existing

        if self.custom_url or prov == "custom":
            if not is_first_batch:
                clicked_new = False
                if new_chat_sel:
                    try:
                        btn = page.locator(new_chat_sel).first
                        if btn.count() > 0 and btn.is_visible():
                            btn.click()
                            clicked_new = True
                            time.sleep(0.4)
                    except Exception:
                        clicked_new = False
                if not clicked_new and target_url:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            input_loc = self._resolve_input_locator(page, timeout_sec=8.0, allow_click_fallback=True)
            if input_loc is None and not self.is_stop_requested():
                raise TimeoutError(f"[{provider_name}] 입력창을 찾지 못했습니다.")
            return input_loc

        nav_url = (
            cfg.get("temp_url", "https://chatgpt.com/?temporary-chat=true")
            if (temp_chat and prov == "chatgpt")
            else target_url
        )
        try:
            page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            if nav_url != target_url and target_url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            elif new_chat_sel:
                btn = page.locator(new_chat_sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()

        if temp_chat and prov != "chatgpt":
            clicked_temp = False
            temp_sel = cfg.get("temp_chat", "")
            if temp_sel:
                try:
                    temp_btn = page.locator(temp_sel).first
                    if temp_btn.count() > 0 and temp_btn.is_visible():
                        temp_btn.click()
                        clicked_temp = True
                        time.sleep(0.3)
                except Exception:
                    clicked_temp = False

            if not clicked_temp:
                self.emit_log(
                    "INFO",
                    f"[{provider_name}] 임시 채팅 버튼을 찾지 못해 일반 새 채팅으로 진행합니다.",
                )

        input_loc = self._resolve_input_locator(page, timeout_sec=18.0, allow_click_fallback=True)
        if input_loc is None and not self.is_stop_requested():
            raise TimeoutError(f"[{provider_name}] 새 채팅 입력창을 찾지 못했습니다.")
        return input_loc

    def _start_new_chat(self, page: Any) -> Any:
        """하위 호환 래퍼: 설정된 공급자 및 임시 채팅 옵션으로 새 채팅을 엽니다."""
        return self._open_new_chat(page, self.provider, self.use_temp_chat)

    def _read_input_box_text(self, input_loc: Any) -> str:
        """입력창 현재 텍스트를 안전하게 조회합니다."""
        try:
            return str(
                input_loc.evaluate(
                    "el => ('value' in el && el.value !== undefined) ? el.value : (el.innerText || el.textContent || '')"
                )
                or ""
            )
        except Exception:
            return ""

    def _inject_prompt_text(self, page: Any, input_loc: Any, prompt_text: str) -> None:
        """
        리치 에디터 호환성을 위해:
        1) 시스템 클립보드 복사 후 입력창 클릭 + Control+V 우선 시도
        2) 실패 시 fill() 시도
        3) 실패 시 page.keyboard.insert_text() / JS InputEvent 폴백
        """
        input_loc.click()
        time.sleep(0.15)

        # 1순위: 시스템 클립보드 복사 + Control+V 붙여넣기
        copied = set_clipboard_text_threadsafe(prompt_text)
        if not copied:
            try:
                page.evaluate("t => navigator.clipboard.writeText(t)", prompt_text)
                copied = True
            except Exception:
                copied = False

        if copied:
            try:
                page.keyboard.press("Control+V")
                time.sleep(0.25)
                current_text = self._read_input_box_text(input_loc)
                if len(current_text.strip()) >= min(16, max(1, len(prompt_text.strip()) // 2)):
                    return
            except Exception:
                pass

        # 2순위: fill() 시도
        try:
            input_loc.fill(prompt_text)
            time.sleep(0.15)
            current_text = self._read_input_box_text(input_loc)
            if len(current_text.strip()) > 0:
                return
        except Exception:
            pass

        # 3순위: keyboard.insert_text() 폴백
        try:
            input_loc.click()
            page.keyboard.insert_text(prompt_text)
            time.sleep(0.15)
            current_text = self._read_input_box_text(input_loc)
            if len(current_text.strip()) > 0:
                return
        except Exception:
            pass

        # 최종 JS 폴백
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
        except Exception as exc:
            raise RuntimeError(f"프롬프트 입력창에 텍스트를 삽입하지 못했습니다: {exc}") from exc

    def _submit_prompt(self, page: Any, input_loc: Any) -> None:
        """입력된 프롬프트를 Enter 또는 전송 버튼 클릭으로 전송합니다."""
        stop_sel = self.provider_config["stop"]
        send_sel = self.provider_config["send"]

        time.sleep(0.3)
        try:
            input_loc.press("Enter")
        except Exception:
            pass

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

    def _fill_and_submit_prompt(self, page: Any, input_loc: Any, prompt_text: str) -> None:
        """프롬프트를 안전하게 주입하고 전송합니다."""
        self._inject_prompt_text(page, input_loc, prompt_text)
        self._submit_prompt(page, input_loc)

    def _read_latest_response_text(self, page: Any, min_index: int = 0) -> str:
        """마지막 응답 요소의 전체 텍스트를 읽어옵니다."""
        resp_sel = self.provider_config.get("response") or GENERIC_RESPONSE_SELECTORS
        try:
            responses = page.locator(resp_sel)
            count = responses.count()
            if count <= min_index:
                return ""
            return (responses.nth(count - 1).inner_text() or "").strip()
        except Exception:
            return ""

    def _collect_semi_auto_candidate_texts(self, page: Any, pre_count: int) -> List[str]:
        """반자동 모드 회수 시 우선순위에 따라 응답 후보 텍스트들을 수집합니다."""
        candidates: List[str] = []
        if self._manual_override_response:
            candidates.append(self._manual_override_response)

        latest = self._read_latest_response_text(page, min_index=pre_count)
        if latest:
            candidates.append(latest)

        # 페이지 내 code/pre 블록에서 최신 JSON 후보 역순 수집
        try:
            code_blocks = page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('pre, code, .markdown, [class*="message"]'));
                    return els.slice(-12).reverse().map(e => (e.innerText || e.textContent || '').trim()).filter(Boolean);
                }"""
            )
            if isinstance(code_blocks, list):
                for blk in code_blocks:
                    if blk and blk not in candidates:
                        candidates.append(str(blk))
        except Exception:
            pass

        try:
            body_text = str(page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''") or "").strip()
            if body_text and body_text not in candidates:
                candidates.append(body_text)
        except Exception:
            pass

        clip_text = get_clipboard_text_threadsafe().strip()
        if clip_text and clip_text not in candidates:
            candidates.append(clip_text)

        return candidates

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

    def _build_execution_batches(self) -> List[Dict[str, Any]]:
        """실행 대상 배치 목록을 생성합니다 (엑셀 배치 또는 단일 프롬프트)."""
        if self.single_prompt:
            return [
                {
                    "batch_index": 1,
                    "students": [],
                    "estimated_tokens": max(1, len(self.single_prompt) // 2),
                    "final_prompt": self.single_prompt,
                    "is_single_prompt": True,
                }
            ]

        if self.excel_path is None:
            raise ValueError("대상 엑셀 파일 또는 프롬프트가 지정되지 않았습니다.")

        target_excel = (
            self.output_path
            if (self.output_path is not None and self.output_path.exists())
            else self.excel_path
        )
        df = pd.read_excel(target_excel)
        return build_student_batches(
            df=df,
            prompt_template=self._prompt_template,
            reference_context=self._reference_context,
            max_students=self.max_students,
            max_input_tokens=self.max_input_tokens,
            is_grading=self.is_grading,
            max_bytes=self.max_bytes,
        )

    def _run_semi_auto_batch(
        self,
        page: Any,
        batch: Dict[str, Any],
        b_idx: int,
        total_batches: int,
        resp_sel: str,
    ) -> Optional[Dict[str, Any]]:
        """반자동 모드 단일 배치: 프롬프트 입력 후 사용자 회수 신호를 대기하고 결과를 파싱합니다."""
        provider_name = self.provider_config["name"]
        is_single = bool(batch.get("is_single_prompt", False))

        input_loc = self._open_new_chat(
            page, self.provider, self.use_temp_chat, is_first_batch=(b_idx == 1)
        )
        if input_loc is None or self.is_stop_requested():
            return None

        try:
            pre_count = page.locator(resp_sel).count()
        except Exception:
            pre_count = 0

        self._inject_prompt_text(page, input_loc, batch["final_prompt"])
        self.emit_log(
            "INFO",
            f"[{provider_name}] Batch {b_idx}/{total_batches} 프롬프트 입력 완료. "
            "웹 브라우저에서 확인/전송 후 응답이 완료되면 '응답 회수 · 다음 배치'를 눌러주세요.",
        )
        self.emit_progress(
            max(10, int(((b_idx - 0.5) / max(1, total_batches)) * 100)),
            f"[{provider_name}] Batch {b_idx}/{total_batches} 전송 및 응답 대기 중 ('응답 회수 · 다음 배치' 클릭)...",
        )

        while not self.is_stop_requested():
            self._collect_next_event.clear()
            self._manual_override_response = ""
            self.waiting_for_semi_auto_collect.emit(b_idx, total_batches)

            while not self._collect_next_event.is_set():
                if self.is_stop_requested():
                    return None
                time.sleep(0.2)

            if self.is_stop_requested():
                return None

            candidates = self._collect_semi_auto_candidate_texts(page, pre_count)
            self._manual_override_response = ""

            if is_single:
                for cand in candidates:
                    if not cand:
                        continue
                    try:
                        parsed_json = extract_json_from_llm_response(cand)
                        formatted = format_parsed_json_to_readable_text(parsed_json)
                        return {
                            "raw_reply": cand,
                            "parsed_json": parsed_json,
                            "formatted_text": formatted,
                            "parsed_items": [],
                            "is_single_prompt": True,
                        }
                    except Exception:
                        continue
                if candidates and candidates[0].strip():
                    raw_txt = candidates[0].strip()
                    return {
                        "raw_reply": raw_txt,
                        "parsed_json": {},
                        "formatted_text": raw_txt,
                        "parsed_items": [],
                        "is_single_prompt": True,
                    }
            else:
                for cand in candidates:
                    if not cand:
                        continue
                    try:
                        extract_json_from_llm_response(cand)
                        parsed_items = extract_batch_items_from_response(cand)
                        if parsed_items:
                            return {
                                "raw_reply": cand,
                                "parsed_items": parsed_items,
                                "is_single_prompt": False,
                            }
                    except Exception:
                        continue

            self.emit_log(
                "WARN",
                f"[{provider_name}] Batch {b_idx}/{total_batches} 유효한 JSON 응답을 회수하지 못했습니다. "
                "응답이 완료되었는지 확인하거나 응답을 클립보드로 복사한 뒤 다시 '응답 회수 · 다음 배치'를 눌러주세요.",
            )

        return None

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
                "output_path": str(self.output_path) if self.output_path else "",
            }

        batches = self._build_execution_batches()
        total_batches = len(batches)
        if total_batches == 0:
            self.emit_progress(100, "처리할 미완료 학생이 없습니다.")
            self.emit_log("INFO", "모든 학생의 결과가 이미 작성되어 있어 자동 실행을 종료합니다.")
            return {
                "completed_batches": 0,
                "total_batches": 0,
                "applied_students": 0,
                "missing_playwright": False,
                "output_path": str(self.output_path) if self.output_path else "",
            }

        if not self.provider_config.get("url"):
            raise ValueError("접속할 웹 AI URL이 비어 있습니다. 대상 웹 AI 또는 직접 URL을 입력해 주세요.")

        profile_dir = get_data_path("playwright_web_llm_profile")
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        provider_name = self.provider_config["name"]
        mode_str = "반자동(수동 전송·회수)" if self.execution_mode == "semi_auto" else "완전 자동(무인 반복)"
        self.emit_log(
            "INFO",
            f"[{provider_name} · {mode_str}] 미완료 배치 {total_batches}개 실행 시작",
        )

        playwright_obj = None
        browser = None
        context = None
        completed_batches = 0
        total_applied = 0
        failed_batches: List[int] = []
        last_saved_path = str(self.output_path) if self.output_path else ""
        last_single_result: Dict[str, Any] = {}

        try:
            playwright_obj = sync_playwright().start()
            if self.keep_login_session:
                context = playwright_obj.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=self.headless,
                    permissions=["clipboard-read", "clipboard-write"],
                    args=["--disable-blink-features=AutomationControlled"],
                )
            else:
                browser = playwright_obj.chromium.launch(
                    headless=self.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    permissions=["clipboard-read", "clipboard-write"],
                )

            page = context.pages[0] if context.pages else context.new_page()

            self.emit_progress(2, f"[{provider_name}] 접속 및 세션 상태 확인 중...")
            self._ensure_logged_in(page)
            if self.is_stop_requested():
                self.emit_log("WARN", "사용자 요청으로 실행을 중단했습니다.")
                return {
                    "completed_batches": completed_batches,
                    "total_batches": total_batches,
                    "applied_students": total_applied,
                    "missing_playwright": False,
                    "output_path": last_saved_path,
                }

            resp_sel = self.provider_config.get("response") or GENERIC_RESPONSE_SELECTORS
            for b_idx, batch in enumerate(batches, start=1):
                if self.is_stop_requested():
                    self.emit_log("WARN", "사용자 요청으로 배치 루프를 중단했습니다.")
                    break

                students = batch.get("students", [])
                is_single = bool(batch.get("is_single_prompt", False))
                est_tokens = batch.get("estimated_tokens", 0)
                pct_base = int(((b_idx - 1) / max(1, total_batches)) * 100)
                stu_info = "단일 OCR/보정" if is_single else f"{len(students)}명"
                self.emit_progress(
                    max(5, pct_base),
                    f"[{provider_name}] Batch {b_idx}/{total_batches} 실행 중 ({stu_info} | 예상 {est_tokens:,} 토큰)...",
                )

                if self.execution_mode == "semi_auto":
                    semi_res = self._run_semi_auto_batch(
                        page=page,
                        batch=batch,
                        b_idx=b_idx,
                        total_batches=total_batches,
                        resp_sel=resp_sel,
                    )
                    if semi_res is None or self.is_stop_requested():
                        break

                    if is_single:
                        completed_batches += 1
                        last_single_result = semi_res
                        self.emit_progress(100, f"[{provider_name}] 응답 회수 완료")
                        self.batch_completed.emit(
                            {
                                "provider": self.provider,
                                "batch_index": b_idx,
                                "total_batches": total_batches,
                                "is_single_prompt": True,
                                "raw_response": semi_res.get("raw_reply", ""),
                                "parsed_json": semi_res.get("parsed_json", {}),
                                "formatted_text": semi_res.get("formatted_text", ""),
                            }
                        )
                    else:
                        save_res = apply_batch_results_to_excel(
                            excel_path=self.excel_path,  # type: ignore[arg-type]
                            output_path=self.output_path,
                            parsed_items=semi_res["parsed_items"],
                            is_grading=self.is_grading,
                        )
                        applied = int(save_res.get("applied_count", 0))
                        last_saved_path = save_res.get("output_path", last_saved_path)
                        self.output_path = Path(last_saved_path)
                        completed_batches += 1
                        total_applied += applied
                        pct_done = int((b_idx / max(1, total_batches)) * 100)
                        self.emit_progress(
                            pct_done,
                            f"[{provider_name}] Batch {b_idx}/{total_batches} 회수·저장 완료 ({applied}명 반영)",
                        )
                        self.emit_log(
                            "SUCCESS",
                            f"[{provider_name}] Batch {b_idx}/{total_batches} 반영 완료: {applied}명 ({Path(last_saved_path).name})",
                        )
                        self.batch_completed.emit(
                            {
                                "provider": self.provider,
                                "batch_index": b_idx,
                                "total_batches": total_batches,
                                "applied_count": applied,
                                "unmatched_ids": save_res.get("unmatched_ids", []),
                                "output_path": last_saved_path,
                                "raw_response": semi_res.get("raw_reply", ""),
                                "parsed_items": semi_res["parsed_items"],
                            }
                        )
                    continue

                # 완전 자동(auto) 모드
                batch_ok = False
                last_err: Optional[Exception] = None

                for attempt in range(1, 4):
                    if self.is_stop_requested():
                        break
                    try:
                        self.emit_log(
                            "INFO",
                            f"[{provider_name}] Batch {b_idx}/{total_batches} 처리 시도 ({attempt}/3) — {stu_info}",
                        )
                        input_loc = self._open_new_chat(
                            page, self.provider, self.use_temp_chat, is_first_batch=(b_idx == 1 and attempt == 1)
                        )
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

                        if is_single:
                            try:
                                parsed_json = extract_json_from_llm_response(raw_reply)
                                formatted = format_parsed_json_to_readable_text(parsed_json)
                            except Exception:
                                parsed_json = {}
                                formatted = raw_reply.strip()
                            completed_batches += 1
                            batch_ok = True
                            last_single_result = {
                                "raw_reply": raw_reply,
                                "parsed_json": parsed_json,
                                "formatted_text": formatted,
                            }
                            self.emit_progress(100, f"[{provider_name}] 응답 수신 및 변환 완료")
                            self.batch_completed.emit(
                                {
                                    "provider": self.provider,
                                    "batch_index": b_idx,
                                    "total_batches": total_batches,
                                    "is_single_prompt": True,
                                    "raw_response": raw_reply,
                                    "parsed_json": parsed_json,
                                    "formatted_text": formatted,
                                }
                            )
                            break

                        extract_json_from_llm_response(raw_reply)
                        parsed_items = extract_batch_items_from_response(raw_reply)

                        save_res = apply_batch_results_to_excel(
                            excel_path=self.excel_path,  # type: ignore[arg-type]
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
                if self.single_prompt:
                    self.emit_progress(100, f"[{provider_name}] 처리 완료")
                else:
                    self.emit_progress(
                        100,
                        f"[{provider_name}] 실행 완료: {completed_batches}/{total_batches} 배치 ({total_applied}명 저장)",
                    )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright_obj is not None:
                try:
                    playwright_obj.stop()
                except Exception:
                    pass

        return {
            "provider": self.provider,
            "execution_mode": self.execution_mode,
            "completed_batches": completed_batches,
            "total_batches": total_batches,
            "applied_students": total_applied,
            "failed_batches": failed_batches,
            "missing_playwright": False,
            "output_path": last_saved_path,
            "formatted_text": last_single_result.get("formatted_text", ""),
            "raw_response": last_single_result.get("raw_reply", ""),
        }


WebLlmAutoWorker = WebGeminiAutoWorker
