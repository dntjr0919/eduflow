"""
Playwright 기반 멀티 웹 AI(Gemini, ChatGPT, Claude, 커스텀 URL) 자동화/반자동화 워커 모듈 (services/web_gemini_auto_worker.py).
- BaseWorker를 상속하여 QThread 백그라운드에서 실행
- 1단계 배치 엔진(services/batch_prompt_service.py)을 재사용하여 미완료 배치를 처리
- 통합 영구 프로필(playwright_web_llm_profile)로 웹 AI 서비스의 로그인 세션 유지 지원
- 임의 URL 3단계 입력창 탐지 폴백 (도메인/캐시 매칭 -> 최대 면적 휴리스틱 -> 1회성 클릭 리스너 학습)
- 리치 에디터 호환 클립보드 + Control+V 우선 프롬프트 주입 (fill / insert_text 폴백)
- 완전 자동(auto) 및 반자동(semi_auto: 프롬프트 입력 후 수동 전송, 회수 버튼으로 다음 배치 진행) 지원
"""

import base64
import ctypes
from ctypes import wintypes
import mimetypes
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
        "stop": 'button[aria-label*="대답 중지"], button[aria-label*="Stop generating"], button[aria-label*="응답 중지"]',
        "response": "message-content, model-response",
        "done_action": 'button[aria-label*="복사"], button[aria-label*="Copy"], message-actions',
        "send": 'button[aria-label*="보내기"], button[aria-label*="전송"], button[aria-label*="Send"], button.send-button',
        "new_chat": 'a[href="/app"], button[aria-label*="새 채팅"], button[aria-label*="New chat"]',
        "temp_chat": 'button[aria-label*="임시 채팅"], button[aria-label*="Temporary chat"], [data-test-id*="temporary-chat"]',
        "file_input": 'input[type="file"]',
        "attach_btn": 'button[aria-label*="업로드 및 도구"], button[aria-label*="Upload"], button[aria-label*="파일"], button[data-test-id*="upload"], button.upload-card-button',
    },
    "chatgpt": {
        "name": "ChatGPT",
        "url": "https://chatgpt.com/",
        "temp_url": "https://chatgpt.com/?temporary-chat=true",
        "input": '#prompt-textarea, div[contenteditable="true"]',
        "stop": 'button[data-testid="stop-button"], button[aria-label="스트리밍 중지"], button[aria-label="Stop streaming"], button[aria-label="생성 중지"]',
        "response": '[data-message-author-role="assistant"], article[data-turn="assistant"] .markdown, article[data-testid^="conversation-turn-"] div.markdown.prose, div.markdown.prose',
        "done_action": 'button[data-testid="copy-turn-action-button"], button[aria-label*="복사"], button[aria-label*="Copy"]',
        "send": (
            'button[data-testid="send-button"], '
            'button#composer-submit-button, '
            'button[aria-label*="프롬프트 보내기"], '
            'button[aria-label*="메시지 보내기"], '
            'button[aria-label*="보내기"], '
            'button[aria-label*="Send"], '
            'form button[type="submit"]'
        ),
        "new_chat": 'a[href="/"], button[aria-label*="새 채팅"], button[aria-label*="New chat"]',
        "file_input": 'input[type="file"]',
        "attach_btn": 'button[aria-label*="파일"], button[aria-label*="Attach"], button[data-testid="composer-plus-btn"]',
    },
    "claude": {
        "name": "Claude",
        "url": "https://claude.ai/new",
        "input": 'div[contenteditable="true"]',
        "stop": 'button[aria-label*="Stop response"], button[aria-label*="응답 중지"], button[aria-label*="생성 중지"], button[aria-label*="멈추"], button[data-testid="stop-button"]',
        "response": "div[data-is-streaming], div.font-claude-message",
        "done_action": 'button[aria-label*="Copy"], button[aria-label*="복사"]',
        "send": 'button[aria-label*="Send"], button[aria-label*="보내기"], button[aria-label*="전송"]',
        "new_chat": 'a[href="/new"], button[aria-label*="New chat"], button[aria-label*="새 채팅"]',
        "temp_chat": 'button[aria-label*="Temporary"], button[aria-label*="Incognito"], button[aria-label*="임시"], button[aria-label*="시크릿"], a[href*="incognito"]',
        "file_input": 'input[type="file"]',
        "attach_btn": 'button[aria-label*="Attach"], button[aria-label*="Upload"], button[aria-label*="파일"], button[data-testid="file-upload"]',
    },
    "custom": {
        "name": "Custom Web AI",
        "url": "",
        "input": GENERIC_INPUT_SELECTORS,
        "stop": 'button[aria-label*="Stop"], button[aria-label*="중지"], button[data-testid="stop-button"]',
        "response": GENERIC_RESPONSE_SELECTORS,
        "done_action": 'button[aria-label*="Copy"], button[aria-label*="복사"]',
        "send": 'button[type="submit"], button[aria-label*="Send"], button[aria-label*="보내기"], button[aria-label*="전송"], button[data-testid="send-button"]',
        "new_chat": 'a[href="/"], button[aria-label*="새 채팅"], button[aria-label*="New chat"]',
        "temp_chat": "",
        "file_input": 'input[type="file"]',
        "attach_btn": 'button[aria-label*="파일"], button[aria-label*="첨부"], button[aria-label*="Upload"], button[aria-label*="Attach"]',
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
        attachment_paths: Optional[List[str]] = None,
        task_label: str = "단일 AI 작업",
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
        self.task_label = str(task_label or "단일 AI 작업").strip() or "단일 AI 작업"
        self.attachment_paths: List[str] = [
            str(p).strip() for p in (attachment_paths or []) if str(p).strip()
        ]
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

    def _detect_blocking_overlay_or_auth(self, page: Any) -> Optional[str]:
        """
        입력창 미표시/비활성, 약관/안내 모달, 봇 아님 인증(CAPTCHA/Cloudflare) 여부를 감지합니다.
        차단 상태이면 사유 문자열을, 정상이면 None을 반환합니다.
        """
        # 1) 봇 아님 인증 (Cloudflare Turnstile / CAPTCHA)
        try:
            cf = page.locator('iframe[src*="challenges.cloudflare"]')
            if cf.count() > 0 and cf.first.is_visible():
                return "봇 아님 인증(Cloudflare)"
        except Exception:
            pass

        bot_keywords = ("사람인지 확인", "verify you are human")
        try:
            body_text = page.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
            )
            if isinstance(body_text, str):
                lower_body = body_text.lower()
                if any(kw in lower_body for kw in bot_keywords):
                    return "봇 아님 인증(사람인지 확인)"
        except Exception:
            pass

        # 2) 약관/안내 모달 ([role="dialog"] 등)
        consent_keywords = ("동의", "시작하기", "계속", "accept", "agree", "get started")
        try:
            dialogs = page.locator('[role="dialog"]')
            d_cnt = dialogs.count()
            for idx in range(min(d_cnt, 5)):
                dlg = dialogs.nth(idx)
                try:
                    if not dlg.is_visible():
                        continue
                    d_txt = (dlg.inner_text() or "").strip().lower()
                    if any(kw in d_txt for kw in consent_keywords) or any(
                        kw in d_txt for kw in bot_keywords
                    ):
                        return "약관 동의/안내 모달 또는 인증창"
                except Exception:
                    continue
        except Exception:
            pass

        if self._is_likely_login_page(page):
            return "로그인 화면"

        # 3) 입력창 미표시 또는 비활성 상태 확인
        input_loc = self._resolve_input_locator(page, timeout_sec=0.5, allow_click_fallback=False)
        if input_loc is None:
            return "채팅 입력창 미표시"
        try:
            if not input_loc.is_visible() or not input_loc.is_enabled():
                return "채팅 입력창 비활성 상태"
        except Exception:
            pass

        return None

    def _wait_for_user_auth_resume(self, page: Any, reason: str) -> bool:
        """로그인·약관 동의·봇 인증 완료 후 사용자의 재개 버튼 클릭을 대기합니다."""
        provider_name = self.provider_config["name"]
        self._login_confirmed_event.clear()
        self.emit_log(
            "WARN",
            f"[{provider_name}] {reason}이(가) 감지되어 대기합니다. "
            "브라우저에서 로그인·약관 동의·봇 인증을 완료한 뒤 '인증/로그인 완료 · 작업 재개' 버튼을 눌러주세요.",
        )
        self.emit_progress(
            5,
            f"[{provider_name}] {reason} 대기 중 ('인증/로그인 완료 · 작업 재개' 클릭)...",
        )
        self.waiting_for_login.emit(provider_name)
        while not self._login_confirmed_event.is_set():
            if self.is_stop_requested():
                return False
            time.sleep(0.2)
        self.emit_log("INFO", f"[{provider_name}] 사용자 인증/확인 완료 — 작업을 재개합니다.")
        return True

    def _ensure_ready_before_input(self, page: Any, input_loc: Any) -> Any:
        """프롬프트 입력 직전 차단 모달·봇 인증·입력창 비활성을 최종 점검하고 필요 시 대기합니다."""
        reason = self._detect_blocking_overlay_or_auth(page)
        if reason is not None:
            if not self._wait_for_user_auth_resume(page, reason):
                return None
            return self._resolve_input_locator(page, timeout_sec=10.0, allow_click_fallback=True)
        return input_loc

    def _ensure_logged_in(self, page: Any) -> Any:
        """대상 웹 AI 접속 후 로그인·약관·봇 인증 상태를 점검하고 필요 시 사용자 완료를 대기합니다."""
        provider_name = self.provider_config["name"]
        target_url = self.provider_config["url"]
        if not target_url:
            raise ValueError("접속할 웹 AI URL이 지정되지 않았습니다.")

        self.emit_log("INFO", f"[{provider_name}] 페이지로 이동합니다 ({target_url})...")
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

        if not self.require_login:
            if not self._is_likely_login_page(page):
                wait_s = 5.0 if (self.provider == "custom" or self.custom_url) else 10.0
                input_loc = self._resolve_input_locator(
                    page, timeout_sec=wait_s, allow_click_fallback=False
                )
                blocking_reason = self._detect_blocking_overlay_or_auth(page)
                if input_loc is not None and blocking_reason is None:
                    return input_loc

            if self.is_stop_requested():
                return None

            if not self._is_likely_login_page(page) and (self.provider == "custom" or self.custom_url):
                blocking_reason = self._detect_blocking_overlay_or_auth(page)
                if blocking_reason is None or blocking_reason == "채팅 입력창 미표시":
                    input_loc = self._wait_for_user_click_input(
                        page, extract_domain_from_url(str(getattr(page, "url", "") or target_url))
                    )
                    if input_loc is not None:
                        return input_loc
                    if self.is_stop_requested():
                        return None

            reason = self._detect_blocking_overlay_or_auth(page) or "로그인/인증 확인"
            if not self._wait_for_user_auth_resume(page, reason):
                return None
            return True

        # 명시적 로그인 후 작업 모드
        if not self._wait_for_user_auth_resume(page, "로그인 확인"):
            return None
        blocking_reason = self._detect_blocking_overlay_or_auth(page)
        if blocking_reason is not None:
            if not self._wait_for_user_auth_resume(page, blocking_reason):
                return None
        return True

    def _verify_temp_chat_active(
        self, page: Any, prov: str, timeout_sec: float = 8.0
    ) -> bool:
        """임시 채팅 모드가 실제로 활성화되었는지 최대 timeout_sec초 동안 폴링 검증합니다."""
        deadline = time.monotonic() + timeout_sec
        temp_sel = PROVIDER_CONFIGS.get(prov, {}).get("temp_chat", "")
        banner_keywords = (
            "임시 채팅",
            "temporary chat",
            "incognito",
            "won't appear in your history",
            "기록에 남지",
        )

        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return False

            body_lower = ""
            try:
                body_text = page.evaluate(
                    "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                )
                if isinstance(body_text, str):
                    body_lower = body_text.lower()
            except Exception:
                body_lower = ""

            has_banner = any(kw in body_lower for kw in banner_keywords)

            if prov == "chatgpt":
                cur_url = str(getattr(page, "url", "") or "").lower()
                if "temporary-chat=true" in cur_url and has_banner:
                    return True
            else:
                if temp_sel:
                    try:
                        btns = page.locator(temp_sel)
                        cnt = btns.count()
                        for idx in range(min(cnt, 3)):
                            b = btns.nth(idx)
                            pressed = (b.get_attribute("aria-pressed") or "").strip().lower()
                            checked = (b.get_attribute("aria-checked") or "").strip().lower()
                            if pressed == "true" or checked == "true":
                                return True
                    except Exception:
                        pass
                if has_banner:
                    return True

            time.sleep(0.3)

        return False

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

        if temp_chat:
            temp_confirmed = False
            if prov == "chatgpt":
                temp_confirmed = self._verify_temp_chat_active(page, prov, timeout_sec=8.0)
            else:
                clicked_temp = False
                temp_sel = cfg.get("temp_chat", "")
                if temp_sel:
                    try:
                        temp_btn = page.locator(temp_sel).first
                        if temp_btn.count() > 0 and temp_btn.is_visible():
                            temp_btn.click()
                            clicked_temp = True
                    except Exception:
                        clicked_temp = False

                if clicked_temp:
                    temp_confirmed = self._verify_temp_chat_active(page, prov, timeout_sec=8.0)
                else:
                    # 이미 임시 채팅 상태일 수도 있으므로 한 번 확인
                    temp_confirmed = self._verify_temp_chat_active(page, prov, timeout_sec=1.5)
                    if not temp_confirmed:
                        if prov == "claude":
                            self.emit_log(
                                "INFO",
                                f"[{provider_name}] 임시 채팅 버튼을 찾지 못해 일반 새 채팅으로 진행합니다.",
                            )
                            temp_confirmed = True
                        else:
                            self.emit_log(
                                "WARN",
                                f"[{provider_name}] 임시 채팅 버튼을 찾지 못했습니다. 일반 채팅 입력 방지를 위해 확인이 필요합니다.",
                            )

            if not temp_confirmed:
                if self.is_stop_requested():
                    return None
                self.emit_log(
                    "WARN",
                    f"[{provider_name}] 임시 채팅 모드 전환이 확인되지 않았습니다. 일반 채팅에 프롬프트가 입력되지 않도록 대기합니다.",
                )
                if not self._wait_for_user_auth_resume(page, "임시 채팅 모드 전환 확인"):
                    return None

        input_loc = self._wait_for_input_ready(page, timeout_sec=18.0)
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
                page.keyboard.press("Control+A")
                time.sleep(0.05)
                page.keyboard.press("Backspace")
                time.sleep(0.05)
                page.keyboard.press("Control+V")
                time.sleep(0.25)
                try:
                    current_text = str(input_loc.inner_text() or "")
                except Exception:
                    current_text = ""
                if not current_text.strip():
                    current_text = self._read_input_box_text(input_loc)
                stripped_len = len(current_text.strip())
                prompt_len = len(prompt_text.strip())
                if stripped_len >= 10 or (prompt_len < 10 and stripped_len >= max(1, prompt_len)):
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

    def _is_submission_started(
        self,
        page: Any,
        input_loc: Any,
        stop_sel: str,
        resp_sel: str,
        pre_count: int,
    ) -> bool:
        """전송 시작 여부(stop 버튼 출현, 입력창 비워짐, 어시스턴트 응답 수 증가)를 확인합니다."""
        try:
            if page.locator(stop_sel).first.is_visible() is True:
                return True
        except Exception:
            pass
        try:
            if len(self._read_input_box_text(input_loc).strip()) == 0:
                return True
        except Exception:
            pass
        try:
            if int(page.locator(resp_sel).count()) > pre_count:
                return True
        except Exception:
            pass
        return False

    def _submit_prompt(
        self,
        page: Any,
        input_loc: Any,
        pre_response_count: Optional[int] = None,
    ) -> None:
        """입력된 프롬프트를 Enter -> 전송 버튼 순회 클릭 -> Ctrl+Enter/requestSubmit 순으로 검증 전송합니다."""
        stop_sel = self.provider_config["stop"]
        send_sel = self.provider_config["send"]
        resp_sel = self.provider_config.get("response") or GENERIC_RESPONSE_SELECTORS

        if pre_response_count is None:
            try:
                pre_count = int(page.locator(resp_sel).count())
            except Exception:
                pre_count = 0
        else:
            pre_count = int(pre_response_count)

        def _poll_started(timeout_sec: float) -> bool:
            deadline = time.time() + timeout_sec
            while True:
                if self.is_stop_requested():
                    return True
                if self._is_submission_started(page, input_loc, stop_sel, resp_sel, pre_count):
                    return True
                if time.time() >= deadline:
                    return False
                time.sleep(0.2)

        # 1) 입력창 포커스 유지 상태에서 Enter 전송 및 최대 1.2초 검증
        time.sleep(0.25)
        try:
            input_loc.focus()
        except Exception:
            pass
        try:
            input_loc.press("Enter")
        except Exception:
            pass
        if _poll_started(1.2):
            return

        # 2) send_sel 매칭 모든 버튼 순회하며 visible & enabled 버튼 클릭 + 최대 2초 재확인
        clicked_btn = False
        try:
            btns = page.locator(send_sel)
            cnt = int(btns.count())
            for idx in range(cnt):
                cand = btns.nth(idx)
                try:
                    if cand.is_visible() and cand.is_enabled():
                        cand.click()
                        clicked_btn = True
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if clicked_btn and _poll_started(2.0):
            return

        # 3) Ctrl+Enter 및 JS form.requestSubmit() 폴백 1회 시도
        try:
            input_loc.press("Control+Enter")
        except Exception:
            pass
        try:
            input_loc.evaluate(
                "el => { const f = el.closest ? el.closest('form') : null; if (f && f.requestSubmit) f.requestSubmit(); }"
            )
        except Exception:
            pass
        if _poll_started(1.5):
            return

        self.emit_log(
            "WARN",
            f"[{self.provider_config['name']}] 프롬프트 전송 시작 확인 실패 (Enter/전송버튼/Ctrl+Enter 폴백 모두 미반영)",
        )
        raise RuntimeError("프롬프트 전송 버튼 클릭에 실패했습니다.")

    def _wait_for_upload_complete(self, page: Any, timeout_sec: float = 15.0) -> None:
        """첨부파일 업로드 스피너가 사라지고 전송 버튼이 활성화될 때까지 최대 timeout_sec초 대기합니다."""
        send_sel = self.provider_config.get("send") or SEND_BTN_SELECTORS
        spinner_sel = '[role="progressbar"], .loading, .spinner, [aria-busy="true"]'
        deadline = time.time() + timeout_sec
        time.sleep(0.5)

        while time.time() < deadline:
            if self.is_stop_requested():
                return
            spinner_active = False
            try:
                sp = page.locator(spinner_sel)
                if sp.count() > 0 and sp.first.is_visible():
                    spinner_active = True
            except Exception:
                spinner_active = False

            send_ready = True
            try:
                send_btn = page.locator(send_sel).first
                if send_btn.count() > 0 and send_btn.is_visible():
                    send_ready = bool(send_btn.is_enabled())
            except Exception:
                send_ready = True

            if not spinner_active and send_ready:
                time.sleep(0.4)
                return
            time.sleep(0.3)

    def _snapshot_attachment_state(self, page: Any, file_paths: List[str]) -> Dict[str, Any]:
        """
        입력창/컴포저 주변(aside, nav 사이드바 제외)의 현재 첨부 상태를 스냅샷으로 기록합니다.
        반환: {"chip_count": int, "present_names": set[str]}
        """
        basenames = [Path(p).name for p in (file_paths or []) if p]
        remove_btn_sel = (
            'button[aria-label*="제거"], '
            'button[aria-label*="삭제"], '
            'button[aria-label*="Remove"], '
            'button[aria-label*="Delete"], '
            '[data-testid*="attachment"]'
        )
        try:
            res = page.evaluate(
                """({ basenames, removeSel }) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
                        if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
                        const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
                        return Boolean(rect && rect.width > 0 && rect.height > 0);
                    };
                    const inSidebar = (el) => Boolean(el && el.closest && el.closest('aside, nav'));

                    let chipCount = 0;
                    const btns = document.querySelectorAll(removeSel);
                    for (const btn of btns) {
                        if (!inSidebar(btn) && isVisible(btn)) {
                            chipCount++;
                        }
                    }

                    const composerScopes = document.querySelectorAll(
                        'input-area, rich-textarea, form, [role="main"], main'
                    );
                    let scopeText = '';
                    if (composerScopes.length > 0) {
                        for (const sc of composerScopes) {
                            if (!inSidebar(sc)) {
                                scopeText += '\\n' + (sc.innerText || '');
                            }
                        }
                    } else if (document.body) {
                        const clone = document.body.cloneNode(true);
                        for (const sb of clone.querySelectorAll('aside, nav')) {
                            sb.remove();
                        }
                        scopeText = clone.innerText || '';
                    }

                    const present = [];
                    for (const name of basenames) {
                        if (name && scopeText.includes(name)) {
                            present.push(name);
                        }
                    }
                    return { chip_count: chipCount, present_names: present };
                }""",
                {"basenames": basenames, "removeSel": remove_btn_sel},
            )
            if isinstance(res, dict) and "chip_count" in res:
                return {
                    "chip_count": int(res.get("chip_count", 0) or 0),
                    "present_names": set(res.get("present_names") or []),
                }
        except Exception:
            pass

        # 단위 테스트 모킹 등 page.evaluate가 dict를 반환하지 않는 경우 Locator 기반 폴백
        chip_count = 0
        present_names: set[str] = set()
        try:
            rem = page.locator(remove_btn_sel)
            cnt = rem.count()
            for idx in range(min(cnt, 20)):
                cand = rem.nth(idx)
                try:
                    if cand.evaluate("el => Boolean(el.closest('aside, nav'))") is True:
                        continue
                    if cand.is_visible() is True:
                        chip_count += 1
                except Exception:
                    continue
        except Exception:
            pass

        for name in basenames:
            try:
                name_loc = page.get_by_text(name, exact=False)
                if name_loc.count() > 0:
                    first_loc = name_loc.first
                    if (
                        first_loc.evaluate("el => Boolean(el.closest('aside, nav'))") is not True
                        and first_loc.is_visible() is True
                    ):
                        present_names.add(name)
            except Exception:
                pass

        return {"chip_count": chip_count, "present_names": present_names}

    def _verify_attachment_appeared(
        self,
        page: Any,
        file_paths: List[str],
        timeout_sec: float = 6.0,
        baseline: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        첨부 시도 전/후를 비교해 실제로 컴포저 영역(aside, nav 제외)에 파일이 올라갔는지 확인합니다.
        - 파일명(basename) 텍스트가 새로 나타났거나
        - 컴포저 내부 첨부 미리보기/제거 버튼 개수가 사전 스냅샷보다 증가한 경우에만 True를 반환합니다.
        """
        basenames = [Path(p).name for p in (file_paths or []) if p]
        if not basenames:
            return False

        base_chips = int((baseline or {}).get("chip_count", 0) or 0)
        base_names = set((baseline or {}).get("present_names") or set())

        deadline = time.time() + timeout_sec
        while True:
            if self.is_stop_requested():
                return False

            cur = self._snapshot_attachment_state(page, file_paths)
            new_names = set(cur.get("present_names") or set()) - base_names
            if new_names:
                return True
            if int(cur.get("chip_count", 0) or 0) > base_chips:
                return True

            if time.time() >= deadline:
                break
            time.sleep(0.3)

        return False

    @staticmethod
    def _is_file_input_compatible(input_elem: Any, valid_paths: List[str]) -> bool:
        """
        첨부 파일 목록에 문서/PDF가 포함된 경우 accept="image/*" 전용 input은 건너뜁니다.
        """
        has_doc = any(
            Path(p).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
            for p in valid_paths
        )
        if not has_doc:
            return True

        try:
            accept_attr = input_elem.get_attribute("accept") or ""
        except Exception:
            accept_attr = ""

        accept_lower = accept_attr.strip().lower()
        if not accept_lower:
            return True
        accept_tokens = [t.strip() for t in accept_lower.split(",") if t.strip()]
        if not accept_tokens:
            return True
        if any(
            t in ("*", "*/*")
            or any(doc_tok in t for doc_tok in ("pdf", ".doc", ".hwp", ".txt", "application/"))
            for t in accept_tokens
        ):
            return True
        if all(
            t.startswith("image/")
            or t in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
            for t in accept_tokens
        ):
            return False
        return True

    def _attach_via_js_drop(self, page: Any, valid_paths: List[str]) -> bool:
        """
        2단계: 입력창 및 상위 컨테이너(form, [role="main"] 등)에 JS DataTransfer drag & drop을 디스패치합니다.
        """
        files_payload = []
        for p in valid_paths:
            try:
                raw_bytes = Path(p).read_bytes()
                mime, _ = mimetypes.guess_type(p)
                files_payload.append(
                    {
                        "name": Path(p).name,
                        "mime": mime or "application/octet-stream",
                        "b64": base64.b64encode(raw_bytes).decode("ascii"),
                    }
                )
            except Exception:
                continue

        if not files_payload:
            return False

        input_sel = self.provider_config.get("input") or GENERIC_INPUT_SELECTORS
        candidate_selectors = [
            s.strip() for s in str(input_sel).split(",") if s.strip()
        ] + ["rich-textarea", "[xap-file-drop]", "main", "form", '[role="main"]', "body"]

        js_script = """
        ({ filesPayload, selectors }) => {
            const dt = new DataTransfer();
            for (const f of filesPayload) {
                const binStr = atob(f.b64);
                const len = binStr.length;
                const bytes = new Uint8Array(len);
                for (let i = 0; i < len; i++) {
                    bytes[i] = binStr.charCodeAt(i);
                }
                const fileObj = new File([bytes], f.name, { type: f.mime });
                dt.items.add(fileObj);
            }
            const targetSet = new Set();
            const targets = [];
            const addTarget = (el) => {
                if (el && !targetSet.has(el)) {
                    targetSet.add(el);
                    targets.push(el);
                }
            };
            for (const sel of selectors) {
                try {
                    const el = document.querySelector(sel);
                    if (el) addTarget(el);
                } catch (e) {}
            }
            if (targets.length === 0) return false;

            const firstTarget = targets[0];
            let cur = firstTarget.parentElement;
            let depth = 0;
            while (cur && depth < 5) {
                const tag = (cur.tagName || '').toUpperCase();
                if (
                    tag === 'FORM' ||
                    tag === 'MAIN' ||
                    tag === 'RICH-TEXTAREA' ||
                    cur.getAttribute('role') === 'main' ||
                    cur.hasAttribute('xap-file-drop')
                ) {
                    addTarget(cur);
                }
                cur = cur.parentElement;
                depth += 1;
            }

            for (const t of targets) {
                for (const evtName of ['dragenter', 'dragover', 'drop']) {
                    const evt = new DragEvent(evtName, {
                        bubbles: true,
                        cancelable: true,
                        dataTransfer: dt
                    });
                    t.dispatchEvent(evt);
                }
            }
            return targets.length;
        }
        """
        try:
            dispatched = page.evaluate(
                js_script,
                {"filesPayload": files_payload, "selectors": candidate_selectors},
            )
            self.emit_log(
                "INFO",
                f"[첨부 진단] 2단계 JS DataTransfer drop 시도 결과: target_count={dispatched!r}",
            )
            return bool(dispatched)
        except Exception as exc:
            self.emit_log("INFO", f"[첨부 진단] 2단계 JS DataTransfer drop 예외: {exc}")
            return False

    def _attach_via_menu_or_button(self, page: Any, valid_paths: List[str]) -> bool:
        """
        3단계: + / 첨부 버튼을 클릭한 뒤
        - 0.6초 대기 후 DOM에 새 input[type="file"]이 생기면 set_input_files
        - 그렇지 않으면 .cdk-overlay-container 및 페이지 전체 visible 메뉴/버튼을 스캔해 로그로 남기고
          "파일", "업로드", "컴퓨터", "Upload", "File", "Computer" 항목을 expect_file_chooser로 클릭합니다.
        """
        exclude_keywords = ("옵션 더보기", "more options", "캘린더", "삭제", "고정")

        def _is_excluded_attach_btn(cand: Any, b_txt: str, b_aria: str) -> bool:
            combined_lbl = f"{b_txt} {b_aria}".lower()
            if any(kw in combined_lbl for kw in exclude_keywords):
                return True
            try:
                if cand.evaluate("el => Boolean(el.closest('aside, nav'))") is True:
                    return True
            except Exception:
                pass
            return False

        def _safe_inner_text(loc: Any) -> str:
            try:
                return str(loc.inner_text(timeout=500) or "").strip()
            except TypeError:
                return str(loc.inner_text() or "").strip()
            except Exception:
                return ""

        def _safe_attr(loc: Any, attr: str) -> str:
            try:
                return str(loc.get_attribute(attr, timeout=500) or "").strip()
            except TypeError:
                return str(loc.get_attribute(attr) or "").strip()
            except Exception:
                return ""

        attach_cfg_sel = self.provider_config.get("attach_btn") or ""
        universal_btn_sel = (
            'button[aria-label*="파일"], '
            'button[aria-label*="첨부"], '
            'button[aria-label*="업로드"], '
            'button[aria-label*="Attach"], '
            'button[aria-label*="Upload"], '
            '[data-testid*="attach"], '
            '[data-testid*="composer-plus"]'
        )
        if self.provider == "gemini":
            selector_stages = [
                'button[aria-label*="업로드 및 도구"], button[aria-label*="Upload"]',
                (
                    'uploader button, '
                    'button:has(mat-icon:text-is("add")), '
                    'button[aria-label*="업로드"], '
                    'button[aria-label*="파일"], '
                    'button[data-test-id*="upload"], '
                    'button.upload-card-button'
                ),
            ]
        else:
            fallback_sel = (
                f"{attach_cfg_sel}, {universal_btn_sel}" if attach_cfg_sel else universal_btn_sel
            )
            selector_stages = [fallback_sel]

        trigger_btn = None
        found_btn_logs: List[str] = []
        for stage_sel in selector_stages:
            if trigger_btn is not None:
                break
            try:
                btns = page.locator(stage_sel)
                cnt = btns.count()
                for idx in range(cnt):
                    cand = btns.nth(idx)
                    try:
                        b_txt = _safe_inner_text(cand)
                        b_aria = _safe_attr(cand, "aria-label")
                        b_tip = _safe_attr(cand, "mattooltip")
                        if _is_excluded_attach_btn(cand, b_txt, b_aria):
                            if len(found_btn_logs) < 15:
                                found_btn_logs.append(
                                    f"[제외:{idx}] text={b_txt!r}, aria={b_aria!r}"
                                )
                            continue
                        is_vis = cand.is_visible()
                        is_en = cand.is_enabled()
                        if len(found_btn_logs) < 15:
                            found_btn_logs.append(
                                f"[{idx}] text={b_txt!r}, aria={b_aria!r}, tip={b_tip!r}, vis={is_vis}, en={is_en}"
                            )
                        if trigger_btn is None and is_vis and is_en:
                            trigger_btn = cand
                            break
                    except Exception:
                        continue
            except Exception:
                continue

        if self.provider == "gemini" or found_btn_logs:
            self.emit_log(
                "INFO",
                f"[첨부 진단] 3단계 +/첨부 버튼 후보 탐색: {found_btn_logs}",
            )

        if trigger_btn is None:
            self.emit_log("INFO", "[첨부 진단] 3단계 +/첨부 버튼을 찾지 못했습니다.")
            return False

        # 먼저 기존 file_input 개수를 기록한 뒤 버튼 클릭
        before_count = 0
        try:
            before_count = page.locator('input[type="file"]').count()
        except Exception:
            before_count = 0

        # 일부 UI는 1차 버튼 클릭만으로 곧바로 file chooser를 열기도 함
        try:
            with page.expect_file_chooser(timeout=1200) as fc_direct:
                trigger_btn.click()
            fc_direct.value.set_files(valid_paths)
            return True
        except Exception:
            # 팝업 메뉴가 열렸거나 DOM에 동적 input이 생성된 경우
            pass

        time.sleep(0.6)

        # 3-A: DOM에 새 input[type="file"]이 생성되었는지 확인
        try:
            all_inputs = page.locator('input[type="file"]')
            after_count = all_inputs.count()
            if after_count > before_count:
                self.emit_log(
                    "INFO",
                    f"[첨부 진단] 3단계 클릭 후 새 input[type='file'] 생성 감지 ({before_count} -> {after_count})",
                )
                for idx in range(after_count - 1, -1, -1):
                    inp = all_inputs.nth(idx)
                    if self._is_file_input_compatible(inp, valid_paths):
                        inp.set_input_files(valid_paths)
                        return True
        except Exception:
            pass

        # 3-B-1: Gemini 전용 div.cdk-overlay-container 짧은 폴링(최대 2초, 0.2초 간격) + expect_file_chooser
        if self.provider == "gemini":
            cdk_sel = (
                'div.cdk-overlay-container [role="menuitem"], '
                'div.cdk-overlay-container button, '
                'div.cdk-overlay-container mat-list-item, '
                'div.cdk-overlay-container li'
            )
            gemini_upload_keywords = ("파일", "업로드", "컴퓨터", "upload", "file")
            cdk_logs: List[str] = []
            cdk_target = None
            cdk_cnt = 0
            try:
                poll_deadline = time.time() + 2.0
                while True:
                    cdk_logs = []
                    cdk_target = None
                    cdk_items = page.locator(cdk_sel)
                    cdk_cnt = cdk_items.count()
                    for idx in range(cdk_cnt):
                        item = cdk_items.nth(idx)
                        try:
                            is_vis = item.is_visible()
                            txt = _safe_inner_text(item)
                            aria = _safe_attr(item, "aria-label")
                            cdk_logs.append(f"[{idx}] text={txt!r}, aria-label={aria!r}, vis={is_vis}")
                            if not is_vis:
                                continue
                            combined = f"{txt} {aria}".lower()
                            if cdk_target is None and any(kw in combined for kw in gemini_upload_keywords):
                                cdk_target = item
                        except Exception:
                            continue
                    if cdk_target is not None or time.time() >= poll_deadline:
                        break
                    time.sleep(0.2)

                self.emit_log(
                    "INFO",
                    f"[첨부 진단] div.cdk-overlay-container 내부 항목 ({cdk_cnt}개): {cdk_logs}",
                )
                if cdk_target is not None:
                    with page.expect_file_chooser(timeout=6000) as fc_info:
                        cdk_target.click()
                    fc_info.value.set_files(valid_paths)
                    return True
            except Exception as exc:
                self.emit_log("INFO", f"[첨부 진단] div.cdk-overlay-container 클릭 실패: {exc}")

        # 3-B-2: .cdk-overlay-container 및 페이지 전체 visible 메뉴/버튼 스캔 + expect_file_chooser
        menu_item_sel = (
            '.cdk-overlay-container [role="menuitem"], '
            '.cdk-overlay-container button, '
            '.cdk-overlay-container mat-list-item, '
            '[role="menu"] [role="menuitem"], '
            '[role="menuitemradio"], '
            '[role="menu"] button, '
            '[role="dialog"] button, '
            '[role="listbox"] [role="option"], '
            'mat-list-item, '
            '[role="menuitem"], '
            'button, '
            'div[role="button"]'
        )
        upload_keywords = (
            "파일",
            "업로드",
            "컴퓨터",
            "사진",
            "upload",
            "file",
            "computer",
            "add photos",
        )
        scanned_labels: List[str] = []
        try:
            items = page.locator(menu_item_sel)
            cnt = items.count()
            target_item = None
            for idx in range(cnt):
                item = items.nth(idx)
                try:
                    if not item.is_visible():
                        continue
                    txt = (item.inner_text() or "").strip()
                    aria = (item.get_attribute("aria-label") or "").strip()
                    label_repr = txt or aria
                    if label_repr and label_repr not in scanned_labels and len(scanned_labels) < 25:
                        scanned_labels.append(label_repr)
                    combined_label = f"{txt} {aria}".lower()
                    if target_item is None and any(kw in combined_label for kw in upload_keywords):
                        target_item = item
                except Exception:
                    continue

            self.emit_log(
                "INFO",
                f"[첨부 진단] 3단계 오버레이/메뉴 스캔 결과 ({len(scanned_labels)}개): {scanned_labels}",
            )

            if target_item is not None:
                with page.expect_file_chooser(timeout=6000) as fc_info:
                    target_item.click()
                fc_info.value.set_files(valid_paths)
                return True
        except Exception as exc:
            self.emit_log("INFO", f"[첨부 진단] 3단계 오버레이 메뉴 클릭 실패: {exc}")

        return False

    def _attach_files(self, page: Any, file_paths: List[str]) -> bool:
        """
        모든 외부 LLM 호환 3단계 범용 파일 첨부 파이프라인:
        1) 호환되는 <input type="file"> 직접 주입 (image/* 전용 input 건너뜀)
        2) 범용 JS Drag & Drop (DataTransfer) 시뮬레이션
        3) + / 첨부 버튼 클릭 -> 동적 input 또는 2단계 팝업 메뉴(expect_file_chooser)
        각 단계 후 _verify_attachment_appeared()가 True면 즉시 완료,
        모두 실패하면 경고 로그를 남기고 안전하게 진행합니다.
        """
        valid_paths = [
            str(Path(p).resolve())
            for p in (file_paths or [])
            if p and Path(p).is_file()
        ]
        if not valid_paths:
            return True

        file_input_sel = self.provider_config.get("file_input") or 'input[type="file"]'

        # ── 1단계: 호환되는 <input type="file"> 직접 주입 ─────────────────────
        try:
            file_inputs = page.locator(file_input_sel)
            cnt = file_inputs.count()
            self.emit_log("INFO", f"[첨부 진단] 1단계 input[type='file'] 탐지: {cnt}개")
            for idx in range(cnt):
                inp = file_inputs.nth(idx)
                try:
                    accept_val = inp.get_attribute("accept") or "(없음)"
                except Exception:
                    accept_val = "(조회 실패)"
                self.emit_log("INFO", f"[첨부 진단] 1단계 input[{idx}] accept={accept_val!r}")
                if not self._is_file_input_compatible(inp, valid_paths):
                    self.emit_log(
                        "INFO",
                        f"[첨부 진단] 1단계 input[{idx}] 이미지 전용(accept={accept_val!r})이므로 건너뜀",
                    )
                    continue
                try:
                    baseline = self._snapshot_attachment_state(page, valid_paths)
                    inp.set_input_files(valid_paths)
                    if self._verify_attachment_appeared(
                        page, valid_paths, timeout_sec=6.0, baseline=baseline
                    ):
                        self.emit_log(
                            "INFO",
                            f"[{self.provider_config['name']}] 첨부파일 {len(valid_paths)}개 업로드 중... (최대 15초 대기)",
                        )
                        self._wait_for_upload_complete(page, timeout_sec=15.0)
                        return True
                    self.emit_log(
                        "INFO",
                        f"[첨부 진단] 1단계 input[{idx}] 주입 후 UI 첨부 확인 실패",
                    )
                except Exception as exc:
                    self.emit_log("INFO", f"[첨부 진단] 1단계 input[{idx}] 주입 예외: {exc}")
                    continue
        except Exception as exc:
            self.emit_log("INFO", f"[첨부 진단] 1단계 탐색 예외: {exc}")

        # ── 2단계: 범용 JS Drag & Drop (DataTransfer) 시뮬레이션 ───────────────
        try:
            baseline = self._snapshot_attachment_state(page, valid_paths)
            if self._attach_via_js_drop(page, valid_paths):
                drop_verify_sec = 2.0 if self.provider == "gemini" else 6.0
                if self._verify_attachment_appeared(
                    page, valid_paths, timeout_sec=drop_verify_sec, baseline=baseline
                ):
                    self.emit_log(
                        "INFO",
                        f"[{self.provider_config['name']}] 첨부파일 {len(valid_paths)}개(Drag&Drop) 업로드 중... (최대 15초 대기)",
                    )
                    self._wait_for_upload_complete(page, timeout_sec=15.0)
                    return True
                self.emit_log(
                    "INFO",
                    "[첨부 진단] 2단계 Drag&Drop 후 컴포저 첨부 미확인 -> 3단계 버튼/메뉴 첨부로 진행",
                )
        except Exception:
            pass

        # ── 3단계: + / 첨부 버튼 클릭 -> 동적 input 또는 2단계 팝업 메뉴 ───────
        try:
            baseline = self._snapshot_attachment_state(page, valid_paths)
            if self._attach_via_menu_or_button(page, valid_paths):
                if self._verify_attachment_appeared(
                    page, valid_paths, timeout_sec=6.0, baseline=baseline
                ):
                    self.emit_log(
                        "INFO",
                        f"[{self.provider_config['name']}] 첨부파일 {len(valid_paths)}개 업로드 중... (최대 15초 대기)",
                    )
                    self._wait_for_upload_complete(page, timeout_sec=15.0)
                    return True
        except Exception:
            pass

        self.emit_log(
            "WARN",
            "자동 파일 첨부에 실패했습니다. 열린 브라우저 창에 파일을 직접 드래그해 첨부해 주세요.",
        )
        return False

    def _fill_and_submit_prompt(
        self,
        page: Any,
        input_loc: Any,
        prompt_text: str,
        pre_response_count: Optional[int] = None,
    ) -> None:
        """프롬프트를 안전하게 주입하고 전송합니다."""
        self._inject_prompt_text(page, input_loc, prompt_text)
        time.sleep(0.6)
        self._submit_prompt(page, input_loc, pre_response_count=pre_response_count)

    def _is_user_turn_element(self, loc: Any) -> bool:
        """ChatGPT 폴백 셀렉터 매칭 요소가 사용자 메시지인지 확인합니다."""
        try:
            res = loc.evaluate(
                'e => Boolean(e.closest && e.closest(\'[data-message-author-role="user"]\'))'
            )
            return res is True
        except Exception:
            return False

    def _read_latest_response_text(self, page: Any, min_index: int = 0) -> str:
        """마지막 응답 요소의 전체 텍스트를 읽어옵니다."""
        resp_sel = self.provider_config.get("response") or GENERIC_RESPONSE_SELECTORS
        try:
            responses = page.locator(resp_sel)
            count = int(responses.count())
            if count <= min_index:
                return ""
            for idx in range(count - 1, min_index - 1, -1):
                cand = responses.nth(idx)
                if self.provider == "chatgpt" and self._is_user_turn_element(cand):
                    continue
                txt = (cand.inner_text() or "").strip()
                if txt:
                    return txt
            return ""
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

    def _is_done_action_visible(self, page: Any, resp_sel: str) -> bool:
        """마지막 어시스턴트 응답 내부 또는 직계 턴 컨테이너의 완료 액션(복사 등) 버튼만 확인합니다."""
        done_sel = (
            self.provider_config.get("done_action")
            or 'button[aria-label*="Copy"], button[aria-label*="복사"]'
        )
        try:
            responses = page.locator(resp_sel)
            cnt = int(responses.count())
            if cnt <= 0:
                return False
            last_msg = None
            for idx in range(cnt - 1, -1, -1):
                cand = responses.nth(idx)
                if self.provider == "chatgpt" and self._is_user_turn_element(cand):
                    continue
                last_msg = cand
                break
            if last_msg is None:
                return False

            msg_done = last_msg.locator(done_sel)
            if int(msg_done.count()) > 0 and msg_done.last.is_visible() is True:
                return True

            for container_xpath in ("xpath=ancestor::article[1]", "xpath=.."):
                try:
                    container = last_msg.locator(container_xpath)
                    if int(container.count()) > 0:
                        c_done = container.first.locator(done_sel)
                        if int(c_done.count()) > 0 and c_done.last.is_visible() is True:
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _is_incomplete_json_response(self, text: str) -> bool:
        """JSON 코드블록이 닫히지 않았거나 중괄호/대괄호가 미완성인 상태인지 판별합니다."""
        stripped = (text or "").strip()
        if not stripped:
            return False
        if "```json" in stripped:
            last_open = stripped.rfind("```json")
            if stripped.find("```", last_open + 7) == -1:
                return True
        if stripped.startswith("{") and not stripped.endswith("}"):
            return True
        if stripped.startswith("[") and not stripped.endswith("]"):
            return True
        return False

    def _is_chatgpt_copy_btn_visible(self, page: Any, resp_sel: str) -> bool:
        """ChatGPT 마지막 어시스턴트 응답 또는 액션 바에 복사 버튼이 표시되었는지 확인합니다."""
        return self._is_done_action_visible(page, resp_sel)

    def _wait_for_response_complete(
        self, page: Any, pre_response_count: int, timeout_sec: float = 180.0
    ) -> str:
        """응답 완료 대기 (5/6 단계 상태 머신 위임)."""
        return self._step_wait_response_complete(
            page, pre_response_count=pre_response_count, timeout_sec=timeout_sec
        )

    def _clear_composer_draft(self, page: Any, input_loc: Any) -> None:
        """브라우저 프로필이 복원한 미전송 초안(잔여 텍스트 및 잔여 첨부파일)을 깨끗이 비웁니다."""
        # 1) 입력창 잔여 텍스트 비우기
        if input_loc is not None:
            try:
                input_loc.click()
            except Exception:
                pass
            try:
                input_loc.press("Control+A")
                input_loc.press("Backspace")
            except Exception:
                pass

        # 2) 컴포저 내부 잔여 첨부파일 삭제 버튼 클릭 (aside, nav 사이드바 제외)
        remove_sel = (
            'button[aria-label*="Remove"], '
            'button[aria-label*="제거"], '
            'button[aria-label*="삭제"], '
            'button[data-testid*="remove"]'
        )
        for _ in range(10):
            clicked_any = False
            try:
                btns = page.locator(remove_sel)
                cnt = int(btns.count())
                for idx in range(cnt):
                    cand = btns.nth(idx)
                    try:
                        in_sidebar = cand.evaluate(
                            "e => Boolean(e.closest && e.closest('aside, nav'))"
                        )
                        if in_sidebar is True:
                            continue
                        if cand.is_visible() is True and (
                            not hasattr(cand, "is_enabled") or cand.is_enabled() is not False
                        ):
                            cand.click()
                            clicked_any = True
                            time.sleep(0.15)
                            break
                    except Exception:
                        continue
            except Exception:
                break
            if not clicked_any:
                break

    def _step_ensure_ready(self, page: Any, is_first_batch: bool = False) -> tuple[Any, int]:
        """[1/6 페이지 준비] 입력창 가시성/활성화 및 임시 채팅 전환 후 준비 상태를 검증합니다."""
        provider_name = self.provider_config["name"]
        resp_sel = self.provider_config.get("response") or GENERIC_RESPONSE_SELECTORS
        self.emit_log("INFO", f"[{provider_name}] [1/6 페이지 준비] 입력창 및 대화 상태 확인 중...")

        input_loc = self._open_new_chat(
            page, self.provider, self.use_temp_chat, is_first_batch=is_first_batch
        )
        if input_loc is None or self.is_stop_requested():
            return None, 0

        input_loc = self._ensure_ready_before_input(page, input_loc)
        if input_loc is None or self.is_stop_requested():
            return None, 0

        try:
            if hasattr(input_loc, "is_visible") and input_loc.is_visible() is False:
                raise RuntimeError("프롬프트 입력창이 화면에 표시되지 않았습니다.")
            if hasattr(input_loc, "is_enabled") and input_loc.is_enabled() is False:
                raise RuntimeError("프롬프트 입력창이 비활성화 상태입니다.")
        except RuntimeError:
            raise
        except Exception:
            pass

        try:
            pre_count = int(page.locator(resp_sel).count())
        except Exception:
            pre_count = 0

        self._clear_composer_draft(page, input_loc)
        return input_loc, pre_count

    def _step_attach_and_wait(self, page: Any, file_paths: List[str]) -> bool:
        """[2/6 파일 첨부] 파일 첨부 후 업로드 스피너 소멸 및 전송 가능 상태를 최대 20초 대기합니다."""
        provider_name = self.provider_config["name"]
        if not file_paths:
            self.emit_log("INFO", f"[{provider_name}] [2/6 파일 첨부] 첨부 파일 없음 (건너뜀)")
            return True

        self.emit_log(
            "INFO",
            f"[{provider_name}] [2/6 파일 첨부] {len(file_paths)}개 파일 첨부 및 업로드 완료 대기 중...",
        )
        self._attach_files(page, file_paths)

        spinner_sel = '[role="progressbar"], [aria-busy="true"], .uploading, [data-testid*="uploading"]'
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return False
            spinner_visible = False
            try:
                sp = page.locator(spinner_sel)
                if int(sp.count()) > 0 and sp.first.is_visible() is True:
                    spinner_visible = True
            except Exception:
                spinner_visible = False
            if not spinner_visible:
                return True
            time.sleep(0.3)

        raise RuntimeError(f"[{provider_name}] 파일 업로드가 20초 내에 완료되지 않았습니다.")

    def _step_fill_and_verify(self, page: Any, input_loc: Any, prompt_text: str) -> bool:
        """[3/6 프롬프트 입력] 프롬프트를 입력하고 입력창 내 텍스트 존재를 최대 3초 검증합니다."""
        provider_name = self.provider_config["name"]
        self.emit_log("INFO", f"[{provider_name}] [3/6 프롬프트 입력] 프롬프트 주입 및 입력 검증 중...")
        if len(self._read_input_box_text(input_loc).strip()) > 0:
            try:
                input_loc.click()
            except Exception:
                pass
            try:
                input_loc.press("Control+A")
                input_loc.press("Backspace")
            except Exception:
                pass
        self._inject_prompt_text(page, input_loc, prompt_text)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return False
            if len(self._read_input_box_text(input_loc).strip()) > 0:
                time.sleep(0.4)
                return True
            time.sleep(0.2)

        raise RuntimeError(f"[{provider_name}] 프롬프트 입력 검증 실패: 입력창이 비어 있습니다.")

    def _step_submit_and_confirm(
        self, page: Any, input_loc: Any, pre_response_count: int
    ) -> bool:
        """[4/6 전송 확인] Enter -> 전송 버튼 -> Ctrl+Enter/requestSubmit 순으로 전송하고 시작을 확인합니다."""
        provider_name = self.provider_config["name"]
        self.emit_log("INFO", f"[{provider_name}] [4/6 전송 확인] 프롬프트 전송 및 시작 확인 중...")
        self._submit_prompt(page, input_loc, pre_response_count=pre_response_count)
        if self.is_stop_requested():
            return False
        return True

    def _step_wait_response_complete(
        self, page: Any, pre_response_count: int, timeout_sec: float = 180.0
    ) -> str:
        """
        [5/6 응답 대기] stop 버튼 소멸 + 신규 응답 존재 + (Claude 스트리밍 종료) +
        done_action 표시 시 1.0초 / 미표시 시 2.5초 텍스트 고정으로 응답 완료를 판정합니다.
        """
        provider_name = self.provider_config["name"]
        self.emit_log("INFO", f"[{provider_name}] [5/6 응답 대기] AI 응답 생성 완료 대기 중...")
        stop_sel = self.provider_config["stop"]
        resp_sel = self.provider_config.get("response") or GENERIC_RESPONSE_SELECTORS
        deadline = time.monotonic() + timeout_sec
        saw_stop_button = False
        last_text = ""
        last_change_ts = time.monotonic()
        last_log_ts = time.monotonic()
        fallback_probed = False
        matched_fallback_sel: Optional[str] = None
        json_grace_start: Optional[float] = None

        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return ""

            stop_visible = False
            try:
                stop_loc = page.locator(stop_sel).first
                stop_visible = stop_loc.count() > 0 and stop_loc.is_visible() is True
            except Exception:
                stop_visible = False

            try:
                resp_count = int(page.locator(resp_sel).count())
            except Exception:
                resp_count = 0

            is_streaming = False
            if self.provider == "claude":
                try:
                    if int(page.locator('div[data-is-streaming="true"]').count()) > 0:
                        is_streaming = True
                        stop_visible = True
                except Exception:
                    pass
                if not is_streaming and resp_count > pre_response_count:
                    try:
                        streaming_attr = (
                            page.locator(resp_sel).nth(resp_count - 1).get_attribute("data-is-streaming")
                        )
                        if str(streaming_attr).lower() == "true":
                            is_streaming = True
                            stop_visible = True
                    except Exception:
                        pass

            curr_text = ""
            if stop_visible or is_streaming:
                saw_stop_button = True
                last_change_ts = time.monotonic()
            else:
                curr_text = self._read_latest_response_text(page, min_index=pre_response_count)
                if (
                    not curr_text
                    and self.provider == "chatgpt"
                    and resp_count <= pre_response_count
                    and (not fallback_probed or matched_fallback_sel)
                ):
                    if matched_fallback_sel:
                        try:
                            fb_loc = page.locator(matched_fallback_sel)
                            fb_cnt = int(fb_loc.count())
                            if fb_cnt > 0:
                                cand = fb_loc.nth(fb_cnt - 1)
                                if not self._is_user_turn_element(cand):
                                    curr_text = (cand.inner_text() or "").strip()
                        except Exception:
                            pass
                    elif not fallback_probed:
                        has_done_btn = self._is_done_action_visible(page, resp_sel)
                        if not has_done_btn and resp_count == 0:
                            try:
                                done_sel = (
                                    self.provider_config.get("done_action")
                                    or 'button[aria-label*="Copy"], button[aria-label*="복사"]'
                                )
                                p_done = page.locator(done_sel)
                                has_done_btn = int(p_done.count()) > 0 and p_done.last.is_visible() is True
                            except Exception:
                                has_done_btn = False
                        if has_done_btn:
                            fallback_probed = True
                            for fb_sel in (
                                ".markdown",
                                "div.markdown.prose",
                                "pre code",
                                'article[data-turn="assistant"]',
                            ):
                                try:
                                    fb_loc = page.locator(fb_sel)
                                    fb_cnt = int(fb_loc.count())
                                    if fb_cnt <= 0:
                                        continue
                                    cand = fb_loc.nth(fb_cnt - 1)
                                    if self._is_user_turn_element(cand):
                                        continue
                                    fb_text = (cand.inner_text() or "").strip()
                                    if fb_text:
                                        matched_fallback_sel = fb_sel
                                        curr_text = fb_text
                                        self.emit_log(
                                            "INFO",
                                            f"[{provider_name}] resp={resp_count} 폴백 탐색 매칭: selector={fb_sel}, len={len(fb_text)}",
                                        )
                                        break
                                except Exception:
                                    continue

                active_resp_sel = matched_fallback_sel or resp_sel
                done_visible = bool(curr_text) and (
                    self._is_chatgpt_copy_btn_visible(page, active_resp_sel)
                    if self.provider == "chatgpt"
                    else self._is_done_action_visible(page, active_resp_sel)
                )
                required_stable_sec = 1.0 if done_visible else 2.5

                if curr_text != last_text:
                    last_text = curr_text
                    last_change_ts = time.monotonic()
                    json_grace_start = None
                elif curr_text and (time.monotonic() - last_change_ts) >= required_stable_sec:
                    if self._is_incomplete_json_response(curr_text):
                        if json_grace_start is None:
                            json_grace_start = time.monotonic()
                        if (time.monotonic() - json_grace_start) < 10.0:
                            time.sleep(0.5)
                            continue
                    return curr_text

            if saw_stop_button and not stop_visible and not is_streaming:
                if not curr_text:
                    curr_text = self._read_latest_response_text(page, min_index=pre_response_count)
                done_visible = bool(curr_text) and (
                    self._is_chatgpt_copy_btn_visible(page, resp_sel)
                    if self.provider == "chatgpt"
                    else self._is_done_action_visible(page, resp_sel)
                )
                required_stable_sec = 1.0 if done_visible else 2.5
                if (
                    curr_text
                    and curr_text == last_text
                    and (time.monotonic() - last_change_ts) >= required_stable_sec
                ):
                    if self._is_incomplete_json_response(curr_text):
                        if json_grace_start is None:
                            json_grace_start = time.monotonic()
                        if (time.monotonic() - json_grace_start) < 10.0:
                            time.sleep(0.5)
                            continue
                    return curr_text

            now = time.monotonic()
            if (now - last_log_ts) >= 15.0:
                last_log_ts = now
                self.emit_log(
                    "INFO",
                    f"[{provider_name}] 단계 대기 중: stop={stop_visible}, resp={resp_count}, len={len(curr_text)}",
                )

            time.sleep(0.5)

        if last_text:
            return last_text
        raise TimeoutError(
            f"[{provider_name}] 응답 생성 대기 시간이 초과되었습니다 (180초)."
        )

    def _step_collect_response(self, raw_reply: str) -> str:
        """[6/6 응답 회수] 회수된 응답 텍스트가 비어 있지 않은지 검증하고 반환합니다."""
        provider_name = self.provider_config["name"]
        self.emit_log("INFO", f"[{provider_name}] [6/6 응답 회수] 응답 텍스트 유효성 검증 중...")
        cleaned = (raw_reply or "").strip()
        if not cleaned:
            raise RuntimeError(f"[{provider_name}] 회수된 AI 응답이 비어 있습니다.")
        return cleaned

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

        input_loc = self._ensure_ready_before_input(page, input_loc)
        if input_loc is None or self.is_stop_requested():
            return None

        batch_attachments = batch.get("attachment_paths") or self.attachment_paths
        if batch_attachments:
            self._attach_files(page, batch_attachments)

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
                stu_info = self.task_label if is_single else f"{len(students)}명"
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
                        # [1/6 페이지 준비]
                        input_loc, pre_count = self._step_ensure_ready(
                            page, is_first_batch=(b_idx == 1 and attempt == 1)
                        )
                        if input_loc is None or self.is_stop_requested():
                            break

                        # [2/6 파일 첨부]
                        batch_attachments = batch.get("attachment_paths") or self.attachment_paths
                        if not self._step_attach_and_wait(page, batch_attachments):
                            if self.is_stop_requested():
                                break
                            raise RuntimeError("파일 첨부 단계 검증에 실패했습니다.")

                        # [3/6 프롬프트 입력]
                        if not self._step_fill_and_verify(page, input_loc, batch["final_prompt"]):
                            if self.is_stop_requested():
                                break
                            raise RuntimeError("프롬프트 입력 단계 검증에 실패했습니다.")

                        # [4/6 전송 확인]
                        if not self._step_submit_and_confirm(
                            page, input_loc, pre_response_count=pre_count
                        ):
                            if self.is_stop_requested():
                                break
                            raise RuntimeError("프롬프트 전송 확인 단계에 실패했습니다.")

                        # [5/6 응답 대기]
                        raw_reply = self._step_wait_response_complete(
                            page, pre_response_count=pre_count, timeout_sec=180.0
                        )
                        if self.is_stop_requested():
                            break

                        # [6/6 응답 회수]
                        raw_reply = self._step_collect_response(raw_reply)

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
