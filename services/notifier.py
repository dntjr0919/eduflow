"""
외부 알림 및 단체 카카오톡 연동 서비스 레이어 (services/notifier.py).
- 새 구글 시트 응답 감지, 대량 파일 분류 완료, AI OCR 완료 시 알림 발송
- 1) Windows 데스크톱 시스템 트레이 토스트 알림
- 2) PC 카카오톡 창 제어 매크로 방식 (Win32 API 연동 인터페이스)
- 3) 카카오워크 / 웹훅(Webhook) / REST API 방식
확장 가능한 추상 클래스(BaseNotificationChannel) 구조로 설계되어 있습니다.
"""

import abc
import json
import urllib.request
from typing import Callable, List, Optional
from core.config_manager import ConfigManager
from core.database import DatabaseManager


class BaseNotificationChannel(abc.ABC):
    """알림 채널 추상 기본 클래스."""

    @property
    @abc.abstractmethod
    def channel_name(self) -> str:
        """채널 식별 이름."""

    @abc.abstractmethod
    def send(self, title: str, message: str) -> bool:
        """알림을 전송하고 성공 여부를 반환합니다."""


class DesktopToastChannel(BaseNotificationChannel):
    """Windows 데스크톱 시스템 트레이 토스트 알림 채널."""

    def __init__(self, tray_callback: Optional[Callable[[str, str], None]] = None):
        self._tray_callback = tray_callback

    @property
    def channel_name(self) -> str:
        return "Windows 데스크톱 토스트"

    def set_tray_callback(self, callback: Callable[[str, str], None]) -> None:
        self._tray_callback = callback

    def send(self, title: str, message: str) -> bool:
        if self._tray_callback is not None:
            try:
                self._tray_callback(title, message)
                return True
            except Exception as e:
                print(f"[DesktopToastChannel] 트레이 알림 오류: {e}")
                return False
        return False


class KakaoPcMacroChannel(BaseNotificationChannel):
    """
    PC 카카오톡 열려 있는 채팅방 핸들(HWND)을 찾아 메시지를 전송하는 매크로 채널.
    """

    def __init__(self, target_room_name: str = ""):
        self.target_room_name = target_room_name

    @property
    def channel_name(self) -> str:
        return "PC 카카오톡 매크로"

    def send(self, title: str, message: str) -> bool:
        room = self.target_room_name.strip()
        if not room:
            raise ValueError("PC 카카오톡 대상 채팅방 이름이 설정되지 않았습니다.")

        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, room)
            if not hwnd:
                raise RuntimeError(
                    f"열려 있는 카카오톡 채팅방('{room}') 창을 찾을 수 없습니다. "
                    "채팅방을 독립 창으로 열어두세요."
                )
            return True
        except ImportError:
            return False


class KakaoWebhookChannel(BaseNotificationChannel):
    """카카오워크 / 사내 메신저 / Slack / Discord 호환 Webhook 채널."""

    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url

    @property
    def channel_name(self) -> str:
        return "웹훅(Webhook) / 카카오 API"

    def send(self, title: str, message: str) -> bool:
        url = self.webhook_url.strip()
        if not url:
            raise ValueError("웹훅(Webhook) URL이 설정되지 않았습니다.")

        payload = {
            "text": f"🔔 *[{title}]*\n{message}",
            "content": f"🔔 **[{title}]**\n{message}",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300


class NotificationService:
    """
    전역 알림 통합 서비스 (싱글톤).
    환경설정(ConfigManager)에 따라 데스크톱 토스트 및 카카오톡/웹훅 채널로 알림을 발송합니다.
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self.config = ConfigManager()
        self.db = DatabaseManager()
        self.toast_channel = DesktopToastChannel()
        self._initialized = True

    def register_tray_notifier(self, callback: Callable[[str, str], None]) -> None:
        self.toast_channel.set_tray_callback(callback)

    def notify(self, title: str, message: str, force_external: bool = False) -> List[str]:
        results: List[str] = []

        if self.config.get("system", "show_tray_notifications", True):
            ok = self.toast_channel.send(title, message)
            if ok:
                results.append("데스크톱 토스트 발송 완료")

        mode = self.config.get("api_and_integrations", "kakao_notify_mode", "toast_only")
        if force_external or mode in ("pc_macro", "webhook"):
            try:
                if mode == "pc_macro":
                    room = self.config.get("api_and_integrations", "kakao_target_room_name", "")
                    macro_ch = KakaoPcMacroChannel(target_room_name=room)
                    macro_ch.send(title, message)
                    results.append(f"PC 카카오톡('{room}') 전송 완료")
                elif mode == "webhook":
                    url = self.config.get("api_and_integrations", "kakao_webhook_url", "")
                    webhook_ch = KakaoWebhookChannel(webhook_url=url)
                    webhook_ch.send(title, message)
                    results.append("웹훅(Webhook) 알림 전송 완료")
            except Exception as exc:
                err_msg = f"외부 알림({mode}) 전송 실패: {exc}"
                results.append(err_msg)
                self.db.add_log("WARN", "NotificationService", err_msg)

        self.db.add_log("INFO", "NotificationService", f"[{title}] {message} -> {', '.join(results) or '로그 기록됨'}")
        return results
