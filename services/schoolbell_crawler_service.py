"""
Playwright 기반 학교종이(https://schoolbell-e.com/) 공지·안내문 수집 서비스.
- %LOCALAPPDATA%\\EduFlow\\schoolbell_profile 영구 브라우저 프로필을 사용하여 1회 로그인 후 세션을 유지합니다.
- 로그인이 필요한 경우 브라우저를 띄워 사용자가 직접 로그인할 때까지 대기한 뒤 최근 공지(제목, 작성일, 본문, 원문 URL)를 추출합니다.
- SchoolbellCrawlWorker(BaseWorker)를 통해 QThread(TaskRunner)에서 비동기 실행됩니다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from PyQt6.QtCore import pyqtSignal

from core.base_worker import BaseWorker

_LOGGER = logging.getLogger(__name__)

SCHOOLBELL_BASE_URL = "https://schoolbell-e.com/"

_LOGIN_URL_HINTS = ("/login", "/signin", "/auth", "/oauth", "accounts.kakao", "nid.naver")
_LOGIN_CTA_HINTS = (
    "로그인",
    "무료 체험",
    "무료체험",
    "가입하기",
    "회원가입",
    "비밀번호",
    "아이디",
    "카카오로 시작",
    "네이버로 시작",
    "간편 로그인",
)
_LOGGED_IN_DASHBOARD_HINTS = (
    "로그아웃",
    "우리반",
    "우리 반",
    "학교안내문",
    "학교 안내문",
    "학급관리",
    "학급 관리",
    "마이페이지",
    "내 정보",
)
_SERVICE_NOTICE_TITLE_HINTS = (
    "학교종이 공식",
    "학교종이 공지",
    "서비스 점검",
    "시스템 점검",
    "서버 점검",
    "개인정보처리방침",
    "개인정보 처리방침",
    "이용약관",
    "서비스 이용약관",
    "무료 체험",
    "도입 문의",
    "고객센터",
    "자주 묻는 질문",
    "학교종이 앱",
    "앱 업데이트",
    "버전 업데이트",
)
_HOME_PATHS = ("", "/", "/home", "/main", "/index", "/index.html")


def get_schoolbell_profile_dir() -> Path:
    """학교종이 전용 Playwright persistent context 프로필 경로를 반환합니다."""
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    base = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
    profile_dir = base / "EduFlow" / "schoolbell_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def is_schoolbell_service_notice(
    title: str,
    content: str = "",
    source_url: str = "",
) -> bool:
    """학교종이 서비스 자체 공지·홍보·점검 배너인지 판별합니다."""
    t = str(title or "").strip()
    c = str(content or "").strip()
    u = str(source_url or "").strip().lower()
    if any(h in t for h in _SERVICE_NOTICE_TITLE_HINTS):
        return True
    if any(seg in u for seg in ("/terms", "/privacy", "/faq", "/pricing", "/intro", "/about")):
        return True
    if len(t) <= 30 and ("학교종이" in t and any(w in (t + " " + c) for w in ("점검", "업데이트", "이벤트", "약관"))):
        return True
    return False


def is_schoolbell_home_url(url: str) -> bool:
    """현재 URL이 학교/학급 게시판이 아닌 학교종이 기본 홈/루트 URL인지 판별합니다."""
    raw = str(url or "").strip()
    if not raw:
        return True
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/").lower()
    return path in ("", "/home", "/main", "/index", "/index.html") and not parsed.query and not parsed.fragment


def load_saved_schoolbell_board_url(profile_dir: Optional[Path] = None) -> str:
    """프로필 폴더에 기억해 둔 마지막 학교/학급 게시판 URL을 반환합니다."""
    pdir = Path(profile_dir) if profile_dir else get_schoolbell_profile_dir()
    meta_file = pdir / "last_board_url.json"
    if not meta_file.is_file():
        return ""
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        url = str(data.get("board_url") or "").strip()
        if url.startswith("http") and not is_schoolbell_home_url(url):
            return url
    except Exception:
        pass
    return ""


def save_schoolbell_board_url(board_url: str, profile_dir: Optional[Path] = None) -> None:
    """수집에 성공한 학교/학급 게시판 URL을 프로필 폴더에 저장합니다."""
    url = str(board_url or "").strip()
    if not url.startswith("http") or is_schoolbell_home_url(url):
        return
    if any(h in url.lower() for h in _LOGIN_URL_HINTS):
        return
    pdir = Path(profile_dir) if profile_dir else get_schoolbell_profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    meta_file = pdir / "last_board_url.json"
    try:
        meta_file.write_text(
            json.dumps({"board_url": url}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def is_schoolbell_login_required(
    url: str,
    body_text: str = "",
    has_password_input: bool = False,
) -> bool:
    """
    현재 페이지 URL·DOM 텍스트를 바탕으로 로그인이 필요한 상태인지 판별합니다.
    랜딩 페이지('무료 체험', '가입하기', '로그인')는 로그인 필요로 판정하고,
    실제 로그인 후 대시보드 요소('로그아웃', '우리반', '학교안내문' 등)가 확인될 때만 로그인 완료로 판정합니다.
    """
    low_url = str(url or "").strip().lower()
    text = str(body_text or "").strip()
    if any(hint in low_url for hint in _LOGIN_URL_HINTS):
        return True
    if has_password_input:
        return True
    if not text:
        return True

    has_logout = "로그아웃" in text
    has_login_cta = any(cta in text for cta in _LOGIN_CTA_HINTS)
    has_dashboard = any(dash in text for dash in _LOGGED_IN_DASHBOARD_HINTS)

    if has_logout:
        return False
    if has_login_cta:
        return True
    if has_dashboard:
        return False
    return True


def _clean_text(text: Any) -> str:
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


_FOOTER_LINE_PATTERNS = (
    re.compile(r"주식회사\s*학교종이"),
    re.compile(r"사업자\s*등록\s*번호"),
    re.compile(r"통신판매업\s*신고"),
    re.compile(r"^대표\s*(?:이사)?\s*[:：]"),
    re.compile(r"^copyright\s*[©(c)]", re.IGNORECASE),
    re.compile(r"all\s+rights\s+reserved", re.IGNORECASE),
)


def strip_schoolbell_footer_text(text: Any) -> str:
    """학교종이 하단 푸터('주식회사 학교종이', 사업자등록번호, Copyright 등) 문구를 제거합니다."""
    cleaned = _clean_text(text)
    if not cleaned:
        return ""
    kept: List[str] = []
    for ln in cleaned.split("\n"):
        if any(pat.search(ln) for pat in _FOOTER_LINE_PATTERNS):
            break
        kept.append(ln)
    return "\n".join(kept).strip()


def format_schoolbell_detail_content(
    body_text: Any,
    attachments: Optional[List[Dict[str, Any]]] = None,
    base_url: str = SCHOOLBELL_BASE_URL,
) -> Tuple[str, str]:
    """
    상세 본문에서 푸터를 제거하고 첨부파일 목록(`[첨부파일] 파일명 (URL)`)을 하단에 정리합니다.
    본문이 비어 있고 첨부파일만 있으면 '첨부파일을 확인해 주세요.'를 기본 본문으로 사용합니다.
    반환값: (정리된 본문, 첫 번째 첨부파일 절대 URL)
    """
    clean_body = strip_schoolbell_footer_text(body_text)
    norm_atts: List[Dict[str, str]] = []
    seen_keys = set()
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        raw_url = str(att.get("url") or att.get("href") or "").strip()
        if raw_url.startswith("javascript:"):
            raw_url = ""
        abs_url = urljoin(base_url, raw_url) if raw_url else ""
        name = _clean_text(att.get("name") or att.get("filename") or "")
        if not name and abs_url:
            name = Path(urlparse(abs_url).path).name or "첨부파일"
        if not name and not abs_url:
            continue
        dedup_key = (abs_url or name.lower())
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        norm_atts.append({"name": name or "첨부파일", "url": abs_url})

    first_att_url = next((a["url"] for a in norm_atts if a["url"]), "")
    if not clean_body and norm_atts:
        clean_body = "첨부파일을 확인해 주세요."

    if norm_atts:
        att_lines = [
            f"[첨부파일] {a['name']} ({a['url']})" if a["url"] else f"[첨부파일] {a['name']} (본문 내 다운로드 버튼)"
            for a in norm_atts
        ]
        if clean_body:
            clean_body = f"{clean_body}\n\n" + "\n".join(att_lines)
        else:
            clean_body = "\n".join(att_lines)

    return clean_body, first_att_url


def _extract_date_str(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{4})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m_short = re.search(r"(\d{1,2})[.\-/월\s]+(\d{1,2})일?", s)
    if m_short:
        month, day = int(m_short.group(1)), int(m_short.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{time.localtime().tm_year:04d}-{month:02d}-{day:02d}"
    return s[:20]


def normalize_schoolbell_notices(
    raw_items: List[Dict[str, Any]],
    base_url: str = SCHOOLBELL_BASE_URL,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """추출된 학교종이 원시 항목을 표준 공지 딕셔너리 목록으로 정규화·중복 제거합니다."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title") or "")
        content = _clean_text(item.get("content") or item.get("body") or "")
        published_at = _extract_date_str(str(item.get("published_at") or item.get("date") or ""))
        raw_url = str(item.get("source_url") or item.get("url") or "").strip()
        source_url = urljoin(base_url, raw_url) if raw_url else base_url
        source_label = _clean_text(item.get("source_label") or "학교종이") or "학교종이"

        if not title and content:
            first_line = content.split("\n")[0].strip()
            title = first_line[:60] + ("..." if len(first_line) > 60 else "")
        if not title or len(title) < 2:
            continue
        if is_schoolbell_service_notice(title, content, source_url):
            continue

        dedup_key = (title.lower(), published_at)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        summary = content[:180].replace("\n", " ").strip()
        if len(content) > 180:
            summary += "..."

        out.append(
            {
                "title": title,
                "published_at": published_at,
                "content": content or title,
                "summary": summary or title,
                "source_url": source_url,
                "source_label": source_label,
                "source_type": "schoolbell",
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return out


_EXTRACT_NOTICES_JS = """
() => {
  const results = [];
  const selectors = [
    'main table tbody tr',
    'table tbody tr',
    'main [class*="board"] li',
    'main [class*="notice"] li',
    'main [class*="list"] li',
    '[class*="board"] [class*="item"]',
    '[class*="notice"] [class*="item"]',
    '[class*="feed"] article',
    'main article',
    'article',
    '[class*="post"]',
    'ul li',
    'a[href*="notice"]',
    'a[href*="board"]',
    'a[href*="post"]',
    'a[href*="letter"]'
  ];
  const seenNodes = new Set();
  const dateRe = /(\\d{4}[.\\-/년\\s]+\\d{1,2}[.\\-/월\\s]+\\d{1,2}|\\d{1,2}[.\\-/월]\\s*\\d{1,2})/;

  for (const sel of selectors) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const el of nodes) {
      if (seenNodes.has(el)) continue;
      const text = (el.innerText || '').trim();
      if (!text || text.length < 6 || text.length > 3500) continue;
      if (el.closest('nav, header, footer, aside, [class*="banner"], [class*="promo"], [class*="official"]')) continue;
      seenNodes.add(el);

      const titleEl = el.querySelector('h1, h2, h3, h4, strong, [class*="title"], [class*="subject"], a');
      let title = titleEl ? (titleEl.innerText || '').trim() : '';
      const lines = text.split(/\\n+/).map(s => s.trim()).filter(Boolean);
      if (!title && lines.length > 0) {
        title = lines[0].slice(0, 80);
      }

      const timeEl = el.querySelector('time, [class*="date"], [class*="time"]');
      let dateStr = timeEl ? (timeEl.innerText || '').trim() : '';
      if (!dateStr) {
        const m = text.match(dateRe);
        if (m) dateStr = m[1];
      }

      const linkEl = el.tagName === 'A' ? el : el.querySelector('a[href]');
      const href = linkEl ? (linkEl.getAttribute('href') || '') : '';

      const bodyEl = el.querySelector('[class*="content"], [class*="body"], [class*="desc"], p');
      let body = bodyEl ? (bodyEl.innerText || '').trim() : '';
      if (!body && lines.length > 1) {
        body = lines.slice(1).join('\\n');
      }
      if (!body) body = text;

      results.push({
        title: title,
        date: dateStr,
        content: body,
        url: href
      });
      if (results.length >= 60) break;
    }
    if (results.length >= 40) break;
  }
  return results;
}
"""


_EXTRACT_DETAIL_JS = """
() => {
  const containerSelectors = [
    '[role="dialog"]',
    '[class*="modal"]',
    '[class*="detail"]',
    '[class*="view"]',
    '[class*="content"]',
    'article',
    'main',
    '[role="main"]',
    'body'
  ];
  let container = null;
  for (const sel of containerSelectors) {
    const el = document.querySelector(sel);
    if (el && ((el.innerText || '').trim().length > 0 || el.querySelector('a[href], [download], [class*="file"], [class*="attach"]'))) {
      container = el;
      break;
    }
  }
  if (!container) container = document.body;

  // 실제 렌더링된 DOM에서 footer/nav/header/aside를 건너뛰며 텍스트 수집 (cloneNode 미사용)
  const skipSelector = 'footer, nav, header, aside, script, style, noscript';
  const walker = document.createTreeWalker(
    container,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode: (node) => {
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (parent.closest(skipSelector)) return NodeFilter.FILTER_REJECT;
        if (!(node.nodeValue || '').trim()) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    }
  );
  const chunks = [];
  let prevBlock = null;
  let curNode;
  while ((curNode = walker.nextNode())) {
    const txt = (curNode.nodeValue || '').replace(/\\s+/g, ' ').trim();
    if (!txt) continue;
    const block = curNode.parentElement.closest('p, div, li, h1, h2, h3, h4, h5, h6, tr, blockquote, pre, section, article');
    if (prevBlock && block !== prevBlock) {
      chunks.push('\\n');
    } else if (chunks.length > 0 && chunks[chunks.length - 1] !== '\\n') {
      chunks.push(' ');
    }
    chunks.push(txt);
    prevBlock = block;
  }
  let bodyText = chunks.join('').trim();

  // TreeWalker 결과가 너무 짧으면 실제 렌더링된 container.innerText에서 footer 텍스트만 제외해 보완
  const renderedText = (container.innerText || '').trim();
  if (renderedText.length > bodyText.length) {
    let stripped = renderedText;
    container.querySelectorAll('footer, nav, header, aside').forEach(skipEl => {
      const skipTxt = (skipEl.innerText || '').trim();
      if (skipTxt && stripped.includes(skipTxt)) {
        stripped = stripped.split(skipTxt).join('').trim();
      }
    });
    if (stripped.length > bodyText.length) {
      bodyText = stripped;
    }
  }

  const attHrefRe = /\\.(pdf|hwpx?|png|jpe?g|gif|webp|docx?|xlsx?|pptx?|zip)(\\?|$)|api\\/|cdn\\.schoolbell-e\\.com|download|^blob:/i;
  const attNameRe = /\\.(pdf|hwpx?|png|jpe?g|gif|webp|docx?|xlsx?|pptx?|zip)\\b/i;
  const attachments = [];
  const seen = new Set();
  const debugTags = [];

  const links = Array.from(container.querySelectorAll('a[href], a[download], [download]'));
  for (const a of links) {
    if (a.closest('footer, nav, header')) continue;
    const rawHref = (a.getAttribute('href') || a.getAttribute('data-url') || a.getAttribute('data-href') || '').trim();
    const hasDownload = a.hasAttribute('download');
    const labelText = (a.getAttribute('download') || a.innerText || a.textContent || '').trim();
    if (rawHref.startsWith('javascript:')) continue;
    if (!rawHref && !hasDownload && !attNameRe.test(labelText)) continue;
    if (rawHref && !hasDownload && !attHrefRe.test(rawHref) && !attNameRe.test(labelText)) continue;
    let absUrl = rawHref;
    if (rawHref && !rawHref.startsWith('blob:')) {
      try { absUrl = new URL(rawHref, window.location.href).href; } catch (e) {}
    }
    const key = absUrl || labelText.toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    attachments.push({ name: labelText, url: absUrl });
  }

  // SPA 파일명/다운로드 버튼 요소 탐색 (href 없이 클릭으로 다운로드하는 구조 대응)
  const btnCandidates = Array.from(container.querySelectorAll('button, [role="button"], [class*="file"], [class*="attach"]'));
  for (const el of btnCandidates) {
    if (el.closest('footer, nav, header')) continue;
    const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!txt || txt.length > 160 || !attNameRe.test(txt)) continue;
    const nestedLink = el.querySelector('a[href]');
    const rawUrl = (
      (nestedLink && nestedLink.getAttribute('href')) ||
      el.getAttribute('data-url') ||
      el.getAttribute('data-href') ||
      el.getAttribute('data-file-url') ||
      ''
    ).trim();
    let absUrl = rawUrl;
    if (rawUrl && !rawUrl.startsWith('javascript:') && !rawUrl.startsWith('blob:')) {
      try { absUrl = new URL(rawUrl, window.location.href).href; } catch (e) {}
    } else if (rawUrl.startsWith('javascript:')) {
      absUrl = '';
    }
    const key = absUrl || txt.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      attachments.push({ name: txt, url: absUrl });
      if (!absUrl) {
        debugTags.push(el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).trim().split(/\\s+/).join('.') : ''));
      }
    }
  }

  return { body: bodyText, attachments: attachments, attachDebug: debugTags.slice(0, 5).join(', ') };
}
"""


_EXTRACT_SINGLE_ANGULAR_CARD_JS = """
(card) => {
  if (!card) return null;
  const bgUrlRe = /url\\(\\s*["']?([^"')]+)["']?\\s*\\)/i;

  const titleEl =
    card.querySelector('.card-header h6.card-title app-translation-viewer') ||
    card.querySelector('.card-header h6.card-title') ||
    card.querySelector('.card-title');
  const title = titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : '';

  const dateEl =
    card.querySelector('.card-header extended-letter-metadata p') ||
    card.querySelector('.card-header extended-letter-metadata');
  const dateStr = dateEl ? (dateEl.innerText || dateEl.textContent || '').trim() : '';

  // .card-body.p-3 본문 수집 (app-media-browser 및 .card-footer 제외, cloneNode 미사용)
  const bodyEl = card.querySelector('.card-body.p-3') || card.querySelector('.card-body');
  let bodyText = '';
  if (bodyEl) {
    const skipSel = 'app-media-browser, .card-footer, script, style, noscript';
    const walker = document.createTreeWalker(
      bodyEl,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode: (node) => {
          const p = node.parentElement;
          if (!p || p.closest(skipSel)) return NodeFilter.FILTER_REJECT;
          if (!(node.nodeValue || '').trim()) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      }
    );
    const chunks = [];
    let prevBlock = null;
    let cur;
    while ((cur = walker.nextNode())) {
      const txt = (cur.nodeValue || '').replace(/\\s+/g, ' ').trim();
      if (!txt) continue;
      const block = cur.parentElement.closest('p, div, li, h1, h2, h3, h4, h5, h6, tr, blockquote, pre');
      if (prevBlock && block !== prevBlock) {
        chunks.push('\\n');
      } else if (chunks.length > 0 && chunks[chunks.length - 1] !== '\\n') {
        chunks.push(' ');
      }
      chunks.push(txt);
      prevBlock = block;
    }
    bodyText = chunks.join('').trim();
    const rendered = (bodyEl.innerText || '').trim();
    if (rendered.length > bodyText.length) {
      let stripped = rendered;
      bodyEl.querySelectorAll('app-media-browser, .card-footer').forEach(skipEl => {
        const st = (skipEl.innerText || '').trim();
        if (st && stripped.includes(st)) {
          stripped = stripped.split(st).join('').trim();
        }
      });
      if (stripped.length > bodyText.length) {
        bodyText = stripped;
      }
    }
  }

  // <app-media-browser> 내부 gallery-item / gallery-thumb .g-image-item background-image URL 추출
  const previewImages = [];
  const seenImgs = new Set();
  const imgEls = card.querySelectorAll(
    'app-media-browser gallery-item .g-image-item, app-media-browser gallery-thumb .g-image-item'
  );
  for (const imgEl of imgEls) {
    const rawStyle = imgEl.style.backgroundImage || window.getComputedStyle(imgEl).backgroundImage || '';
    const m = rawStyle.match(bgUrlRe);
    if (!m || !m[1]) continue;
    const imgUrl = m[1].trim();
    if (!imgUrl || imgUrl.startsWith('data:') || seenImgs.has(imgUrl)) continue;
    seenImgs.add(imgUrl);
    previewImages.push(imgUrl);
  }

  // .card-footer app-letter-filelist .file-item 첨부파일명 추출
  const fileItems = [];
  const seenFiles = new Set();
  const fileEls = card.querySelectorAll('.card-footer app-letter-filelist .file-item');
  for (const fEl of fileEls) {
    const fTxt = (fEl.innerText || fEl.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!fTxt || seenFiles.has(fTxt)) continue;
    seenFiles.add(fTxt);
    fileItems.push(fTxt);
  }

  return {
    title: title,
    date: dateStr,
    body: bodyText,
    file_items: fileItems,
    preview_images: previewImages
  };
}
"""

_EXTRACT_ANGULAR_LETTERS_JS = """
(maxItems) => {
  const cards = Array.from(document.querySelectorAll('app-letter-item-short'));
  const limit = Math.max(1, Number(maxItems || 15));
  const out = [];
  const bgUrlRe = /url\\(\\s*["']?([^"')]+)["']?\\s*\\)/i;

  for (const card of cards.slice(0, limit)) {
    const titleEl =
      card.querySelector('.card-header h6.card-title app-translation-viewer') ||
      card.querySelector('.card-header h6.card-title') ||
      card.querySelector('.card-title');
    const title = titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : '';

    const dateEl =
      card.querySelector('.card-header extended-letter-metadata p') ||
      card.querySelector('.card-header extended-letter-metadata');
    const dateStr = dateEl ? (dateEl.innerText || dateEl.textContent || '').trim() : '';

    // .card-body.p-3 본문 수집 (app-media-browser 및 .card-footer 제외, cloneNode 미사용)
    const bodyEl = card.querySelector('.card-body.p-3') || card.querySelector('.card-body');
    let bodyText = '';
    if (bodyEl) {
      const skipSel = 'app-media-browser, .card-footer, script, style, noscript';
      const walker = document.createTreeWalker(
        bodyEl,
        NodeFilter.SHOW_TEXT,
        {
          acceptNode: (node) => {
            const p = node.parentElement;
            if (!p || p.closest(skipSel)) return NodeFilter.FILTER_REJECT;
            if (!(node.nodeValue || '').trim()) return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_ACCEPT;
          }
        }
      );
      const chunks = [];
      let prevBlock = null;
      let cur;
      while ((cur = walker.nextNode())) {
        const txt = (cur.nodeValue || '').replace(/\\s+/g, ' ').trim();
        if (!txt) continue;
        const block = cur.parentElement.closest('p, div, li, h1, h2, h3, h4, h5, h6, tr, blockquote, pre');
        if (prevBlock && block !== prevBlock) {
          chunks.push('\\n');
        } else if (chunks.length > 0 && chunks[chunks.length - 1] !== '\\n') {
          chunks.push(' ');
        }
        chunks.push(txt);
        prevBlock = block;
      }
      bodyText = chunks.join('').trim();
      const rendered = (bodyEl.innerText || '').trim();
      if (rendered.length > bodyText.length) {
        let stripped = rendered;
        bodyEl.querySelectorAll('app-media-browser, .card-footer').forEach(skipEl => {
          const st = (skipEl.innerText || '').trim();
          if (st && stripped.includes(st)) {
            stripped = stripped.split(st).join('').trim();
          }
        });
        if (stripped.length > bodyText.length) {
          bodyText = stripped;
        }
      }
    }

    // <app-media-browser> 내부 gallery-item / gallery-thumb .g-image-item background-image URL 추출
    const previewImages = [];
    const seenImgs = new Set();
    const imgEls = card.querySelectorAll(
      'app-media-browser gallery-item .g-image-item, app-media-browser gallery-thumb .g-image-item'
    );
    for (const imgEl of imgEls) {
      const rawStyle = imgEl.style.backgroundImage || window.getComputedStyle(imgEl).backgroundImage || '';
      const m = rawStyle.match(bgUrlRe);
      if (!m || !m[1]) continue;
      const imgUrl = m[1].trim();
      if (!imgUrl || imgUrl.startsWith('data:') || seenImgs.has(imgUrl)) continue;
      seenImgs.add(imgUrl);
      previewImages.push(imgUrl);
    }

    // .card-footer app-letter-filelist .file-item 첨부파일명 추출
    const fileItems = [];
    const seenFiles = new Set();
    const fileEls = card.querySelectorAll('.card-footer app-letter-filelist .file-item');
    for (const fEl of fileEls) {
      const fTxt = (fEl.innerText || fEl.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!fTxt || seenFiles.has(fTxt)) continue;
      seenFiles.add(fTxt);
      fileItems.push(fTxt);
    }

    out.push({
      title: title,
      date: dateStr,
      body: bodyText,
      file_items: fileItems,
      preview_images: previewImages
    });
  }
  return out;
}
"""


def normalize_angular_letter_items(
    raw_items: List[Dict[str, Any]],
    board_url: str = SCHOOLBELL_BASE_URL,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """학교종이 Angular 카드(app-letter-item-short) 추출 결과를 표준 공지 딕셔너리 목록으로 정규화합니다."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title") or "")
        published_at = _extract_date_str(str(item.get("date") or item.get("published_at") or ""))
        body_clean = strip_schoolbell_footer_text(item.get("body") or "")

        file_items = [
            _clean_text(f) for f in (item.get("file_items") or []) if _clean_text(f)
        ]
        preview_images: List[str] = []
        seen_imgs = set()
        for raw_u in (item.get("preview_images") or []):
            u = str(raw_u or "").strip()
            if not u or u.startswith("data:"):
                continue
            abs_u = urljoin(board_url, u)
            if abs_u not in seen_imgs:
                seen_imgs.add(abs_u)
                preview_images.append(abs_u)

        if not title and body_clean:
            first_ln = body_clean.split("\n")[0].strip()
            title = first_ln[:60] + ("..." if len(first_ln) > 60 else "")
        if not title or len(title) < 2:
            continue
        if is_schoolbell_service_notice(title, body_clean, board_url):
            continue

        dedup_key = (title.lower(), published_at)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        extra_lines: List[str] = []
        for f_name in file_items:
            extra_lines.append(f"[첨부파일] {f_name}")
        for img_url in preview_images:
            extra_lines.append(f"[문서 미리보기] {img_url}")

        if not body_clean and extra_lines:
            body_clean = "첨부파일을 확인해 주세요."

        if extra_lines:
            content = f"{body_clean}\n\n" + "\n".join(extra_lines) if body_clean else "\n".join(extra_lines)
        else:
            content = body_clean or title

        source_url = preview_images[0] if preview_images else board_url
        summary = content[:180].replace("\n", " ").strip()
        if len(content) > 180:
            summary += "..."

        out.append(
            {
                "title": title,
                "published_at": published_at,
                "content": content,
                "summary": summary or title,
                "source_url": source_url,
                "source_label": "학교종이",
                "source_type": "schoolbell",
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return out


class SchoolbellCrawlerService:
    """Playwright persistent context로 학교종이 최근 공지·안내문을 수집합니다."""

    def __init__(self, profile_dir: Optional[Path] = None) -> None:
        self.profile_dir = Path(profile_dir) if profile_dir else get_schoolbell_profile_dir()
        self._login_confirmed_event = threading.Event()

    def confirm_login(self) -> None:
        """사용자가 로그인을 완료했음을 수동으로 알릴 때 호출합니다."""
        self._login_confirmed_event.set()

    def fetch_recent_notices(
        self,
        max_items: int = 15,
        login_timeout_sec: int = 180,
        headless: bool = False,
        school_name: str = "",
        target_url: str = "",
        progress_callback: Optional[Callable[[int, str], None]] = None,
        login_prompt_callback: Optional[Callable[[str], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """학교종이에 접속해 로그인 확인 후 현재/대상 게시판의 최근 공지 목록과 상세 본문을 반환합니다."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {
                "ok": False,
                "missing_playwright": True,
                "error": "Playwright가 설치되어 있지 않습니다. 'pip install playwright && playwright install chromium'을 실행해 주세요.",
                "notices": [],
            }

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._login_confirmed_event.clear()

        def _emit(pct: int, msg: str) -> None:
            if progress_callback:
                progress_callback(pct, msg)

        def _stopped() -> bool:
            return bool(stop_check and stop_check())

        start_url = (
            str(target_url or "").strip()
            or load_saved_schoolbell_board_url(self.profile_dir)
            or SCHOOLBELL_BASE_URL
        )
        was_login_required = False
        _emit(10, "학교종이 브라우저 프로필 여는 중...")

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=headless,
                viewport={"width": 1280, "height": 860},
                locale="ko-KR",
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                _emit(20, f"학교종이 접속 중 ({start_url})...")
                page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

                # 로그인 필요 여부 검사
                body_text = ""
                has_pw = False
                try:
                    body_text = page.inner_text("body")[:4000]
                    has_pw = page.locator('input[type="password"]').count() > 0
                except Exception:
                    pass

                if is_schoolbell_login_required(page.url, body_text, has_password_input=has_pw):
                    was_login_required = True
                    msg = (
                        "브라우저에서 학교종이 로그인 후, 수집할 우리 학교 또는 우리 반의 "
                        "학교안내문/알림장 목록 화면까지 이동한 뒤 [인증/로그인 완료 · 작업 재개]를 눌러주세요."
                    )
                    _emit(30, msg)
                    if login_prompt_callback:
                        login_prompt_callback(msg)

                    deadline = time.monotonic() + max(15, int(login_timeout_sec))
                    logged_in = False
                    while time.monotonic() < deadline:
                        if _stopped():
                            return {"ok": False, "cancelled": True, "notices": [], "error": "사용자가 수집을 중단했습니다."}
                        if self._login_confirmed_event.is_set():
                            logged_in = True
                            break
                        time.sleep(0.5)

                    if not logged_in:
                        return {
                            "ok": False,
                            "login_timeout": True,
                            "notices": [],
                            "error": "로그인 대기 시간이 초과되었습니다. 로그인 후 다시 시도해 주세요.",
                        }
                    try:
                        page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass

                # 사용자가 이동해 둔 현재 페이지(page.url)를 그대로 유지하되,
                # 아직 기본 홈에 머물러 있는 경우에만 소속 학교명 / 우리반 / 학교안내문 링크 탐색·이동 시도
                if is_schoolbell_home_url(page.url):
                    candidates = [c for c in [str(school_name or "").strip(), "학교안내문", "우리반", "우리 반", "알림장", "가정통신문"] if c]
                    for kw in candidates:
                        try:
                            loc = page.locator(f"a:has-text('{kw}'), button:has-text('{kw}')").first
                            if loc.count() > 0 and loc.is_visible():
                                _emit(45, f"'{kw}' 메뉴로 이동 중...")
                                loc.click(timeout=5000)
                                try:
                                    page.wait_for_load_state("networkidle", timeout=6000)
                                except Exception:
                                    pass
                                time.sleep(1.0)
                                break
                        except Exception:
                            continue

                board_url = page.url
                save_schoolbell_board_url(board_url, self.profile_dir)

                _emit(55, "학교·학급 공지/안내문 목록 추출 중...")
                time.sleep(1.0)

                angular_count = 0
                try:
                    angular_count = page.locator("app-letter-item-short").count()
                except Exception:
                    angular_count = 0

                if angular_count > 0:
                    target_cnt = min(angular_count, max(1, int(max_items)))
                    raw_letters: List[Dict[str, Any]] = []
                    for idx in range(target_cnt):
                        if _stopped():
                            break
                        _emit(
                            60 + int(((idx + 1) / max(1, target_cnt)) * 35),
                            f"학교종이 공지 카드 클릭·추출 중 ({idx + 1}/{target_cnt})...",
                        )
                        try:
                            card_loc = page.locator("app-letter-item-short").nth(idx)
                            header_loc = card_loc.locator(".card-header, .card-title").first
                            try:
                                if header_loc.count() > 0:
                                    header_loc.scroll_into_view_if_needed(timeout=3000)
                                    header_loc.click(timeout=3000)
                                else:
                                    card_loc.scroll_into_view_if_needed(timeout=3000)
                                    card_loc.click(timeout=3000)
                            except Exception:
                                try:
                                    card_loc.click(timeout=2000)
                                except Exception:
                                    pass

                            try:
                                page.wait_for_timeout(350)
                            except Exception:
                                time.sleep(0.35)

                            # app-letter-filelist가 접혀 있으면(.fa-caret-down 등) 첨부파일 헤더 클릭해 펼침
                            try:
                                file_items_loc = card_loc.locator(".card-footer app-letter-filelist .file-item")
                                if file_items_loc.count() == 0:
                                    caret_loc = card_loc.locator(
                                        '.card-footer app-letter-filelist .fa-caret-down, '
                                        '.card-footer app-letter-filelist [data-bs-toggle="collapse"], '
                                        '.card-footer app-letter-filelist button'
                                    ).first
                                    if caret_loc.count() > 0 and caret_loc.is_visible():
                                        caret_loc.click(timeout=1500)
                                        try:
                                            page.wait_for_timeout(200)
                                        except Exception:
                                            time.sleep(0.2)
                            except Exception:
                                pass

                            card_data = card_loc.evaluate(_EXTRACT_SINGLE_ANGULAR_CARD_JS)
                            if isinstance(card_data, dict):
                                raw_letters.append(card_data)
                        except Exception:
                            continue

                    notices = normalize_angular_letter_items(
                        raw_letters, board_url=board_url, limit=max_items
                    )
                else:
                    raw_items = page.evaluate(_EXTRACT_NOTICES_JS) or []
                    notices = normalize_schoolbell_notices(raw_items, base_url=board_url, limit=max_items)

                    # 범용 페이지 폴백: 각 최근 공지 항목(최대 max_items건)의 상세 페이지/모달 추출
                    detail_limit = min(len(notices), max(1, int(max_items)))
                    for idx, notice in enumerate(notices[:detail_limit]):
                        if _stopped():
                            break
                        n_url = str(notice.get("source_url") or "").strip()
                        _emit(
                            60 + int(((idx + 1) / max(1, detail_limit)) * 35),
                            f"공지 상세 본문 추출 중 ({idx + 1}/{detail_limit})...",
                        )
                        if n_url and n_url.rstrip("/") != board_url.rstrip("/") and not is_schoolbell_home_url(n_url):
                            try:
                                detail_page = context.new_page()
                                try:
                                    detail_page.goto(n_url, wait_until="domcontentloaded", timeout=15000)
                                    try:
                                        detail_page.wait_for_load_state("networkidle", timeout=4000)
                                    except Exception:
                                        pass
                                    detail_info = detail_page.evaluate(_EXTRACT_DETAIL_JS)
                                    if isinstance(detail_info, dict) and ("body" in detail_info or "attachments" in detail_info):
                                        raw_body = detail_info.get("body") or ""
                                        raw_atts = detail_info.get("attachments") or []
                                        if detail_info.get("attachDebug"):
                                            _LOGGER.info("학교종이 SPA 첨부 영역 구조: %s", detail_info["attachDebug"])
                                    else:
                                        raw_body = detail_page.inner_text("main, article, body")[:8000]
                                        raw_atts = []
                                    cur_detail_url = detail_page.url or n_url
                                    detail_text, first_att_url = format_schoolbell_detail_content(
                                        raw_body, raw_atts, base_url=cur_detail_url
                                    )
                                    if detail_text:
                                        notice["content"] = detail_text
                                        notice["summary"] = detail_text[:180].replace("\n", " ").strip() + (
                                            "..." if len(detail_text) > 180 else ""
                                        )
                                        notice["source_url"] = (
                                            cur_detail_url
                                            if (cur_detail_url and cur_detail_url.rstrip("/") != board_url.rstrip("/"))
                                            else (first_att_url or n_url)
                                        )
                                finally:
                                    detail_page.close()
                            except Exception:
                                pass
                        else:
                            # SPA 아코디언/모달/라우트 전환형 게시글 클릭 추출 시도
                            title_txt = str(notice.get("title") or "").strip()
                            if title_txt and len(str(notice.get("content") or "")) < 150:
                                try:
                                    item_loc = page.locator(f"text={title_txt[:30]}").first
                                    if item_loc.count() > 0 and item_loc.is_visible():
                                        before_url = page.url
                                        item_loc.click(timeout=4000)
                                        time.sleep(0.8)
                                        modal_info = page.evaluate(_EXTRACT_DETAIL_JS)
                                        if isinstance(modal_info, dict) and ("body" in modal_info or "attachments" in modal_info):
                                            raw_body = modal_info.get("body") or ""
                                            raw_atts = modal_info.get("attachments") or []
                                            if modal_info.get("attachDebug"):
                                                _LOGGER.info("학교종이 모달 SPA 첨부 영역 구조: %s", modal_info["attachDebug"])
                                        else:
                                            raw_body = page.inner_text(
                                                '[role="dialog"], [class*="modal"], [class*="detail"], [class*="view"], article, main'
                                            )[:8000]
                                            raw_atts = []
                                        modal_text, first_att_url = format_schoolbell_detail_content(
                                            raw_body, raw_atts, base_url=page.url
                                        )
                                        if modal_text and (
                                            len(modal_text) > len(str(notice.get("content") or "")) or first_att_url
                                        ):
                                            notice["content"] = modal_text
                                            notice["summary"] = modal_text[:180].replace("\n", " ").strip() + (
                                                "..." if len(modal_text) > 180 else ""
                                            )
                                        if page.url != before_url:
                                            notice["source_url"] = page.url
                                            page.go_back(wait_until="domcontentloaded", timeout=10000)
                                            time.sleep(0.5)
                                        else:
                                            if first_att_url:
                                                notice["source_url"] = first_att_url
                                            page.keyboard.press("Escape")
                                except Exception:
                                    pass

                _emit(100, f"학교종이 공지 {len(notices)}건 수집 완료")
                return {
                    "ok": True,
                    "notices": notices,
                    "board_url": board_url,
                    "profile_dir": str(self.profile_dir),
                    "login_required": was_login_required,
                }
            finally:
                try:
                    context.close()
                except Exception:
                    pass


class SchoolbellCrawlWorker(BaseWorker):
    """TaskRunner(QThread)에서 학교종이 공지 수집을 비동기 실행하는 워커."""

    login_required = pyqtSignal(str)

    def __init__(
        self,
        max_items: int = 15,
        login_timeout_sec: int = 180,
        school_name: str = "",
        target_url: str = "",
        profile_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(task_name="학교종이 공지 수집")
        self.max_items = max_items
        self.login_timeout_sec = login_timeout_sec
        self.school_name = school_name
        self.target_url = target_url
        self.service = SchoolbellCrawlerService(profile_dir=profile_dir)

    def confirm_login(self) -> None:
        self.service.confirm_login()

    def execute(self) -> Dict[str, Any]:
        return self.service.fetch_recent_notices(
            max_items=self.max_items,
            login_timeout_sec=self.login_timeout_sec,
            headless=False,
            school_name=self.school_name,
            target_url=self.target_url,
            progress_callback=self.emit_progress,
            login_prompt_callback=lambda msg: self.login_required.emit(msg),
            stop_check=self.is_stop_requested,
        )
