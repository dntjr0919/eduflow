# ✨ EduFlow (교무·학급 업무 자동화 데스크톱 워크스테이션)

**EduFlow**는 교사 및 교무·행정 실무자의 반복적인 파일 정리, 학생 제출물 취합·점검, 공문/보고서 요약, 학급 경영 업무를 자동화하기 위해 설계된 **PyQt6 기반 Windows 로컬 데스크톱 워크스테이션**입니다.

모듈형 사이드바-스택 아키텍처(`QListWidget` + `QStackedWidget`)와 비동기 백그라운드 워커(`QThread`) 기반으로 설계되어, 대량 문서 처리나 실시간 폴더 감시 중에도 UI 프리징 없이 쾌적하게 사용할 수 있습니다.

---

## 🌟 주요 기능 (Key Features)

### 1. 📁 키워드 및 확장자 기반 스마트 파일 자동 분류
- **실시간 폴더 감시 & 즉시 1회 일괄 분류**: 다운로드 폴더나 바탕화면을 감시하여 파일명 키워드(예: `공문`, `가정통신문`, `성적`)와 확장자 우선순위 규칙에 따라 하위 폴더를 자동 생성하고 이동/복사합니다.
- **안전한 파일 처리**: 브라우저 임시 다운로드 파일(`.crdownload`, `.part`, `~$`) 무시 및 파일 쓰기 잠금 해제 대기 로직이 내장되어 있습니다.
- **실행 취소 (Undo) 지원**: 분류 이력을 로컬 DB에 기록하여 언제든 개별 파일 또는 최근 실행 묶음(Batch) 전체를 원래 위치로 즉시 복구할 수 있습니다.

### 2. 📄 학번/이름 기준 학생 제출 파일 취합 · 미제출자 대조 · 단일 PDF 병합
- **정규표현식(Regex) 자동 추출**: 학생들이 제출한 다양한 파일(`PDF`, `HWP`, `DOCX`, `XLSX` 등)에서 학번(4~5자리 숫자)과 이름을 자동으로 추출합니다.
- **미제출자 즉시 색출**: 등록된 학생 명렬표 DB와 대조하여 제출 완료자와 미제출자 명단을 실시간으로 분류하고, 클립보드 복사 및 엑셀(`.xlsx`) 리포트 내보내기를 지원합니다.
- **표준 파일명 일괄 변경 & 단일 PDF 병합**: 학번 순서대로 `[학번]_[이름]_과제명.확장자` 표준 형식으로 파일명을 일괄 정렬하거나, `pypdf` 엔진을 통해 학생별 책갈피(Outline)가 포함된 단일 PDF로 병합합니다.

### 3. 🤖 멀티 LLM & 수동 프롬프트(No-API) 하이브리드 AI 시스템
- **멀티 API 공급자 표준 지원**: **Google Gemini**, **OpenAI (GPT-4o 등)**, **Anthropic Claude**, **OpenRouter / Ollama (로컬 LLM)**를 단일 인터페이스로 연동합니다.
- **No-API 수동 프롬프트 복붙 모드 (`ManualLlmDialog`)**: 유료 API 키가 없거나 보안망 환경인 경우, 구조화된 JSON 출력 강제 프롬프트를 원클릭으로 복사하여 ChatGPT/Claude/Gemini 웹 화면에 붙여넣고, 응답 텍스트를 다시 붙여넣으면 정규표현식 파서가 순수 JSON 데이터만 안전하게 추출하여 워크스테이션에 반영합니다.

### 4. 📊 학생 명렬표 마스터 DB 및 확장형 교무 탭
- **학생 명렬표 마스터 데이터 관리**: `CSV` 또는 `Excel(.xlsx)` 파일로 학번·학년·반·번호·이름·연락처를 일괄 등록하고 SQLite에 안전하게 보관합니다.
- **모듈형 확장 탭**: 성적·수업자료 관리, 학급 경영 도구함(청소 당번/1인1역 랜덤 추첨기 등), 외부 연동·알림 센터(구글 시트 폴링 및 데스크톱 트레이 토스트/카카오톡/웹훅 알림)를 제공합니다.
- **시스템 트레이 상주**: 창을 닫아도 시스템 트레이(`QSystemTrayIcon`)에서 백그라운드 폴더/시트 감시를 지속합니다.

---

## 🛠️ 기술 스택 (Tech Stack)

- **Language**: Python 3.10+
- **GUI Framework**: PyQt6 (Modern Dark Sidebar & Material 3 QSS Theme)
- **File & Document Processing**: `watchdog`, `openpyxl`, `pypdf` (`>=4.0.0`)
- **Database & Config**: `sqlite3` (`data/workstation.db`), JSON (`config.json`)
- **AI Integrations**: `google-genai`, `openai`, `anthropic`, `requests`
- **Packaging**: `pyinstaller`

---

## 🚀 로컬 실행 방법 (Getting Started)

### 1. 저장소 클론 및 가상환경 설정
```bash
git clone https://github.com/dntjr0919/eduflow.git
cd eduflow

python -m venv venv
venv\Scripts\activate
```

### 2. 필수 패키지 설치 및 실행
```bash
pip install -r requirements.txt
python main.py
```

---

## 📦 단일 실행 파일(`.exe`) 빌드 방법

PyInstaller 기반의 자동 빌드 스크립트를 실행하면 콘솔 창이 숨겨진(`--noconsole`) 단일 실행 파일(`dist/WorkstationApp.exe`)이 생성됩니다.

```bash
python build_exe.py
```

---

## 📂 프로젝트 디렉토리 구조

```text
eduflow/
├── main.py                      # 앱 진입점 (모던 폰트 및 QSS 테마 로드)
├── build_exe.py                 # PyInstaller 단일 .exe 자동 빌드 스크립트
├── requirements.txt             # 의존성 패키지 목록
├── core/
│   ├── config_manager.py        # 로컬 설정(config.json) 관리
│   ├── database.py              # SQLite 마스터 DB (학생 명렬표, 분류 규칙, 이력)
│   └── base_worker.py           # QThread & QObject 비동기 백그라운드 워커 코어
├── services/
│   ├── file_sorter_service.py   # 키워드/확장자 기반 파일 분류 및 Undo 서비스
│   ├── llm_client.py            # 멀티 LLM API 클라이언트 & JSON 정규식 파서
│   └── notifier.py              # 트레이 토스트 / PC 카톡 / 웹훅 알림 서비스
├── ui/
│   ├── main_window.py           # 다크 사이드바 + 스택 워크스페이스 + 접이식 로그 서랍
│   ├── styles/
│   │   └── theme.qss            # 모던 웹 AI 스타일 QSS 테마 시트
│   ├── dialogs/
│   │   └── manual_llm_dialog.py # No-API 프롬프트 복사/붙여넣기 하이브리드 모달
│   └── tabs/
│       ├── tab_file_sorter.py     # [탭 1] 스마트 파일 분류기
│       ├── tab_doc_ocr.py         # [탭 2] 문서 처리 & AI OCR / 학번순 PDF 병합
│       ├── tab_grade_material.py  # [탭 3] 성적 및 수업 자료 관리
│       ├── tab_classroom_tools.py # [탭 4] 학급 경영 도구함
│       ├── tab_notification.py    # [탭 5] 외부 연동 및 알림 센터
│       └── tab_settings.py        # [탭 6] 환경설정 및 학생 명렬표 마스터 관리
└── utils/
    └── path_helper.py           # PyInstaller(sys._MEIPASS) 호환 경로 헬퍼
```

---

## 🔒 개인정보 및 보안 유의사항

- `config.json`(API Key 포함), `credentials.json`, 그리고 학생 개인정보가 저장되는 `data/`(`*.db`, `*.sqlite3`) 디렉토리는 `.gitignore`에 의해 원격 저장소 커밋 대상에서 엄격히 제외됩니다.
