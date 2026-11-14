/**
 * ============================================================================
 * EduFlow — Google Sheets + Apps Script 실시간 양방향 동기화 API 템플릿
 * 파일: docs/apps_script_template.gs
 * ============================================================================
 * [설치 및 배포 방법]
 * 1. 연동할 Google 스프레드시트를 열고 상단 메뉴 [확장 프로그램] -> [Apps Script] 클릭
 * 2. 기존 코드를 모두 지우고 이 파일 전체 내용을 붙여넣은 뒤 저장(Ctrl+S)
 * 3. (선택) 상단 함수 드롭다운에서 `initSheets` 또는 `seedSampleData`를 선택해 [실행]하면
 *    Schedules / Tasks / Notifications 3개 시트 탭이 즉시 생성됩니다.
 * 4. 우측 상단 [배포] -> [새 배포] -> 유형: [웹 앱] 선택
 *    - 다음 사용자 권한으로 실행: 나(Me)
 *    - 액세스 권한이 있는 사용자: 모든 사용자(Anyone) (또는 조직 내 사용자)
 * 5. 생성된 웹 앱 URL(https://script.google.com/macros/s/.../exec)을 복사하여
 *    EduFlow -> [환경설정 & 명렬표] -> [외부 연동] 섹션에 붙여넣고 [연결 테스트]를 누르세요.
 * ============================================================================
 */

var SHEET_SCHEMAS = {
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
    'is_read'
  ]
};

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
 * 3개 필수 시트(Schedules, Tasks, Notifications)가 없으면 자동 생성 및 헤더 세팅
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
      // 기존 헤더 누락 컬럼이 있으면 보완
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
 * 수동 실행용: 3개 시트 탭 초기화
 */
function initSheets() {
  ensureAllSheets('');
}

/**
 * 테스트용 샘플 데이터 삽입 (모바일 예약 작업, 학사일정, 외부 알림 예시)
 */
function seedSampleData() {
  var info = ensureAllSheets('');
  var ss = info.ss;
  var nowStr = Utilities.formatDate(new Date(), info.timezone, 'yyyy-MM-dd HH:mm:ss');
  var todayStr = Utilities.formatDate(new Date(), info.timezone, 'yyyy-MM-dd');

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
}

/**
 * 시트 전체 행을 객체 배열로 직렬화 (Date 셀은 yyyy-MM-dd / yyyy-MM-dd HH:mm:ss 변환)
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
      var val = row[col];
      if (val instanceof Date) {
        if (key === 'start_time') {
          val = Utilities.formatDate(val, tz, 'HH:mm');
        } else if (key === 'start_date' || key === 'end_date' || key === 'due_date') {
          val = Utilities.formatDate(val, tz, 'yyyy-MM-dd');
        } else {
          val = Utilities.formatDate(val, tz, 'yyyy-MM-dd HH:mm:ss');
        }
      } else if (val === null || val === undefined) {
        val = '';
      }
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
    rid = sheet.getName().toLowerCase() + '_' + new Date().getTime();
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
    // 기존 값 보존 (부분 업데이트 지원)
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
 * GET 엔드포인트
 * - ?action=ping : 연결 테스트 및 3개 시트 자동 초기화 확인
 * - ?action=pull_all (기본값) : Schedules, Tasks, Notifications 전체 조회
 */
function doGet(e) {
  try {
    var params = (e && e.parameter) ? e.parameter : {};
    var action = (params.action || 'pull_all').toString().trim();
    var sheetId = (params.sheetId || '').toString().trim();
    var info = ensureAllSheets(sheetId);
    var ss = info.ss;
    var tz = info.timezone;

    var schedules = _readSheetAsObjects(ss.getSheetByName('Schedules'), SHEET_SCHEMAS.Schedules, tz);
    var tasks = _readSheetAsObjects(ss.getSheetByName('Tasks'), SHEET_SCHEMAS.Tasks, tz);
    var notifications = _readSheetAsObjects(ss.getSheetByName('Notifications'), SHEET_SCHEMAS.Notifications, tz);

    if (action === 'ping') {
      return _jsonResponse({
        ok: true,
        status: 'connected',
        spreadsheet_name: ss.getName(),
        spreadsheet_id: ss.getId(),
        sheets: ['Schedules', 'Tasks', 'Notifications'],
        counts: {
          schedules: schedules.length,
          tasks: tasks.length,
          notifications: notifications.length
        },
        server_time: Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss')
      });
    }

    return _jsonResponse({
      ok: true,
      spreadsheet_id: ss.getId(),
      schedules: schedules,
      tasks: tasks,
      notifications: notifications,
      server_time: Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss')
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
 * 지원 action:
 * - init_sheets
 * - upsert_schedule / delete_schedule
 * - upsert_task / update_task_status
 * - add_notification / mark_notification_read
 * - push_batch
 */
function doPost(e) {
  try {
    var raw = (e && e.postData && e.postData.contents) ? e.postData.contents : '{}';
    var body = JSON.parse(raw);
    var action = (body.action || '').toString().trim();
    var sheetId = (body.sheetId || '').toString().trim();
    var info = ensureAllSheets(sheetId);
    var ss = info.ss;
    var tz = info.timezone;

    if (action === 'init_sheets') {
      return _jsonResponse({ ok: true, sheets: ['Schedules', 'Tasks', 'Notifications'] });
    }

    if (action === 'upsert_schedule') {
      var schSheet = ss.getSheetByName('Schedules');
      var schId = _upsertRowByRemoteId(schSheet, SHEET_SCHEMAS.Schedules, body.data || body, tz);
      return _jsonResponse({ ok: true, remote_id: schId });
    }

    if (action === 'delete_schedule') {
      var deleted = _deleteRowByRemoteId(ss.getSheetByName('Schedules'), body.remote_id);
      return _jsonResponse({ ok: true, deleted: deleted });
    }

    if (action === 'upsert_task') {
      var taskSheet = ss.getSheetByName('Tasks');
      var taskId = _upsertRowByRemoteId(taskSheet, SHEET_SCHEMAS.Tasks, body.data || body, tz);
      return _jsonResponse({ ok: true, remote_id: taskId });
    }

    if (action === 'update_task_status') {
      var tSheet = ss.getSheetByName('Tasks');
      var rid = (body.remote_id || '').toString().trim();
      var patch = {
        remote_id: rid,
        exec_status: (body.exec_status || 'acknowledged').toString(),
        is_done: body.is_done ? 1 : 0,
        updated_at: Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm:ss')
      };
      _upsertRowByRemoteId(tSheet, SHEET_SCHEMAS.Tasks, patch, tz);
      return _jsonResponse({ ok: true, remote_id: rid, exec_status: patch.exec_status });
    }

    if (action === 'add_notification') {
      var nSheet = ss.getSheetByName('Notifications');
      var nId = _upsertRowByRemoteId(nSheet, SHEET_SCHEMAS.Notifications, body.data || body, tz);
      return _jsonResponse({ ok: true, remote_id: nId });
    }

    if (action === 'mark_notification_read') {
      var notiSheet = ss.getSheetByName('Notifications');
      var nRid = (body.remote_id || '').toString().trim();
      _upsertRowByRemoteId(notiSheet, SHEET_SCHEMAS.Notifications, {
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

    return _jsonResponse({ ok: false, error: 'Unsupported action: ' + action });
  } catch (err) {
    return _jsonResponse({
      ok: false,
      error: err && err.message ? err.message : String(err)
    });
  }
}
