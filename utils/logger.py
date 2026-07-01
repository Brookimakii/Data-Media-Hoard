"""
utils/logger.py
---------------
Log file writing utility.
Handles timestamped session logs written to disk.
Completely decoupled from the UI — callers feed it plain strings.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Module-level logger (for internal errors only) ────────────────────────────
_internal = logging.getLogger(__name__)


class SessionLogger:
    """
    Writes timestamped log lines for one download/upload session to a file.

    Usage
    -----
        log = SessionLogger(log_dir="./logs")
        log.start_session("my_session")
        log.info("Starting download of pixiv/12345")
        log.error("gallery-dl exited with code 1")
        log.end_session()
        path = log.current_path   # Path object to the log file
    """

    def __init__(self, log_dir: str | Path = "./logs"):
        self.log_dir      = Path(log_dir)
        self._file        = None   # open file handle
        self._path: Optional[Path] = None

    # ── Session lifecycle ──────────────────────────────────────────────────────

    def start_session(self, name: str = "session") -> Path:
        """
        Open a new log file named  <name>_YYYY-MM-DD_HH-MM-SS.log
        inside log_dir.  Closes any previously open session first.
        Returns the Path of the new file.
        """
        self.end_session()
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename  = f"{name}_{timestamp}.log"
        self._path = self.log_dir / filename

        try:
            self._file = self._path.open("w", encoding="utf-8")
            self._write_raw(f"=== Session started: {datetime.now().isoformat()} ===\n\n")
        except OSError as e:
            _internal.error("Could not open log file %s: %s", self._path, e)
            self._file = None
            self._path = None

        return self._path

    def end_session(self) -> None:
        """Flush and close the current log file, if any."""
        if self._file:
            try:
                self._write_raw(f"\n=== Session ended: {datetime.now().isoformat()} ===\n")
                self._file.flush()
                self._file.close()
            except OSError:
                pass
            finally:
                self._file = None

    # ── Log level helpers ──────────────────────────────────────────────────────

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERR ", message)

    def raw(self, text: str) -> None:
        """Write raw text (e.g. stdout from a subprocess) without a prefix."""
        self._write_raw(text)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def current_path(self) -> Optional[Path]:
        """Path of the currently open log file, or None if no session is active."""
        return self._path

    @property
    def is_open(self) -> bool:
        return self._file is not None

    # ── Internals ─────────────────────────────────────────────────────────────

    def _write(self, level: str, message: str) -> None:
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {message}\n"
        self._write_raw(line)

    def _write_raw(self, text: str) -> None:
        if not self._file:
            return
        try:
            self._file.write(text)
            self._file.flush()
        except OSError as e:
            _internal.error("Failed to write to log: %s", e)

    def __del__(self):
        self.end_session()


# ── Convenience: configure stdlib logging to also write to a file ─────────────

def setup_file_logging(
    log_dir:  str | Path = "./logs",
    filename: str        = "app.log",
    level:    int        = logging.DEBUG,
) -> Path:
    """
    Attach a rotating file handler to the root stdlib logger.
    Useful for catching internal exceptions independently of session logs.
    Returns the path of the log file.
    """
    from logging.handlers import RotatingFileHandler

    log_dir  = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / filename

    handler = RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
    ))

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)

    # Console output: DEBUG and up, so every debug() call across the app
    # is visible live while the app runs, not just written to the log file.
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
               for h in root_logger.handlers):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.DEBUG)
        stderr_handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root_logger.addHandler(stderr_handler)

    return log_path