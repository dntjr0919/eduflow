"""
레거시 GoogleSheetsSyncService 호환 래퍼 모듈.
기존 Google Apps Script 웹 앱 및 gspread 호출을 완전히 제거하고,
services.firestore_sync_service의 Cloud Firestore 통합 매니저로 위임합니다.
"""

from __future__ import annotations

from services.firestore_sync_service import (
    FirestoreSyncService as GoogleSheetsSyncService,
    FirestoreSyncWorker as GoogleSheetsSyncWorker,
    _is_truthy,
)

__all__ = [
    "GoogleSheetsSyncService",
    "GoogleSheetsSyncWorker",
    "_is_truthy",
]
