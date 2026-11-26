# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

_gapi_datas, _gapi_bins, _gapi_hidden = collect_all('googleapiclient')
_goauth_datas, _goauth_bins, _goauth_hidden = collect_all('google_auth_oauthlib')

a = Analysis(
    ['C:/vibecoding_school/eduflow/main.py'],
    pathex=[],
    binaries=_gapi_bins + _goauth_bins,
    datas=[('ui/styles/theme.qss', 'ui/styles'), ('ui/assets/icons', 'ui/assets/icons'), ('assets/blank_template.hwpx', 'assets'), ('docs', 'docs')] + _gapi_datas + _goauth_datas,
    hiddenimports=['PyQt6.QtNetwork', 'PyQt6.QtSvg', 'lxml.etree', 'win32com.client', 'pythoncom', 'win32api', 'win32con', 'watchdog.observers', 'watchdog.events', 'openpyxl', 'olefile', 'playwright.sync_api', 'pypdf', 'sqlite3', 'requests', 'urllib3', 'firebase_admin', 'firebase_admin.credentials', 'firebase_admin.firestore', 'google.cloud.firestore', 'google.auth', 'google.auth.transport.requests', 'google.oauth2.credentials', 'google_auth_oauthlib', 'google_auth_oauthlib.flow', 'google_auth_httplib2', 'googleapiclient', 'googleapiclient.discovery', 'googleapiclient.errors', 'googleapiclient.http', 'core.firebase_client', 'services.firestore_sync_service', 'services.google_calendar_service', 'services.timetable_manual_service', 'services.comcigan_playwright_fetcher', 'services.mobile_integration_worker'] + _gapi_hidden + _goauth_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EduFlow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['ui/assets/icons/eduflow_app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='EduFlow',
)
