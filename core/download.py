"""
core/download.py
----------------
Two responsibilities, deliberately kept in one file because they are
tightly coupled by the shared event queue:

1. run_gallery_dl(job, event_cb, stop_event)
   Pure function. Runs one gallery-dl subprocess and fires a callback
   for every event (log line, start, success/failure).
   Terminates the subprocess cleanly when stop_event is set.
   No threading, no UI imports.

2. DownloadController
   Orchestrates sequential or parallel execution of a job list.
   Communicates back to the UI exclusively through a queue of typed
   event tuples consumed by the UI layer via root.after() polling.
   Zero tkinter imports — the UI wires itself in after construction.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import requests
from bs4 import BeautifulSoup
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from utils.logger import SessionLogger


# ── Event types posted to the queue ──────────────────────────────────────────
#
# ("log",           str)                          raw text line for the log widget
# ("status_site",   (artist, site, url, status))  update a site row in StatusPanel
# ("status_artist", (artist, status))             update an artist row in StatusPanel
# ("progress",      (done: int, total: int))      update the progress bar
# ("file_count",    int)                          total files downloaded so far
# ("done",          None)                         all jobs finished
#
# status values: "pending" | "running" | "ok" | "fail"


# ── Command builder ───────────────────────────────────────────────────────────

def build_command(job: dict[str, str]) -> list[str]:
    """
    Build the gallery-dl command list for a single job.

    Returns a list of string arguments ready to pass to subprocess.
    The command follows the format:
        gallery-dl -c <config> -D <output> --download-archive <archive> <url>
    """
    gdl_config   = job.get("gdl_config", "./config.json")
    archive_path = os.path.join("./archives", f"{job['artist']}.sqlite3")
    return [
        "gallery-dl",
        "--write-metadata",
        "-c", gdl_config,
        "-D", job["output"],
        # "--no-skip", "--no-download",
        "--download-archive", archive_path,
        "-o", "archive-events=file,skip",
        job["url"],
    ]


# ── Empty file cleanup ────────────────────────────────────────────────────────

def delete_empty_files(folder: str) -> int:
    """
    Recursively delete all zero-byte files under *folder*.
    Returns the number of files deleted.
    """
    deleted = 0
    for path in Path(folder).rglob("*"):
        if path.is_file() and path.stat().st_size == 0:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


# ── Low-level runner ──────────────────────────────────────────────────────────

# gallery-dl output lines that indicate a file was downloaded
_DOWNLOAD_PREFIXES = ("[download]", "[gallery-dl]", "# ")

def _is_download_line(line: str) -> bool:
    """
    Return True only if this gallery-dl output line represents a downloaded file.

    gallery-dl prints the destination filename (no prefix) for each file it
    actually downloads. All other output uses bracketed prefixes like:
        [pixiv][info]          ...
        [twitter:user][warning] ...
        [danbooru][error]      ...
        [download]             ...
        [#]                    skipped / already in archive
    """
    stripped = line.strip()
    if not stripped:
        return False
    # Any bracketed prefix → not a file download
    if stripped.startswith("["):
        return False
    # Indented lines, separators, comments
    if stripped.startswith((" ", "\t", "─", "#")):
        return False
    return True


def run_gallery_dl(
    job: dict[str, str],
    event_cb: Callable[[str, object], None],
    logger: SessionLogger | None = None,
    stop_event:  threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> tuple[bool, list[str]]:
    """
    Run gallery-dl for a single job.

    Returns (success, error_lines) where error_lines is a list of
    [extractor][error] and [extractor][warning] lines emitted by gallery-dl.
    """
    os.makedirs(job["output"], exist_ok=True)
    os.makedirs("./archives", exist_ok=True)

    cmd     = build_command(job)
    cmd_str = " ".join(cmd)
    sep     = "─" * 60

    header = (
        f"\n{sep}\n"
        f"  {job['artist']} / {job['site']}\n"
        f"  $ {cmd_str}\n"
        f"{sep}\n"
    )
    event_cb("log", header)
    if logger:
        logger.info(f"START {job['artist']} / {job['site']}")
        logger.info(f"$ {cmd_str}")

    error_lines: list[str] = []

    def _register_file_in_db(raw_path: str, only_if_missing: bool = False) -> None:
        """Register a file path in FileDB (optionally only when missing)."""
        try:
            from core.file_db import get_db
            db = get_db()
            if not db or not raw_path:
                return

            p = Path(raw_path.strip())
            # If gallery-dl emits relative paths for skipped files,
            # try to anchor them to this job's output directory.
            if not p.is_absolute() and not p.exists():
                candidate = Path(job["output"]) / p
                if candidate.exists() or not p.parts:
                    p = candidate

            filepath = str(p)
            if only_if_missing and db.get(filepath):
                return

            db.register(
                filename=p.name,
                filepath=filepath,
                artist=job.get("artist", ""),
                site=job.get("site", ""),
                source_url=job.get("url", ""),
                file_size=p.stat().st_size if p.exists() else 0,
            )
        except Exception:
            pass


    def _extract_title_from_html(text: str) -> str | None:
        """Return the contents of the <title> tag if present, else None."""
        m = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            title = m.group(1).strip()
            # Collapse whitespace
            title = re.sub(r"\s+", " ", title)
            return title
        return None


    def _extract_links(text: str) -> list[str]:
        """Extract URLs from text, handling common cases where a key may sit on the next line.

        Strategy:
        - Keep the original text but allow matching across whitespace for known patterns
        - First find explicit HTTP/HTTPS URLs and try to capture an adjacent key (e.g. mega links)
        - Fall back to a general URL regex on a whitespace-normalised version of the text
        """
        links: list[str] = []

        # Attempt to capture mega-style links where the key may be separated by whitespace/newline
        for m in re.finditer(r"(https?://(?:www\.)?mega\.nz/[^\s#'\">]+)\s*#?\s*([A-Za-z0-9_-]{8,})?", text, flags=re.IGNORECASE | re.DOTALL):
            url = m.group(1)
            key = m.group(2)
            if key:
                full = f"{url}#{key}"
            else:
                full = url
            links.append(full)

        # Normalise whitespace to allow URLs split across lines to be matched by the general regex
        norm = re.sub(r"\s+", " ", text)
        # General URL regex: stops at common delimiters
        for m in re.finditer(r"https?://[^\s'\">]+", norm):
            u = m.group(0).strip()
            links.append(u)

        # Deduplicate preserving order
        seen = set()
        out = []
        for l in links:
            if l not in seen:
                seen.add(l)
                out.append(l)
        return out


    def _postprocess_extract_links(job: dict, event_cb: Callable[[str, object], None]) -> None:
        """Scan text-like files under the job output and write a summary file listing links per post.

        The summary file is written to: <output>/links_extracted.txt
        Each post (file) produces a header:
          ===== NAME/URL =====
        followed by all links found in that file, one per line.
        """
        out_dir = Path(job["output"]).resolve()
        if not out_dir.exists():
            return

        # File extensions to consider text-like
        text_exts = {".html", ".htm", ".txt", ".md", ".json", ".xml"}
        summary_lines: list[str] = []

        # First: attempt to fetch the remote post page and extract its description
        desc_links: list[str] = []
        try:
            resp = requests.get(job.get("url", ""), timeout=10)
            if resp.ok and resp.text:
                soup = BeautifulSoup(resp.text, "html.parser")
                # Prefer OpenGraph description, then meta description
                meta = soup.find("meta", attrs={"property": "og:description"})
                if not meta:
                    meta = soup.find("meta", attrs={"name": "description"})
                desc_text = None
                if meta and meta.get("content"):
                    desc_text = meta.get("content")
                else:
                    # Fallback: common containers
                    article = soup.find("article")
                    if article:
                        desc_text = article.get_text(separator="\n", strip=True)
                    else:
                        # look for elements with class or id containing 'desc' or 'description'
                        candidate = soup.find(attrs={"class": re.compile(r"desc|description", re.I)}) or soup.find(id=re.compile(r"desc|description", re.I))
                        if candidate:
                            desc_text = candidate.get_text(separator="\n", strip=True)

                if desc_text:
                    desc_links = _extract_links(desc_text)
                    if desc_links:
                        header = f"===== DESCRIPTION/{job.get('url','')} ====="
                        summary_lines.append(header)
                        summary_lines.extend(desc_links)
                        summary_lines.append("")
        except Exception:
            # Non-fatal: log and continue to file-based extraction
            event_cb("log", "  [postprocess] failed fetching remote description\n")

        for path in sorted(out_dir.rglob("*")):
            if path.is_dir():
                continue
            if path.suffix.lower() not in text_exts:
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Determine a human-friendly name if possible
            name = None
            if path.suffix.lower() in {".html", ".htm"}:
                name = _extract_title_from_html(raw)
            if not name:
                name = path.stem

            links = _extract_links(raw)
            if not links:
                continue

            header = f"===== {name}/{job.get('url','')} ====="
            summary_lines.append(header)
            summary_lines.extend(links)
            summary_lines.append("")

        if summary_lines:
            target = out_dir / "links_extracted.txt"
            try:
                target.write_text("\n".join(summary_lines), encoding="utf-8")
                event_cb("log", f"  [postprocess] wrote link summary: {str(target)}\n")
            except Exception:
                event_cb("log", "  [postprocess] failed writing link summary\n")

    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        file_count = 0
        skip_count = 0

        for line in proc.stdout:
            # Pause — block here until resumed; check stop immediately after waking
            if pause_event and pause_event.is_set():
                event_cb("log", "\n  [PAUSED]\n")
                while pause_event.is_set():  # spin until resumed
                    pause_event.wait(timeout=0.1)  # wake periodically to check stop
                    if stop_event and stop_event.is_set():
                        break
                if stop_event and stop_event.is_set():
                    proc.terminate()
                    event_cb("log", "\n  [STOPPED]\n")
                    if logger:
                        logger.info(f"STOPPED {job['artist']} / {job['site']}")
                    proc.wait()
                    return False, error_lines
                event_cb("log", "  [RESUMED]\n")

            if stop_event and stop_event.is_set():
                proc.terminate()
                event_cb("log", "\n  [STOPPED]\n")
                if logger:
                    logger.info(f"STOPPED {job['artist']} / {job['site']}")
                proc.wait()
                return False, error_lines

            event_cb("log", line)
            if logger:
                logger.raw(line)

            # Collect error/warning lines for the recap.
            # gallery-dl format: [ExtractorName][error] or [Site:User][warning]
            # The second bracketed token is the severity — match that instead
            # of the literal word "extractor" which is never actually used.
            stripped = line.strip()
            if re.search(r'^\[[^\]]+\]\[(?:error|warning)\]', stripped):
                error_lines.append(stripped)
                event_cb("error_count", (job["artist"], job["site"], job["url"], len(error_lines)))

            if _is_download_line(line):
                file_count += 1
                filepath = line.strip()
                event_cb("file_count", (job["artist"], job["site"], job["url"], file_count, skip_count))
                _register_file_in_db(filepath)
            elif stripped.startswith("# "):
                skip_count += 1
                event_cb("file_count", (job["artist"], job["site"], job["url"], file_count, skip_count))
                skipped_path = stripped[2:].strip()
                if skipped_path and not skipped_path.startswith("["):
                    _register_file_in_db(skipped_path, only_if_missing=True)

        proc.wait()

        deleted = delete_empty_files(job["output"])
        if deleted:
            event_cb("log", f"  [cleanup] removed {deleted} empty file(s)\n")

        # Post-process: extract links using the generic extractor
        try:
            from core.postprocess import extract_links_for_job

            extract_links_for_job(job, event_cb)
        except Exception:
            # Never let the post-process crash the download; log and continue
            event_cb("log", "  [postprocess] link extraction failed (see logs)\n")

        success    = proc.returncode == 0
        status_str = "OK" if success else f"EXIT {proc.returncode}"
        footer     = f"  [{status_str}] {job['artist']} / {job['site']}\n"
        event_cb("log", footer)
        if logger:
            log_fn = logger.info if success else logger.error
            log_fn(f"{status_str} {job['artist']} / {job['site']}")

        return success, error_lines

    except FileNotFoundError:
        msg = "[ERROR] gallery-dl not found — is it installed and on PATH?\n"
        event_cb("log", msg)
        if logger:
            logger.error(msg.strip())
        return False, error_lines

    except Exception as exc:
        msg = f"[ERROR] {exc}\n"
        event_cb("log", msg)
        if logger:
            logger.error(msg.strip())
        return False, error_lines


# ── Download controller ───────────────────────────────────────────────────────

class DownloadController:
    """
    Orchestrates sequential or parallel gallery-dl downloads.

    The controller owns a queue.Queue of event tuples.
    The UI layer polls this queue via root.after() and updates itself.
    The controller never imports tkinter.

    Lifecycle
    ---------
    ctrl = DownloadController(on_event=my_queue.put)
    ctrl.start(jobs, parallel=False)
    # … UI polls my_queue …
    ctrl.stop()   # terminates the current subprocess and cancels remaining jobs
    """

    def __init__(
        self,
        on_event: Callable[[tuple], None],
        logger: SessionLogger | None = None,
    ):
        self._on_event  = on_event
        self._logger    = logger
        self._stop_evt  = threading.Event()
        self._pause_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ── public API ─────────────────────────────────────────────────────────────

    def start(
        self,
        jobs: list[dict],
        parallel: bool = False,
        max_parallel: int = 4,
    ) -> None:
        """Start downloading. No-op if already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._pause_evt.clear()

        # Guard against invalid values from UI/state.
        max_parallel = max(1, int(max_parallel))

        if self._logger:
            self._logger.start_session("download")
            from core.jobs import summarise_jobs
            self._logger.info(summarise_jobs(jobs))

        self._thread = threading.Thread(
            target=self._run,
            args=(jobs, parallel, max_parallel),
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Request cancellation.
        Sets the stop event — the current subprocess is terminated at the
        next line of output, and no further jobs are started.
        Also clears the pause event so the thread can wake up and see the stop.
        """
        self._pause_evt.clear()   # wake thread if paused so it can see stop
        self._stop_evt.set()

    def pause(self) -> None:
        """Pause after the current output line is processed."""
        self._pause_evt.set()

    def resume(self) -> None:
        """Resume a paused download."""
        self._pause_evt.clear()

    @property
    def is_paused(self) -> bool:
        return self._pause_evt.is_set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internals ──────────────────────────────────────────────────────────────

    def _emit(self, event_type: str, payload: object = None) -> None:
        self._on_event((event_type, payload))

    def _run(self, jobs: list[dict], parallel: bool, max_parallel: int) -> None:
        total          = len(jobs)
        done           = [0]
        total_files    = [0]
        artist_results: dict[str, dict[str, int]] = {}
        # errors_recap: list of (label, cmd_str, [error_lines])
        errors_recap: list[tuple[str, str, list[str]]] = []

        for job in jobs:
            artist_results.setdefault(job["artist"], {"ok": 0, "fail": 0})

        def run_one(job: dict) -> tuple[dict, bool | None, list[str]]:
            if self._stop_evt.is_set():
                return job, None, []

            artist = job["artist"]
            site   = job["site"]
            url    = job["url"]

            self._emit("status_site",   (artist, site, url, "running"))
            self._emit("status_artist", (artist, "running"))

            success, error_lines = run_gallery_dl(
                job,
                event_cb=self._emit,
                logger=self._logger,
                stop_event=self._stop_evt,
                pause_event=self._pause_evt,
            )

            self._emit("status_site", (artist, site, url, "ok" if success else "fail"))
            done[0] += 1
            self._emit("progress", (done[0], total))

            return job, success, error_lines

        if parallel:
            # Run at most max_parallel jobs concurrently using a worker pool so
            # we can track per-worker progress and emit worker-scoped events.
            worker_count = max(1, min(max_parallel, total or 1))
            job_q: queue.Queue = queue.Queue()
            for job in jobs:
                job_q.put(job)

            lock = threading.Lock()

            def run_one_worker(worker_idx: int) -> None:
                while not self._stop_evt.is_set():
                    try:
                        job = job_q.get_nowait()
                    except queue.Empty:
                        break

                    # Wrapper to duplicate certain events with worker id
                    def _worker_cb(event_type: str, payload: object = None) -> None:
                        # Forward original event
                        self._emit(event_type, payload)
                        # Additionally emit worker-scoped file_count events
                        if event_type == "file_count":
                            self._emit("worker_file_count", (worker_idx, payload))

                    # Emit worker_start so UI can create/label the worker row
                    self._emit("worker_start", (worker_idx, (job["artist"], job["site"], job["url"])))

                    # Run the job using the worker-scoped callback
                    success, error_lines = run_gallery_dl(
                        job,
                        event_cb=_worker_cb,
                        logger=self._logger,
                        stop_event=self._stop_evt,
                        pause_event=self._pause_evt,
                    )

                    # Update shared counters under lock and emit progress
                    with lock:
                        if success is not None:
                            artist_results[job["artist"]]["ok" if success else "fail"] += 1
                            if error_lines:
                                cmd_str = " ".join(build_command(job))
                                errors_recap.append((f"{job['artist']} / {job['site']}", cmd_str, error_lines))
                        done[0] += 1
                        self._emit("progress", (done[0], total))

                    # Tell UI the worker finished its assignment
                    self._emit("worker_done", (worker_idx, (job["artist"], job["site"], job["url"]), success))

                    job_q.task_done()

            threads: list[threading.Thread] = []
            for i in range(worker_count):
                t = threading.Thread(target=run_one_worker, args=(i,), daemon=True)
                threads.append(t)
                t.start()

            # Wait for workers to finish or for a stop request
            try:
                for t in threads:
                    t.join()
            except KeyboardInterrupt:
                self._stop_evt.set()
        else:
            for job in jobs:
                if self._stop_evt.is_set():
                    break
                job, success, error_lines = run_one(job)
                if success is not None:
                    artist_results[job["artist"]]["ok" if success else "fail"] += 1
                    if error_lines:
                        cmd_str = " ".join(build_command(job))
                        errors_recap.append((f"{job['artist']} / {job['site']}", cmd_str, error_lines))

        # Final per-artist status
        for artist, counts in artist_results.items():
            if counts["fail"] > 0:
                final = "fail"
            elif counts["ok"] > 0:
                final = "ok"
            else:
                final = "pending"
            self._emit("status_artist", (artist, final))

        if self._logger:
            self._logger.end_session()

        self._emit("done", errors_recap)