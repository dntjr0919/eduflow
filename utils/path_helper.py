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
    사용자 설정(config.json), 로컬 DB(SQLite), 로그 등 영구 보존되어야 하는
    파일들의 기준 폴더를 반환합니다.
    - PyInstaller(.exe) 실행 시: .exe 실행 파일이 위치한 폴더
    - 일반 Python 실행 시: 프로젝트 루트 폴더
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_resource_path(relative_path: str) -> str:
    """
    읽기 전용 리소스 파일(아이콘, 기본 템플릿 등)의 절대 경로를 문자열로 반환합니다.
    """
    target = get_bundle_dir() / relative_path
    return str(target)


def get_data_path(relative_path: str, ensure_parent: bool = True) -> str:
    """
    읽기/쓰기가 필요한 로컬 데이터 파일(config.json, SQLite DB 등)의 절대 경로를 반환합니다.
    ensure_parent=True일 경우 상위 디렉토리가 없으면 자동 생성합니다.
    """
    target = get_app_dir() / relative_path
    if ensure_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


def ensure_dir(dir_path: str) -> str:
    """지정한 디렉토리가 존재하지 않으면 생성하고 절대 경로를 반환합니다."""
    path_obj = Path(dir_path).resolve()
    path_obj.mkdir(parents=True, exist_ok=True)
    return str(path_obj)
