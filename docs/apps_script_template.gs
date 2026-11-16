/**
 * ============================================================================
 * EduFlow — Google Sheets + Apps Script 실시간 양방향 동기화 & 모바일 통합 API
 * 파일: docs/apps_script_template.gs
 * ============================================================================
 * [설치 및 배포 방법]
 * ※ 주의: 교육청/기관 Google Workspace 계정은 도메인 보안 정책으로 인해
 *    외부(PC Python / curl) 익명 요청 시 403 오류가 발생할 수 있으므로
 *    개인 Google 계정(@gmail.com) 소유 스프레드시트에서 배포하는 것을 권장합니다.
 *
 * 1. 연동할 Google 스프레드시트를 열고 상단 메뉴 [확장 프로그램] -> [Apps Script] 클릭
 * 2. 기존 코드를 모두 지우고 이 파일 전체 내용을 붙여넣은 뒤 저장(Ctrl+S)
 * 3. 상단 함수 드롭다운에서 `initSheets` (또는 `seedSampleData`)를 선택해 [실행]하면
 *    데스크톱(Schedules/Tasks/Notifications) + 모바일 연동용 14개 시트 탭이 자동 생성됩니다.
 *    (기존 Notifications 시트가 있다면 1행 맨 오른쪽에 target_role, target_student_id,
 *     grade, class_no 헤더가 정상 추가되었는지 확인하세요.)
 * 4. [프로젝트 설정] -> [스크립트 속성]에 아래 속성을 등록하세요.
 *    - API_SECRET (강력 권장): 임의의 영문·숫자 비밀키 (Vercel `GAS_API_SECRET` 및
 *      PC EduFlow [Apps Script API Secret]과 동일한 값 입력 시 외부 직접 요청 차단)
 *    - (선택) 나이스(NEIS) 급식 자동 수집용: NEIS_API_KEY, NEIS_OFFICE_CODE, NEIS_SCHOOL_CODE
 *      등록 후 `installNeisMealTrigger` 함수를 1회 실행 (매일 새벽 4시 자동 수집)
 * 5. 우측 상단 [배포] -> [새 배포] -> 유형: [웹 앱] 선택 (업데이트 시 [배포 관리] -> [새 버전])
 *    - 다음 사용자 권한으로 실행: 나(Me)
 *    - 액세스 권한이 있는 사용자: 모든 사용자(Anyone)
 * 6. Vercel 대시보드 -> [Settings] -> [Environment Variables]에 아래 환경변수를 등록 후 Redeploy:
 *    - GAS_WEB_APP_URL: 생성된 웹 앱 URL (https://script.google.com/macros/s/.../exec)
 *    - GAS_API_SECRET : 4번에서 설정한 API_SECRET과 동일한 값
 *    - (Web Push OS 알림 사용 시) 터미널에서 `npx web-push generate-vapid-keys` 실행 후 추가:
 *      · VAPID_PUBLIC_KEY  : 생성된 Public Key
 *      · VAPID_PRIVATE_KEY : 생성된 Private Key
 *      · VAPID_SUBJECT     : mailto:본인이메일@gmail.com
 * 7. PC EduFlow -> [환경설정 & 명렬표] -> [Apps Script 웹 앱 URL] 및 [API Secret] 입력
 * ============================================================================
 */

var SHEET_SCHEMAS = {
  // ── 1. 기존 데스크톱 코어 시트 (100% 하위 호환 유지) ─────────────────────────
  Schedules: [
    'remote_id',
    'title',
    'category',
    'start_date',
    'end_date',
    'start_time',
    'reminder_type',
    'memo',
    'updated_at'
  ],
  Tasks: [
    'remote_id',
    'task_type',
    'title',
    'due_date',
    'priority',
    'is_mobile_task',
    'scheduled_time',
    'target_info',
    'action_payload',
    'exec_status',
    'is_done',
    'memo',
    'updated_at'
  ],
  Notifications: [
    'remote_id',
    'channel',
    'sender',
    'title',
    'message',
    'created_at',
    'is_read',
    'target_role',
    'target_student_id',
    'grade',
    'class_no'
  ],

  // ── 2. 모바일 연동 확장 시트 (12종) ──────────────────────────────────────────
  '계정_교사': [
    'remote_id',
    'teacher_id',
    'name',
    'password_hash',
    'school_name',
    'grade',
    'class_no',
    'subject',
    'phone',
    'consultation_enabled',
    'consultation_hours',
    'neis_office_code',
    'neis_school_code',
    'updated_at'
  ],
  '계정_학생': [
    'remote_id',
    'student_id',
    'name',
    'password_hash',
    'grade',
    'class_no',
    'student_no',
    'parent_id',
    'phone',
    'status',
    'updated_at'
  ],
  '계정_학부모': [
    'remote_id',
    'parent_id',
    'name',
    'password_hash',
    'student_id',
    'student_name',
    'relation',
    'phone',
    'updated_at'
  ],
  '일일_출결': [
    'remote_id',
    'date',
    'student_id',
    'student_name',
    'grade',
    'class_no',
    'student_no',
    'attendance_type',
    'reason',
    'submitted_by',
    'role',
    'teacher_confirmed',
    'memo',
    'created_at',
    'updated_at',
    'att_category',
    'att_sub_type',
    'period_info'
  ],
  '설정_서류규정': [
    'remote_id',
    'rule_code',
    'doc_name',
    'category',
    'due_business_days',
    'target_roles',
    'description',
    'is_active',
    'updated_at'
  ],
  '서류_추적': [
    'remote_id',
    'doc_type',
    'rule_code',
    'student_id',
    'student_name',
    'base_date',
    'due_date',
    'status',
    'submitted_at',
    'submitted_by',
    'file_url',
    'memo',
    'updated_at'
  ],
  '가정통신문_목록': [
    'remote_id',
    'title',
    'category',
    'content',
    'target_grade',
    'target_class',
    'due_date',
    'requires_reply',
    'reply_schema_json',
    'attachment_url',
    'author_name',
    'is_published',
    'created_at',
    'updated_at'
  ],
  '가정통신문_회신': [
    'remote_id',
    'notice_id',
    'student_id',
    'student_name',
    'parent_id',
    'parent_name',
    'consent',
    'reply_data_json',
    'signature_drive_url',
    'signature_file_id',
    'signed_at',
    'updated_at'
  ],
  '시간표_기본': [
    'remote_id',
    'grade',
    'class_no',
    'day_of_week',
    'period',
    'subject',
    'teacher_name',
    'room',
    'updated_at',
    'entry_type',
    'week_start',
    'date'
  ],
  '시간표_변동': [
    'remote_id',
    'date',
    'grade',
    'class_no',
    'period',
    'original_subject',
    'changed_subject',
    'original_teacher',
    'changed_teacher',
    'reason',
    'source',
    'updated_at'
  ],
  '급식_식단표': [
    'remote_id',
    'meal_date',
    'meal_type',
    'menu_text',
    'allergy_info',
    'calories',
    'origin_info',
    'nutrients_info',
    'source',
    'updated_at'
  ],
  '1:1_상담채팅': [
    'remote_id',
    'room_id',
    'sender_id',
    'sender_name',
    'sender_role',
    'receiver_id',
    'message',
    'is_read',
    'created_at'
  ],
  '학급회비': [
    'remote_id',
    'grade',
    'class_no',
    'date',
    'title',
    'type',
    'amount',
    'store',
    'card',
    'receipt_url',
    'updated_at'
  ],
  Push_Subscriptions: [
    'remote_id',
    'user_id',
    'role',
    'endpoint',
    'p256dh',
    'auth',
    'user_agent',
    'updated_at'
  ]
};

// 대한민국 주요 법정 공휴일 (영업일 마감 계산용, 필요 시 추가)
var KR_HOLIDAYS = [
  '2025-01-01', '2025-01-28', '2025-01-29', '2025-01-30',
  '2025-03-01', '2025-03-03', '2025-05-05', '2025-05-06',
  '2025-06-06', '2025-08-15', '2025-10-03', '2025-10-05',
  '2025-10-06', '2025-10-07', '2025-10-08', '2025-10-09',
  '2025-12-25',
  '2026-01-01', '2026-02-16', '2026-02-17', '2026-02-18',
  '2026-03-01', '2026-03-02', '2026-05-05', '2026-05-24',
  '2026-05-25', '2026-06-06', '2026-08-15', '2026-08-17',
  '2026-09-24', '2026-09-25', '2026-09-26', '2026-10-03',
  '2026-10-05', '2026-10-09', '2026-12-25',
  '2027-01-01', '2027-02-06', '2027-02-07', '2027-02-08',
  '2027-02-09', '2027-03-01', '2027-05-05', '2027-05-13',
  '2027-06-06', '2027-08-15', '2027-08-16', '2027-09-14',
  '2027-09-15', '2027-09-16', '2027-10-03', '2027-10-04',
  '2027-10-09', '2027-10-11', '2027-12-25'
];

var SIGNATURE_DRIVE_FOLDER = 'EduFlow_Signatures';
// 선택적 API 비밀키 (스크립트 속성 API_SECRET 또는 여기에 설정; 비워두면 검증 생략)
var API_SECRET = '';

/**
 * 대상 스프레드시트 객체 반환 (sheetId 파라미터가 있으면 해당 시트, 없으면 활성 시트)
 */
function _getSpreadsheet(sheetId) {
  var id = (sheetId || '').toString().trim();
  if (id) {
    try {
      return SpreadsheetApp.openById(id);
    } catch (err) {
      // 활성 시트 폴백
    }
  }
  return SpreadsheetApp.getActiveSpreadsheet();
}

/**
 * 모든 정의된 시트가 없으면 자동 생성 및 헤더 세팅
 */
function ensureAllSheets(sheetId) {
  var ss = _getSpreadsheet(sheetId);
  if (!ss) {
    throw new Error('연결된 Google 스프레드시트를 찾을 수 없습니다.');
  }

  var tz = Session.getScriptTimeZone() || 'Asia/Seoul';
  Object.keys(SHEET_SCHEMAS).forEach(function (name) {
    var headers = SHEET_SCHEMAS[name];
    var sheet = ss.getSheetByName(name);
    if (!sheet) {
      sheet = ss.insertSheet(name);
    }
    if (sheet.getLastRow() === 0) {
      sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
      sheet.getRange(1, 1, 1, headers.length)
        .setBackground('#0F172A')
        .setFontColor('#FFFFFF')
        .setFontWeight('bold');
      sheet.setFrozenRows(1);
    } else {
      var existingLastCol = Math.max(sheet.getLastColumn(), headers.length);
      var currentHeaders = sheet.getRange(1, 1, 1, existingLastCol).getValues()[0];
      var changed = false;
      for (var i = 0; i < headers.length; i++) {
        if (!currentHeaders[i]) {
          currentHeaders[i] = headers[i];
          changed = true;
        }
      }
      if (changed) {
        sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
      }
    }
  });

  return { ss: ss, timezone: tz };
}

/**
 * 수동 실행용: 전체 시트 탭 초기화
 */
function initSheets() {
  ensureAllSheets('');
  populateGuideSheet('');
}

/**
 * 시트1 탭에 전체 시트 및 변수(컬럼) 설명서를 자동으로 작성합니다.
 * Apps Script 편집기에서 `initSheets` 또는 `populateGuideSheet`를 1회 실행하세요.
 */
function populateGuideSheet(sheetId) {
  var ss = _getSpreadsheet(sheetId);
  if (!ss) {
    throw new Error('연결된 Google 스프레드시트를 찾을 수 없습니다.');
  }
  var guideSheet = ss.getSheetByName('시트1') || ss.getSheets()[0];
  if (!guideSheet) {
    guideSheet = ss.insertSheet('시트1', 0);
  }
  guideSheet.clear();

  var rows = [
    ['EduFlow 통합 DB 시트 및 변수(컬럼) 설명서', '', '', '', ''],
    ['분류 / 시트명', '컬럼명(변수)', '설명', '예시값 / 허용값', '관리 주체'],
    // 스크립트 속성 안내
    ['[스크립트 속성]', 'API_SECRET', '외부 요청 인증용 비밀키 (Vercel GAS_API_SECRET 및 PC 앱 설정과 동일)', 'my-secret-key-1234', '사용자 설정'],
    ['[스크립트 속성]', 'NEIS_API_KEY', '나이스 교육정보 개방 포털 급식 API 인증키', 'abcdef123456...', '사용자 설정'],
    ['[스크립트 속성]', 'NEIS_OFFICE_CODE', '시도교육청 코드 (충청남도교육청: N10)', 'N10', '사용자 설정'],
    ['[스크립트 속성]', 'NEIS_SCHOOL_CODE', '나이스 7자리 표준학교코드 (천안오성고: 8140246)', '8140246', '사용자 설정'],

    // 1. Schedules
    ['Schedules', 'remote_id', '행 고유 식별자 (비워두지 말 것)', 'sch_20261116_01', 'PC / GAS 자동'],
    ['Schedules', 'title', '일정 제목', '2학기 기말고사', 'PC 앱'],
    ['Schedules', 'category', '일정 분류', '학사일정 / 개인 / 행사', 'PC 앱'],
    ['Schedules', 'start_date', '시작일 (YYYY-MM-DD)', '2026-12-14', 'PC 앱'],
    ['Schedules', 'end_date', '종료일 (YYYY-MM-DD)', '2026-12-17', 'PC 앱'],
    ['Schedules', 'start_time', '시작 시각 (HH:MM)', '09:00', 'PC 앱'],
    ['Schedules', 'reminder_type', '알림 방식', 'none / toast / push', 'PC 앱'],
    ['Schedules', 'memo', '일정 메모', '감독 배정 확인', 'PC 앱'],
    ['Schedules', 'updated_at', '최종 수정 시각', '2026-11-16 09:00:00', '자동'],

    // 2. Tasks
    ['Tasks', 'remote_id', '할 일 고유 식별자', 'task_101', 'PC / 모바일'],
    ['Tasks', 'task_type', '작업 유형 (일반 할 일 / 원격 자동화 작업)', 'todo / file_sort / excel_merge / comcigan_sync', 'PC / 모바일'],
    ['Tasks', 'title', '할 일 제목', '화학 수행평가 채점', 'PC / 모바일'],
    ['Tasks', 'due_date', '마감일 (YYYY-MM-DD)', '2026-11-20', 'PC / 모바일'],
    ['Tasks', 'priority', '우선순위', 'high / normal / low', 'PC / 모바일'],
    ['Tasks', 'is_mobile_task', '모바일에서 요청한 원격 작업 여부', 'TRUE / FALSE', '모바일'],
    ['Tasks', 'scheduled_time', '예약 실행 시각', '16:30', '모바일 / PC'],
    ['Tasks', 'target_info', '작업 대상 폴더 또는 파일 경로', 'C:/Users/...', '모바일 / PC'],
    ['Tasks', 'action_payload', '원격 작업 실행용 JSON 데이터', '{"grade":1,"class":5}', '모바일 / PC'],
    ['Tasks', 'exec_status', '원격 작업 실행 상태', 'pending / running / done / error', 'PC 앱'],
    ['Tasks', 'is_done', '완료 여부', 'TRUE / FALSE', 'PC / 모바일'],
    ['Tasks', 'memo', '비고 / 실행 결과 요약', '처리 완료', 'PC / 모바일'],
    ['Tasks', 'updated_at', '최종 수정 시각', '2026-11-16 09:00:00', '자동'],

    // 3. Notifications
    ['Notifications', 'remote_id', '알림 고유 식별자', 'noti_1731700000', 'GAS / PC / 모바일'],
    ['Notifications', 'channel', '알림 채널/유형 구분', 'mobile_notice / mobile_doc_reminder / desktop', 'GAS / 모바일'],
    ['Notifications', 'sender', '발신자 이름', '유우석 선생님', '교사 / PC'],
    ['Notifications', 'title', '알림 제목', '[서류 안내] 체험학습 신청서 제출', '교사 / PC'],
    ['Notifications', 'message', '알림 본문 내용', '금요일까지 제출 바랍니다.', '교사 / PC'],
    ['Notifications', 'created_at', '생성 시각', '2026-11-16 09:00:00', '자동'],
    ['Notifications', 'is_read', '읽음 여부', 'TRUE / FALSE', '모바일 / PC'],
    ['Notifications', 'target_role', '수신 대상 역할', 'all / student / parent / teacher', '모바일'],
    ['Notifications', 'target_student_id', '특정 학생 학번 (전체일 경우 ALL)', '10501 / ALL', '모바일'],
    ['Notifications', 'grade', '대상 학년 (전체면 빈칸)', '1', '모바일'],
    ['Notifications', 'class_no', '대상 반 (전체면 빈칸)', '5', '모바일'],

    // 4. 계정_교사
    ['계정_교사', 'remote_id', '행 고유 ID (수동 입력 시 teacher_id와 동일 권장)', 'dntjr0919', '수동 / GAS'],
    ['계정_교사', 'teacher_id', '교사 로그인 아이디', 'dntjr0919', '수동 입력'],
    ['계정_교사', 'name', '교사 실명 (컴시간 교사 시간표 매칭에 사용)', '유우석', '수동 / 프로필 수정'],
    ['계정_교사', 'password_hash', '비밀번호 (평문 또는 SHA-256 해시 모두 로그인 지원)', '1234 또는 sha256값', '수동 입력'],
    ['계정_교사', 'school_name', '소속 학교명', '천안오성고', '수동 / 프로필 수정'],
    ['계정_교사', 'grade', '담임 학년 (담임 반 시간표/학급비 매칭)', '1', '수동 / 프로필 수정'],
    ['계정_교사', 'class_no', '담임 반 (담임 반 시간표/학급비 매칭)', '5', '수동 / 프로필 수정'],
    ['계정_교사', 'subject', '담당 과목', '화학', '수동 / 프로필 수정'],
    ['계정_교사', 'phone', '연락처', '010-0000-0000', '수동 / 프로필 수정'],
    ['계정_교사', 'consultation_enabled', '1:1 상담 채팅 허용 여부', 'TRUE / FALSE', '모바일 설정'],
    ['계정_교사', 'consultation_hours', '상담 가능 시간 안내 문구', '평일 16:30~18:00', '모바일 설정'],
    ['계정_교사', 'neis_office_code', '나이스 교육청 코드 (스크립트 속성 없을 시 폴백)', 'N10', '수동 / 프로필 수정'],
    ['계정_교사', 'neis_school_code', '나이스 7자리 학교코드 (스크립트 속성 없을 시 폴백)', '8140246', '수동 / 프로필 수정'],
    ['계정_교사', 'updated_at', '최종 수정 시각', '2026-11-16 09:00:00', '자동'],

    // 5. 계정_학생
    ['계정_학생', 'remote_id', '행 고유 ID (student_id와 동일 권장)', '10501', '수동 입력'],
    ['계정_학생', 'student_id', '학생 로그인 ID (학년*10000+반*100+번호 권장)', '10501', '수동 입력'],
    ['계정_학생', 'name', '학생 이름', '홍길동', '수동 입력'],
    ['계정_학생', 'password_hash', '비밀번호 (평문 또는 SHA-256 해시)', '1234', '수동 입력'],
    ['계정_학생', 'grade', '학년', '1', '수동 입력'],
    ['계정_학생', 'class_no', '반', '5', '수동 입력'],
    ['계정_학생', 'student_no', '번호', '1', '수동 입력'],
    ['계정_학생', 'parent_id', '연결된 학부모 로그인 ID', 'p10501', '수동 입력'],
    ['계정_학생', 'phone', '학생 연락처', '010-1111-2222', '수동 입력'],
    ['계정_학생', 'status', '학적 상태', '재학 / 전출 / 휴학', '수동 입력'],
    ['계정_학생', 'updated_at', '최종 수정 시각', '2026-11-16 09:00:00', '자동'],

    // 6. 계정_학부모
    ['계정_학부모', 'remote_id', '행 고유 ID (parent_id와 동일 권장)', 'p10501', '수동 입력'],
    ['계정_학부모', 'parent_id', '학부모 로그인 ID', 'p10501', '수동 입력'],
    ['계정_학부모', 'name', '학부모 성함', '홍판서', '수동 입력'],
    ['계정_학부모', 'password_hash', '비밀번호 (평문 또는 SHA-256 해시)', '1234', '수동 입력'],
    ['계정_학부모', 'student_id', '자녀 학번 (계정_학생.student_id와 매칭)', '10501', '수동 입력'],
    ['계정_학부모', 'student_name', '자녀 이름', '홍길동', '수동 입력'],
    ['계정_학부모', 'relation', '학생과의 관계', '부 / 모 / 보호자', '수동 입력'],
    ['계정_학부모', 'phone', '학부모 연락처', '010-3333-4444', '수동 입력'],
    ['계정_학부모', 'updated_at', '최종 수정 시각', '2026-11-16 09:00:00', '자동'],

    // 7. 일일_출결
    ['일일_출결', 'remote_id', '출결 신고 고유 ID', 'att_1731700000', '모바일 자동'],
    ['일일_출결', 'date', '해당 날짜 (YYYY-MM-DD)', '2026-11-16', '모바일'],
    ['일일_출결', 'student_id', '학생 학번', '10501', '모바일'],
    ['일일_출결', 'student_name', '학생 이름', '홍길동', '모바일'],
    ['일일_출결', 'grade', '학년', '1', '모바일'],
    ['일일_출결', 'class_no', '반', '5', '모바일'],
    ['일일_출결', 'student_no', '번호', '1', '모바일'],
    ['일일_출결', 'attendance_type', '출결 구분', '병결 / 인정결 / 지각 / 조퇴 / 체험학습', '모바일'],
    ['일일_출결', 'reason', '사유', '감기 몸살', '모바일'],
    ['일일_출결', 'submitted_by', '제출자 이름', '홍판서', '모바일'],
    ['일일_출결', 'role', '제출자 역할', 'student / parent', '모바일'],
    ['일일_출결', 'teacher_confirmed', '교사 확인 여부', 'TRUE / FALSE', '모바일(교사)'],
    ['일일_출결', 'memo', '교사 메모', '진단서 필요 안내함', '모바일(교사)'],
    ['일일_출결', 'created_at', '신고 시각', '2026-11-16 08:15:00', '자동'],
    ['일일_출결', 'updated_at', '수정 시각', '2026-11-16 08:20:00', '자동'],

    // 8. 설정_서류규정
    ['설정_서류규정', 'remote_id', '규정 고유 ID', 'rule_field_trip', '교사 / GAS'],
    ['설정_서류규정', 'rule_code', '규정 코드 (서류_추적과 연결 키)', 'FIELD_TRIP / MEDICAL', '교사'],
    ['설정_서류규정', 'doc_name', '서류명', '교외체험학습 신청서', '교사'],
    ['설정_서류규정', 'category', '서류 분류', '체험학습 / 출결증빙 / 동의서', '교사'],
    ['설정_서류규정', 'due_business_days', '기준일로부터 제출 마감 영업일수', '5', '교사'],
    ['설정_서류규정', 'target_roles', '대상 역할', 'student,parent', '교사'],
    ['설정_서류규정', 'description', '규정 안내 설명', '복귀 후 5영업일 이내 제출', '교사'],
    ['설정_서류규정', 'is_active', '사용 여부', 'TRUE / FALSE', '교사'],
    ['설정_서류규정', 'updated_at', '수정 시각', '2026-11-16 09:00:00', '자동'],

    // 9. 서류_추적
    ['서류_추적', 'remote_id', '서류 제출 건 고유 ID', 'doc_1731700000', '모바일 자동'],
    ['서류_추적', 'doc_type', '서류명', '교외체험학습 보고서', '모바일'],
    ['서류_추적', 'rule_code', '연결된 서류 규정 코드', 'FIELD_TRIP', '모바일'],
    ['서류_추적', 'student_id', '학생 학번', '10501', '모바일'],
    ['서류_추적', 'student_name', '학생 이름', '홍길동', '모바일'],
    ['서류_추적', 'base_date', '사유 발생일 / 기준일 (YYYY-MM-DD)', '2026-11-13', '모바일'],
    ['서류_추적', 'due_date', '자동 계산된 제출 마감일', '2026-11-20', 'GAS 자동'],
    ['서류_추적', 'status', '진행 상태', '미제출 / 제출완료 / 승인완료', '모바일'],
    ['서류_추적', 'submitted_at', '제출 일시', '2026-11-16 10:00:00', '모바일'],
    ['서류_추적', 'submitted_by', '제출자 이름', '홍길동', '모바일'],
    ['서류_추적', 'file_url', '첨부 증빙 파일 Drive URL', 'https://drive.google.com/...', 'GAS 자동'],
    ['서류_추적', 'memo', '비고', '교사 확인 완료', '교사'],
    ['서류_추적', 'updated_at', '수정 시각', '2026-11-16 10:05:00', '자동'],

    // 10. 가정통신문_목록
    ['가정통신문_목록', 'remote_id', '가정통신문 고유 ID', 'cir_202611_01', '교사 / GAS'],
    ['가정통신문_목록', 'title', '통신문 제목', '2학기 현장체험학습 안내', '교사'],
    ['가정통신문_목록', 'category', '분류', '안내 / 동의서 / 설문', '교사'],
    ['가정통신문_목록', 'content', '본문 내용', '일시 및 준비물 안내...', '교사'],
    ['가정통신문_목록', 'target_grade', '대상 학년 (전체면 빈칸)', '1', '교사'],
    ['가정통신문_목록', 'target_class', '대상 반 (전체면 빈칸)', '5', '교사'],
    ['가정통신문_목록', 'due_date', '회신 마감일 (YYYY-MM-DD)', '2026-11-22', '교사'],
    ['가정통신문_목록', 'requires_reply', '회신/서명 필요 여부', 'TRUE / FALSE', '교사'],
    ['가정통신문_목록', 'reply_schema_json', '동적 회신 항목 JSON 스키마', '[{"key":"meal","label":"중식신청"}]', '교사'],
    ['가정통신문_목록', 'attachment_url', '원본 PDF/첨부 파일 URL', 'https://drive.google.com/...', '교사'],
    ['가정통신문_목록', 'author_name', '작성 교사명', '유우석', '교사'],
    ['가정통신문_목록', 'is_published', '게시(공개) 여부', 'TRUE / FALSE', '교사'],
    ['가정통신문_목록', 'created_at', '작성 시각', '2026-11-16 09:00:00', '자동'],
    ['가정통신문_목록', 'updated_at', '수정 시각', '2026-11-16 09:00:00', '자동'],

    // 11. 가정통신문_회신
    ['가정통신문_회신', 'remote_id', '회신 건 고유 ID', 'rep_1731700000', '모바일 자동'],
    ['가정통신문_회신', 'notice_id', '대상 가정통신문 ID', 'cir_202611_01', '모바일'],
    ['가정통신문_회신', 'student_id', '학생 학번', '10501', '모바일'],
    ['가정통신문_회신', 'student_name', '학생 이름', '홍길동', '모바일'],
    ['가정통신문_회신', 'parent_id', '제출 학부모 ID', 'p10501', '모바일'],
    ['가정통신문_회신', 'parent_name', '제출 학부모 이름', '홍판서', '모바일'],
    ['가정통신문_회신', 'consent', '동의 여부', '동의 / 미동의', '모바일'],
    ['가정통신문_회신', 'reply_data_json', '추가 회신 응답 JSON', '{"meal":"신청"}', '모바일'],
    ['가정통신문_회신', 'signature_drive_url', '전자서명 PNG Drive 보기 URL', 'https://drive.google.com/...', 'GAS 자동'],
    ['가정통신문_회신', 'signature_file_id', '전자서명 Drive 파일 ID', '1AbCdEf...', 'GAS 자동'],
    ['가정통신문_회신', 'signed_at', '서명 일시', '2026-11-16 20:10:00', '자동'],
    ['가정통신문_회신', 'updated_at', '수정 시각', '2026-11-16 20:10:00', '자동'],

    // 12. 시간표_기본
    ['시간표_기본', 'remote_id', '고유 키 (tb_학년_반_요일_교시)', 'tb_1_5_월_2', 'PC(컴시간) 자동'],
    ['시간표_기본', 'grade', '학년', '1', 'PC(컴시간) 자동'],
    ['시간표_기본', 'class_no', '반', '5', 'PC(컴시간) 자동'],
    ['시간표_기본', 'day_of_week', '요일 (월~토)', '월', 'PC(컴시간) 자동'],
    ['시간표_기본', 'period', '교시 (숫자)', '2', 'PC(컴시간) 자동'],
    ['시간표_기본', 'subject', '과목명', '통과2', 'PC(컴시간) 자동'],
    ['시간표_기본', 'teacher_name', '담당 교사 실명 (교사 시간표 필터 키)', '유우석', 'PC(컴시간) 자동'],
    ['시간표_기본', 'room', '교실/특별실', '화학실', 'PC(컴시간) / 수동'],
    ['시간표_기본', 'updated_at', '동기화 시각', '2026-11-16 09:00:00', '자동'],

    // 13. 시간표_변동
    ['시간표_변동', 'remote_id', '고유 키 (tc_날짜_학년_반_교시)', 'tc_20261116_1_5_3', 'PC(컴시간) 자동'],
    ['시간표_변동', 'date', '변동 적용 날짜 (YYYY-MM-DD)', '2026-11-16', 'PC(컴시간) 자동'],
    ['시간표_변동', 'grade', '학년', '1', 'PC(컴시간) 자동'],
    ['시간표_변동', 'class_no', '반', '5', 'PC(컴시간) 자동'],
    ['시간표_변동', 'period', '교시', '3', 'PC(컴시간) 자동'],
    ['시간표_변동', 'original_subject', '기존 과목', '통과2', 'PC(컴시간) 자동'],
    ['시간표_변동', 'changed_subject', '변경된 과목', '자율', 'PC(컴시간) 자동'],
    ['시간표_변동', 'original_teacher', '기존 교사', '김현우', 'PC(컴시간) 자동'],
    ['시간표_변동', 'changed_teacher', '변경된 교사', '유우석', 'PC(컴시간) 자동'],
    ['시간표_변동', 'reason', '변동 사유', '수업 교체 / 컴시간 웹 자동 감지', 'PC(컴시간) 자동'],
    ['시간표_변동', 'source', '수집 출처', 'comcigan_playwright', 'PC(컴시간) 자동'],
    ['시간표_변동', 'updated_at', '동기화 시각', '2026-11-16 09:00:00', '자동'],

    // 14. 급식_식단표
    ['급식_식단표', 'remote_id', '고유 키 (meal_날짜_식사구분)', 'meal_20261116_중식', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'meal_date', '급식 날짜 (YYYY-MM-DD)', '2026-11-16', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'meal_type', '식사 구분', '조식 / 중식 / 석식', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'menu_text', '메뉴 목록 (줄바꿈 구분)', '현미밥\n미역국...', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'allergy_info', '알레르기 유발 정보 번호', '1.2.5.6.10', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'calories', '칼로리 정보', '785.4 Kcal', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'origin_info', '원산지 정보', '쌀:국내산...', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'nutrients_info', '영양소 정보', '탄수화물/단백질...', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'source', '수집 출처', 'NEIS_API', 'GAS(NEIS) 자동'],
    ['급식_식단표', 'updated_at', '동기화 시각', '2026-11-16 04:00:00', '자동'],

    // 15. 1:1_상담채팅
    ['1:1_상담채팅', 'remote_id', '메시지 고유 ID', 'chat_1731700000', '모바일 자동'],
    ['1:1_상담채팅', 'room_id', '채팅방 ID (학생ID 또는 학부모_학생ID)', 'room_10501', '모바일 자동'],
    ['1:1_상담채팅', 'sender_id', '보낸 사람 ID', 'p10501', '모바일'],
    ['1:1_상담채팅', 'sender_name', '보낸 사람 이름', '홍판서', '모바일'],
    ['1:1_상담채팅', 'sender_role', '보낸 사람 역할', 'teacher / student / parent', '모바일'],
    ['1:1_상담채팅', 'receiver_id', '받는 사람 ID', 'dntjr0919', '모바일'],
    ['1:1_상담채팅', 'message', '채팅 메시지 본문', '내일 진로 상담 문의드립니다.', '모바일'],
    ['1:1_상담채팅', 'is_read', '읽음 여부', 'TRUE / FALSE', '모바일'],
    ['1:1_상담채팅', 'created_at', '전송 시각', '2026-11-16 14:20:00', '자동'],

    // 16. 학급회비
    ['학급회비', 'remote_id', '회비 내역 고유 ID', 'bdg_1731700000', '모바일 자동'],
    ['학급회비', 'grade', '해당 학년', '1', '모바일(교사)'],
    ['학급회비', 'class_no', '해당 반', '5', '모바일(교사)'],
    ['학급회비', 'date', '거래 날짜 (YYYY-MM-DD)', '2026-11-16', '모바일(교사)'],
    ['학급회비', 'title', '항목명 / 내역 제목', '체육대회 간식 구입', '모바일(교사)'],
    ['학급회비', 'type', '입금 / 출금 구분', '수입 / 지출 (또는 income / expense)', '모바일(교사)'],
    ['학급회비', 'amount', '금액 (숫자, 원)', '45000', '모바일(교사)'],
    ['학급회비', 'store', '사용처 / 거래처', '파리바게뜨', '모바일(교사)'],
    ['학급회비', 'card', '결제 수단 / 카드명', '학급카드 / 현금', '모바일(교사)'],
    ['학급회비', 'receipt_url', '영수증 이미지/Drive URL', 'https://drive.google.com/...', '모바일(교사)'],
    ['학급회비', 'updated_at', '수정 시각', '2026-11-16 15:00:00', '자동'],

    // 17. Push_Subscriptions
    ['Push_Subscriptions', 'remote_id', '구독 고유 ID', 'push_1731700000', '모바일 자동'],
    ['Push_Subscriptions', 'user_id', '구독한 사용자 로그인 ID', 'p10501 / dntjr0919', '모바일 자동'],
    ['Push_Subscriptions', 'role', '사용자 역할 (학부모 매칭에 사용)', 'teacher / student / parent', '모바일 자동'],
    ['Push_Subscriptions', 'endpoint', '브라우저 푸시 서버 엔드포인트 URL', 'https://fcm.googleapis.com/fcm/send/...', '브라우저 자동'],
    ['Push_Subscriptions', 'p256dh', 'Web Push 암호화 공개키', 'BEl62i...', '브라우저 자동'],
    ['Push_Subscriptions', 'auth', 'Web Push 인증 시크릿', 'tBHItJ...', '브라우저 자동'],
    ['Push_Subscriptions', 'user_agent', '구독한 기기/브라우저 정보', 'Mozilla/5.0 (Linux; Android...)', '브라우저 자동'],
    ['Push_Subscriptions', 'updated_at', '구독 갱신 시각', '2026-11-16 09:00:00', '자동']
  ];

  // 1행 타이틀 병합 및 스타일링
  guideSheet.getRange(1, 1, rows.length, 5).setValues(rows);
  guideSheet.getRange(1, 1, 1, 5).merge()
    .setBackground('#1E293B')
    .setFontColor('#FFFFFF')
    .setFontWeight('bold')
    .setFontSize(13)
    .setVerticalAlignment('middle');
  guideSheet.setRowHeight(1, 34);

  // 2행 테이블 헤더 스타일링
  guideSheet.getRange(2, 1, 1, 5)
    .setBackground('#0F172A')
    .setFontColor('#FFFFFF')
    .setFontWeight('bold')
    .setVerticalAlignment('middle');
  guideSheet.setRowHeight(2, 26);

  // 시트 그룹이 바뀔 때마다 옅은 배경색 교차 적용
  var prevSheet = '';
  var toggle = false;
  for (var r = 2; r < rows.length; r++) {
    var sheetLabel = rows[r][0];
    if (sheetLabel !== prevSheet) {
      toggle = !toggle;
      prevSheet = sheetLabel;
    }
    var bg = sheetLabel === '[스크립트 속성]' ? '#FEF3C7' : (toggle ? '#F8FAFC' : '#FFFFFF');
    guideSheet.getRange(r + 1, 1, 1, 5).setBackground(bg);
  }

  guideSheet.getRange(2, 1, rows.length - 1, 5)
    .setVerticalAlignment('middle')
    .setWrap(true);
  guideSheet.getRange(3, 1, rows.length - 2, 2).setFontWeight('bold');
  guideSheet.setFrozenRows(2);

  guideSheet.setColumnWidth(1, 150);
  guideSheet.setColumnWidth(2, 180);
  guideSheet.setColumnWidth(3, 420);
  guideSheet.setColumnWidth(4, 240);
  guideSheet.setColumnWidth(5, 130);
}


/**
 * SHA-256 해시 유틸리티 (소문자 64자리 Hex 반환)
 */
function _sha256Hex(text) {
  var raw = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256,
    (text || '').toString(),
    Utilities.Charset.UTF_8
  );
  var hex = '';
  for (var i = 0; i < raw.length; i++) {
    var v = (raw[i] < 0) ? raw[i] + 256 : raw[i];
    var h = v.toString(16);
    if (h.length === 1) {
      h = '0' + h;
    }
    hex += h;
  }
  return hex;
}

/**
 * 테스트용 샘플 데이터 삽입 (데스크톱 + 모바일 연동 시트 초기 데이터)
 */
function seedSampleData() {
  var info = ensureAllSheets('');
  var ss = info.ss;
  var tz = info.timezone;
  var nowStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss');
  var todayStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd');

  var schedulesSheet = ss.getSheetByName('Schedules');
  if (schedulesSheet.getLastRow() <= 1) {
    schedulesSheet.appendRow([
      'sch_sample_001',
      '[학사] 2학기 기말고사 집중 관리 기간',
      '학사일정',
      todayStr,
      todayStr,
      '09:00',
      'morning_9am',
      '시험지 보안 점검 및 감독 시간표 확인',
      nowStr
    ]);
  }

  var tasksSheet = ss.getSheetByName('Tasks');
  if (tasksSheet.getLastRow() <= 1) {
    tasksSheet.appendRow([
      'mob_sample_001',
      'file_sort',
      '교무실 PC 자동 분류: 3반 수행평가 제출물 폴더 정리',
      todayStr,
      'high',
      1,
      todayStr + ' 14:30',
      '다운로드 / 3반_과제',
      JSON.stringify({ source_dir: '', action: 'run_batch_sort' }),
      'pending',
      0,
      '모바일 교무 수첩에서 예약됨',
      nowStr
    ]);
    tasksSheet.appendRow([
      'todo_sample_001',
      'todo',
      '나이스(NEIS) 동아리 활동 특기사항 초안 검토',
      todayStr,
      'normal',
      0,
      '',
      '',
      '{}',
      'pending',
      0,
      '2학년 3반 기준',
      nowStr
    ]);
  }

  var notifySheet = ss.getSheetByName('Notifications');
  if (notifySheet.getLastRow() <= 1) {
    notifySheet.appendRow([
      'noti_sample_001',
      'google_sheets',
      '학부모 상담 설문지',
      '새로운 상담 신청 응답이 도착했습니다',
      '10101 김민준 학부모님 - 11/18(수) 16:00 희망',
      nowStr,
      0
    ]);
  }

  // 모바일 샘플 계정 (초기 비밀번호: 1234 -> SHA-256)
  var defaultPwHash = _sha256Hex('1234');
  var teacherSheet = ss.getSheetByName('계정_교사');
  if (teacherSheet.getLastRow() <= 1) {
    teacherSheet.appendRow([
      'teacher_001', 'teacher01', '김선생', defaultPwHash, '에듀플로우고',
      '2', '3', '정보', '010-1234-5678', 1, '09:00~17:00', '', '', nowStr
    ]);
  }

  var studentSheet = ss.getSheetByName('계정_학생');
  if (studentSheet.getLastRow() <= 1) {
    studentSheet.appendRow([
      'stu_20301', '20301', '김민준', defaultPwHash, '2', '3', '1',
      'parent_20301', '010-2222-3333', 'active', nowStr
    ]);
  }

  var parentSheet = ss.getSheetByName('계정_학부모');
  if (parentSheet.getLastRow() <= 1) {
    parentSheet.appendRow([
      'parent_20301', 'parent01', '김민준 학부모', defaultPwHash,
      '20301', '김민준', '모', '010-4444-5555', nowStr
    ]);
  }

  var ruleSheet = ss.getSheetByName('설정_서류규정');
  if (ruleSheet.getLastRow() <= 1) {
    ruleSheet.appendRow([
      'rule_absence', 'ABSENCE_REPORT', '결석계 및 증빙서류', '출결서류',
      5, 'student,parent', '결석일 기준 5영업일 이내 제출', 1, nowStr
    ]);
    ruleSheet.appendRow([
      'rule_fieldtrip', 'FIELD_TRIP', '교외체험학습 결과보고서', '체험학습',
      7, 'student,parent', '체험학습 종료 후 7영업일 이내 제출', 1, nowStr
    ]);
  }
}

/**
 * 날짜/시간 포맷 헬퍼 (시트 Date 셀 -> 문자열 직렬화)
 */
function _formatCellValue(key, val, tz) {
  if (val instanceof Date) {
    if (key === 'start_time') {
      return Utilities.formatDate(val, tz, 'HH:mm');
    }
    if (
      key === 'start_date' ||
      key === 'end_date' ||
      key === 'due_date' ||
      key === 'date' ||
      key === 'week_start' ||
      key === 'base_date' ||
      key === 'meal_date'
    ) {
      return Utilities.formatDate(val, tz, 'yyyy-MM-dd');
    }
    return Utilities.formatDate(val, tz, 'yyyy-MM-dd HH:mm:ss');
  }
  if (val === null || val === undefined) {
    return '';
  }
  return val;
}

/**
 * 시트 전체 행을 객체 배열로 직렬화
 */
function _readSheetAsObjects(sheet, schemaHeaders, tz) {
  if (!sheet || sheet.getLastRow() <= 1) {
    return [];
  }
  var numRows = sheet.getLastRow() - 1;
  var numCols = Math.max(sheet.getLastColumn(), schemaHeaders.length);
  var rawHeaders = sheet.getRange(1, 1, 1, numCols).getValues()[0];
  var headers = [];
  for (var c = 0; c < numCols; c++) {
    headers.push((rawHeaders[c] || schemaHeaders[c] || ('col_' + c)).toString().trim());
  }

  var values = sheet.getRange(2, 1, numRows, numCols).getValues();
  var out = [];
  for (var r = 0; r < values.length; r++) {
    var row = values[r];
    var hasContent = false;
    var obj = {};
    for (var col = 0; col < headers.length; col++) {
      var key = headers[col];
      var val = _formatCellValue(key, row[col], tz);
      if (val !== '') {
        hasContent = true;
      }
      obj[key] = val;
    }
    if (hasContent) {
      if (!obj.remote_id) {
        obj.remote_id = sheet.getName().toLowerCase() + '_row_' + (r + 2);
      }
      out.push(obj);
    }
  }
  return out;
}

/**
 * remote_id 기준으로 행 업서트(Upsert)
 */
function _upsertRowByRemoteId(sheet, schemaHeaders, data, tz) {
  var rid = (data.remote_id || '').toString().trim();
  if (!rid) {
    rid = sheet.getName().toLowerCase() + '_' + new Date().getTime() + '_' + Math.floor(Math.random() * 1000);
    data.remote_id = rid;
  }
  var nowStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss');
  if (schemaHeaders.indexOf('updated_at') >= 0 && !data.updated_at) {
    data.updated_at = nowStr;
  }
  if (schemaHeaders.indexOf('created_at') >= 0 && !data.created_at) {
    data.created_at = nowStr;
  }

  var lastRow = sheet.getLastRow();
  var targetRow = -1;
  if (lastRow > 1) {
    var ids = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
    for (var i = 0; i < ids.length; i++) {
      if ((ids[i][0] || '').toString().trim() === rid) {
        targetRow = i + 2;
        break;
      }
    }
  }

  var rowValues = schemaHeaders.map(function (colName) {
    if (data.hasOwnProperty(colName) && data[colName] !== undefined && data[colName] !== null) {
      return typeof data[colName] === 'object' ? JSON.stringify(data[colName]) : data[colName];
    }
    return '';
  });

  if (targetRow > 0) {
    var prevValues = sheet.getRange(targetRow, 1, 1, schemaHeaders.length).getValues()[0];
    for (var j = 0; j < schemaHeaders.length; j++) {
      var col = schemaHeaders[j];
      if (!data.hasOwnProperty(col)) {
        rowValues[j] = prevValues[j];
      }
    }
    sheet.getRange(targetRow, 1, 1, schemaHeaders.length).setValues([rowValues]);
  } else {
    sheet.appendRow(rowValues);
  }
  return rid;
}

/**
 * remote_id 기준으로 다수 행을 1회 일괄 조회 + setValues()로 대량 업서트(Bulk Upsert)
 */
function _bulkUpsertRowsByRemoteId(sheet, schemaHeaders, items, tz) {
  if (!sheet || !Array.isArray(items) || items.length === 0) {
    return 0;
  }
  var numCols = schemaHeaders.length;
  var lastRow = sheet.getLastRow();
  var existingValues = lastRow > 1 ? sheet.getRange(2, 1, lastRow - 1, numCols).getValues() : [];

  var ridToIndex = {};
  for (var i = 0; i < existingValues.length; i++) {
    var existingRid = (existingValues[i][0] || '').toString().trim();
    if (existingRid) {
      ridToIndex[existingRid] = i;
    }
  }

  var nowStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss');
  var hasUpdatedAt = schemaHeaders.indexOf('updated_at') >= 0;
  var hasCreatedAt = schemaHeaders.indexOf('created_at') >= 0;
  var newRows = [];
  var newRidToIndex = {};
  var hasUpdates = false;
  var processed = 0;

  for (var k = 0; k < items.length; k++) {
    var rawItem = items[k];
    if (!rawItem || typeof rawItem !== 'object') {
      continue;
    }
    processed++;
    var rid = (rawItem.remote_id || '').toString().trim();
    if (!rid) {
      rid = sheet.getName().toLowerCase() + '_' + new Date().getTime() + '_' + k + '_' + Math.floor(Math.random() * 1000);
    }

    if (ridToIndex.hasOwnProperty(rid)) {
      var exIdx = ridToIndex[rid];
      var exRow = existingValues[exIdx];
      for (var j = 0; j < numCols; j++) {
        var colName = schemaHeaders[j];
        if (colName === 'remote_id') {
          exRow[j] = rid;
        } else if (colName === 'updated_at' && !rawItem.updated_at) {
          exRow[j] = nowStr;
        } else if (rawItem.hasOwnProperty(colName) && rawItem[colName] !== undefined && rawItem[colName] !== null) {
          exRow[j] = typeof rawItem[colName] === 'object' ? JSON.stringify(rawItem[colName]) : rawItem[colName];
        }
      }
      hasUpdates = true;
    } else if (newRidToIndex.hasOwnProperty(rid)) {
      var nrIdx = newRidToIndex[rid];
      var nrRow = newRows[nrIdx];
      for (var j2 = 0; j2 < numCols; j2++) {
        var col2 = schemaHeaders[j2];
        if (col2 === 'remote_id') {
          nrRow[j2] = rid;
        } else if (col2 === 'updated_at' && !rawItem.updated_at) {
          nrRow[j2] = nowStr;
        } else if (rawItem.hasOwnProperty(col2) && rawItem[col2] !== undefined && rawItem[col2] !== null) {
          nrRow[j2] = typeof rawItem[col2] === 'object' ? JSON.stringify(rawItem[col2]) : rawItem[col2];
        }
      }
    } else {
      var rowValues = schemaHeaders.map(function (cName) {
        if (cName === 'remote_id') {
          return rid;
        }
        if (cName === 'updated_at' && hasUpdatedAt && !rawItem.updated_at) {
          return nowStr;
        }
        if (cName === 'created_at' && hasCreatedAt && !rawItem.created_at) {
          return nowStr;
        }
        if (rawItem.hasOwnProperty(cName) && rawItem[cName] !== undefined && rawItem[cName] !== null) {
          return typeof rawItem[cName] === 'object' ? JSON.stringify(rawItem[cName]) : rawItem[cName];
        }
        return '';
      });
      newRidToIndex[rid] = newRows.length;
      newRows.push(rowValues);
    }
  }

  if (hasUpdates && existingValues.length > 0) {
    sheet.getRange(2, 1, existingValues.length, numCols).setValues(existingValues);
  }
  if (newRows.length > 0) {
    var startNewRow = Math.max(2, lastRow + 1);
    sheet.getRange(startNewRow, 1, newRows.length, numCols).setValues(newRows);
  }
  return processed;
}

function _deleteRowByRemoteId(sheet, remoteId) {
  var rid = (remoteId || '').toString().trim();
  if (!rid || sheet.getLastRow() <= 1) {
    return false;
  }
  var ids = sheet.getRange(2, 1, sheet.getLastRow() - 1, 1).getValues();
  for (var i = ids.length - 1; i >= 0; i--) {
    if ((ids[i][0] || '').toString().trim() === rid) {
      sheet.deleteRow(i + 2);
      return true;
    }
  }
  return false;
}

function _jsonResponse(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * 주말 및 대한민국 공휴일 여부 판별
 */
function _isNonBusinessDay(dateObj, tz) {
  var day = dateObj.getDay(); // 0=Sun, 6=Sat
  if (day === 0 || day === 6) {
    return true;
  }
  var ymd = Utilities.formatDate(dateObj, tz, 'yyyy-MM-dd');
  return KR_HOLIDAYS.indexOf(ymd) >= 0;
}

/**
 * 기준일(yyyy-MM-dd)로부터 N 영업일(주말·공휴일 제외) 이후 날짜 계산
 */
function _addBusinessDays(baseDateStr, businessDays, tz) {
  var str = (baseDateStr || '').toString().trim();
  var parts = str.split('-');
  var d;
  if (parts.length === 3) {
    d = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10), 12, 0, 0);
  } else {
    d = new Date();
  }
  var daysToAdd = parseInt(businessDays, 10);
  if (isNaN(daysToAdd) || daysToAdd <= 0) {
    return Utilities.formatDate(d, tz, 'yyyy-MM-dd');
  }
  var added = 0;
  var guard = 0;
  while (added < daysToAdd && guard < 365) {
    d.setDate(d.getDate() + 1);
    if (!_isNonBusinessDay(d, tz)) {
      added++;
    }
    guard++;
  }
  return Utilities.formatDate(d, tz, 'yyyy-MM-dd');
}

/**
 * Base64 PNG 전자서명을 Google Drive 폴더에 저장하고 URL 반환
 */
function _saveSignatureToDrive(base64Data, fileName, folderName) {
  var raw = (base64Data || '').toString().trim();
  if (!raw) {
    return null;
  }
  var clean = raw.replace(/^data:image\/[a-zA-Z0-9+.-]+;base64,/, '');
  var bytes = Utilities.base64Decode(clean);
  var safeName = (fileName || ('signature_' + new Date().getTime() + '.png')).toString();
  var blob = Utilities.newBlob(bytes, 'image/png', safeName);

  var targetFolderName = folderName || SIGNATURE_DRIVE_FOLDER;
  var folders = DriveApp.getFoldersByName(targetFolderName);
  var folder = folders.hasNext() ? folders.next() : DriveApp.createFolder(targetFolderName);

  var file = folder.createFile(blob);
  try {
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
  } catch (err) {
    // 조직 보안 정책으로 외부 공유 제한 시 기본 권한 유지
  }
  var fileId = file.getId();
  return {
    file_id: fileId,
    url: file.getUrl(),
    download_url: 'https://drive.google.com/uc?export=view&id=' + fileId
  };
}

/**
 * 비밀번호 해시 제거 후 안전한 사용자 프로필 반환
 */
function _sanitizeUser(role, row) {
  var copy = {};
  Object.keys(row).forEach(function (k) {
    if (k !== 'password_hash') {
      copy[k] = row[k];
    }
  });
  copy.role = role;
  return copy;
}

/**
 * 역할별 SHA-256 로그인 인증
 */
function _authenticateUser(ss, tz, role, userId, rawPassword, passwordHash) {
  var sheetMap = {
    teacher: { name: '계정_교사', idCol: 'teacher_id' },
    student: { name: '계정_학생', idCol: 'student_id' },
    parent: { name: '계정_학부모', idCol: 'parent_id' }
  };
  var cfg = sheetMap[(role || '').toString().trim().toLowerCase()];
  if (!cfg) {
    return { ok: false, error: '지원하지 않는 역할(role)입니다: ' + role };
  }

  var uid = (userId || '').toString().trim();
  if (!uid) {
    return { ok: false, error: '아이디를 입력해 주세요.' };
  }

  var inputHash = (passwordHash || '').toString().trim().toLowerCase();
  if (!inputHash && rawPassword !== undefined && rawPassword !== null && rawPassword !== '') {
    inputHash = _sha256Hex(rawPassword.toString());
  }
  if (!inputHash) {
    return { ok: false, error: '비밀번호를 입력해 주세요.' };
  }

  var rows = _readSheetAsObjects(ss.getSheetByName(cfg.name), SHEET_SCHEMAS[cfg.name], tz);
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var rowId = (r[cfg.idCol] || '').toString().trim();
    var remoteId = (r.remote_id || '').toString().trim();
    if (rowId === uid || remoteId === uid) {
      var stored = (r.password_hash || '').toString().trim().toLowerCase();
      var isMatched = (stored === inputHash);
      if (!isMatched && stored && !/^[0-9a-f]{64}$/.test(stored) && rawPassword) {
        // 시트에 평문 임시 비밀번호가 입력된 경우 호환
        isMatched = (stored === rawPassword.toString().trim());
      }
      if (!isMatched) {
        return { ok: false, error: '비밀번호가 일치하지 않습니다.' };
      }
      return { ok: true, user: _sanitizeUser(role, r) };
    }
  }
  return { ok: false, error: '등록된 계정을 찾을 수 없습니다.' };
}

/**
 * 나이스(NEIS) 급식 Open API (mealServiceDietInfo) 수집 및 급식_식단표 시트 업서트
 */
function fetchNeisMeals(fromYmd, toYmd, sheetId) {
  var info = ensureAllSheets(sheetId || '');
  var ss = info.ss;
  var tz = info.timezone;
  var props = PropertiesService.getScriptProperties();

  var apiKey = (props.getProperty('NEIS_API_KEY') || '').trim();
  var officeCode = (props.getProperty('NEIS_OFFICE_CODE') || '').trim();
  var schoolCode = (props.getProperty('NEIS_SCHOOL_CODE') || '').trim();

  // 스크립트 속성에 학교 코드가 없으면 계정_교사 첫 행에서 보조 조회
  if (!officeCode || !schoolCode) {
    var teachers = _readSheetAsObjects(ss.getSheetByName('계정_교사'), SHEET_SCHEMAS['계정_교사'], tz);
    if (teachers.length > 0) {
      officeCode = officeCode || (teachers[0].neis_office_code || '').toString().trim();
      schoolCode = schoolCode || (teachers[0].neis_school_code || '').toString().trim();
    }
  }

  if (!apiKey || !officeCode || !schoolCode) {
    return {
      ok: false,
      error: 'NEIS_API_KEY, NEIS_OFFICE_CODE, NEIS_SCHOOL_CODE 스크립트 속성이 설정되지 않았습니다.'
    };
  }

  var now = new Date();
  var end = new Date(now.getTime() + 14 * 24 * 60 * 60 * 1000);
  var startStr = (fromYmd || Utilities.formatDate(now, tz, 'yyyyMMdd')).toString().replace(/-/g, '');
  var endStr = (toYmd || Utilities.formatDate(end, tz, 'yyyyMMdd')).toString().replace(/-/g, '');

  var url = 'https://open.neis.go.kr/hub/mealServiceDietInfo'
    + '?KEY=' + encodeURIComponent(apiKey)
    + '&Type=json&pIndex=1&pSize=100'
    + '&ATPT_OFCDC_SC_CODE=' + encodeURIComponent(officeCode)
    + '&SD_SCHUL_CODE=' + encodeURIComponent(schoolCode)
    + '&MLSV_FROM_YMD=' + encodeURIComponent(startStr)
    + '&MLSV_TO_YMD=' + encodeURIComponent(endStr);

  var resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  var json = JSON.parse(resp.getContentText());
  if (!json.mealServiceDietInfo || !json.mealServiceDietInfo[1] || !json.mealServiceDietInfo[1].row) {
    return { ok: true, count: 0, message: '조회된 급식 데이터가 없습니다.' };
  }

  var rows = json.mealServiceDietInfo[1].row;
  var mealSheet = ss.getSheetByName('급식_식단표');
  var count = 0;

  rows.forEach(function (item) {
    var ymd = (item.MLSV_YMD || '').toString().trim();
    var formattedDate = ymd.length === 8
      ? (ymd.slice(0, 4) + '-' + ymd.slice(4, 6) + '-' + ymd.slice(6, 8))
      : ymd;
    var mealCode = (item.MMEAL_SC_CODE || '2').toString().trim();
    var mealType = (item.MMEAL_SC_NM || '중식').toString().trim();
    var rawDish = (item.DDISH_NM || '').toString().replace(/<br\s*\/?>/gi, '\n').trim();
    var allergyMatches = rawDish.match(/\(([0-9.]+)\)/g) || [];
    var cleanMenu = rawDish.replace(/\s*\([0-9.]+\)/g, '').trim();

    _upsertRowByRemoteId(mealSheet, SHEET_SCHEMAS['급식_식단표'], {
      remote_id: 'meal_' + ymd + '_' + mealCode,
      meal_date: formattedDate,
      meal_type: mealType,
      menu_text: cleanMenu,
      allergy_info: allergyMatches.join(' '),
      calories: (item.CAL_INFO || '').toString().trim(),
      origin_info: (item.ORPLC_INFO || '').toString().replace(/<br\s*\/?>/gi, ', ').trim(),
      nutrients_info: (item.NTR_INFO || '').toString().replace(/<br\s*\/?>/gi, ', ').trim(),
      source: 'neis_openapi'
    }, tz);
    count++;
  });

  return { ok: true, count: count };
}

/**
 * 매일 새벽 4시 NEIS 급식 자동 수집 트리거용 핸들러
 */
function scheduledFetchNeisMeals() {
  return fetchNeisMeals('', '', '');
}

/**
 * NEIS 급식 새벽 자동 수집 트리거 등록 (수동 1회 실행)
 */
function installNeisMealTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'scheduledFetchNeisMeals') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger('scheduledFetchNeisMeals')
    .timeBased()
    .everyDays(1)
    .atHour(4)
    .create();
}

/**
 * 선택적 API_SECRET 검증 (PropertiesService 'API_SECRET' 우선, 없으면 상단 API_SECRET 사용)
 * - 미설정(빈 문자열) 시 하위 호환을 위해 통과
 * - 설정 시 GET e.parameter.api_secret 또는 POST body.api_secret 일치 여부 검증
 */
function _verifyApiSecret(e, body) {
  var expected = '';
  try {
    expected = (PropertiesService.getScriptProperties().getProperty('API_SECRET') || '').toString().trim();
  } catch (err) {
    expected = '';
  }
  if (!expected) {
    expected = (API_SECRET || '').toString().trim();
  }
  if (!expected) {
    return true;
  }
  var provided = '';
  if (body && body.api_secret !== undefined && body.api_secret !== null) {
    provided = body.api_secret.toString().trim();
  } else if (e && e.parameter && e.parameter.api_secret !== undefined && e.parameter.api_secret !== null) {
    provided = e.parameter.api_secret.toString().trim();
  }
  return provided === expected;
}

/**
 * GET 엔드포인트
 * - 기존 호환: ?action=ping, ?action=pull_all (기본값)
 * - 모바일 확장: ?action=get_mobile_bundle, ?action=get_chat_messages,
 *               ?action=get_consultation_status, ?action=calculate_due_date,
 *               ?action=list_sheet&sheet=...
 */
function doGet(e) {
  try {
    if (!_verifyApiSecret(e, null)) {
      return _jsonResponse({ ok: false, error: 'Unauthorized' });
    }
    var params = (e && e.parameter) ? e.parameter : {};
    var action = (params.action || 'pull_all').toString().trim();
    var sheetId = (params.sheetId || '').toString().trim();
    var info = ensureAllSheets(sheetId);
    var ss = info.ss;
    var tz = info.timezone;
    var nowStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss');

    if (action === 'ping') {
      var schedules = _readSheetAsObjects(ss.getSheetByName('Schedules'), SHEET_SCHEMAS.Schedules, tz);
      var tasks = _readSheetAsObjects(ss.getSheetByName('Tasks'), SHEET_SCHEMAS.Tasks, tz);
      var notifications = _readSheetAsObjects(ss.getSheetByName('Notifications'), SHEET_SCHEMAS.Notifications, tz);
      return _jsonResponse({
        ok: true,
        status: 'connected',
        spreadsheet_name: ss.getName(),
        spreadsheet_id: ss.getId(),
        sheets: ['Schedules', 'Tasks', 'Notifications'],
        all_sheets: Object.keys(SHEET_SCHEMAS),
        counts: {
          schedules: schedules.length,
          tasks: tasks.length,
          notifications: notifications.length
        },
        server_time: nowStr
      });
    }

    if (action === 'calculate_due_date') {
      var baseDate = (params.base_date || Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd')).toString().trim();
      var days = parseInt(params.due_business_days || '0', 10);
      var ruleCode = (params.rule_code || '').toString().trim();
      if (ruleCode && !days) {
        var rules = _readSheetAsObjects(ss.getSheetByName('설정_서류규정'), SHEET_SCHEMAS['설정_서류규정'], tz);
        for (var rIdx = 0; rIdx < rules.length; rIdx++) {
          if (rules[rIdx].rule_code === ruleCode) {
            days = parseInt(rules[rIdx].due_business_days || '0', 10);
            break;
          }
        }
      }
      return _jsonResponse({
        ok: true,
        base_date: baseDate,
        due_business_days: isNaN(days) ? 0 : days,
        due_date: _addBusinessDays(baseDate, isNaN(days) ? 0 : days, tz)
      });
    }

    if (action === 'get_consultation_status') {
      var teachers = _readSheetAsObjects(ss.getSheetByName('계정_교사'), SHEET_SCHEMAS['계정_교사'], tz);
      var teacherId = (params.teacher_id || '').toString().trim();
      var target = teachers[0] || {};
      if (teacherId) {
        for (var tIdx = 0; tIdx < teachers.length; tIdx++) {
          if (teachers[tIdx].teacher_id === teacherId || teachers[tIdx].remote_id === teacherId) {
            target = teachers[tIdx];
            break;
          }
        }
      }
      return _jsonResponse({
        ok: true,
        teacher_id: target.teacher_id || '',
        teacher_name: target.name || '',
        consultation_enabled: String(target.consultation_enabled || '0') === '1' || target.consultation_enabled === true || target.consultation_enabled === 1,
        consultation_hours: target.consultation_hours || ''
      });
    }

    if (action === 'get_chat_messages') {
      var roomId = (params.room_id || '').toString().trim();
      var chats = _readSheetAsObjects(ss.getSheetByName('1:1_상담채팅'), SHEET_SCHEMAS['1:1_상담채팅'], tz);
      var filteredChats = roomId
        ? chats.filter(function (c) { return (c.room_id || '').toString().trim() === roomId; })
        : chats;
      return _jsonResponse({
        ok: true,
        room_id: roomId,
        messages: filteredChats,
        server_time: nowStr
      });
    }

    if (action === 'list_sheet') {
      var sheetName = (params.sheet || '').toString().trim();
      if (!SHEET_SCHEMAS[sheetName]) {
        return _jsonResponse({ ok: false, error: '알 수 없는 시트 이름: ' + sheetName });
      }
      var rows = _readSheetAsObjects(ss.getSheetByName(sheetName), SHEET_SCHEMAS[sheetName], tz);
      return _jsonResponse({
        ok: true,
        sheet: sheetName,
        rows: rows,
        server_time: nowStr
      });
    }

    if (action === 'get_push_subscriptions') {
      var subs = _readSheetAsObjects(ss.getSheetByName('Push_Subscriptions'), SHEET_SCHEMAS.Push_Subscriptions, tz);
      var filterRole = (params.target_role || '').toString().trim().toLowerCase();
      var filterIdsRaw = (params.target_student_ids || '').toString().trim();
      var filterIds = filterIdsRaw ? filterIdsRaw.split(',').map(function (s) { return String(s).trim(); }).filter(Boolean) : [];
      var filterUserId = (params.target_user_id || '').toString().trim();
      var filterEndpoint = (params.endpoint || '').toString().trim();

      var parentToStudent = {};
      var parentRows = _readSheetAsObjects(ss.getSheetByName('계정_학부모'), SHEET_SCHEMAS['계정_학부모'], tz);
      for (var pi = 0; pi < parentRows.length; pi++) {
        var pid = String(parentRows[pi].parent_id || '').trim();
        var psid = String(parentRows[pi].student_id || '').trim();
        if (pid && psid) parentToStudent[pid] = psid;
      }

      var filteredSubs = subs.filter(function (sub) {
        var subRole = String(sub.role || '').trim().toLowerCase();
        var subUserId = String(sub.user_id || '').trim();
        var subEndpoint = String(sub.endpoint || '').trim();

        if (filterEndpoint && subEndpoint !== filterEndpoint) {
          return false;
        }
        if (filterUserId) {
          return subUserId === filterUserId;
        }
        if (filterRole && filterRole !== 'all' && subRole !== filterRole) {
          return false;
        }
        if (filterIds.length > 0 && filterIds.indexOf('ALL') < 0 && filterIds.indexOf('all') < 0) {
          if (filterIds.indexOf(subUserId) >= 0) return true;
          if (subRole === 'parent' && parentToStudent[subUserId] && filterIds.indexOf(parentToStudent[subUserId]) >= 0) {
            return true;
          }
          return false;
        }
        return true;
      });
      return _jsonResponse({
        ok: true,
        subscriptions: filteredSubs,
        server_time: nowStr
      });
    }

    if (action === 'get_mobile_bundle') {
      var role = (params.role || '').toString().trim().toLowerCase();
      var studentId = (params.student_id || '').toString().trim();
      var grade = (params.grade || '').toString().trim();
      var classNo = (params.class_no || '').toString().trim();

      var teachersList = _readSheetAsObjects(ss.getSheetByName('계정_교사'), SHEET_SCHEMAS['계정_교사'], tz)
        .map(function (t) { return _sanitizeUser('teacher', t); });
      var studentsList = _readSheetAsObjects(ss.getSheetByName('계정_학생'), SHEET_SCHEMAS['계정_학생'], tz)
        .map(function (s) { return _sanitizeUser('student', s); });
      var attendance = _readSheetAsObjects(ss.getSheetByName('일일_출결'), SHEET_SCHEMAS['일일_출결'], tz);
      var docRules = _readSheetAsObjects(ss.getSheetByName('설정_서류규정'), SHEET_SCHEMAS['설정_서류규정'], tz);
      var docTracking = _readSheetAsObjects(ss.getSheetByName('서류_추적'), SHEET_SCHEMAS['서류_추적'], tz);
      var notices = _readSheetAsObjects(ss.getSheetByName('가정통신문_목록'), SHEET_SCHEMAS['가정통신문_목록'], tz);
      var noticeReplies = _readSheetAsObjects(ss.getSheetByName('가정통신문_회신'), SHEET_SCHEMAS['가정통신문_회신'], tz);
      var timetableBase = _readSheetAsObjects(ss.getSheetByName('시간표_기본'), SHEET_SCHEMAS['시간표_기본'], tz);
      var timetableChanges = _readSheetAsObjects(ss.getSheetByName('시간표_변동'), SHEET_SCHEMAS['시간표_변동'], tz);
      var meals = _readSheetAsObjects(ss.getSheetByName('급식_식단표'), SHEET_SCHEMAS['급식_식단표'], tz);
      var budgetList = _readSheetAsObjects(ss.getSheetByName('학급회비'), SHEET_SCHEMAS['학급회비'], tz);
      var chatsAll = _readSheetAsObjects(ss.getSheetByName('1:1_상담채팅'), SHEET_SCHEMAS['1:1_상담채팅'], tz);
      var tasksAll = _readSheetAsObjects(ss.getSheetByName('Tasks'), SHEET_SCHEMAS.Tasks, tz);
      var schedulesAll = _readSheetAsObjects(ss.getSheetByName('Schedules'), SHEET_SCHEMAS.Schedules, tz);
      var notificationsAll = _readSheetAsObjects(ss.getSheetByName('Notifications'), SHEET_SCHEMAS.Notifications, tz);

      // 학생/학부모 역할일 경우 본인 관련 민감 데이터만 필터링
      if (role !== 'teacher' && studentId) {
        attendance = attendance.filter(function (a) { return String(a.student_id) === studentId; });
        docTracking = docTracking.filter(function (d) { return String(d.student_id) === studentId; });
        noticeReplies = noticeReplies.filter(function (nr) { return String(nr.student_id) === studentId; });
        chatsAll = chatsAll.filter(function (c) {
          return String(c.room_id || '').indexOf(studentId) >= 0 ||
            String(c.sender_id) === studentId ||
            String(c.receiver_id) === studentId;
        });
      }
      if (role !== 'teacher') {
        var normStudentId = String(studentId || '').trim();
        var normGrade = String(grade || '').trim();
        var normClassNo = String(classNo || '').trim();
        notificationsAll = notificationsAll.filter(function (n) {
          var ch = String(n.channel === undefined || n.channel === null ? '' : n.channel).trim();
          var tRole = String(n.target_role === undefined || n.target_role === null ? '' : n.target_role).trim().toLowerCase();
          var tStu = String(n.target_student_id === undefined || n.target_student_id === null ? '' : n.target_student_id).trim();
          var nGrade = String(n.grade === undefined || n.grade === null ? '' : n.grade).trim();
          var nClass = String(n.class_no === undefined || n.class_no === null ? '' : n.class_no).trim();

          if (ch === 'mobile_attendance' || ch === 'mobile_chat') {
            if (tRole !== role) return false;
          }
          var roleMatch = (!tRole || tRole === role || tRole === 'all');
          var isAllStu = (!tStu || tStu.toUpperCase() === 'ALL');
          var stuMatch = isAllStu || (normStudentId !== '' && tStu === normStudentId);
          var classMatch = (!nGrade && !nClass) || (!normGrade && !normClassNo) ||
            (nGrade === normGrade && nClass === normClassNo);
          return roleMatch && stuMatch && classMatch;
        });
      }
      if (grade && classNo) {
        budgetList = budgetList.filter(function (b) {
          return (!b.grade && !b.class_no) || (String(b.grade) === grade && String(b.class_no) === classNo);
        });
      }
      if (role !== 'teacher' && grade && classNo) {
        timetableBase = timetableBase.filter(function (tb) {
          var typeOk = !tb.entry_type || String(tb.entry_type) === 'class';
          return typeOk && String(tb.grade) === grade && String(tb.class_no) === classNo;
        });
        timetableChanges = timetableChanges.filter(function (tc) {
          return String(tc.grade) === grade && String(tc.class_no) === classNo;
        });
      }

      return _jsonResponse({
        ok: true,
        spreadsheet_id: ss.getId(),
        server_time: nowStr,
        teachers: teachersList,
        students: role === 'teacher' ? studentsList : studentsList.filter(function (s) {
          return !studentId || String(s.student_id) === studentId;
        }),
        attendance: attendance,
        doc_rules: docRules,
        doc_tracking: docTracking,
        notices: notices,
        notice_replies: noticeReplies,
        timetable_base: timetableBase,
        timetable_changes: timetableChanges,
        meals: meals,
        budget: budgetList,
        chat_messages: chatsAll,
        schedules: schedulesAll,
        remote_tasks: tasksAll.filter(function (t) {
          return String(t.is_mobile_task) === '1' || t.is_mobile_task === true;
        }),
        notifications: notificationsAll
      });
    }

    // 기본값: pull_all (데스크톱 GoogleSheetsSyncService 계약 100% 유지)
    var schedulesData = _readSheetAsObjects(ss.getSheetByName('Schedules'), SHEET_SCHEMAS.Schedules, tz);
    var tasksData = _readSheetAsObjects(ss.getSheetByName('Tasks'), SHEET_SCHEMAS.Tasks, tz);
    var notificationsData = _readSheetAsObjects(ss.getSheetByName('Notifications'), SHEET_SCHEMAS.Notifications, tz);

    return _jsonResponse({
      ok: true,
      spreadsheet_id: ss.getId(),
      schedules: schedulesData,
      tasks: tasksData,
      notifications: notificationsData,
      server_time: nowStr
    });
  } catch (err) {
    return _jsonResponse({
      ok: false,
      error: err && err.message ? err.message : String(err)
    });
  }
}

/**
 * POST 엔드포인트 (JSON Body)
 * 기존 데스크톱 action 100% 유지 + 모바일/확장 action 지원
 */
function doPost(e) {
  try {
    var raw = (e && e.postData && e.postData.contents) ? e.postData.contents : '{}';
    var body = JSON.parse(raw);
    if (!_verifyApiSecret(e, body)) {
      return _jsonResponse({ ok: false, error: 'Unauthorized' });
    }
    var action = (body.action || '').toString().trim();
    var sheetId = (body.sheetId || '').toString().trim();
    var singleSheetActionMap = {
      save_push_subscription: 'Push_Subscriptions',
      send_notification: 'Notifications',
      mark_notification_read: 'Notifications',
      upsert_teacher_profile: '계정_교사',
      set_consultation_status: '계정_교사',
      upsert_budget: '학급회비',
      delete_budget: '학급회비',
      send_chat_message: '1:1_상담채팅',
      mark_chat_read: '1:1_상담채팅',
      confirm_attendance: '일일_출결',
      confirm_document: '서류_추적'
    };
    var ss = null;
    var tz = Session.getScriptTimeZone() || 'Asia/Seoul';
    var fastSheetName = singleSheetActionMap[action];
    if (fastSheetName) {
      var fastSs = _getSpreadsheet(sheetId);
      if (fastSs && fastSs.getSheetByName(fastSheetName)) {
        ss = fastSs;
      }
    }
    if (!ss) {
      var info = ensureAllSheets(sheetId);
      ss = info.ss;
      tz = info.timezone;
    }
    var nowStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss');

    // ── 1. 기존 데스크톱 액션 ────────────────────────────────────────────────
    if (action === 'init_sheets') {
      return _jsonResponse({
        ok: true,
        sheets: ['Schedules', 'Tasks', 'Notifications'],
        all_sheets: Object.keys(SHEET_SCHEMAS)
      });
    }

    if (action === 'upsert_schedule') {
      var schId = _upsertRowByRemoteId(ss.getSheetByName('Schedules'), SHEET_SCHEMAS.Schedules, body.data || body, tz);
      return _jsonResponse({ ok: true, remote_id: schId });
    }

    if (action === 'delete_schedule') {
      var deleted = _deleteRowByRemoteId(ss.getSheetByName('Schedules'), body.remote_id);
      return _jsonResponse({ ok: true, deleted: deleted });
    }

    if (action === 'upsert_task') {
      var taskId = _upsertRowByRemoteId(ss.getSheetByName('Tasks'), SHEET_SCHEMAS.Tasks, body.data || body, tz);
      return _jsonResponse({ ok: true, remote_id: taskId });
    }

    if (action === 'update_task_status') {
      var rid = (body.remote_id || '').toString().trim();
      var patch = {
        remote_id: rid,
        exec_status: (body.exec_status || 'acknowledged').toString(),
        is_done: body.is_done ? 1 : 0,
        updated_at: nowStr
      };
      if (body.memo !== undefined) {
        patch.memo = body.memo;
      }
      _upsertRowByRemoteId(ss.getSheetByName('Tasks'), SHEET_SCHEMAS.Tasks, patch, tz);
      return _jsonResponse({ ok: true, remote_id: rid, exec_status: patch.exec_status });
    }

    if (action === 'add_notification') {
      var nId = _upsertRowByRemoteId(ss.getSheetByName('Notifications'), SHEET_SCHEMAS.Notifications, body.data || body, tz);
      return _jsonResponse({ ok: true, remote_id: nId });
    }

    if (action === 'mark_notification_read') {
      var nRid = (body.remote_id || '').toString().trim();
      _upsertRowByRemoteId(ss.getSheetByName('Notifications'), SHEET_SCHEMAS.Notifications, {
        remote_id: nRid,
        is_read: 1
      }, tz);
      return _jsonResponse({ ok: true, remote_id: nRid });
    }

    if (action === 'push_batch') {
      var count = 0;
      var sList = Array.isArray(body.schedules) ? body.schedules : [];
      var tList = Array.isArray(body.tasks) ? body.tasks : [];
      var nList = Array.isArray(body.notifications) ? body.notifications : [];
      var sSheet = ss.getSheetByName('Schedules');
      var tkSheet = ss.getSheetByName('Tasks');
      var ntSheet = ss.getSheetByName('Notifications');
      sList.forEach(function (item) {
        _upsertRowByRemoteId(sSheet, SHEET_SCHEMAS.Schedules, item, tz);
        count++;
      });
      tList.forEach(function (item) {
        _upsertRowByRemoteId(tkSheet, SHEET_SCHEMAS.Tasks, item, tz);
        count++;
      });
      nList.forEach(function (item) {
        _upsertRowByRemoteId(ntSheet, SHEET_SCHEMAS.Notifications, item, tz);
        count++;
      });
      return _jsonResponse({ ok: true, upserted: count });
    }

    // ── 2. 모바일 인증 & 계정 관리 ───────────────────────────────────────────
    if (action === 'login' || action === 'verify_auth') {
      var authRes = _authenticateUser(
        ss,
        tz,
        body.role,
        body.user_id || body.id,
        body.password,
        body.password_hash
      );
      return _jsonResponse(authRes);
    }

    if (action === 'upsert_account') {
      var accRole = (body.role || '').toString().trim().toLowerCase();
      var accSheetMap = { teacher: '계정_교사', student: '계정_학생', parent: '계정_학부모' };
      var targetSheetName = accSheetMap[accRole];
      if (!targetSheetName) {
        return _jsonResponse({ ok: false, error: '지원하지 않는 계정 역할: ' + accRole });
      }
      var accData = body.data || body;
      if (accData.password && !accData.password_hash) {
        accData.password_hash = _sha256Hex(accData.password);
        delete accData.password;
      }
      var accId = _upsertRowByRemoteId(ss.getSheetByName(targetSheetName), SHEET_SCHEMAS[targetSheetName], accData, tz);
      return _jsonResponse({ ok: true, remote_id: accId });
    }

    // ── 3. 출결 관리 ─────────────────────────────────────────────────────────
    if (action === 'submit_attendance') {
      var attData = body.data || body;
      var attId = _upsertRowByRemoteId(ss.getSheetByName('일일_출결'), SHEET_SCHEMAS['일일_출결'], attData, tz);
      if (attData.notify_teacher !== false) {
        _upsertRowByRemoteId(ss.getSheetByName('Notifications'), SHEET_SCHEMAS.Notifications, {
          remote_id: 'noti_att_' + attId,
          channel: 'mobile_attendance',
          sender: attData.student_name || attData.submitted_by || '모바일',
          title: '[출결] ' + (attData.student_name || '') + ' (' + (attData.attendance_type || '출결') + ')',
          message: (attData.date || '') + ' 사유: ' + (attData.reason || '미입력'),
          target_role: 'teacher',
          target_student_id: attData.student_id || '',
          grade: attData.grade || '',
          class_no: attData.class_no || '',
          created_at: nowStr,
          is_read: 0
        }, tz);
      }
      return _jsonResponse({ ok: true, remote_id: attId });
    }

    if (action === 'confirm_attendance') {
      var confId = (body.remote_id || '').toString().trim();
      _upsertRowByRemoteId(ss.getSheetByName('일일_출결'), SHEET_SCHEMAS['일일_출결'], {
        remote_id: confId,
        teacher_confirmed: 1,
        memo: body.memo
      }, tz);
      return _jsonResponse({ ok: true, remote_id: confId });
    }

    if (action === 'save_class_attendance') {
      var payloadData = body.data || body;
      var rawRecords = Array.isArray(payloadData.records) ? payloadData.records : [];
      var targetDate = (payloadData.date || Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd')).toString().trim();
      var targetGrade = (payloadData.grade !== undefined && payloadData.grade !== null) ? String(payloadData.grade) : '';
      var targetClassNo = (payloadData.class_no !== undefined && payloadData.class_no !== null) ? String(payloadData.class_no) : '';
      var teacherName = (payloadData.submitted_by || '담임교사').toString();

      var attSheet = ss.getSheetByName('일일_출결');
      var existingAtt = _readSheetAsObjects(attSheet, SHEET_SCHEMAS['일일_출결'], tz);
      var existingByStu = {};
      existingAtt.forEach(function (row) {
        if (String(row.date || '').trim() === targetDate && row.student_id) {
          existingByStu[String(row.student_id).trim()] = String(row.remote_id || '').trim();
        }
      });

      var bulkItems = rawRecords.map(function (rec) {
        var sid = String(rec.student_id || '').trim();
        var rid = (rec.remote_id || existingByStu[sid] || ('ATT_CHK_' + targetDate + '_' + sid)).toString().trim();
        return {
          remote_id: rid,
          date: targetDate,
          student_id: sid,
          student_name: rec.student_name || '',
          grade: rec.grade !== undefined && rec.grade !== '' ? rec.grade : targetGrade,
          class_no: rec.class_no !== undefined && rec.class_no !== '' ? rec.class_no : targetClassNo,
          student_no: rec.student_no !== undefined ? rec.student_no : '',
          attendance_type: rec.attendance_type || '출석',
          att_category: rec.att_category || '',
          att_sub_type: rec.att_sub_type || '',
          period_info: rec.period_info || '',
          reason: rec.reason || '',
          submitted_by: rec.submitted_by || teacherName,
          role: 'teacher',
          teacher_confirmed: 1,
          memo: rec.memo || ''
        };
      });

      var savedCount = _bulkUpsertRowsByRemoteId(attSheet, SHEET_SCHEMAS['일일_출결'], bulkItems, tz);
      return _jsonResponse({ ok: true, count: savedCount, date: targetDate });
    }

    // ── 4. 서류 규정 & 영업일 마감 계산 & "제출했습니다" 상태 전환 ───────────
    if (action === 'upsert_doc_rule') {
      var ruleId = _upsertRowByRemoteId(ss.getSheetByName('설정_서류규정'), SHEET_SCHEMAS['설정_서류규정'], body.data || body, tz);
      return _jsonResponse({ ok: true, remote_id: ruleId });
    }

    if (action === 'upsert_document_tracking') {
      var docData = body.data || body;
      if (!docData.due_date && docData.base_date) {
        var bDays = parseInt(docData.due_business_days || '0', 10);
        if (!bDays && docData.rule_code) {
          var ruleRows = _readSheetAsObjects(ss.getSheetByName('설정_서류규정'), SHEET_SCHEMAS['설정_서류규정'], tz);
          for (var ri = 0; ri < ruleRows.length; ri++) {
            if (ruleRows[ri].rule_code === docData.rule_code) {
              bDays = parseInt(ruleRows[ri].due_business_days || '0', 10);
              break;
            }
          }
        }
        docData.due_date = _addBusinessDays(docData.base_date, bDays || 5, tz);
      }
      if (!docData.status) {
        docData.status = '미제출';
      }
      var docId = _upsertRowByRemoteId(ss.getSheetByName('서류_추적'), SHEET_SCHEMAS['서류_추적'], docData, tz);
      return _jsonResponse({ ok: true, remote_id: docId, due_date: docData.due_date || '' });
    }

    if (action === 'mark_document_submitted') {
      var docRid = (body.remote_id || '').toString().trim();
      var submitPatch = {
        remote_id: docRid,
        status: '제출했습니다',
        submitted_at: nowStr,
        submitted_by: (body.submitted_by || '').toString(),
        memo: body.memo !== undefined ? body.memo : undefined
      };
      if (body.file_url) {
        submitPatch.file_url = body.file_url;
      }
      _upsertRowByRemoteId(ss.getSheetByName('서류_추적'), SHEET_SCHEMAS['서류_추적'], submitPatch, tz);
      return _jsonResponse({ ok: true, remote_id: docRid, status: '제출했습니다', submitted_at: nowStr });
    }

    if (action === 'confirm_document') {
      var dRid = (body.remote_id || '').toString().trim();
      _upsertRowByRemoteId(ss.getSheetByName('서류_추적'), SHEET_SCHEMAS['서류_추적'], {
        remote_id: dRid,
        status: (body.status || '확인완료').toString()
      }, tz);
      return _jsonResponse({ ok: true, remote_id: dRid });
    }

    // ── 5. 가정통신문 & 전자서명 Google Drive 업로드 ─────────────────────────
    if (action === 'upsert_notice') {
      var noticeId = _upsertRowByRemoteId(ss.getSheetByName('가정통신문_목록'), SHEET_SCHEMAS['가정통신문_목록'], body.data || body, tz);
      return _jsonResponse({ ok: true, remote_id: noticeId });
    }

    if (action === 'submit_notice_reply') {
      var rep = body.data || body;
      var nIdRef = (rep.notice_id || '').toString().trim();
      var stuIdRef = (rep.student_id || '').toString().trim();
      if (!rep.remote_id && nIdRef && stuIdRef) {
        rep.remote_id = 'reply_' + nIdRef + '_' + stuIdRef;
      }
      if (rep.signature_base64) {
        var sigFileName = 'sig_' + (nIdRef || 'notice') + '_' + (stuIdRef || new Date().getTime()) + '.png';
        var driveInfo = _saveSignatureToDrive(rep.signature_base64, sigFileName, rep.folder_name);
        if (driveInfo) {
          rep.signature_drive_url = driveInfo.download_url;
          rep.signature_file_id = driveInfo.file_id;
        }
        delete rep.signature_base64;
      }
      if (!rep.signed_at) {
        rep.signed_at = nowStr;
      }
      var replyId = _upsertRowByRemoteId(ss.getSheetByName('가정통신문_회신'), SHEET_SCHEMAS['가정통신문_회신'], rep, tz);
      return _jsonResponse({
        ok: true,
        remote_id: replyId,
        signature_drive_url: rep.signature_drive_url || '',
        signature_file_id: rep.signature_file_id || ''
      });
    }

    // ── 6. 시간표 (기본 & 컴시간 변동 푸시) ─────────────────────────────────
    if (action === 'upsert_timetable_base') {
      var tbList = Array.isArray(body.rows) ? body.rows : [body.data || body];
      var tbSheet = ss.getSheetByName('시간표_기본');
      var tbCount = _bulkUpsertRowsByRemoteId(tbSheet, SHEET_SCHEMAS['시간표_기본'], tbList, tz);
      return _jsonResponse({ ok: true, upserted: tbCount });
    }

    if (action === 'push_timetable_changes') {
      var tcList = Array.isArray(body.rows) ? body.rows : [body.data || body];
      var tcSheet = ss.getSheetByName('시간표_변동');
      var tcCount = _bulkUpsertRowsByRemoteId(tcSheet, SHEET_SCHEMAS['시간표_변동'], tcList, tz);
      return _jsonResponse({ ok: true, upserted: tcCount });
    }

    // ── 7. 1:1 상담 채팅 & 상담 ON/OFF 상태 동기화 ───────────────────────────
    if (action === 'send_chat_message') {
      var chat = body.data || body;
      var chatId = _upsertRowByRemoteId(ss.getSheetByName('1:1_상담채팅'), SHEET_SCHEMAS['1:1_상담채팅'], chat, tz);
      if (chat.notify !== false) {
        var isFromTeacher = String(chat.sender_role || '').toLowerCase() === 'teacher';
        _upsertRowByRemoteId(ss.getSheetByName('Notifications'), SHEET_SCHEMAS.Notifications, {
          remote_id: 'noti_chat_' + chatId,
          channel: 'mobile_chat',
          sender: chat.sender_name || chat.sender_id || '상담채팅',
          title: '[1:1 상담] ' + (chat.sender_name || '새 메시지'),
          message: (chat.message || '').toString().slice(0, 120),
          target_role: isFromTeacher ? 'all' : 'teacher',
          target_student_id: isFromTeacher ? (chat.receiver_id || '') : (chat.sender_id || ''),
          created_at: nowStr,
          is_read: 0
        }, tz);
      }
      return _jsonResponse({ ok: true, remote_id: chatId, created_at: chat.created_at || nowStr });
    }

    if (action === 'mark_chat_read') {
      var targetRoom = (body.room_id || '').toString().trim();
      var targetRole = (body.reader_role || '').toString().trim();
      var chatSheet = ss.getSheetByName('1:1_상담채팅');
      var allChats = _readSheetAsObjects(chatSheet, SHEET_SCHEMAS['1:1_상담채팅'], tz);
      var updated = 0;
      allChats.forEach(function (c) {
        if ((!targetRoom || c.room_id === targetRoom) &&
            (!targetRole || c.sender_role !== targetRole) &&
            String(c.is_read) !== '1') {
          _upsertRowByRemoteId(chatSheet, SHEET_SCHEMAS['1:1_상담채팅'], {
            remote_id: c.remote_id,
            is_read: 1
          }, tz);
          updated++;
        }
      });
      return _jsonResponse({ ok: true, updated: updated });
    }

    if (action === 'set_consultation_status') {
      var tId = (body.teacher_id || body.remote_id || 'teacher_001').toString().trim();
      var tSheetObj = ss.getSheetByName('계정_교사');
      var tRows = _readSheetAsObjects(tSheetObj, SHEET_SCHEMAS['계정_교사'], tz);
      var matchedRid = tId;
      for (var ti = 0; ti < tRows.length; ti++) {
        if (tRows[ti].teacher_id === tId || tRows[ti].remote_id === tId) {
          matchedRid = tRows[ti].remote_id;
          break;
        }
      }
      var enabledVal = (body.consultation_enabled === true || body.consultation_enabled === 1 || body.consultation_enabled === '1') ? 1 : 0;
      var tPatch = {
        remote_id: matchedRid,
        consultation_enabled: enabledVal
      };
      if (body.consultation_hours !== undefined) {
        tPatch.consultation_hours = body.consultation_hours;
      }
      _upsertRowByRemoteId(tSheetObj, SHEET_SCHEMAS['계정_교사'], tPatch, tz);
      return _jsonResponse({ ok: true, remote_id: matchedRid, consultation_enabled: enabledVal === 1 });
    }

    // ── 8. 모바일 원격 예약 작업 큐 (Tasks 시트 정합성 유지) ────────────────
    if (action === 'enqueue_remote_task') {
      var rt = body.data || body;
      rt.is_mobile_task = 1;
      if (!rt.exec_status) {
        rt.exec_status = 'pending';
      }
      if (rt.is_done === undefined) {
        rt.is_done = 0;
      }
      var rtId = _upsertRowByRemoteId(ss.getSheetByName('Tasks'), SHEET_SCHEMAS.Tasks, rt, tz);
      return _jsonResponse({ ok: true, remote_id: rtId });
    }

    // ── 9. 나이스(NEIS) 급식 수동 동기화 트리거 ──────────────────────────────
    if (action === 'sync_neis_meals') {
      var mealRes = fetchNeisMeals(body.from_ymd || '', body.to_ymd || '', sheetId);
      return _jsonResponse(mealRes);
    }

    // ── 10. 범용 시트 업서트 (관리용) ────────────────────────────────────────
    if (action === 'upsert_sheet_row') {
      var sName = (body.sheet || '').toString().trim();
      if (!SHEET_SCHEMAS[sName]) {
        return _jsonResponse({ ok: false, error: '알 수 없는 시트 이름: ' + sName });
      }
      var rowRid = _upsertRowByRemoteId(ss.getSheetByName(sName), SHEET_SCHEMAS[sName], body.data || {}, tz);
      return _jsonResponse({ ok: true, sheet: sName, remote_id: rowRid });
    }

    // ── 11. 교사 프로필 수정 ──────────────────────────────────────────────────
    if (action === 'upsert_teacher_profile') {
      var profData = body.data || body;
      var targetTeacherId = (profData.teacher_id || profData.remote_id || '').toString().trim();
      var tpSheet = ss.getSheetByName('계정_교사');
      var tpRows = _readSheetAsObjects(tpSheet, SHEET_SCHEMAS['계정_교사'], tz);
      var matchedTeacherRid = '';
      for (var tpi = 0; tpi < tpRows.length; tpi++) {
        if (
          (targetTeacherId && (String(tpRows[tpi].teacher_id) === targetTeacherId || String(tpRows[tpi].remote_id) === targetTeacherId)) ||
          (!targetTeacherId && tpi === 0)
        ) {
          matchedTeacherRid = tpRows[tpi].remote_id;
          break;
        }
      }
      var allowedFields = [
        'name', 'school_name', 'grade', 'class_no', 'subject',
        'phone', 'consultation_hours', 'neis_office_code', 'neis_school_code'
      ];
      var profPatch = {
        remote_id: matchedTeacherRid || targetTeacherId || ('teacher_' + new Date().getTime())
      };
      if (!matchedTeacherRid && profData.teacher_id) {
        profPatch.teacher_id = profData.teacher_id;
      }
      allowedFields.forEach(function (field) {
        if (profData.hasOwnProperty(field) && profData[field] !== undefined) {
          profPatch[field] = profData[field];
        }
      });
      var savedTeacherRid = _upsertRowByRemoteId(tpSheet, SHEET_SCHEMAS['계정_교사'], profPatch, tz);
      return _jsonResponse({ ok: true, remote_id: savedTeacherRid, profile: profPatch });
    }

    // ── 12. 교사 알림 발송 (단건 또는 다건 일괄 생성) ─────────────────────────
    if (action === 'send_notification') {
      var nPayload = body.data || body;
      var nSheet = ss.getSheetByName('Notifications');
      var stuIds = Array.isArray(nPayload.target_student_ids) ? nPayload.target_student_ids : null;
      var createdIds = [];
      var baseChannel = (nPayload.channel || 'teacher_notice').toString().trim();
      var baseSender = (nPayload.sender || '담임교사').toString().trim();
      var baseTitle = (nPayload.title || '').toString().trim();
      var baseMessage = (nPayload.message || '').toString().trim();
      var baseTargetRole = (nPayload.target_role || 'all').toString().trim().toLowerCase();
      var baseGrade = (nPayload.grade || '').toString().trim();
      var baseClassNo = (nPayload.class_no || '').toString().trim();

      if (stuIds && stuIds.length > 0) {
        stuIds.forEach(function (sid, idx) {
          var rowId = 'noti_' + new Date().getTime() + '_' + idx + '_' + Math.floor(Math.random() * 1000);
          _upsertRowByRemoteId(nSheet, SHEET_SCHEMAS.Notifications, {
            remote_id: rowId,
            channel: baseChannel,
            sender: baseSender,
            title: baseTitle,
            message: baseMessage,
            target_role: baseTargetRole,
            target_student_id: String(sid).trim(),
            grade: baseGrade,
            class_no: baseClassNo,
            created_at: nowStr,
            is_read: 0
          }, tz);
          createdIds.push(rowId);
        });
      } else {
        var singleId = (nPayload.remote_id || ('noti_' + new Date().getTime() + '_' + Math.floor(Math.random() * 1000))).toString().trim();
        _upsertRowByRemoteId(nSheet, SHEET_SCHEMAS.Notifications, {
          remote_id: singleId,
          channel: baseChannel,
          sender: baseSender,
          title: baseTitle,
          message: baseMessage,
          target_role: baseTargetRole,
          target_student_id: (nPayload.target_student_id || 'ALL').toString().trim(),
          grade: baseGrade,
          class_no: baseClassNo,
          created_at: nowStr,
          is_read: 0
        }, tz);
        createdIds.push(singleId);
      }
      return _jsonResponse({ ok: true, count: createdIds.length, remote_ids: createdIds });
    }

    // ── 13. Web Push 구독 저장 (user_id + endpoint 기준 업서트) ──────────────
    if (action === 'save_push_subscription') {
      var subData = body.data || body;
      var subUserId = (subData.user_id || '').toString().trim();
      var subEndpoint = (subData.endpoint || '').toString().trim();
      if (!subEndpoint) {
        return _jsonResponse({ ok: false, error: 'endpoint가 필요합니다.' });
      }
      var pushSheet = ss.getSheetByName('Push_Subscriptions');
      var existingSubs = _readSheetAsObjects(pushSheet, SHEET_SCHEMAS.Push_Subscriptions, tz);
      var matchedSubRid = '';
      for (var si = 0; si < existingSubs.length; si++) {
        if (String(existingSubs[si].user_id || '').trim() === subUserId &&
            String(existingSubs[si].endpoint || '').trim() === subEndpoint) {
          matchedSubRid = existingSubs[si].remote_id;
          break;
        }
      }
      var subPatch = {
        remote_id: matchedSubRid || ('push_' + new Date().getTime() + '_' + Math.floor(Math.random() * 1000)),
        user_id: subUserId,
        role: (subData.role || '').toString().trim().toLowerCase(),
        endpoint: subEndpoint,
        p256dh: (subData.p256dh || '').toString().trim(),
        auth: (subData.auth || '').toString().trim(),
        user_agent: (subData.user_agent || '').toString().slice(0, 200)
      };
      var savedSubRid = _upsertRowByRemoteId(pushSheet, SHEET_SCHEMAS.Push_Subscriptions, subPatch, tz);
      return _jsonResponse({ ok: true, remote_id: savedSubRid });
    }

    // ── 14. 학급회비 업서트 & 삭제 ───────────────────────────────────────────
    if (action === 'upsert_budget') {
      var bData = body.data || body;
      var bRid = _upsertRowByRemoteId(ss.getSheetByName('학급회비'), SHEET_SCHEMAS['학급회비'], bData, tz);
      return _jsonResponse({ ok: true, remote_id: bRid });
    }

    if (action === 'delete_budget') {
      var bDeleted = _deleteRowByRemoteId(ss.getSheetByName('학급회비'), body.remote_id);
      return _jsonResponse({ ok: true, deleted: bDeleted });
    }

    return _jsonResponse({ ok: false, error: 'Unsupported action: ' + action });
  } catch (err) {
    return _jsonResponse({
      ok: false,
      error: err && err.message ? err.message : String(err)
    });
  }
}