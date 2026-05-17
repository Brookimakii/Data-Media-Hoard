"""
main.py
-------
Entry point for Booru Manager.
Run with:  python main.py
"""

import os
import signal
import shutil
import tkinter as tk
from pathlib import Path

from utils.logger import setup_file_logging
from ui.app import create_app


def _setup_sigint(root: tk.Tk) -> None:
    """
    Make Ctrl+C work even when the window is minimized.

    tkinter's mainloop() blocks Python's signal handler from running
    until an event is processed. The fix is to schedule a no-op every
    200 ms so the interpreter gets a chance to check for signals.
    """
    def _check() -> None:
        root.after(200, _check)

    signal.signal(signal.SIGINT, lambda *_: root.destroy())
    root.after(200, _check)


def main() -> None:
    setup_file_logging(log_dir="./logs", filename="app.log")

    # Load .env file if present (fast — just reads a file)
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    # Load completion cache (fast — just reads a JSON file)
    from core.ao3_scraper import init_cache, load_session_from_env
    init_cache(Path(__file__).parent / "ao3_cache.json")

    # Initialise file download database (path from config.yaml)
    from core.file_db import init_db
    from core.catalogue import load_config as _load_cfg
    _main_dir = Path(__file__).parent
    try:
        _cfg = _load_cfg(str(_main_dir / "config.yaml"))
        _db_path = _main_dir / _cfg.get("database", "hoard.db")
    except Exception:
        _db_path = _main_dir / "hoard.db"

    # One-time file-level migration for users moving downloads.db -> hoard.db
    try:
        _legacy_db = _main_dir / "downloads.db"
        if (not _db_path.exists()
                and _db_path.name == "hoard.db"
                and _legacy_db.exists()):
            shutil.copy2(_legacy_db, _db_path)
    except Exception:
        pass

    init_db(_db_path)

    root = create_app()
    _setup_sigint(root)

    # Log in to AO3 in the background — network call, don't block startup
    import threading
    threading.Thread(target=load_session_from_env, daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()