# EduFlow — Teacher Workstation

**EduFlow**는 교사 및 교무·행정 실무자의 반복적인 파일 정리, 학생 과제 취합·점검, NEIS 연계 성적·세특 기록, 학급 경영 업무를 단일 데스크톱 환경에서 신속하게 처리할 수 있도록 설계된 **PyQt6 기반 Windows 전용 지능형 워크스테이션**입니다.

Linear, Raycast 스타일의 고밀도 다크 슬레이트 프레임과 눈부심을 최소화한 캔버스 레이아웃(`EduFlow Slate Workstation` / Stitch Design System)을 기반으로 구축되었으며, 비동기 백그라운드 워커(`QThread`) 아키텍처를 채택하여 대용량 파일 처리나 실시간 감시 중에도 UI 멈춤 없이 쾌적한 작업 환경을 보장합니다.

---

## 디자인 시스템 및 아키텍처 원칙 (Design Philosophy)

- **EduFlow Slate Workstation 규격:**
  - **Navigation Shell:** 딥 다크 슬레이트 (`#0F172A`)
  - **Workspace Canvas:** 글레어 억제 캔버스 (`#F8F9FF`)
  - **Cards & Surfaces:** 정적 인스트루먼트 카드 (`#FFFFFF`, 1px solid `#E2E8F0`)
  - **Primary Accent:** 프로페셔널 블루 (`#004AC6` / `#2563EB`)
  - **Typography:** `Inter`, `Pretendard`, 고정폭 숫자(`JetBrains Mono` Tabular Numbers)
- **Zero-Emoji Discipline:** 버튼 레이블, 테이블 헤더, 카드 타이틀에서 이모지를 배제하고 정밀한 텍스트, 상태 배지, 벡터 아이콘을 적용하여 업무 집중도를 극대화했습니다.
- **안정적인 정적 카드 (No-Jitter):** 마우스 호버 시 화면 크기가 왜곡되던 애니메이션을 전면 배제하고, 클릭형 아코디언(`AccordionCard`)과 정적 인스트루먼트 카드로 재구성했습니다.
- **반응형 유연 배치 (Fluid Layout):** 가로 스크롤바 클리핑을 제거하고 창 크기에 맞춰 데이터 테이블 컬럼이 자동 확장·축소되도록 최적화했습니다.

---

## 탭별 주요 기능 (Workstation Tabs)

### 1. 스마트 파일 자동 분류 (`ui/tabs/tab_file_sorter.py`)
- **수직 파이프라인 레이아웃:**
  - **상단:** 감시/원본 폴더 (`Source Directories`) 관리
  - **중간:** 원클릭 실행 트리거 (`▼ 즉시 1회 자동 분류 실행 (Source ➔ Target)`)
  - **하단:** 분류 기준 상위 폴더 (`Target Base Directory`) 경로 및 이동/복사/중복처리 옵션
- **실시간 폴더 감시:** 브라우저 다운로드 폴더나 바탕화면의 신규 파일을 감지하여 키워드/확장자 우선순위 규칙에 따라 자동 분류.
- **안전 장치 및 실행 취소 (Undo):** 임시 다운로드 파일(`.crdownload`, `.part`, `~$`) 자동 무시 및 개별 파일 / 실행 묶음(Batch) 단위 원래 위치 완벽 복구.

### 2. 문서 처리 & AI 과제 파이프라인 (`ui/tabs/tab_doc_ocr.py`)
- **학번/이름 자동 추출 및 명렬표 대조:** 제출 파일(`PDF`, `HWP`, `DOCX`, `XLSX` 등)에서 정규표현식으로 학번·이름을 파싱하여 미제출자를 실시간 색출 및 명단 리포트 출력.
- **표준 파일명 일괄 변경 & 단일 PDF 병합:** 학번순 `[학번]_[이름]_과제명.확장자` 일괄 정렬 및 학생별 책갈피(Bookmark)가 포함된 단일 PDF 병합.
- **DOCX 서식 구조화 추출:** 교과 활동지 및 수행평가 양식을 파싱하여 학생별 응답을 단일 엑셀 시트로 종합.
- **하이브리드 AI OCR 및 NEIS 세특 생성기:**
  - 유료 API 연동(Gemini, GPT-4o, Claude) 및 API 키 없는 환경을 위한 **No-API 수동 복붙 모달 (`ManualLlmDialog`)** 지원.
  - 학생별 과제 내용을 바탕으로 NEIS 바이트 규격(500자/1500Byte)을 준수하는 과목별 세특 초안 자동 생성.

### 3. 성적 및 수업 자료 관리 (`ui/tabs/tab_grade_material.py`)
- **상단 KPI 메트릭 카드:** 재적 인원, 1차 지필/수행 채점 완료 인원, 특기사항 미입력 인원을 프로그레스 바와 함께 실시간 집계.
- **좌측 성적 그리드:** 학번 기준 Tabular Numbers 중앙 정렬, 실시간 검색 필터, 셀 즉시 편집 및 성적 일람표(`.csv`, `.xlsx`) 내보내기.
- **우측 수업 자료 디렉토리 트리:** 학기·단원별 수업 자료(PPT, DOCX, PDF) 버전 계층 뷰어, 폴더 열기, 신규 자료 업로드 및 단원 압축 백업(`.zip`) 지원.

### 4. 학급 경영 도구함 (`ui/tabs/tab_classroom_tools.py`)
2x2 벤토 인스트루먼트 그리드로 구성된 학급 운영 통합 도구함:
1. **출결 및 결석계 관리:** 출석 / 질병 결석 / 체험학습 상태 배지, 당일 유고 증빙 대장, 결석계 서식 자동 생성 및 출석인정 의결서 출력.
2. **청소 당번 및 1인 1역 랜덤 배정기:** 직전 주차 당번 제외 공정 가중치 알고리즘, 구역별 당번 배정 테이블, 결과 클립보드 복사.
3. **자리 배치 및 모둠 편성기:** 칠판/교탁(Front Podium) 기준 6x5 좌석 배치도, 시력 배려 앞자리 우선 배치 옵션, 무작위 좌석 셔플 및 좌석표 내보내기.
4. **학생 관찰 및 상담 메모장:** 학생별 선택 드롭다운, NEIS 핵심 역량 키워드 칩(`+탐구능력`, `+자기주도성` 등), 수시 관찰 한줄 입력 및 최근 관찰 타임라인 피드.

### 5. 외부 연동 및 자동 알림 센터 (`ui/tabs/tab_notification.py`)
- **상단 채널 상태 표시줄:** Desktop Toast, KakaoTalk Macro, Webhook 연결 상태 칩 실시간 표기.
- **구글 시트/설문지 실시간 폴링 감시:** `GoogleSheetWatchWorker` 기반 자동 감시, 응답 감지 시뮬레이션 및 알림 발송.
- **즉시 브로드캐스트 발송:** 채널 선택(토스트/카카오톡/웹훅) 후 다중 채널로 공지 즉시 전송.
- **라이브 터미널 로그 드로어:** `#0F172A` 다크 배경과 `JetBrains Mono` 폰트로 스트리밍되는 실시간 이벤트 콘솔.

### 6. 환경설정 & 학생 명렬표 마스터 관리 (`ui/tabs/tab_settings.py`)
- **좌측 설정 아코디언 패널 (45%):**
  - **AI 설정:** Google Gemini, OpenAI, Anthropic Claude, OpenRouter, 로컬 Ollama 연동 지원. 실시간 가용 모델 자동 조회 및 마우스 휠 오조작 방지 필터 적용.
  - **폴더 감시 및 시스템 설정:** 상위 폴더 경로 지정, 폴링 주기, 시스템 트레이 상주 여부 설정.
- **우측 학생 명렬표 마스터 데이터 (55%):**
  - 학번, 학년, 반, 번호, 이름, 연락처, 비고 관리.
  - CSV 및 Excel(`.xlsx`) 일괄 등록/내보내기, 개별 추가/수정/삭제 모달 지원.

---

## 기술 스택 (Tech Stack)

| 분류 | 기술 / 라이브러리 |
| :--- | :--- |
| **언어** | Python 3.10+ |
| **GUI 프레임워크** | PyQt6 (Slate Workstation QSS Theme, Fusion Style) |
| **파일 및 문서 처리** | `openpyxl`, `python-docx`, `pypdf`, `watchdog` |
| **데이터베이스 및 설정**| SQLite3 (`data/workstation.db`), JSON (`config.json`) |
| **비동기 스레딩** | `QThread`, `QObject` 기반 커스텀 `TaskRunner` & `BaseWorker` |
| **AI 공급자 연동** | `google-genai`, `openai`, `anthropic`, `requests` (Ollama/OpenRouter) |
| **패키징** | `pyinstaller` |

---

## 빠른 시작 (Getting Started)

### 1. 저장소 클론 및 가상환경 구성
```bash
git clone https://github.com/dntjr0919/eduflow.git
cd eduflow

python -m venv venv
# Windows 가상환경 활성화
venv\Scripts\activate
```

### 2. 의존성 패키지 설치
```bash
pip install -r requirements.txt
```

### 3. 애플리케이션 실행
```bash
python main.py
```

---

## 단일 실행 파일(`.exe`) 빌드

PyInstaller 스크립트를 통해 콘솔 창 없는 단일 실행 파일(`dist/EduFlow.exe`)을 패키징할 수 있습니다.

```bash
python build_exe.py
```

---

## 프로젝트 디렉토리 구조

```text
eduflow/
├── main.py                      # 애플리케이션 진입점 (모던 폰트 및 QSS 테마 주입)
├── build_exe.py                 # PyInstaller 배포 빌드 스크립트
├── requirements.txt             # 의존성 패키지 목록
├── core/
│   ├── base_worker.py           # 비동기 QThread 워커 코어 (FunctionWorker, PeriodicWorker 등)
│   ├── config_manager.py        # 로컬 설정(config.json) 입출력 관리자
│   └── database.py              # SQLite3 마스터 DB (학생 명렬표, 규칙, 작업 이력)
├── services/
│   ├── docx_extractor_service.py # DOCX 양식 분석 및 엑셀 변환 서비스
│   ├── excel_merger_service.py   # 엑셀 과제 데이터 통합 서비스
│   ├── file_sorter_service.py   # 파일 분류 및 Undo 실행 엔진
│   ├── llm_client.py            # 멀티 LLM API 클라이언트 & JSON 파서
│   ├── notifier.py              # 트레이 토스트 / PC 카카오톡 / 웹훅 알림 서비스
│   └── record_generator_service.py # NEIS 규격 학교생활기록부 세특 생성 엔진
├── ui/
│   ├── main_window.py           # 다크 사이드바 네비게이션 및 메인 셸
│   ├── components/
│   │   ├── collapsible_card.py  # 클릭형 슬라이딩 아코디언 카드 (AccordionCard)
│   │   └── hover_zoom_card.py   # 정적 인스트루먼트 카드 베이스 (HoverZoomCard)
│   ├── dialogs/
│   │   ├── docx_schema_create_dialog.py # 활동지 파싱 스키마 생성 다이얼로그
│   │   ├── manual_llm_dialog.py # No-API 프롬프트 복사/붙여넣기 하이브리드 모달
│   │   └── prompt_edit_dialog.py # 세특 및 OCR 프롬프트 편집기
│   ├── styles/
│   │   └── theme.qss            # EduFlow Slate Workstation 모던 QSS 스타일시트
│   ├── tabs/
│   │   ├── tab_file_sorter.py     # [탭 1] 스마트 파일 자동 분류기
│   │   ├── tab_doc_ocr.py         # [탭 2] 문서 처리 & AI 과제 파이프라인
│   │   ├── tab_grade_material.py  # [탭 3] 성적 및 수업 자료 관리
│   │   ├── tab_classroom_tools.py # [탭 4] 학급 경영 도구함 (2x2 그리드)
│   │   ├── tab_notification.py    # [탭 5] 외부 연동 및 자동 알림 센터
│   │   └── tab_settings.py        # [탭 6] 환경설정 및 학생 명렬표 관리
│   └── utils.py                 # 마우스 휠 오조작 방지 필터 및 공통 UI 유틸리티
└── utils/
    └── path_helper.py           # 실행 환경별 리소스 절대 경로 헬퍼
```

---

## 보안 및 데이터 관리 안내

- **로컬 우선 원칙:** 학생 명렬표, 출결 기록, 파일 처리 이력 등 모든 데이터는 사용자의 로컬 PC(`data/workstation.db`)에만 저장되며 외부 서버로 전송되지 않습니다.
- **민감 정보 배제:** `config.json`(API Key 포함), `credentials.json`, `data/*.db` 파일은 `.gitignore`에 등록되어 Git 저장소에 커밋되지 않습니다.
