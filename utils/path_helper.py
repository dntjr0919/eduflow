"""
PyInstaller(.exe) 빌드 환경과 일반 Python 실행 환경 모두에서
안전하게 리소스 경로와 영구 데이터 저장 경로를 처리하는 유틸리티 모듈.
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """PyInstaller 등에 의해 실행 파일(.exe)로 패키징된 상태인지 확인합니다."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def get_bundle_dir() -> Path:
    """
    번들링된 정적 리소스(아이콘, UI 에셋 등)의 기준 폴더를 반환합니다.
    - PyInstaller --onefile 실행 시: sys._MEIPASS 임시 압축 해제 폴더
    - 일반 Python 실행 시: 프로젝트 루트 폴더
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent.parent


def get_app_dir() -> Path:
    """
    실행 파일(.exe)이 위치한 기준 폴더를 반환합니다.
    - PyInstaller(.exe) 실행 시: .exe 실행 파일이 위치한 폴더 (Firebase JSON 키 탐색 등)
    - 일반 Python 실행 시: 프로젝트 루트 폴더
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_user_data_dir() -> Path:
    """
    사용자 설정(config.json), 로컬 DB(SQLite), 로그, 캐시 등 쓰기 작업이 발생하는
    영구 데이터 기준 폴더를 반환합니다.
    - PyInstaller(.exe) 실행 시: OS 사용자 데이터 디렉터리 (%LOCALAPPDATA%\\EduFlow)
    - 일반 Python 실행 시: 프로젝트 루트 폴더
    """
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            base_dir = Path(local_app_data) / "EduFlow"
        else:
            base_dir = Path.home() / "AppData" / "Local" / "EduFlow"
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir.resolve()
    return Path(__file__).resolve().parent.parent


def get_resource_path(relative_path: str) -> str:
    """
    읽기 전용 리소스 파일(아이콘, 기본 템플릿 등)의 절대 경로를 문자열로 반환합니다.
    """
    target = get_bundle_dir() / relative_path
    return str(target)


def get_data_path(relative_path: str, ensure_parent: bool = True) -> str:
    """
    읽기/쓰기가 필요한 로컬 데이터 파일(config.json, SQLite DB, 로그, 캐시 등)의 절대 경로를 반환합니다.
    ensure_parent=True일 경우 상위 디렉토리가 없으면 자동 생성합니다.
    """
    target = get_user_data_dir() / relative_path
    if ensure_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


def ensure_dir(dir_path: str) -> str:
    """지정한 디렉토리가 존재하지 않으면 생성하고 절대 경로를 반환합니다."""
    path_obj = Path(dir_path).resolve()
    path_obj.mkdir(parents=True, exist_ok=True)
    return str(path_obj)


def get_default_workspace_dir() -> Path:
    """
    사용자 작업 결과물(문서 처리, HWPX, 병합, 분류 출력 등) 기본 폴더를 반환합니다.
    .exe 실행 파일 폴더와 섞이지 않도록 Documents/EduFlow 를 기본값으로 사용합니다.
    """
    try:
        docs_dir = Path.home() / "Documents" / "EduFlow"
        docs_dir.mkdir(parents=True, exist_ok=True)
        return docs_dir.resolve()
    except Exception:
        fallback = get_app_dir() / "EduFlow_Output"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback.resolve()


def get_active_workspace_dir(config=None) -> Path:
    """
    환경설정(file_sorter.target_base_dir)에 지정된 폴더가 있으면 해당 경로를 생성·반환하고,
    비어 있거나 사용할 수 없으면 Documents/EduFlow 기본 작업 폴더를 반환합니다.
    """
    cfg = config
    if cfg is None:
        try:
            from core.config_manager import ConfigManager

            cfg = ConfigManager()
        except Exception:
            cfg = None

    target_dir = ""
    if cfg is not None:
        try:
            target_dir = str(cfg.get("file_sorter", "target_base_dir", "") or "").strip()
        except Exception:
            target_dir = ""

    if target_dir:
        try:
            resolved = Path(target_dir).expanduser().resolve()
            resolved.mkdir(parents=True, exist_ok=True)
            return resolved
        except Exception:
            pass

    return get_default_workspace_dir()
