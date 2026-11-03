"""
PyInstaller 기반 Windows .exe 자동 빌드 스크립트 (build_exe.py).
"""

import subprocess
import sys
from pathlib import Path


def build_executable(onefile: bool = True) -> None:
    root_dir = Path(__file__).resolve().parent
    entry_script = str(root_dir / "main.py")
    app_name = "SchoolWorkstation"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        f"--name={app_name}",
        "--add-data=ui/styles/theme.qss;ui/styles",
        "--hidden-import=watchdog.observers",
        "--hidden-import=watchdog.events",
        "--hidden-import=openpyxl",
        "--hidden-import=pypdf",
        "--hidden-import=sqlite3",
    ]

    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    cmd.append(entry_script)

    print("=" * 65)
    print(f"[PyInstaller 빌드 시작] 명령어:\n  {' '.join(cmd)}")
    print("=" * 65)
    subprocess.run(cmd, cwd=str(root_dir), check=True)
    print("\n✅ 빌드가 완료되었습니다! dist/ 폴더에서 실행 파일(.exe)을 확인하세요.")


if __name__ == "__main__":
    use_onefile = "--onedir" not in sys.argv
    build_executable(onefile=use_onefile)
