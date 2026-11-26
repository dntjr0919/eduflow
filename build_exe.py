"""
PyInstaller 기반 Windows .exe 자동 빌드 및 배포용 HTML 안내서 생성 스크립트 (build_exe.py).
- 보안상 Firebase 서비스 계정 JSON 키 파일은 .exe 내부에 번들링하지 않습니다.
- 빌드 완료 시 dist/ 폴더에 EduFlow.exe 와 EduFlow_사용_및_배포_안내서.html 을 함께 생성합니다.
"""

import subprocess
import sys
from pathlib import Path


GUIDE_HTML_FILENAME = "EduFlow_사용_및_배포_안내서.html"


def generate_deployment_guide_html(dist_dir: Path) -> Path:
    """dist/ 폴더에 배포용 전용 HTML 사용 및 배포 안내서를 생성합니다."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    out_path = dist_dir / GUIDE_HTML_FILENAME
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>EduFlow 데스크톱 · 모바일 사용 및 배포 안내서</title>
  <style>
    :root {
      --bg: #f8fafc;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --border: #e2e8f0;
      --primary: #4f46e5;
      --primary-soft: #eef2ff;
      --success: #059669;
      --success-soft: #ecfdf5;
      --warn: #b45309;
      --warn-soft: #fffbeb;
      --danger: #b91c1c;
      --danger-soft: #fef2f2;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.65;
    }
    .wrap {
      max-width: 960px;
      margin: 0 auto;
      padding: 36px 20px 64px;
    }
    .hero {
      background: linear-gradient(135deg, #312e81 0%, #4f46e5 100%);
      color: #ffffff;
      border-radius: 20px;
      padding: 32px 30px;
      box-shadow: 0 12px 30px rgba(79, 70, 229, 0.18);
      margin-bottom: 24px;
    }
    .hero .badge {
      display: inline-block;
      background: rgba(255, 255, 255, 0.18);
      border: 1px solid rgba(255, 255, 255, 0.28);
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 10px;
    }
    .hero h1 {
      margin: 0 0 8px;
      font-size: 26px;
      font-weight: 800;
    }
    .hero p {
      margin: 0;
      color: rgba(255, 255, 255, 0.9);
      font-size: 14.5px;
    }
    .grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 24px 26px;
      box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
    }
    .card-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
    }
    .num {
      width: 28px;
      height: 28px;
      border-radius: 8px;
      background: var(--primary-soft);
      color: var(--primary);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 14px;
      flex-shrink: 0;
    }
    .card h2 {
      margin: 0;
      font-size: 18px;
      font-weight: 800;
    }
    .card p, .card li {
      color: var(--muted);
      font-size: 14px;
    }
    ul, ol {
      margin: 8px 0 0;
      padding-left: 20px;
    }
    li + li { margin-top: 6px; }
    code {
      background: #f1f5f9;
      color: #0f172a;
      padding: 2px 6px;
      border-radius: 6px;
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
    }
    .callout {
      margin-top: 14px;
      padding: 12px 14px;
      border-radius: 12px;
      font-size: 13.5px;
    }
    .callout.info { background: var(--primary-soft); border: 1px solid #c7d2fe; color: #312e81; }
    .callout.success { background: var(--success-soft); border: 1px solid #a7f3d0; color: #065f46; }
    .callout.warn { background: var(--warn-soft); border: 1px solid #fde68a; color: #92400e; }
    .callout.danger { background: var(--danger-soft); border: 1px solid #fecaca; color: #991b1b; }
    .feature-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .mini-card {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 16px;
    }
    .mini-card h3 {
      margin: 0 0 6px;
      font-size: 14.5px;
      color: var(--text);
    }
    .mini-card p {
      margin: 0;
      font-size: 13px;
      color: var(--muted);
    }
    .footer {
      margin-top: 28px;
      text-align: center;
      font-size: 12.5px;
      color: #64748b;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="badge">EduFlow Hybrid Desktop &amp; Mobile PWA</div>
      <h1>EduFlow 사용 및 배포 안내서</h1>
      <p>별도의 Firebase JSON 키 없이도 즉시 연동되는 기본 Vercel API 모드부터, 독립 서버 운영 및 모바일 PWA 연동까지 한눈에 확인하세요.</p>
    </header>

    <div class="grid">
      <section class="card">
        <div class="card-header">
          <span class="num">1</span>
          <h2>빠른 시작 (JSON 키 없이 바로 쓰기 — 기본 Vercel API 모드)</h2>
        </div>
        <p><code>EduFlow.exe</code>는 로컬에 Firebase 서비스 계정 JSON 키 파일이 없으면 자동으로 <strong>Vercel API 모드</strong>(<code>https://m.eduflow.kro.kr/api/firestore</code>)로 동작합니다.</p>
        <ol>
          <li>별도의 인증 키 파일 없이 <code>EduFlow.exe</code>를 바로 실행합니다.</li>
          <li>상단 메뉴에서 <strong>학급 경영 도구함</strong> 탭을 선택합니다.</li>
          <li>화면 정중앙 로그인 카드에 <strong>EduFlow Mobile 교사 계정 ID · 비밀번호</strong>를 입력하고 <code>[로그인]</code>을 클릭합니다.</li>
          <li>로그인 즉시 소속 학교·학년·반 명렬표가 모바일 DB와 자동 연동됩니다.</li>
        </ol>
        <div class="callout info">
          <strong>신규 교사 가입 승인 절차:</strong> 모바일 PWA(<code>m.eduflow.kro.kr</code>)에서 교사 회원가입을 신청하면 초기 상태가 <code>pending</code>으로 등록됩니다. 최고 관리자 승인 후 <code>active</code> 상태가 되면 데스크톱과 모바일 모두 즉시 로그인할 수 있습니다.
        </div>
      </section>

      <section class="card">
        <div class="card-header">
          <span class="num">2</span>
          <h2>독립 서버 운영 가이드 (자체 Firebase &amp; Vercel 구축 시)</h2>
        </div>
        <p>다른 학교나 교사가 독립된 데이터베이스 및 API 서버를 직접 운영하고 싶다면 아래 두 가지 방법 중 하나를 선택할 수 있습니다.</p>
        <ul>
          <li><strong>방법 A — Direct Admin 모드 (로컬 서비스 계정 JSON 키 사용):</strong><br />
            Firebase 콘솔(무료 Spark 요금제 가능) &gt; 프로젝트 설정 &gt; 서비스 계정에서 새 비공개 키(JSON)를 발급받아 파일명을 <code>eduflow-db-b44f3-5692e5dea8c3.json</code>으로 지정한 뒤 <code>EduFlow.exe</code>와 같은 폴더에 배치합니다.</li>
          <li><strong>방법 B — 자체 Vercel API 서버 연결:</strong><br />
            데스크톱 앱 <strong>환경설정 &amp; 명렬표</strong> 탭에서 <code>연동 서버 URL (Vercel)</code> 항목을 본인이 배포한 <code>https://&lt;도메인&gt;/api/firestore</code> 주소로 변경합니다.</li>
        </ul>
        <div class="callout success">
          환경설정 탭 상단의 <strong>Firebase 연결 모드</strong> 배지에서 현재 <code>Direct Admin 모드</code>인지 <code>Vercel API 모드</code>인지 실시간으로 확인할 수 있습니다.
        </div>
      </section>

      <section class="card">
        <div class="card-header">
          <span class="num">3</span>
          <h2>주요 기능 안내</h2>
        </div>
        <div class="feature-grid">
          <div class="mini-card">
            <h3>명렬표 기반 초기 계정 발급</h3>
            <p>나이스 학급 명렬표를 업로드하면 <strong>5자리 학번(ID) + 생년월일/주민번호 앞 6자리(초기 비밀번호)</strong>로 학생·학부모 계정이 일괄 발급됩니다. 학생·학부모는 로그인 후 <strong>최초 1회 커스텀 ID</strong>로 변경할 수 있습니다.</p>
          </div>
          <div class="mini-card">
            <h3>컴시간 · 수동/엑셀 시간표 동기화</h3>
            <p>컴시간 자동 파싱(5자리 컴시간 코드 + 7자리 NEIS 코드 병행 저장)뿐만 아니라 미지원 학교를 위한 <strong>주간 엑셀/CSV 일괄 업로드</strong> 및 <strong>특정 날짜·교시 변동/보강 수동 입력</strong>을 지원합니다.</p>
          </div>
          <div class="mini-card">
            <h3>AI OCR 가정통신문 &amp; 전자서명</h3>
            <p>PDF 가정통신문을 우측 <strong>AI 실행 패널</strong>(API/수동복붙/Playwright)로 판독해 제목·마감일·본문·회신 항목 JSON을 자동 채움하고, 모바일 앱에서 학부모·학생의 <strong>터치 서명 회신</strong>을 취합합니다.</p>
          </div>
          <div class="mini-card">
            <h3>학급 공지 푸시 &amp; 1:1 상담</h3>
            <p>학급 전체·학생·학부모 대상 공지 및 미제출자 알림을 즉시 발송하고, 모바일에서 주고받은 <strong>1:1 상담 대화 내역</strong>을 조회·복사·텍스트로 저장할 수 있습니다.</p>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-header">
          <span class="num">4</span>
          <h2>보안 주의사항 및 모바일 PWA 설치</h2>
        </div>
        <div class="callout danger">
          <strong>서비스 계정 JSON 키 파일 비공유 원칙:</strong><br />
          Firebase 서비스 계정 JSON 키 파일(<code>eduflow-db-*.json</code>)에는 전체 데이터베이스 관리자 권한이 포함되어 있습니다. <strong>절대 타인에게 공유하거나 Git 저장소·공개 클라우드에 업로드하지 마세요.</strong> (<code>EduFlow.exe</code> 빌드 파일 내부에도 보안상 JSON 키가 포함되지 않습니다.)
        </div>
        <div class="callout warn">
          <strong>모바일 PWA 설치 및 알림 허용 안내:</strong><br />
          스마트폰 브라우저에서 <code>https://m.eduflow.kro.kr</code> 에 접속한 뒤 브라우저 메뉴에서 <strong>[홈 화면에 추가]</strong>를 선택해 앱처럼 설치하고, 로그인 후 <strong>푸시 알림 허용</strong>을 활성화해 주세요.
        </div>
      </section>
    </div>

    <div class="footer">
      EduFlow Workstation &amp; Mobile PWA · Hybrid Firestore Architecture
    </div>
  </div>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")
    return out_path


def build_executable(onefile: bool = False) -> None:
    root_dir = Path(__file__).resolve().parent
    entry_script = str(root_dir / "main.py")
    app_name = "EduFlow"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        f"--name={app_name}",
        "--add-data=ui/styles/theme.qss;ui/styles",
        "--add-data=ui/assets/icons;ui/assets/icons",
        "--add-data=assets/blank_template.hwpx;assets",
        "--add-data=docs;docs",
        "--hidden-import=PyQt6.QtNetwork",
        "--hidden-import=PyQt6.QtSvg",
        "--hidden-import=lxml.etree",
        "--hidden-import=win32com.client",
        "--hidden-import=pythoncom",
        "--hidden-import=win32api",
        "--hidden-import=win32con",
        "--hidden-import=watchdog.observers",
        "--hidden-import=watchdog.events",
        "--hidden-import=openpyxl",
        "--hidden-import=olefile",
        "--hidden-import=playwright.sync_api",
        "--hidden-import=pypdf",
        "--hidden-import=sqlite3",
        "--hidden-import=requests",
        "--hidden-import=urllib3",
        "--hidden-import=firebase_admin",
        "--hidden-import=firebase_admin.credentials",
        "--hidden-import=firebase_admin.firestore",
        "--hidden-import=google.cloud.firestore",
        "--hidden-import=google.auth",
        "--hidden-import=google.auth.transport.requests",
        "--hidden-import=google.oauth2.credentials",
        "--hidden-import=google_auth_oauthlib",
        "--hidden-import=google_auth_oauthlib.flow",
        "--hidden-import=google_auth_httplib2",
        "--hidden-import=googleapiclient",
        "--hidden-import=googleapiclient.discovery",
        "--hidden-import=googleapiclient.errors",
        "--hidden-import=googleapiclient.http",
        "--collect-all=googleapiclient",
        "--collect-all=google_auth_oauthlib",
        "--hidden-import=core.firebase_client",
        "--hidden-import=services.firestore_sync_service",
        "--hidden-import=services.google_calendar_service",
        "--hidden-import=services.timetable_manual_service",
        "--hidden-import=services.comcigan_playwright_fetcher",
        "--hidden-import=services.mobile_integration_worker",
        "--icon=ui/assets/icons/eduflow_app.ico",
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

    guide_dir = (root_dir / "dist") if onefile else (root_dir / "dist" / app_name)
    guide_path = generate_deployment_guide_html(guide_dir)
    print(f"\n[배포 안내서 생성 완료] {guide_path}")
    target_desc = "dist/EduFlow.exe" if onefile else f"dist/{app_name}/{app_name}.exe"
    print(f"\n✅ 빌드가 완료되었습니다! {target_desc} 와 안내서를 확인하세요.")


if __name__ == "__main__":
    use_onefile = "--onefile" in sys.argv
    build_executable(onefile=use_onefile)
