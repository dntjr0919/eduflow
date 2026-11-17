import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.class_notice_digest_service import (
    build_kakao_notice_digest_prompt,
    build_schoolbell_summary_prompt,
    parse_kakao_chat_text,
    parse_notice_digest_ai_response,
    read_kakao_export_file,
)
from services.firestore_sync_service import FirestoreSyncService
from services.schoolbell_crawler_service import (
    _EXTRACT_ANGULAR_LETTERS_JS,
    SchoolbellCrawlerService,
    format_schoolbell_detail_content,
    get_schoolbell_profile_dir,
    is_schoolbell_home_url,
    is_schoolbell_login_required,
    is_schoolbell_service_notice,
    load_saved_schoolbell_board_url,
    normalize_angular_letter_items,
    normalize_schoolbell_notices,
    save_schoolbell_board_url,
    strip_schoolbell_footer_text,
)


class TestKakaoParsingAndDigest(unittest.TestCase):
    def test_parse_windows_kakao_filters_system_and_fillers(self) -> None:
        raw = """김선생 님과 카카오톡 대화
저장한 날짜 : 2026-11-17 18:00:00

--------------- 2026년 11월 17일 화요일 ---------------
[김선생] [오전 9:00] 이영희님이 들어왔습니다.
[학부모A] [오전 9:01] 네
[학부모B] [오전 9:02] 감사합니다~
[김선생] [오전 9:05] 사진
[김선생] [오전 9:10] 내일 현장체험학습 안내입니다.
오전 8시 40분까지 운동장 집결이며 도시락과 돗자리를 지참해 주세요.
[학부모C] [오전 9:11] 확인했습니다!
[학부모D] [오전 9:12] ㅇㅇ
[김선생] [오전 9:15] 우천 시 체육관에서 진행합니다.
"""
        cleaned = parse_kakao_chat_text(raw)
        self.assertIn("[2026년 11월 17일 화요일]", cleaned)
        self.assertIn("내일 현장체험학습 안내입니다.", cleaned)
        self.assertIn("오전 8시 40분까지 운동장 집결이며 도시락과 돗자리를 지참해 주세요.", cleaned)
        self.assertIn("우천 시 체육관에서 진행합니다.", cleaned)
        self.assertNotIn("들어왔습니다", cleaned)
        self.assertNotIn("감사합니다", cleaned)
        self.assertNotIn("확인했습니다", cleaned)
        self.assertNotIn("ㅇㅇ", cleaned)

    def test_parse_mobile_kakao_format(self) -> None:
        raw = (
            "2026. 11. 17. 오후 3:15, 담임교사 : 이번 주 금요일 1교시 독서 골든벨을 진행합니다.\n"
            "2026. 11. 17. 오후 3:16, 학부모1 : 넵\n"
            "2026. 11. 17. 오후 3:17, 담임교사 : 지정 도서를 꼭 지참해 주세요.\n"
        )
        cleaned = parse_kakao_chat_text(raw)
        self.assertIn("독서 골든벨", cleaned)
        self.assertIn("지정 도서를 꼭 지참해 주세요.", cleaned)
        self.assertNotIn("넵", cleaned)

    def test_read_kakao_export_file_cp949_and_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "talk_cp949.txt"
            p.write_bytes("[교사] [오후 1:00] 내일 준비물 안내".encode("cp949"))
            text = read_kakao_export_file(p)
            self.assertIn("내일 준비물 안내", text)

    def test_parse_notice_digest_ai_response_array_and_wrapped_dict(self) -> None:
        fenced = """```json
[
  {
    "title": "현장체험학습 안내",
    "summary": "내일 오전 8시 40분 집결, 도시락 지참",
    "content": "내일 오전 8시 40분까지 운동장으로 집결하며 도시락과 돗자리를 지참해 주세요.",
    "source_label": "학부모 단톡방",
    "event_date": "2026-11-18"
  }
]
```"""
        items = parse_notice_digest_ai_response(fenced)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "현장체험학습 안내")
        self.assertEqual(items[0]["source_label"], "학부모 단톡방")
        self.assertEqual(items[0]["event_date"], "2026-11-18")

        wrapped = '{"notices": [{"title": "독서 행사", "content": "금요일 1교시 진행"}]}'
        items2 = parse_notice_digest_ai_response(wrapped, default_source_label="카카오톡")
        self.assertEqual(len(items2), 1)
        self.assertEqual(items2[0]["source_label"], "카카오톡")
        self.assertEqual(items2[0]["summary"], "금요일 1교시 진행")

    def test_prompt_builders(self) -> None:
        sys_p, usr_p = build_kakao_notice_digest_prompt("내일 체육대회", source_label="반 톡방")
        self.assertIn("JSON 배열", sys_p)
        self.assertIn("반 톡방", usr_p)
        s2, u2 = build_schoolbell_summary_prompt("가정통신문", "긴 본문 내용", published_at="2026-11-17")
        self.assertIn("3줄", s2)
        self.assertIn("긴 본문 내용", u2)


class TestSchoolbellHelpers(unittest.TestCase):
    def test_profile_dir_and_login_detection(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": tempfile.gettempdir()}):
            p = get_schoolbell_profile_dir()
            self.assertTrue(str(p).endswith(os.path.join("EduFlow", "schoolbell_profile")))
            self.assertTrue(p.exists())

        self.assertTrue(is_schoolbell_login_required("https://schoolbell-e.com/login", ""))
        self.assertTrue(is_schoolbell_login_required("https://schoolbell-e.com/", "", has_password_input=True))
        # 랜딩 페이지에 '알림장'/'가정통신문' 소개 문구가 있어도 '무료 체험'/'가입하기'/'로그인'이 있으면 로그인 필요로 판정
        landing_text = "학교종이 서비스 소개 알림장 가정통신문 무료 체험 가입하기 로그인"
        self.assertTrue(is_schoolbell_login_required("https://schoolbell-e.com/", landing_text))
        # 실제 로그인 후 대시보드('로그아웃', '우리반', '학교안내문')에서는 로그인 완료로 판정
        dashboard_text = "학교종이 우리반 학교안내문 알림장 홍길동 선생님 로그아웃"
        self.assertFalse(is_schoolbell_login_required("https://schoolbell-e.com/", dashboard_text))
        self.assertFalse(is_schoolbell_login_required("https://schoolbell-e.com/", "우리 반 알림장 공지사항 목록"))

    def test_normalize_schoolbell_notices(self) -> None:
        raw = [
            {"title": "  11월 급식 안내  ", "date": "2026.11.17", "content": "11월 식단표 안내입니다.", "url": "/notice/101"},
            {"title": "11월 급식 안내", "date": "2026.11.17", "content": "중복", "url": "/notice/101"},
            {"title": "가", "date": "2026-11-16", "content": "너무 짧은 제목"},
        ]
        res = normalize_schoolbell_notices(raw)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "11월 급식 안내")
        self.assertEqual(res[0]["published_at"], "2026-11-17")
        self.assertEqual(res[0]["source_url"], "https://schoolbell-e.com/notice/101")
        self.assertEqual(res[0]["source_type"], "schoolbell")

    def test_service_notice_filter_and_board_url_memory(self) -> None:
        self.assertTrue(is_schoolbell_service_notice("[학교종이 공식 공지] 서비스 점검 안내"))
        self.assertTrue(is_schoolbell_service_notice("개인정보처리방침 개정 안내", source_url="https://schoolbell-e.com/privacy"))
        self.assertFalse(is_schoolbell_service_notice("2학년 11월 체험학습 가정통신문"))

        raw = [
            {"title": "학교종이 공식 공지", "date": "2026-11-17", "content": "점검 안내", "url": "/official/1"},
            {"title": "천안오성고 기말고사 안내", "date": "2026-11-17", "content": "시험 일정", "url": "/board/99"},
        ]
        filtered = normalize_schoolbell_notices(raw)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["title"], "천안오성고 기말고사 안내")

        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp)
            self.assertTrue(is_schoolbell_home_url("https://schoolbell-e.com/"))
            self.assertFalse(is_schoolbell_home_url("https://schoolbell-e.com/school/osung/notices"))
            save_schoolbell_board_url("https://schoolbell-e.com/", pdir)
            self.assertEqual(load_saved_schoolbell_board_url(pdir), "")
            save_schoolbell_board_url("https://schoolbell-e.com/school/osung/notices", pdir)
            self.assertEqual(load_saved_schoolbell_board_url(pdir), "https://schoolbell-e.com/school/osung/notices")

    def test_fetch_keeps_current_user_board_url_and_extracts_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp)
            svc = SchoolbellCrawlerService(profile_dir=pdir)

            list_page = MagicMock()
            list_page.url = "https://schoolbell-e.com/"
            list_page.inner_text.return_value = "학교종이 무료 체험 가입하기 로그인"
            list_page.locator.return_value.count.return_value = 0
            list_page.evaluate.return_value = [
                {
                    "title": "천안오성고 11월 가정통신문",
                    "date": "2026-11-17",
                    "content": "요약만 있음",
                    "url": "/school/osung/notices/55",
                }
            ]

            detail_page = MagicMock()
            detail_page.url = "https://schoolbell-e.com/school/osung/notices/55"
            detail_page.inner_text.return_value = "천안오성고 11월 가정통신문\n상세 본문 전체 내용입니다."

            context = MagicMock()
            context.pages = [list_page]
            context.new_page.return_value = detail_page

            pw_instance = MagicMock()
            pw_instance.chromium.launch_persistent_context.return_value = context
            pw_cm = MagicMock()
            pw_cm.__enter__.return_value = pw_instance
            pw_cm.__exit__.return_value = False

            def _on_login_prompt(_msg: str) -> None:
                # 사용자가 로그인 후 직접 학교 게시판으로 이동하고 확인 버튼 클릭 시뮬레이션
                list_page.url = "https://schoolbell-e.com/school/osung/notices"
                svc.confirm_login()

            with patch("playwright.sync_api.sync_playwright", return_value=pw_cm):
                res = svc.fetch_recent_notices(
                    max_items=10,
                    login_timeout_sec=10,
                    login_prompt_callback=_on_login_prompt,
                )

            self.assertTrue(res["ok"])
            self.assertEqual(res["board_url"], "https://schoolbell-e.com/school/osung/notices")
            # 처음 1회만 기본 URL로 이동하고, 로그인 확인 후에는 홈으로 되돌아가지 않아야 함
            list_page.goto.assert_called_once_with("https://schoolbell-e.com/", wait_until="domcontentloaded", timeout=30000)
            self.assertEqual(len(res["notices"]), 1)
            self.assertIn("상세 본문 전체 내용입니다.", res["notices"][0]["content"])
            self.assertEqual(res["notices"][0]["source_url"], "https://schoolbell-e.com/school/osung/notices/55")
            self.assertEqual(load_saved_schoolbell_board_url(pdir), "https://schoolbell-e.com/school/osung/notices")

    def test_footer_stripping_and_attachment_only_notice(self) -> None:
        raw_with_footer = (
            "11월 20일 학부모 상담 주간 안내입니다.\n"
            "주식회사 학교종이 | 대표이사 홍길동\n"
            "사업자 등록번호 : 123-45-67890\n"
            "Copyright © Schoolbell. All rights reserved."
        )
        cleaned = strip_schoolbell_footer_text(raw_with_footer)
        self.assertEqual(cleaned, "11월 20일 학부모 상담 주간 안내입니다.")
        self.assertNotIn("주식회사 학교종이", cleaned)
        self.assertNotIn("사업자 등록번호", cleaned)

        # 본문 없이 푸터 + 첨부파일만 있는 공지
        footer_only = "주식회사 학교종이\n사업자등록번호 123-45-67890"
        atts = [
            {"name": "2026_가정통신문.pdf", "url": "/api/files/101"},
            {"name": "신청서.hwpx", "url": "https://cdn.schoolbell-e.com/files/form.hwpx"},
        ]
        formatted, first_url = format_schoolbell_detail_content(
            footer_only, atts, base_url="https://schoolbell-e.com/school/osung/notices"
        )
        self.assertTrue(formatted.startswith("첨부파일을 확인해 주세요."))
        self.assertIn("[첨부파일] 2026_가정통신문.pdf (https://schoolbell-e.com/api/files/101)", formatted)
        self.assertIn("[첨부파일] 신청서.hwpx (https://cdn.schoolbell-e.com/files/form.hwpx)", formatted)
        self.assertNotIn("주식회사 학교종이", formatted)
        self.assertEqual(first_url, "https://schoolbell-e.com/api/files/101")

        # SPA 버튼형 첨부(url 없음) + cloneNode 미사용 검증
        from services.schoolbell_crawler_service import _EXTRACT_DETAIL_JS

        self.assertNotIn("cloneNode(", _EXTRACT_DETAIL_JS)
        self.assertIn("createTreeWalker", _EXTRACT_DETAIL_JS)
        spa_formatted, spa_url = format_schoolbell_detail_content(
            "상세 본문입니다.",
            [{"name": "안내장.hwp", "url": ""}],
            base_url="https://schoolbell-e.com/",
        )
        self.assertIn("상세 본문입니다.", spa_formatted)
        self.assertIn("[첨부파일] 안내장.hwp (본문 내 다운로드 버튼)", spa_formatted)
        self.assertEqual(spa_url, "")

        # SPA 모달 상세 추출에서도 첨부파일 URL 및 푸터 제외 동작 확인
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp)
            save_schoolbell_board_url("https://schoolbell-e.com/school/osung/notices", pdir)
            svc = SchoolbellCrawlerService(profile_dir=pdir)

            list_page = MagicMock()
            list_page.url = "https://schoolbell-e.com/school/osung/notices"
            list_page.inner_text.return_value = "우리반 학교안내문 로그아웃"
            def _locator_side_effect(sel: str) -> MagicMock:
                loc = MagicMock()
                if "password" in sel or "app-letter-item-short" in sel:
                    loc.count.return_value = 0
                else:
                    loc.count.return_value = 1
                    loc.first.count.return_value = 1
                    loc.first.is_visible.return_value = True
                return loc

            list_page.locator.side_effect = _locator_side_effect
            list_page.evaluate.side_effect = [
                [
                    {
                        "title": "첨부파일 전용 안내문",
                        "date": "2026-11-18",
                        "content": "첨부파일 전용 안내문",
                        "url": "",
                    }
                ],
                {
                    "body": "주식회사 학교종이\nCopyright © Schoolbell",
                    "attachments": [
                        {"name": "안내문.pdf", "url": "https://cdn.schoolbell-e.com/files/guide.pdf"}
                    ],
                },
            ]

            context = MagicMock()
            context.pages = [list_page]
            pw_instance = MagicMock()
            pw_instance.chromium.launch_persistent_context.return_value = context
            pw_cm = MagicMock()
            pw_cm.__enter__.return_value = pw_instance
            pw_cm.__exit__.return_value = False

            with patch("playwright.sync_api.sync_playwright", return_value=pw_cm):
                res = svc.fetch_recent_notices(max_items=5, login_timeout_sec=5)

            self.assertTrue(res["ok"])
            self.assertEqual(len(res["notices"]), 1)
            n = res["notices"][0]
            self.assertIn("첨부파일을 확인해 주세요.", n["content"])
            self.assertIn("[첨부파일] 안내문.pdf (https://cdn.schoolbell-e.com/files/guide.pdf)", n["content"])
            self.assertNotIn("주식회사 학교종이", n["content"])
            self.assertEqual(n["source_url"], "https://cdn.schoolbell-e.com/files/guide.pdf")

    def test_angular_letter_item_short_parsing_and_crawl(self) -> None:
        # 1) 셀렉터 포함 여부 검증
        self.assertIn("app-letter-item-short", _EXTRACT_ANGULAR_LETTERS_JS)
        self.assertIn(".card-header h6.card-title app-translation-viewer", _EXTRACT_ANGULAR_LETTERS_JS)
        self.assertIn(".card-header extended-letter-metadata p", _EXTRACT_ANGULAR_LETTERS_JS)
        self.assertIn(".card-body.p-3", _EXTRACT_ANGULAR_LETTERS_JS)
        self.assertIn(".card-footer app-letter-filelist .file-item", _EXTRACT_ANGULAR_LETTERS_JS)
        self.assertIn(".g-image-item", _EXTRACT_ANGULAR_LETTERS_JS)

        # 2) normalize_angular_letter_items 검증
        raw_angular = [
            {
                "title": "2026학년도 2학기 학부모 상담 주간 안내",
                "date": "발행 2026년 8월 31일 월요일",
                "body": "학부모님 안녕하십니까.\n2학기 상담 주간 일정을 안내드립니다.",
                "file_items": ["상담신청서.hwpx (129.15 KB)"],
                "preview_images": ["https://s1.schoolbell-e.com/school/123/converted/document_001.jpg"],
            },
            {
                "title": "이미지·첨부 전용 가정통신문",
                "date": "발행 2026년 9월 1일 화요일",
                "body": "",
                "file_items": ["안내문.pdf (210.00 KB)"],
                "preview_images": [],
            },
        ]
        board_url = "https://schoolbell-e.com/school/osung/letters"
        parsed = normalize_angular_letter_items(raw_angular, board_url=board_url, limit=15)
        self.assertEqual(len(parsed), 2)

        self.assertEqual(parsed[0]["published_at"], "2026-08-31")
        self.assertIn("2학기 상담 주간 일정을 안내드립니다.", parsed[0]["content"])
        self.assertIn("[첨부파일] 상담신청서.hwpx (129.15 KB)", parsed[0]["content"])
        self.assertIn("[문서 미리보기] https://s1.schoolbell-e.com/school/123/converted/document_001.jpg", parsed[0]["content"])
        self.assertEqual(parsed[0]["source_url"], "https://s1.schoolbell-e.com/school/123/converted/document_001.jpg")

        self.assertEqual(parsed[1]["published_at"], "2026-09-01")
        self.assertIn("첨부파일을 확인해 주세요.", parsed[1]["content"])
        self.assertIn("[첨부파일] 안내문.pdf (210.00 KB)", parsed[1]["content"])
        self.assertEqual(parsed[1]["source_url"], board_url)

        # 3) fetch_recent_notices가 app-letter-item-short 감지 시 각 카드를 순차 클릭해 직접 파싱하는지 검증
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp)
            save_schoolbell_board_url(board_url, pdir)
            svc = SchoolbellCrawlerService(profile_dir=pdir)

            list_page = MagicMock()
            list_page.url = board_url
            list_page.inner_text.return_value = "우리반 학교안내문 로그아웃"

            card_mocks = []
            for idx, item in enumerate(raw_angular):
                c_mock = MagicMock()
                h_loc = MagicMock()
                h_loc.count.return_value = 1
                f_loc = MagicMock()
                f_loc.count.return_value = 1
                caret_loc = MagicMock()
                caret_loc.count.return_value = 0

                def _card_loc_side_effect(sub_sel: str, _h=h_loc, _f=f_loc, _c=caret_loc) -> MagicMock:
                    sub = MagicMock()
                    if ".card-header" in sub_sel:
                        sub.first = _h
                    elif ".file-item" in sub_sel:
                        sub.count.return_value = _f.count.return_value
                    else:
                        sub.first = _c
                    return sub

                c_mock.locator.side_effect = _card_loc_side_effect
                c_mock.evaluate.return_value = item
                c_mock._header_mock = h_loc
                card_mocks.append(c_mock)

            def _ang_locator(sel: str) -> MagicMock:
                loc = MagicMock()
                if "password" in sel:
                    loc.count.return_value = 0
                elif sel == "app-letter-item-short":
                    loc.count.return_value = len(raw_angular)
                    loc.nth.side_effect = lambda i: card_mocks[i]
                else:
                    loc.count.return_value = 0
                return loc

            list_page.locator.side_effect = _ang_locator

            context = MagicMock()
            context.pages = [list_page]
            pw_instance = MagicMock()
            pw_instance.chromium.launch_persistent_context.return_value = context
            pw_cm = MagicMock()
            pw_cm.__enter__.return_value = pw_instance
            pw_cm.__exit__.return_value = False

            with patch("playwright.sync_api.sync_playwright", return_value=pw_cm):
                res = svc.fetch_recent_notices(max_items=15, login_timeout_sec=5)

            self.assertTrue(res["ok"])
            self.assertEqual(len(res["notices"]), 2)
            # 각 카드의 .card-header를 순차 클릭하고 별도 detail_page는 열지 않음
            for cm in card_mocks:
                cm._header_mock.scroll_into_view_if_needed.assert_called_once()
                cm._header_mock.click.assert_called_once()
            context.new_page.assert_not_called()
            self.assertEqual(res["notices"][0]["published_at"], "2026-08-31")
            self.assertIn("2학기 상담 주간 일정을 안내드립니다.", res["notices"][0]["content"])


class TestFirestoreBoardNotice(unittest.TestCase):
    def test_upsert_board_notice_direct_admin_schema(self) -> None:
        mock_fs = MagicMock()
        mock_col = MagicMock()
        mock_doc = MagicMock()
        mock_fs.collection.return_value = mock_col
        mock_col.document.return_value = mock_doc

        svc = FirestoreSyncService(
            db=MagicMock(),
            config=MagicMock(),
            firestore_client=mock_fs,
        )
        ok, rid = svc.upsert_board_notice(
            {
                "remote_id": "board_123",
                "title": "학교종이 안내",
                "summary": "요약 3줄",
                "content": "상세 본문",
                "source_type": "schoolbell",
                "source_label": "학교종이",
                "source_url": "https://schoolbell-e.com/notice/1",
                "target_grade": "2",
                "target_class": "3",
                "school_code": "7010057",
                "school_name": "테스트고",
                "author_name": "홍길동",
                "is_pinned": True,
                "published_at": "2026-11-17",
            }
        )
        self.assertTrue(ok)
        self.assertEqual(rid, "board_123")
        mock_fs.collection.assert_called_once_with("학급_공지게시판")
        mock_col.document.assert_called_once_with("board_123")
        saved_payload = mock_doc.set.call_args[0][0]
        expected_keys = {
            "remote_id",
            "title",
            "summary",
            "content",
            "source_type",
            "source_label",
            "source_url",
            "target_grade",
            "target_class",
            "school_code",
            "school_name",
            "author_name",
            "is_pinned",
            "published_at",
            "updated_at",
        }
        self.assertEqual(set(saved_payload.keys()), expected_keys)
        self.assertEqual(saved_payload["target_grade"], 2)
        self.assertEqual(saved_payload["target_class"], 3)
        self.assertTrue(saved_payload["is_pinned"])

    def test_upsert_board_notice_vercel_api_mode(self) -> None:
        svc = FirestoreSyncService(db=MagicMock(), config=MagicMock(), firestore_client=None)
        with patch.object(svc, "is_vercel_api_mode", return_value=True), patch.object(
            svc, "_post_vercel_api", return_value={"ok": True, "remote_id": "board_api_1"}
        ) as mock_post:
            ok, rid = svc.upsert_board_notice(
                {
                    "remote_id": "board_api_1",
                    "title": "카톡 공지",
                    "content": "준비물 지참",
                    "source_type": "kakao",
                    "source_label": "학부모 단톡방",
                }
            )
            self.assertTrue(ok)
            self.assertEqual(rid, "board_api_1")
            mock_post.assert_called_once()
            action, payload = mock_post.call_args[0]
            self.assertEqual(action, "upsert_board_notice")
            self.assertEqual(payload["source_type"], "kakao")
            self.assertEqual(payload["summary"], "준비물 지참")


class TestSchoolbellDockLoginAndFullscreenUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_ai_dock_external_login_waiting_and_confirm(self) -> None:
        from core.base_worker import TaskRunner
        from ui.components.ai_execution_dock import AiExecutionDock

        dock = AiExecutionDock(task_runner=TaskRunner())
        mock_worker = MagicMock()

        dock.show_external_login_waiting(
            mock_worker,
            "학교종이 로그인이 필요합니다.",
            provider_name="학교종이",
        )
        self.assertIs(dock._external_login_worker, mock_worker)
        self.assertIsNotNone(dock._pw_inline_card)
        self.assertTrue(dock._pw_inline_card.btn_confirm_login.isVisibleTo(dock._pw_inline_card))

        dock._on_pw_confirm_login_clicked()
        mock_worker.confirm_login.assert_called_once()
        self.assertFalse(dock.btn_pw_confirm_login.isVisible())

        dock.clear_external_login_waiting("수집 완료")
        self.assertIsNone(dock._external_login_worker)
        self.assertIsNone(dock._pw_inline_card)

    def test_classroom_tools_review_fullscreen_and_kakao_dialog(self) -> None:
        from PyQt6.QtWidgets import QDialog, QPlainTextEdit
        from core.base_worker import TaskRunner
        from ui.tabs.tab_classroom_tools import ClassroomToolsTab

        tab = ClassroomToolsTab(task_runner=TaskRunner())

        # 발송 전 검수 목록 전체화면 토글
        tab.btn_toggle_review_fullscreen.setChecked(True)
        tab._on_toggle_review_fullscreen(True)
        self.assertEqual(tab.btn_toggle_review_fullscreen.text(), "원래 크기로")
        self.assertTrue(tab._c4_broadcast_top_widget.isHidden())
        self.assertTrue(tab._c4_ingest_frame.isHidden())
        self.assertTrue(tab.table_broadcast_history.isHidden())
        self.assertGreaterEqual(tab.scroll_review_cards.minimumHeight(), 460)

        tab.btn_toggle_review_fullscreen.setChecked(False)
        tab._on_toggle_review_fullscreen(False)
        self.assertEqual(tab.btn_toggle_review_fullscreen.text(), "전체화면 보기")
        self.assertFalse(tab._c4_broadcast_top_widget.isHidden())
        self.assertFalse(tab._c4_ingest_frame.isHidden())
        self.assertFalse(tab.table_broadcast_history.isHidden())
        self.assertEqual(tab.scroll_review_cards.maximumHeight(), 320)

        # 카톡 입력창 전체화면 다이얼로그 양방향 동기화
        tab.txt_kakao_input.setPlainText("기존 카톡 대화")

        def _fake_exec(dlg_self: QDialog) -> int:
            ed = dlg_self.findChild(QPlainTextEdit)
            self.assertIsNotNone(ed)
            self.assertEqual(ed.toPlainText(), "기존 카톡 대화")
            ed.setPlainText("수정된 카톡 대화 내용")
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog, "exec", _fake_exec):
            tab._on_open_kakao_input_fullscreen()
        self.assertEqual(tab.txt_kakao_input.toPlainText(), "수정된 카톡 대화 내용")

    def test_review_cards_delete_selected(self) -> None:
        from core.base_worker import TaskRunner
        from ui.tabs.tab_classroom_tools import ClassroomToolsTab

        tab = ClassroomToolsTab(task_runner=TaskRunner())
        tab._add_review_notice_card({"title": "공지1", "summary": "요약1", "content": "본문1"})
        tab._add_review_notice_card({"title": "공지2", "summary": "요약2", "content": "본문2"})
        tab._add_review_notice_card({"title": "공지3", "summary": "요약3", "content": "본문3"})
        self.assertEqual(len(tab._notice_review_cards), 3)
        self.assertEqual(tab.lbl_review_count.text(), "3/3건 선택")

        # 2번째 카드만 체크 해제 후 선택 삭제 -> 2번째 카드만 남아야 함
        tab._notice_review_cards[1]["chk_select"].setChecked(False)
        self.assertEqual(tab.lbl_review_count.text(), "2/3건 선택")

        tab._on_delete_selected_review_cards()
        self.assertEqual(len(tab._notice_review_cards), 1)
        self.assertEqual(tab._notice_review_cards[0]["edit_title"].text(), "공지2")
        self.assertEqual(tab.lbl_review_count.text(), "0/1건 선택")


if __name__ == "__main__":
    unittest.main()
