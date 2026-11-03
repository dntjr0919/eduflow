"""
애플리케이션 전역 설정(config.json)을 관리하는 모듈.
사용자가 지정한 폴더 경로, 멀티 AI 공급자(Gemini, OpenAI, Claude, Custom/Ollama),
API 키, Base URL, 수동 프롬프트 모드 여부, 백그라운드 감시 주기, 시스템 트레이 옵션 등을
자동으로 저장하고 불러옵니다.
"""

import copy
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List
from utils.path_helper import get_data_path


DEFAULT_CONFIG: Dict[str, Any] = {
    "file_sorter": {
        "source_dirs": [
            str(Path.home() / "Downloads")
        ],
        "target_base_dir": str(Path.home() / "Documents" / "교무업무_자동분류함"),
        "action_mode": "move",            # "move" (이동) 또는 "copy" (복사)
        "duplicate_policy": "number",     # "number" (파일명_(1).ext) 또는 "date" (파일명_YYYYMMDD_HHMMSS.ext)
        "include_unmatched": False,       # 규칙 미일치 파일을 '기타_미분류' 폴더로 이동할지 여부
        "unmatched_folder_name": "99_기타_미분류",
        "watch_interval_sec": 3,          # 폴링 감시 주기 (초)
        "auto_watch_on_startup": False,   # 시작 시 자동 감시 활성화 여부
        "ignore_temp_files": True         # .crdownload, .part, ~$ 등 임시 파일 무시
    },
    "api_and_integrations": {
        # 멀티 LLM 공급자 및 수동 프롬프트 모드 설정
        "use_manual_prompt_mode": True,   # 기본값: API 키 없이 수동 프롬프트 복사/붙여넣기 모드 사용
        "ai_provider": "gemini",          # "gemini", "openai", "claude", "custom"
        "gemini_api_key": "",
        "gemini_model": "gemini-2.5-flash",
        "openai_api_key": "",
        "openai_model": "gpt-4o",
        "claude_api_key": "",
        "claude_model": "claude-3-7-sonnet-latest",
        "custom_base_url": "http://localhost:11434/v1",
        "custom_api_key": "",
        "custom_model": "llama3.2",
        # Google Sheets & 카카오톡 외부 연동 설정
        "google_credentials_path": "",
        "google_sheet_url": "",
        "google_sheet_poll_interval_sec": 30,
        "kakao_notify_mode": "toast_only",  # "toast_only", "pc_macro", "webhook"
        "kakao_target_room_name": "",
        "kakao_webhook_url": ""
    },
    "system": {
        "minimize_to_tray_on_close": True,
        "show_tray_notifications": True,
        "confirm_before_exit": False,
        "log_retention_days": 30
    }
}


class ConfigManager:
    """싱글톤 패턴 기반의 JSON 설정 매니저."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config_filename: str = "config.json"):
        if getattr(self, "_initialized", False):
            return
        self.config_path: str = get_data_path(config_filename, ensure_parent=True)
        self._config: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self.load()
        self._initialized = True

    def load(self) -> Dict[str, Any]:
        """디스크에서 config.json을 읽어 기본 설정과 병합합니다."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._deep_merge(self._config, loaded)
            except Exception as e:
                print(f"[ConfigManager] 설정 파일 로드 오류(기본값 사용): {e}")
        else:
            self.save()
        return self._config

    def save(self) -> bool:
        """현재 설정을 config.json 파일에 저장하고 리스너에게 알립니다."""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=4)
            self._notify_listeners()
            return True
        except Exception as e:
            print(f"[ConfigManager] 설정 파일 저장 실패: {e}")
            return False

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """특정 섹션과 키의 설정값을 반환합니다."""
        sec = self._config.get(section, {})
        if isinstance(sec, dict):
            return sec.get(key, default)
        return default

    def set(self, section: str, key: str, value: Any, auto_save: bool = True) -> None:
        """특정 섹션과 키의 설정값을 변경하고 필요 시 즉시 저장합니다."""
        if section not in self._config or not isinstance(self._config[section], dict):
            self._config[section] = {}
        self._config[section][key] = value
        if auto_save:
            self.save()

    def get_section(self, section: str) -> Dict[str, Any]:
        """섹션 전체 딕셔너리의 복사본을 반환합니다."""
        return copy.deepcopy(self._config.get(section, {}))

    def update_section(self, section: str, values: Dict[str, Any], auto_save: bool = True) -> None:
        """섹션 내 여러 설정값을 한꺼번에 업데이트합니다."""
        if section not in self._config or not isinstance(self._config[section], dict):
            self._config[section] = {}
        self._config[section].update(values)
        if auto_save:
            self.save()

    def reset_to_defaults(self) -> None:
        """모든 설정을 초기 기본값으로 되돌리고 저장합니다."""
        self._config = copy.deepcopy(DEFAULT_CONFIG)
        self.save()

    def add_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """설정 변경 시 호출될 콜백 함수를 등록합니다."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def _notify_listeners(self) -> None:
        for callback in self._listeners:
            try:
                callback(self._config)
            except Exception as e:
                print(f"[ConfigManager] 리스너 호출 오류: {e}")

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                ConfigManager._deep_merge(base[k], v)
            else:
                base[k] = v
