"""
Firebase Admin SDK 초기화 및 Firestore 클라이언트 제공 모듈.
1순위: 로컬 서비스 계정 JSON 파일 (eduflow-db-b44f3-5692e5dea8c3.json 또는 지정 경로)
2순위: 환경변수 (FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore

DEFAULT_SERVICE_ACCOUNT_FILENAME = "eduflow-db-b44f3-5692e5dea8c3.json"


def resolve_local_cred_path(cred_path: Optional[str] = None) -> Optional[Path]:
    candidates = []
    if cred_path:
        candidates.append(Path(cred_path))
    env_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / DEFAULT_SERVICE_ACCOUNT_FILENAME)

    project_root = Path(__file__).resolve().parent.parent
    candidates.append(project_root / DEFAULT_SERVICE_ACCOUNT_FILENAME)
    candidates.append(Path.cwd() / DEFAULT_SERVICE_ACCOUNT_FILENAME)

    for path in candidates:
        if path.is_file():
            return path
    return None


def _resolve_local_cred_path(cred_path: Optional[str] = None) -> Optional[Path]:
    return resolve_local_cred_path(cred_path)


def has_local_credentials(cred_path: Optional[str] = None) -> bool:
    """로컬 서비스 계정 JSON 키 파일 또는 Firebase 환경변수가 존재하는지 확인합니다."""
    if resolve_local_cred_path(cred_path) is not None:
        return True
    project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
    client_email = os.environ.get("FIREBASE_CLIENT_EMAIL", "").strip()
    private_key = os.environ.get("FIREBASE_PRIVATE_KEY", "").strip()
    return bool(project_id and client_email and private_key)


def _build_credential(cred_path: Optional[str] = None) -> credentials.Certificate:
    local_path = _resolve_local_cred_path(cred_path)
    if local_path is not None:
        return credentials.Certificate(str(local_path))

    project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
    client_email = os.environ.get("FIREBASE_CLIENT_EMAIL", "").strip()
    private_key = os.environ.get("FIREBASE_PRIVATE_KEY", "").strip()

    if not (project_id and client_email and private_key):
        raise RuntimeError(
            "Firebase 인증 정보를 찾을 수 없습니다. "
            f"로컬 파일({DEFAULT_SERVICE_ACCOUNT_FILENAME})을 배치하거나 "
            "FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY 환경변수를 설정하세요."
        )

    # Vercel 등 환경변수에 \n 이 리터럴로 들어간 경우 실제 개행으로 복원
    if "\\n" in private_key:
        private_key = private_key.replace("\\n", "\n")

    cert_info = {
        "type": "service_account",
        "project_id": project_id,
        "client_email": client_email,
        "private_key": private_key,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return credentials.Certificate(cert_info)


def get_firebase_app(cred_path: Optional[str] = None) -> firebase_admin.App:
    if firebase_admin._apps:
        return firebase_admin.get_app()

    cred = _build_credential(cred_path)
    options = {}
    storage_bucket = os.environ.get("FIREBASE_STORAGE_BUCKET", "").strip()
    if storage_bucket:
        options["storageBucket"] = storage_bucket

    return firebase_admin.initialize_app(cred, options or None)


def get_firestore_client(cred_path: Optional[str] = None) -> firestore.Client:
    app = get_firebase_app(cred_path)
    return firestore.client(app=app)
