# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['E:/my_project/eduflow/main.py'],
    pathex=[],
    binaries=[],
    datas=[('ui/styles/theme.qss', 'ui/styles'), ('ui/assets/icons', 'ui/assets/icons'), ('assets/blank_template.hwpx', 'assets'), ('docs', 'docs')],
    hiddenimports=['PyQt6.QtSvg', 'lxml.etree', 'win32com.client', 'pythoncom', 'win32api', 'win32con', 'watchdog.observers', 'watchdog.events', 'openpyxl', 'pypdf', 'sqlite3', 'firebase_admin', 'firebase_admin.credentials', 'firebase_admin.firestore', 'google.cloud.firestore', 'google.auth', 'core.firebase_client', 'services.firestore_sync_service', 'services.timetable_manual_service', 'services.comcigan_playwright_fetcher', 'services.mobile_integration_worker'],
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
    a.binaries,
    a.datas,
    [],
    name='SchoolWorkstation',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['ui/assets/icons/eduflow_app.ico'],
)
