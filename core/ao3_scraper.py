"""
core/ao3_scraper.py
-------------------
Fetches AO3 work/series metadata using the ao3_api library.

Install:  pip install AO3

ao3_api handles rate-limiting, session management, and adult content
automatically. work.completed is a clean bool — no "[Invalid DateTime]".
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

try:
    import requests
    from bs4 import BeautifulSoup
    _HTTP_DEPS_OK = True
except Exception:
    _HTTP_DEPS_OK = False

_DEPS_OK = False
_DEPS_ERR = ""


def _looks_like_ao3_client(mod) -> bool:
    """Return True if module exposes the AO3 client API this file needs."""
    return all(hasattr(mod, name) for name in ("Work", "Series", "Session"))


try:
    import AO3
    # Guard against local namespace/package shadowing (e.g. workspace ./AO3 folder)
    if _looks_like_ao3_client(AO3):
        _DEPS_OK = True
    else:
        _DEPS_ERR = "import AO3 resolved to a module without Work/Series/Session"
        raise ImportError(_DEPS_ERR)
except ImportError as _e:
    _DEPS_ERR = f"import AO3 failed: {_e}"
    try:
        import ao3 as AO3
        if _looks_like_ao3_client(AO3):
            _DEPS_OK = True
            _DEPS_ERR = ""
        else:
            _DEPS_ERR = "import ao3 succeeded but module lacks Work/Series/Session"
    except ImportError as _e2:
        _DEPS_ERR = f"import AO3 failed: {_e} | import ao3 failed: {_e2}"


# ── Session ──────────────────────────────────────────────────────────────────

_session: "AO3.Session | None" = None


def init_session(username: str, password: str,
                 log_cb: Callable | None = None) -> bool:
    """
    Log in to AO3 and store a session for all subsequent requests.
    Called at startup if credentials are found in the environment.
    Returns True on success.
    """
    global _session
    if not _DEPS_OK:
        log.debug("init_session: AO3 client library not available, skipping")
        return False
    if not username or not password:
        log.debug("init_session: no credentials provided, skipping")
        return False
    try:
        if log_cb:
            log_cb(f"[ao3] logging in as {username}...\n")
        log.debug("init_session: logging in as %s", username)
        _session = AO3.Session(username, password)
        if log_cb:
            log_cb(f"[ao3] logged in OK\n")
        log.debug("init_session: login OK for %s", username)
        return True
    except Exception as e:
        if log_cb:
            log_cb(f"[ao3] login failed: {e}\n")
        log.warning("init_session: login failed for %s: %s", username, e)
        _session = None
        return False


def load_session_from_env(log_cb: Callable | None = None) -> bool:
    """Load AO3 credentials from environment variables and log in."""
    username = os.environ.get("AO3_USERNAME", "")
    password = os.environ.get("AO3_PASSWORD", "")
    return init_session(username, password, log_cb=log_cb)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class WorkInfo:
    url:        str
    title:      str  = ""
    date:       str  = ""
    status:     str  = ""
    chapters:   str  = ""
    word_count: str  = ""
    is_series:  bool = False
    error:      str  = ""
    # Rich metadata
    summary:       str  = ""
    categories:    list = field(default_factory=list)   # F/M, Gen, etc.
    tags:          list = field(default_factory=list)   # freeform tags
    authors:       list = field(default_factory=list)
    rating:        str  = ""                            # General Audiences, etc.
    fandoms_list:  list = field(default_factory=list)   # actual fandom tags
    relationships: list = field(default_factory=list)
    characters:    list = field(default_factory=list)
    warnings:      list = field(default_factory=list)   # archive warnings / CWs


# ── ID extraction ─────────────────────────────────────────────────────────────

def _work_id(url: str) -> int | None:
    m = re.search(r"/works/(\d+)", url)
    return int(m.group(1)) if m else None


def _series_id(url: str) -> int | None:
    m = re.search(r"/series/(\d+)", url)
    return int(m.group(1)) if m else None


def _safe_work_date(work) -> object | None:
    """Return best available AO3 date object without propagating parser errors."""
    for attr in ("date_updated", "date_published"):
        try:
            value = getattr(work, attr)
            if value:
                return value
        except Exception:
            continue
    return None


def _scrape_work_http_fallback(url: str, log_cb: Callable | None = None) -> WorkInfo:
    """Fallback scraper using requests + BeautifulSoup when ao3_api fails."""
    info = WorkInfo(url=url)

    if not _HTTP_DEPS_OK:
        info.error = "Fallback scrape unavailable (requests/bs4 not installed)"
        return info

    try:
        resp = requests.get(
            url,
            params={"view_adult": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        info.error = f"HTTP fetch failed: {e}"
        return info

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title
    try:
        t = soup.select_one("h2.title")
        if t:
            info.title = t.get_text(" ", strip=True)
    except Exception:
        pass

    # Date
    try:
        updated_dd = soup.select_one("dd.status")
        published_dd = soup.select_one("dd.published")
        date_str = ""
        if updated_dd:
            date_str = updated_dd.get_text(strip=True)
        elif published_dd:
            date_str = published_dd.get_text(strip=True)
        if date_str and re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            info.date = date_str[:10]
    except Exception:
        pass

    # Chapters + completion status
    info.status = "🔴"
    try:
        ch_dd = soup.select_one("dd.chapters")
        chapters_text = ch_dd.get_text(strip=True) if ch_dd else ""
        m = re.match(r"(\d+)/(\d+|\?)", chapters_text)
        if m:
            current = int(m.group(1))
            total_s = m.group(2)
            if total_s != "?" and int(total_s) > 0 and current >= int(total_s):
                info.status = "🟢"
            info.chapters = chapters_text
    except Exception:
        pass

    # Word count
    try:
        wc_dd = soup.select_one("dd.words")
        if wc_dd:
            info.word_count = wc_dd.get_text(strip=True).replace(",", "")
    except Exception:
        pass

    # Rich metadata
    try:
        summary_block = soup.select_one("div.summary blockquote")
        if summary_block:
            info.summary = summary_block.get_text(separator="\n", strip=True)

        info.authors = [a.get_text(strip=True)
                        for a in soup.select("h3.byline a[rel='author']")]
        rating_dd = soup.select_one("dd.rating a.tag")
        if rating_dd:
            info.rating = rating_dd.get_text(strip=True)
        info.categories = [a.get_text(strip=True)
                           for a in soup.select("dd.category a.tag")]
        info.fandoms_list = [a.get_text(strip=True)
                             for a in soup.select("dd.fandom a.tag")]
        info.warnings = [a.get_text(strip=True)
                         for a in soup.select("dd.warning a.tag")]
        info.relationships = [a.get_text(strip=True)
                              for a in soup.select("dd.relationship a.tag")]
        info.characters = [a.get_text(strip=True)
                           for a in soup.select("dd.character a.tag")]
        info.tags = [a.get_text(strip=True)
                     for a in soup.select("dd.freeform a.tag")]
    except Exception:
        pass

    if not info.title:
        info.error = "Could not parse work page"
    elif log_cb:
        log_cb("[fetch] used HTTP fallback parser\n")
    return info


# ── Single work ───────────────────────────────────────────────────────────────

def _scrape_work(url: str, log_cb: Callable | None = None) -> WorkInfo:
    info = WorkInfo(url=url)

    if not _DEPS_OK:
        info.error = f"AO3 library not installed — run: pip install AO3\n({_DEPS_ERR})"
        return info

    wid = _work_id(url)
    if wid is None:
        info.error = f"Cannot extract work ID from: {url}"
        return info

    if log_cb:
        log_cb(f"[fetch] work {wid}\n")

    try:
        work = AO3.Work(wid, load_chapters=False, session=_session)
    except Exception as e:
        # Try robust fallback parser when ao3_api/library crashes on edge pages.
        fallback = _scrape_work_http_fallback(url, log_cb=log_cb)
        if not fallback.error:
            return fallback

        # Different AO3 library variants expose exceptions in different places
        # (or not at all). Avoid direct `AO3.utils.*` references here.
        exc_name = type(e).__name__
        msg = str(e)
        low = msg.lower()

        if exc_name in {"InvalidIdError", "InvalidId", "InvalidWorkIdError"}:
            info.error = f"Work {wid} not found"
        elif exc_name in {"AuthError", "AuthenticationError", "LoginError"}:
            info.error = "Authentication required — set credentials in config"
        elif "invalid" in low and "id" in low:
            info.error = f"Work {wid} not found"
        elif "auth" in low or "login" in low or "forbidden" in low:
            info.error = "Authentication required — set credentials in config"
        else:
            info.error = msg
        return info

    info.title = work.title or ""

    # Last updated date — prefer soup's "status" (updated) date, fall back to date_updated
    try:
        soup = work._soup
        updated_dd = soup.select_one("dd.status")   # "Updated:" in multi-chapter works
        published_dd = soup.select_one("dd.published")
        date_str = ""
        if updated_dd:
            date_str = updated_dd.get_text(strip=True)
        elif published_dd:
            date_str = published_dd.get_text(strip=True)
        if date_str and re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            info.date = date_str[:10]
        else:
            date_obj = _safe_work_date(work)
            if date_obj:
                info.date = date_obj.strftime("%Y-%m-%d")
    except Exception:
        date_obj = _safe_work_date(work)
        if date_obj:
            info.date = date_obj.strftime("%Y-%m-%d")

    # Completion — ao3_api v0.2.0 has no .completed attribute.
    # Parse from the soup: chapters dd like "12/20" or "1/1" or "12/?"
    info.status = "🔴"   # default
    chapters_text = ""
    try:
        soup = work._soup
        ch_dd = soup.select_one("dd.chapters")
        if ch_dd:
            chapters_text = ch_dd.get_text(strip=True)   # e.g. "1/1" "12/?" "12/20"
    except Exception:
        pass

    if chapters_text:
        m = re.match(r"(\d+)/(\d+|\?)", chapters_text)
        if m:
            current  = int(m.group(1))
            total_s  = m.group(2)
            if total_s != "?" and int(total_s) > 0 and current >= int(total_s):
                info.status = "🟢"
            info.chapters = chapters_text
    elif work.nchapters:
        expected = work.expected_chapters
        total    = str(expected) if expected else "?"
        info.chapters = f"{work.nchapters}/{total}"

    # Word count — from soup (has commas) or work.words
    try:
        wc_dd = work._soup.select_one("dd.words")
        if wc_dd:
            info.word_count = wc_dd.get_text(strip=True).replace(",", "")
        elif work.words:
            info.word_count = str(work.words)
    except Exception:
        if work.words:
            info.word_count = str(work.words)

    # Rich metadata from soup
    try:
        soup = work._soup
        # Summary
        summary_block = soup.select_one("div.summary blockquote")
        if summary_block:
            info.summary = summary_block.get_text(separator="\n", strip=True)

        # Authors
        info.authors = [a.get_text(strip=True)
                        for a in soup.select("h3.byline a[rel='author']")]

        # Rating
        rating_dd = soup.select_one("dd.rating a.tag")
        if rating_dd:
            info.rating = rating_dd.get_text(strip=True)

        # Categories (F/M, Gen, M/M, etc.)
        info.categories = [a.get_text(strip=True)
                           for a in soup.select("dd.category a.tag")]

        # Fandoms
        info.fandoms_list = [a.get_text(strip=True)
                             for a in soup.select("dd.fandom a.tag")]

        # Archive warnings / CWs
        info.warnings = [a.get_text(strip=True)
                         for a in soup.select("dd.warning a.tag")]

        # Relationships
        info.relationships = [a.get_text(strip=True)
                              for a in soup.select("dd.relationship a.tag")]

        # Characters
        info.characters = [a.get_text(strip=True)
                           for a in soup.select("dd.character a.tag")]

        # Additional / freeform tags
        info.tags = [a.get_text(strip=True)
                     for a in soup.select("dd.freeform a.tag")]
    except Exception:
        pass

    if log_cb:
        log_cb(f"[result] {info.status} | date:{info.date} | "
               f"ch:{info.chapters} | wc:{info.word_count}\n")

    return info


# ── Series ────────────────────────────────────────────────────────────────────

def _scrape_series(url: str, log_cb: Callable | None = None) -> WorkInfo:
    info = WorkInfo(url=url, is_series=True)

    if not _DEPS_OK:
        info.error = f"AO3 library not installed — run: pip install AO3\n({_DEPS_ERR})"
        return info

    sid = _series_id(url)
    if sid is None:
        info.error = f"Cannot extract series ID from: {url}"
        return info

    if log_cb:
        log_cb(f"[fetch] series {sid}\n")

    try:
        series = AO3.Series(sid, session=_session)
    except Exception as e:
        info.error = str(e)
        return info

    # series.name can fail if AO3's HTML structure differs from what ao3_api expects
    try:
        info.title = series.name or ""
    except Exception:
        try:
            soup = series._soup
            h2 = soup.find("h2")
            info.title = h2.get_text(strip=True) if h2 else ""
        except Exception:
            info.title = ""

    try:
        work_list = series.work_list or []
    except Exception as e:
        info.error = f"Could not load series works: {e}"
        return info

    if not work_list:
        info.error = "Series has no works"
        return info

    if log_cb:
        log_cb(f"[series] found {len(work_list)} works — scraping each...\n")

    work_infos: list[WorkInfo] = []
    for work in work_list:
        try:
            wid = work.id
        except Exception:
            continue
        work_url = f"https://archiveofourown.org/works/{wid}"
        if log_cb:
            log_cb(f"[series]   → {work_url}\n")

        cached = _get_cached(work_url)
        if cached:
            if log_cb:
                log_cb(f"[cache] skipping complete work\n")
            work_infos.append(cached)
            if log_cb:
                log_cb(f"[series]     chapters: {cached.chapters or '?'}"
                       f"  |  words: {cached.word_count or '?'}\n")
            continue

        wi = _scrape_work(work_url, log_cb=log_cb)
        _put_cache(wi)
        work_infos.append(wi)
        if log_cb:
            log_cb(f"[series]     chapters: {wi.chapters or '?'}"
                   f"  |  words: {wi.word_count or '?'}\n")

    num_works    = len(work_infos)
    all_complete = all(wi.status == "🟢" for wi in work_infos)
    info.status  = "🟢" if all_complete else "🔴"

    dates = [wi.date for wi in work_infos if wi.date]
    info.date = max(dates) if dates else ""

    # Chapter aggregation
    total_published = 0
    total_expected  = 0
    any_incomplete  = not all_complete
    for wi in work_infos:
        m = re.match(r"(\d+)/(\d+|\?)", wi.chapters or "")
        if m:
            total_published += int(m.group(1))
            if m.group(2) == "?":
                any_incomplete = True
            else:
                total_expected += int(m.group(2))

    total_str = "?" if any_incomplete else str(total_expected)
    info.chapters   = f"{num_works} works ({total_published}/{total_str} ch.)"
    info.word_count = str(sum(int(wi.word_count or 0) for wi in work_infos))

    return info


# ── Public API ────────────────────────────────────────────────────────────────

def scrape_work(url: str, gdl_config: str | None = None,
                log_cb: Callable | None = None,
                force: bool = False) -> WorkInfo:
    if not force:
        cached = _get_cached(url)
        if cached:
            if log_cb:
                log_cb(f"[cache] skipping complete: {url}\n")
            log.debug("scrape_work: cache hit for %s", url)
            return cached

    kind = "series" if "/series/" in url else "work"
    log.debug("scrape_work: scraping %s (%s)", url, kind)
    if "/series/" in url:
        info = _scrape_series(url, log_cb=log_cb)
    else:
        info = _scrape_work(url, log_cb=log_cb)

    _put_cache(info)
    return info


def scrape_works_async(
    fics: list,
    on_result: Callable,
    on_done: Callable,
    gdl_config: str | None = None,
    log_cb: Callable | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    force: bool = False,
) -> threading.Thread:
    log.debug("scrape_works_async: starting for %d fic(s), force=%s", len(fics), force)

    def _worker() -> None:
        for fic in fics:
            if stop_event and stop_event.is_set():
                log.debug("scrape_works_async: stop requested, aborting")
                break
            # Spin-wait while paused
            while pause_event and pause_event.is_set():
                if stop_event and stop_event.is_set():
                    break
                threading.Event().wait(0.2)
            if stop_event and stop_event.is_set():
                break
            info = scrape_work(fic.url, gdl_config=gdl_config,
                               log_cb=log_cb, force=force)
            on_result(fic, info)
        log.debug("scrape_works_async: finished")
        on_done()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


# ── Completion cache ──────────────────────────────────────────────────────────

_cache:       dict[str, dict] = {}
_cache_path:  Path | None     = None
_cache_dirty: bool            = False


def init_cache(path: str | Path) -> None:
    global _cache, _cache_path
    _cache_path = Path(path)
    if _cache_path.exists():
        try:
            _cache = json.loads(_cache_path.read_text(encoding="utf-8"))
            log.debug("init_cache: loaded %d entries from %s", len(_cache), _cache_path)
        except Exception as e:
            log.warning("init_cache: failed to parse %s: %s", _cache_path, e)
            _cache = {}
    else:
        log.debug("init_cache: no existing cache at %s, starting fresh", _cache_path)


def _save_cache() -> None:
    global _cache_dirty
    if _cache_path and _cache_dirty:
        try:
            _cache_path.write_text(
                json.dumps(_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _cache_dirty = False
            log.debug("_save_cache: wrote %d entries to %s", len(_cache), _cache_path)
        except Exception as e:
            log.warning("_save_cache: failed to write %s: %s", _cache_path, e)


def _cache_key(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/")


def _get_cached(url: str) -> WorkInfo | None:
    entry = _cache.get(_cache_key(url))
    if not entry:
        return None
    info             = WorkInfo(url=url)
    info.title       = entry.get("title", "")
    info.date        = entry.get("date", "")
    info.status      = entry.get("status", "")
    info.chapters    = entry.get("chapters", "")
    info.word_count  = entry.get("word_count", "")
    info.is_series   = entry.get("is_series", False)
    return info


def _put_cache(info: WorkInfo) -> None:
    global _cache_dirty
    if info.status != "🟢" or info.error:
        return
    _cache[_cache_key(info.url)] = {
        "title":      info.title,
        "date":       info.date,
        "status":     info.status,
        "chapters":   info.chapters,
        "word_count": info.word_count,
        "is_series":  info.is_series,
    }
    _cache_dirty = True
    _save_cache()