"""
ui/fic_tab.py
-------------
AO3 fic tracker tab.

Reads a plain-text fic list file and displays all fics in a scrollable
table with filters: status, fandom, title search, and date range.
Clicking a fic row opens the AO3 link in the browser.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.ao3_scraper import WorkInfo, scrape_works_async
from core.fic_tracker import (
    Fic, FicFile, STATUS_LABEL,
    parse_fic_file, toggle_fic_enabled, update_fic, add_fic,
    update_fic_meta, save_yaml_meta,
)
from core.catalogue import load_config
from ui.scroll import register_scroll_canvas, set_scroll_enabled
from ui.theme import (
    ACCENT, ACCENT2, BG, BORDER, ENTRY_BG, FG, FG_DIM,
    FONT_BOLD, FONT_EMOJI, FONT_HEAD, FONT_MONO, FONT_SUB, FONT_TAGS, PANEL, PAD_OUTER,
    COLOR_OK, COLOR_FAIL, COLOR_RUNNING,
    BTN_BG, BTN_FG, BTN_HOV, ROW_ALT, SEL_BG,
)
from ui.widgets import divider, section_label, styled_button, styled_entry

SCRIPT_DIR     = Path(__file__).parent.parent
DEFAULT_FIC_FILE = str(SCRIPT_DIR / "ao3.txt")

# Status colours matching theme conventions
STATUS_COLOR = {
    "🟢": COLOR_OK,
    "🟡": COLOR_RUNNING,
    "🔴": COLOR_FAIL,
    "🟠": "#e09050",   # stale — orange, distinct from running yellow
}

COL_WIDTHS = {
    "dl":      3,    # download toggle
    "type":    3,    # work 📖 or series 📚
    "status":  6,
    "date":    11,
    "fandom":  20,
    "title":   0,    # expands
    "wc":      10,
    "ch":      16,
    "upd":     3,    # per-row update button
}

HEADER_LABELS = {
    "dl":     "DL",
    "type":   "T",
    "status": "St.",
    "date":   "Updated",
    "fandom": "Fandom",
    "title":  "Title",
    "wc":     "Words",
    "ch":     "Ch.",
    "upd":    "↻",
}


def _fmt_wc(wc: str) -> str:
    """Format a word count string with narrow no-break space as thousands separator."""
    try:
        return f"{int(wc):,}".replace(",", "\u202f")
    except (ValueError, TypeError):
        return wc or ""


def build_fic_tracker(parent: tk.Frame) -> None:
    """Mount the fic tracker tab into *parent*."""

    # ── Outer scrollable canvas ───────────────────────────────────────────────
    canvas    = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    scroll_frame = tk.Frame(canvas, bg=PANEL)

    win_id = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    scroll_frame.bind(
        "<Configure>",
        lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
    )
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    register_scroll_canvas(canvas)

    # ── Header ────────────────────────────────────────────────────────────────
    tk.Frame(scroll_frame, bg=PANEL, height=30).pack()
    tk.Label(scroll_frame, text="AO3 FIC TRACKER", bg=PANEL, fg=ACCENT,
             font=FONT_HEAD, anchor="w",
             ).pack(fill="x", padx=PAD_OUTER)
    tk.Label(scroll_frame, text="fanfiction reading list",
             bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w",
             ).pack(fill="x", padx=PAD_OUTER + 2, pady=(0, 10))
    divider(scroll_frame)

    # ── File picker ───────────────────────────────────────────────────────────
    section_label(scroll_frame, "FIC LIST FILE")
    file_row = tk.Frame(scroll_frame, bg=PANEL)
    file_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 8))
    file_frame, file_entry = styled_entry(file_row, DEFAULT_FIC_FILE, width=60)
    file_frame.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def _browse_file() -> None:
        path = filedialog.askopenfilename(
            title="Select fic list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            file_entry.config(fg=FG)
            file_entry.delete(0, "end")
            file_entry.insert(0, path)
            _load()

    styled_button(file_row, "Browse…", command=_browse_file).pack(side="left", padx=(0, 6))
    styled_button(file_row, "↺ Reload", command=lambda: _load(),
                  bg="#2a2a38", hov="#3a3a50").pack(side="left")

    divider(scroll_frame)

    # ── Add work ──────────────────────────────────────────────────────────────
    section_label(scroll_frame, "ADD WORK")

    add_row = tk.Frame(scroll_frame, bg=PANEL)
    add_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 4))

    tk.Label(add_row, text="URL:", bg=PANEL, fg=FG_DIM,
             font=FONT_MONO, anchor="w").pack(side="left", padx=(0, 6))
    add_url_border = tk.Frame(add_row, bg=BORDER, padx=1, pady=1)
    add_url_border.pack(side="left", fill="x", expand=True, padx=(0, 8))
    add_url_entry = tk.Entry(add_url_border, bg=ENTRY_BG, fg=FG,
                             insertbackground=ACCENT, relief="flat",
                             font=FONT_MONO)
    add_url_entry.pack(fill="x", padx=4, pady=3)

    add_fetch_btn = styled_button(add_row, "＋ Add")
    add_fetch_btn.pack(side="left")

    add_status_lbl = tk.Label(scroll_frame, text="", bg=PANEL, fg=FG_DIM,
                              font=FONT_MONO, anchor="w")
    add_status_lbl.pack(fill="x", padx=PAD_OUTER, pady=(0, 4))

    def _do_add() -> None:
        url = add_url_entry.get().strip()
        if not url:
            return

        if not url.startswith("http"):
            add_status_lbl.config(text="✗  URL must start with http", fg=COLOR_FAIL)
            _append_log("✗  Add failed — URL must start with http\n")
            return

        # Duplicate check: prevent adding fic with same URL
        if any(f.url == url for f in _fic_file[0].all_fics):
            add_status_lbl.config(text="✗  Fic with this URL already exists", fg=COLOR_FAIL)
            _append_log(f"✗  Add failed — duplicate fic URL: {url}\n")
            add_url_entry.delete(0, "end")
            return

        add_fetch_btn.config(state="disabled")
        add_status_lbl.config(text="Fetching metadata…", fg=FG_DIM)
        _append_log(f"[add] fetching metadata for {url}\n")
        scroll_frame.update_idletasks()

        def _fetch() -> None:
            from core.ao3_scraper import scrape_work
            info = scrape_work(url)
            scroll_frame.after(0, lambda: _on_fetched(info))

        def _on_fetched(info) -> None:
            add_fetch_btn.config(state="normal")
            if info.error:
                add_status_lbl.config(
                    text=f"✗  {info.error}", fg=COLOR_FAIL)
                _append_log(f"✗  Add failed — {info.error}\n")
                return

            if not _fic_file[0].path:
                add_status_lbl.config(
                    text="✗  Load a fic file first", fg=COLOR_FAIL)
                _append_log("✗  Add failed — load a fic file first\n")
                return

            # Determine fandom — use existing fandoms or "Unknown"
            add_status_lbl.config(text="Please Select a Fandom.", fg=FG_DIM)
            fandom = _pick_fandom_for_add(info)

            # Determine status
            if is_stale_check_plain(info):
                status = "🟠"
            else:
                status = info.status or "🔴"

            try:
                fic = add_fic(
                    _fic_file[0],
                    fandom=fandom,
                    url=url,
                    status=status,
                    date=info.date or "",
                    title=info.title or url,
                    word_count=info.word_count or "",
                    chapters=info.chapters or "",
                    enabled=True,
                )
                if info.summary or info.tags or info.categories or info.authors:
                    update_fic_meta(
                        _fic_file[0], fic,
                        summary=info.summary,
                        categories=info.categories,
                        tags=info.tags,
                        authors=info.authors,
                        rating=info.rating,
                        fandoms_list=info.fandoms_list,
                        relationships=info.relationships,
                        characters=info.characters,
                        warnings=info.warnings,
                    )
                _all_fics.append(fic)
                add_url_entry.delete(0, "end")
                add_status_lbl.config(
                    text=f"✔  Added: {fic.title[:50]}", fg=COLOR_OK)
                _append_log(
                    f"✔  Added — {fic.title}  |  {fandom}  |  {status}\n")
                _apply_filter()
            except Exception as e:
                add_status_lbl.config(text=f"✗  {e}", fg=COLOR_FAIL)
                _append_log(f"✗  Add failed — {e}\n")

        threading.Thread(target=_fetch, daemon=True).start()

    def _pick_fandom_for_add(info) -> str:
        """Show a small dialog to pick or type a fandom name."""
        existing = sorted(_fic_file[0].fandoms.keys())
        dialog = tk.Toplevel(scroll_frame.winfo_toplevel())
        dialog.title("Choose Fandom")
        dialog.configure(bg=PANEL)
        dialog.resizable(False, False)
        result: list[str] = ["Unknown"]

        tk.Label(dialog, text="Fandom for this work:",
                 bg=PANEL, fg=FG, font=FONT_MONO).pack(padx=20, pady=(16, 4))

        b = tk.Frame(dialog, bg=BORDER, padx=1, pady=1)
        b.pack(padx=20, fill="x")
        fandom_var = tk.StringVar()
        entry = tk.Entry(b, textvariable=fandom_var, bg=ENTRY_BG, fg=FG,
                         insertbackground=ACCENT, relief="flat", font=FONT_MONO)
        entry.pack(fill="x", padx=4, pady=3)

        if existing:
            tk.Label(dialog, text="Or pick existing:",
                     bg=PANEL, fg=FG_DIM, font=FONT_MONO).pack(padx=20, pady=(8, 2))
            lb_frame = tk.Frame(dialog, bg=BORDER, padx=1, pady=1)
            lb_frame.pack(padx=20, fill="x")
            lb = tk.Listbox(lb_frame, bg=ENTRY_BG, fg=FG, font=FONT_MONO,
                            selectbackground=SEL_BG, relief="flat",
                            height=min(6, len(existing)))
            lb.pack(fill="x")
            for f in existing:
                lb.insert("end", f)
            lb.bind("<<ListboxSelect>>",
                    lambda _e: fandom_var.set(lb.get(lb.curselection()[0]))
                    if lb.curselection() else None)

        def _ok() -> None:
            result[0] = fandom_var.get().strip() or "Unknown"
            dialog.destroy()

        styled_button(dialog, "OK", command=_ok).pack(pady=12)
        entry.focus_set()
        dialog.bind("<Return>", lambda _e: _ok())
        dialog.grab_set()
        dialog.wait_window()
        return result[0]

    def is_stale_check_plain(info) -> bool:
        if not info.date or info.status == "🟢":
            return False
        from datetime import date as _date
        try:
            updated = _date.fromisoformat(info.date)
        except ValueError:
            return False
        today = _date.today()
        months = (today.year - updated.year) * 12 + (today.month - updated.month)
        return months >= _stale_months[0]

    add_fetch_btn.config(command=_do_add)
    add_url_entry.bind("<Return>", lambda _e: _do_add())

    divider(scroll_frame)

    # ── Update from AO3 ──────────────────────────────────────────────────────
    section_label(scroll_frame, "UPDATE FROM AO3")

    update_row = tk.Frame(scroll_frame, bg=PANEL)
    update_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))

    update_all_btn = styled_button(update_row, "↻  Update All")
    update_all_btn.pack(side="left", padx=(0, 8))

    update_sel_btn = styled_button(update_row, "↻  Update Selected",
                                   bg="#2a2a38", hov="#3a3a50")
    update_sel_btn.pack(side="left", padx=(0, 16))

    stop_update_btn = styled_button(update_row, "■  Stop",
                                    bg="#3a2a2a", hov="#5a3a3a")
    stop_update_btn.config(state="disabled")
    stop_update_btn.pack(side="left", padx=(0, 6))

    pause_update_btn = styled_button(update_row, "⏸  Pause",
                                     bg="#2a2a38", hov="#3a3a50")
    pause_update_btn.config(state="disabled")
    pause_update_btn.pack(side="left", padx=(0, 16))

    divider(scroll_frame)

    # ── Download section ──────────────────────────────────────────────────────
    section_label(scroll_frame, "DOWNLOAD")

    dl_row = tk.Frame(scroll_frame, bg=PANEL)
    dl_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))

    dl_btn = styled_button(dl_row, "⬇  Download All Enabled")
    dl_btn.pack(side="left", padx=(0, 8))

    dl_sel_btn = styled_button(dl_row, "⬇  Download Selected",
                               bg="#2a2a38", hov="#3a3a50")
    dl_sel_btn.pack(side="left", padx=(0, 8))

    dl_stop_btn = styled_button(dl_row, "■  Stop",
                                bg="#3a2a2a", hov="#5a3a3a")
    dl_stop_btn.config(state="disabled")
    dl_stop_btn.pack(side="left")

    dl_prog_row = tk.Frame(scroll_frame, bg=PANEL)
    dl_prog_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 4))
    dl_status_lbl = tk.Label(dl_prog_row, text="", bg=PANEL, fg=FG_DIM,
                             font=FONT_MONO, anchor="w")
    dl_status_lbl.pack(fill="x")

    _dl_stop  = threading.Event()
    _dl_proc: list = [None]   # holds the running subprocess
    _dl_mode: list[str] = ["all"]
    _dl_selected_urls: list[set[str]] = [set()]

    def _set_downloading(active: bool) -> None:
        dl_btn.config(state="disabled" if active else "normal")
        dl_sel_btn.config(state="disabled" if active else "normal")
        dl_stop_btn.config(state="normal" if active else "disabled")
        if not active:
            dl_status_lbl.config(text="")

    def _run_download_batch(download_fics: list[Fic], mode: str) -> None:
        if not _fic_file[0].path:
            dl_status_lbl.config(text="✗  Load a fic file first", fg=COLOR_FAIL)
            return

        if not download_fics:
            dl_status_lbl.config(text="Nothing to download", fg=FG_DIM)
            return

        _dl_stop.clear()
        _set_downloading(True)
        _dl_mode[0] = mode
        _dl_selected_urls[0] = {f.url for f in download_fics}
        dl_status_lbl.config(text=f"Preparing {len(download_fics)} URLs…", fg=FG_DIM)

        import os as _os
        import subprocess as _sp
        import tempfile as _tempfile

        use_original_file = (mode == "all")
        fic_file_path = str(_fic_file[0].path)
        tmp_path = ""
        if not use_original_file:
            tmp = _tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                               delete=False, encoding="utf-8")
            for fic in download_fics:
                tmp.write(fic.url + "\n")
            tmp.close()
            tmp_path = tmp.name
            fic_file_path = tmp_path

        # Build command: gallery-dl -I <fic_file> [-c config]
        cmd = ["gallery-dl", "-I", fic_file_path, "-d", "./AO3", "-c", "config.json", "--write-metadata", "--download-archive", "./AO3/Archive_Of_Our_Own.sqlite3", "-o", "archive-events=file,skip"]
        # if _gdl_config[0] and Path(_gdl_config[0]).exists():
        #     cmd += ["-c", _gdl_config[0]]

        # print(cmd)
        # print(fic_file_path)
        dl_status_lbl.config(text=f"Running: {' '.join(cmd[:3])}…", fg=FG_DIM)
        _append_log(f"$ {' '.join(cmd)}\n")

        def _worker() -> None:
            try:
                env = _os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                                 text=True, bufsize=1, env=env)
                _dl_proc[0] = proc

                for line in proc.stdout:
                    if _dl_stop.is_set():
                        proc.terminate()
                        break
                    _update_queue.put(("dl_log", line))

                proc.wait()
                _dl_proc[0] = None

                _update_queue.put(("dl_done", (proc.returncode, len(download_fics), mode)))

            except Exception as e:
                _update_queue.put(("dl_log", f"[error] {e}\n"))
                _update_queue.put(("dl_done", (1, 0, mode)))
            finally:
                if tmp_path:
                    try:
                        _os.unlink(tmp_path)
                    except Exception:
                        pass

        threading.Thread(target=_worker, daemon=True).start()
        scroll_frame.after(100, _poll_dl)

    def _do_download_all_enabled() -> None:
        enabled_fics = [f for f in _fic_file[0].all_fics if f.enabled]
        if not enabled_fics:
            dl_status_lbl.config(text="No enabled works to download", fg=FG_DIM)
            return
        _run_download_batch(enabled_fics, mode="all")

    def _do_download_selected() -> None:
        selected_fics = [
            _visible_fics[i] for i in sorted(_selected_rows)
            if i < len(_visible_fics)
        ]
        if not selected_fics:
            dl_status_lbl.config(text="Select one or more rows to download", fg=FG_DIM)
            return
        _run_download_batch(selected_fics, mode="selected")

    def _poll_dl() -> None:
        try:
            while True:
                msg_type, payload = _update_queue.get_nowait()
                if msg_type == "dl_log":
                    _append_log(payload)
                    dl_status_lbl.config(text=payload.strip()[:60], fg=FG_DIM)
                elif msg_type == "dl_done":
                    return_code, requested_n, mode = payload

                    disabled_count = 0
                    if mode == "all":
                        # gallery-dl edits the input file in-place (-I + archive-events=file),
                        # so reload from disk instead of inferring success from console output.
                        try:
                            before_enabled = sum(1 for f in _fic_file[0].all_fics if f.enabled)
                            refreshed = parse_fic_file(_fic_file[0].path)
                            _fic_file[0] = refreshed
                            _all_fics[:] = list(refreshed.all_fics)
                            after_enabled = sum(1 for f in refreshed.all_fics if f.enabled)
                            disabled_count = max(0, before_enabled - after_enabled)
                        except Exception as e:
                            _append_log(f"[warn] reload after download failed: {e}\n")
                    elif return_code == 0 and not _dl_stop.is_set():
                        selected_urls = _dl_selected_urls[0]
                        to_disable = [f for f in _fic_file[0].all_fics
                                      if f.enabled and f.url in selected_urls]
                        for fic in to_disable:
                            toggle_fic_enabled(_fic_file[0], fic)
                        disabled_count = len(to_disable)

                    _set_downloading(False)
                    msg = (
                        f"✔  Done — {disabled_count} links commented"
                        if (not _dl_stop.is_set() and return_code == 0)
                        else ("Stopped" if _dl_stop.is_set()
                              else f"✗  Download finished with errors (code {return_code})")
                    )
                    dl_status_lbl.config(
                        text=msg,
                        fg=(COLOR_OK if return_code == 0 and not _dl_stop.is_set() else FG_DIM))
                    _append_log(msg + "\n")
                    _apply_filter()
                    _draw()
                    return
                else:
                    # Put back non-dl messages for the update poller
                    _update_queue.put((msg_type, payload))
                    break
        except queue.Empty:
            pass
        scroll_frame.after(100, _poll_dl)

    dl_btn.config(command=_do_download_all_enabled)
    dl_sel_btn.config(command=_do_download_selected)
    dl_stop_btn.config(command=lambda: (_dl_stop.set(),
                                         _dl_proc[0] and _dl_proc[0].terminate()))
    prog_row = tk.Frame(scroll_frame, bg=PANEL)
    prog_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 4))

    update_prog_var = tk.IntVar(value=0)
    update_prog = ttk.Progressbar(prog_row, variable=update_prog_var,
                                  maximum=100,
                                  style="Download.Horizontal.TProgressbar")
    update_prog.pack(side="left", fill="x", expand=True, padx=(0, 12))

    update_status_lbl = tk.Label(prog_row, text="", bg=PANEL, fg=FG_DIM,
                                 font=FONT_MONO, anchor="w", width=30)
    update_status_lbl.pack(side="left")

    # ── Collapsible log ───────────────────────────────────────────────────────
    log_section   = tk.Frame(scroll_frame, bg=PANEL)
    log_section.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))

    log_collapsed = [True]
    log_toggle_btn = styled_button(log_section, "▶  Log",
                                   bg="#2a2a38", hov="#3a3a50", padx=8, pady=3)
    log_toggle_btn.pack(anchor="w", pady=(0, 2))

    log_outer = tk.Frame(log_section, bg=BORDER, padx=1, pady=1)
    log_text   = tk.Text(log_outer, bg="#0a0a10", fg=FG_DIM,
                         insertbackground=ACCENT, relief="flat",
                         font=FONT_MONO, height=8, wrap="word", state="disabled")
    log_sb_w   = ttk.Scrollbar(log_outer, command=log_text.yview)
    log_text.configure(yscrollcommand=log_sb_w.set)
    log_sb_w.pack(side="right", fill="y")
    log_text.pack(fill="both", expand=True, padx=4, pady=4)

    def _log_scroll_ev(event: tk.Event) -> str:
        if event.num == 4:   log_text.yview_scroll(-1, "units")
        elif event.num == 5: log_text.yview_scroll(1, "units")
        else:                log_text.yview_scroll(int(-1*(event.delta/120)), "units")
        return "break"
    log_text.bind("<MouseWheel>", _log_scroll_ev)
    log_text.bind("<Button-4>",   _log_scroll_ev)
    log_text.bind("<Button-5>",   _log_scroll_ev)

    def _toggle_log() -> None:
        if log_collapsed[0]:
            log_outer.pack(fill="x", pady=(0, 2))
            log_toggle_btn.config(text="▼  Log")
            log_collapsed[0] = False
        else:
            log_outer.pack_forget()
            log_toggle_btn.config(text="▶  Log")
            log_collapsed[0] = True

    log_toggle_btn.config(command=_toggle_log)

    def _append_log(text: str) -> None:
        log_text.config(state="normal")
        log_text.insert("end", text)
        log_text.see("end")
        log_text.config(state="disabled")

    # Stale threshold — read from config.yaml
    _stale_months: list[int] = [6]
    try:
        cfg = load_config(str(SCRIPT_DIR / "config.yaml"))
        ft  = cfg.get("fic_tracker", {}) or {}
        _stale_months[0] = int(ft.get("stale_months", 6))
    except Exception:
        pass

    _gdl_config: list[str] = [str(SCRIPT_DIR / "config.json")]
    try:
        _gdl_config[0] = str(SCRIPT_DIR / cfg.get("gdl_config", "config.json"))
    except Exception:
        pass

    tk.Label(update_row, text=f"stale threshold: {_stale_months[0]} months",
             bg=PANEL, fg=FG_DIM, font=FONT_MONO).pack(side="right")

    # Update state
    _update_stop  = threading.Event()
    _update_pause = threading.Event()   # set = paused

    def _set_updating(active: bool) -> None:
        update_all_btn.config(state="disabled" if active else "normal")
        update_sel_btn.config(state="disabled" if active else "normal")
        stop_update_btn.config(state="normal" if active else "disabled")
        pause_update_btn.config(state="normal" if active else "disabled")
        if not active:
            _update_pause.clear()
            pause_update_btn.config(text="⏸  Pause", bg="#2a2a38")
            update_status_lbl.config(text="")
            update_prog_var.set(0)

    def _toggle_pause() -> None:
        if _update_pause.is_set():
            _update_pause.clear()
            pause_update_btn.config(text="⏸  Pause", bg="#2a2a38")
            update_status_lbl.config(text="Resumed…")
        else:
            _update_pause.set()
            pause_update_btn.config(text="▶  Resume", bg="#2a3a2a")
            update_status_lbl.config(text="Paused")

    _update_queue: queue.Queue = queue.Queue()

    def _poll_update() -> None:
        try:
            while True:
                msg_type, payload = _update_queue.get_nowait()
                if msg_type == "progress":
                    done, total, label = payload
                    pct = int(done / total * 100) if total else 0
                    update_prog_var.set(pct)
                    update_status_lbl.config(text=label)
                elif msg_type == "log":
                    _append_log(payload)
                elif msg_type == "result":
                    _apply_filter()
                    _draw()
                elif msg_type == "done":
                    _set_updating(False)
                    update_status_lbl.config(text=payload)
                    _append_log(f"\n{payload}\n")
                    _apply_filter()
                    return
        except queue.Empty:
            pass
        scroll_frame.after(100, _poll_update)

    # ── Raw output window (created lazily on button click) ────────────────────
    _raw_win:    list[tk.Toplevel | None] = [None]
    _raw_text:   list[tk.Text | None]     = [None]
    _raw_buffer: list[str]                = []   # holds output while window is closed

    def _open_log_window() -> None:
        """Open (or bring to front) the gallery-dl raw output window."""
        root_w = scroll_frame.winfo_toplevel()
        if _raw_win[0] and _raw_win[0].winfo_exists():
            _raw_win[0].lift()
            _raw_win[0].focus_set()
            return

        win = tk.Toplevel(root_w)
        win.title("gallery-dl output")
        win.geometry("800x500")
        win.configure(bg=ENTRY_BG)
        _raw_win[0] = win

        txt = tk.Text(win, bg="#0a0a10", fg=FG_DIM,
                      insertbackground=ACCENT, relief="flat",
                      font=FONT_MONO, wrap="none", state="disabled")
        _raw_text[0] = txt
        sb_v = ttk.Scrollbar(win, orient="vertical",   command=txt.yview)
        sb_h = ttk.Scrollbar(win, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=sb_v.set, xscrollcommand=sb_h.set)
        sb_v.pack(side="right",  fill="y")
        sb_h.pack(side="bottom", fill="x")
        txt.pack(fill="both", expand=True)

        # Flush any buffered output
        if _raw_buffer:
            txt.config(state="normal")
            txt.insert("end", "".join(_raw_buffer))
            txt.see("end")
            txt.config(state="disabled")

        def _raw_scroll(event: tk.Event) -> str:
            if event.num == 4:   txt.yview_scroll(-1, "units")
            elif event.num == 5: txt.yview_scroll(1,  "units")
            else:                txt.yview_scroll(int(-1*(event.delta/120)), "units")
            return "break"
        txt.bind("<MouseWheel>", _raw_scroll)
        txt.bind("<Button-4>",   _raw_scroll)
        txt.bind("<Button-5>",   _raw_scroll)

        def _on_close() -> None:
            _raw_win[0]  = None
            _raw_text[0] = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _append_raw_global(text: str) -> None:
        """Buffer raw output; if the window is open, write immediately."""
        _raw_buffer.append(text)
        def _do() -> None:
            if not (_raw_text[0] and _raw_win[0] and _raw_win[0].winfo_exists()):
                return
            _raw_text[0].config(state="normal")
            _raw_text[0].insert("end", text)
            _raw_text[0].see("end")
            _raw_text[0].config(state="disabled")
        scroll_frame.after(0, _do)

    def _run_update(fics_to_update: list, force: bool = False) -> None:
        if not fics_to_update:
            return
        _update_stop.clear()
        _set_updating(True)
        total  = len(fics_to_update)
        done_n = [0]
        errors = [0]
        import re as _re
        # Debug: log actual counts
        _update_queue.put(("log", f"[debug] updating {total} fics (force={force}), "
                           f"fic_file has {len(_fic_file[0].all_fics)} total\n"))

        def _current_count(chapters_str: str) -> int:
            if not chapters_str:
                return 0
            m = _re.match(r"(\d+)", chapters_str.strip())
            return int(m.group(1)) if m else 0

        def _on_result(fic: Fic, info: WorkInfo) -> None:
            done_n[0] += 1
            if info.error:
                errors[0] += 1
                msg = f"✗  {fic.title[:40]}  —  {info.error}"
                _update_queue.put(("log", msg + "\n"))
                _update_queue.put(("progress", (
                    done_n[0], total, f"{done_n[0]}/{total}  ✗ {fic.title[:30]}",
                )))
                return

            if fic.status == "🟡":
                new_status = "🟡"
            elif info.status == "🟢":
                new_status = "🟢"
            elif is_stale_check(fic, info.date):
                new_status = "🟠"
            else:
                new_status = "🔴"

            new_ch   = _current_count(info.chapters)
            old_ch   = _current_count(fic.chapters)
            new_date = (info.date or fic.date or "")[:10]
            old_date = (fic.date or "")[:10]
            has_new  = new_ch > old_ch or (new_date > old_date and new_ch != old_ch)
            was_disabled = not fic.enabled
            if has_new and was_disabled:
                toggle_fic_enabled(_fic_file[0], fic)

            re_enabled = "  ⬇ re-enabled" if has_new and was_disabled else ""
            update_fic(
                _fic_file[0], fic,
                date=info.date or fic.date,
                status=new_status,
                chapters=info.chapters or fic.chapters,
                word_count=info.word_count or fic.word_count,
            )
            # Save rich metadata to YAML if we got any
            if info.summary or info.tags or info.categories or info.authors:
                update_fic_meta(
                    _fic_file[0], fic,
                    summary=info.summary or fic.summary,
                    categories=info.categories or fic.categories,
                    tags=info.tags or fic.tags,
                    authors=info.authors or fic.authors,
                    rating=info.rating or fic.rating,
                    fandoms_list=info.fandoms_list or fic.fandoms_list,
                    relationships=info.relationships or fic.relationships,
                    characters=info.characters or fic.characters,
                    warnings=info.warnings or fic.warnings,
                )
            # Simple summary line
            summary = (f"✔  {fic.title[:40]}{re_enabled}\n")
            _update_queue.put(("log", summary))
            # Detail line with fetched metadata
            detail = (f"   {new_status}  date: {info.date or fic.date}"
                      f"  |  ch: {info.chapters or fic.chapters}"
                      f"  |  wc: {info.word_count or fic.word_count}\n")
            _update_queue.put(("log", detail))
            _update_queue.put(("progress", (
                done_n[0], total, f"{done_n[0]}/{total}  ✔ {fic.title[:30]}",
            )))
            _update_queue.put(("result", (fic, info)))

        def _on_done() -> None:
            msg = (f"Done — {total} updated, {errors[0]} errors"
                   if not _update_stop.is_set()
                   else f"Stopped — {done_n[0]}/{total} done")
            _update_queue.put(("done", msg))

        def _log_cb(line: str) -> None:
            """All scraper output goes to main log (no raw window needed)."""
            _update_queue.put(("log", line))

        scrape_works_async(
            fics_to_update, _on_result, _on_done,
            gdl_config=_gdl_config[0],
            log_cb=_log_cb,
            stop_event=_update_stop,
            pause_event=_update_pause,
            force=force,
        )
        scroll_frame.after(100, _poll_update)

    def is_stale_check(fic: Fic, new_date: str) -> bool:
        """Check staleness using the freshly scraped date."""
        from datetime import date as _date
        try:
            updated = _date.fromisoformat(new_date or fic.date)
        except ValueError:
            return False
        today = _date.today()
        months = (today.year - updated.year) * 12 + (today.month - updated.month)
        return months >= _stale_months[0]

    update_all_btn.config(
        command=lambda: _run_update(
            list(_fic_file[0].all_fics),
            force=True))

    def _refresh_update_all_label() -> None:
        n = len(_fic_file[0].all_fics)
        update_all_btn.config(text=f"↻  Update All ({n})")
    update_sel_btn.config(
        command=lambda: _run_update(
            [_visible_fics[i] for i in sorted(_selected_rows)
             if i < len(_visible_fics)] or
            [f for f in _visible_fics if f.enabled]
        ))
    stop_update_btn.config(command=_update_stop.set)
    pause_update_btn.config(command=_toggle_pause)

    # ── Filters ───────────────────────────────────────────────────────────────
    section_label(scroll_frame, "FILTERS")

    def _lbl(parent, text):
        return tk.Label(parent, text=text, bg=PANEL, fg=FG_DIM,
                        font=FONT_MONO, anchor="w", width=8)

    def _entry(parent, var, width=22):
        b = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        b.pack(side="left", padx=(0, 16))
        e = tk.Entry(b, textvariable=var, bg=ENTRY_BG, fg=FG,
                     insertbackground=FG, relief="flat",
                     font=FONT_MONO, width=width)
        e.pack(padx=4, pady=3)
        return e

    def _dropdown(parent, var: tk.StringVar, options: list[tuple[str, str]]) -> tk.Label:
        """Custom single-select dropdown matching STATUS_OPTIONS UI behavior."""
        dd_ref: list[tk.Toplevel | None] = [None]
        current_display = next((disp for val, disp in options if val == var.get()), var.get())

        btn_border = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        btn_border.pack(side="left", padx=(0, 16))
        btn = tk.Label(btn_border, text=f"{current_display}  ▾",
                       bg=ENTRY_BG, fg=FG, font=FONT_MONO,
                       cursor="hand2", padx=8, pady=3)
        btn.pack()

        def _close_dd() -> None:
            if dd_ref[0] and dd_ref[0].winfo_exists():
                dd_ref[0].destroy()
            dd_ref[0] = None

        def _pick(value: str, display: str) -> None:
            var.set(value)
            btn.config(text=f"{display}  ▾")
            _close_dd()
            _apply_filter_later()

        def _on_click_outside(event: tk.Event) -> None:
            if not (dd_ref[0] and dd_ref[0].winfo_exists()):
                return
            dx = dd_ref[0].winfo_rootx()
            dy = dd_ref[0].winfo_rooty()
            dw = dd_ref[0].winfo_width()
            dh = dd_ref[0].winfo_height()
            bx = btn.winfo_rootx()
            by = btn.winfo_rooty()
            bw = btn.winfo_width()
            bh = btn.winfo_height()
            inside = ((dx <= event.x_root < dx + dw and dy <= event.y_root < dy + dh) or
                      (bx <= event.x_root < bx + bw and by <= event.y_root < by + bh))
            if not inside:
                _close_dd()

        def _open_dd() -> None:
            if dd_ref[0] and dd_ref[0].winfo_exists():
                _close_dd()
                return

            root_w = scroll_frame.winfo_toplevel()
            dd = tk.Toplevel(root_w)
            dd.overrideredirect(True)
            dd.configure(bg=BORDER)
            dd_ref[0] = dd

            inner = tk.Frame(dd, bg=ENTRY_BG)
            inner.pack(padx=1, pady=1)

            for value, display in options:
                active = value == var.get()
                bg_c   = SEL_BG if active else ENTRY_BG
                fg_c   = ACCENT if active else FG_DIM
                row    = tk.Frame(inner, bg=bg_c, cursor="hand2")
                row.pack(fill="x")
                lbl    = tk.Label(row, text=display, bg=bg_c, fg=fg_c,
                                  font=FONT_MONO, anchor="w", padx=10, pady=4)
                lbl.pack(fill="x")
                for w in (row, lbl):
                    w.bind("<Enter>", lambda _e, r=row: r.config(bg=BORDER))
                    w.bind("<Leave>", lambda _e, r=row, b=bg_c: r.config(bg=b))
                    w.bind("<Button-1>", lambda _e, v=value, d=display: _pick(v, d))

            dd.geometry(f"+{btn.winfo_rootx()}+{btn.winfo_rooty() + btn.winfo_height() + 4}")
            dd.lift()
            root_w.bind("<Button-1>", _on_click_outside, add="+")

        btn.bind("<Button-1>", lambda _e: _open_dd())
        return btn

    # Row 1 — status + title
    row1 = tk.Frame(scroll_frame, bg=PANEL)
    row1.pack(fill="x", padx=PAD_OUTER, pady=(0, 4))

    _lbl(row1, "status:").pack(side="left", padx=(0, 4))

    STATUS_OPTIONS = [
        ("All",  "All"),
        ("🟢",   f"🟢 {STATUS_LABEL['🟢']}"),
        ("🟡",   f"🟡 {STATUS_LABEL['🟡']}"),
        ("🔴",   f"🔴 {STATUS_LABEL['🔴']}"),
        ("🟠",   f"🟠 {STATUS_LABEL['🟠']}"),
    ]
    status_var = tk.StringVar(value="All")
    _status_dd: list[tk.Toplevel | None] = [None]

    status_btn_border = tk.Frame(row1, bg=BORDER, padx=1, pady=1)
    status_btn_border.pack(side="left", padx=(0, 16))
    status_btn = tk.Label(status_btn_border, text="All  ▾",
                          bg=ENTRY_BG, fg=FG, font=FONT_MONO,
                          cursor="hand2", padx=8, pady=3)
    status_btn.pack()

    def _open_status_dd() -> None:
        if _status_dd[0] and _status_dd[0].winfo_exists():
            _close_status_dd()
            return
        root_w = scroll_frame.winfo_toplevel()
        dd = tk.Toplevel(root_w)
        dd.overrideredirect(True)
        dd.configure(bg=BORDER)
        _status_dd[0] = dd

        inner = tk.Frame(dd, bg=ENTRY_BG)
        inner.pack(padx=1, pady=1)

        for value, display in STATUS_OPTIONS:
            active = value == status_var.get()
            bg_c   = SEL_BG if active else ENTRY_BG
            fg_c   = ACCENT if active else FG_DIM
            row    = tk.Frame(inner, bg=bg_c, cursor="hand2")
            row.pack(fill="x")
            lbl    = tk.Label(row, text=display, bg=bg_c, fg=fg_c,
                              font=FONT_MONO, anchor="w", padx=10, pady=4)
            lbl.pack(fill="x")
            for w in (row, lbl):
                w.bind("<Enter>", lambda _e, r=row: r.config(bg=BORDER))
                w.bind("<Leave>", lambda _e, r=row, b=bg_c: r.config(bg=b))
                w.bind("<Button-1>", lambda _e, v=value, d=display: _pick_status(v, d))

        status_btn.update_idletasks()
        x = status_btn.winfo_rootx() - root_w.winfo_rootx()
        y = (status_btn.winfo_rooty() - root_w.winfo_rooty()
             + status_btn.winfo_height() + 4)
        dd.geometry(f"+{status_btn.winfo_rootx()}+{status_btn.winfo_rooty() + status_btn.winfo_height() + 4}")
        dd.lift()

        root_w.bind("<Button-1>", _on_status_click_outside, add="+")

    def _pick_status(value: str, display: str) -> None:
        status_var.set(value)
        status_btn.config(text=f"{display}  ▾")
        _close_status_dd()
        _apply_filter_later()

    def _close_status_dd() -> None:
        if _status_dd[0] and _status_dd[0].winfo_exists():
            _status_dd[0].destroy()
        _status_dd[0] = None

    def _on_status_click_outside(event: tk.Event) -> None:
        if not (_status_dd[0] and _status_dd[0].winfo_exists()):
            return
        dx = _status_dd[0].winfo_rootx()
        dy = _status_dd[0].winfo_rooty()
        dw = _status_dd[0].winfo_width()
        dh = _status_dd[0].winfo_height()
        bx = status_btn.winfo_rootx()
        by = status_btn.winfo_rooty()
        bw = status_btn.winfo_width()
        bh = status_btn.winfo_height()
        inside = ((dx <= event.x_root < dx+dw and dy <= event.y_root < dy+dh) or
                  (bx <= event.x_root < bx+bw and by <= event.y_root < by+bh))
        if not inside:
            _close_status_dd()

    status_btn.bind("<Button-1>", lambda _e: _open_status_dd())

    tk.Frame(row1, bg=PANEL, width=20).pack(side="left")
    _lbl(row1, "title:").pack(side="left", padx=(0, 4))
    title_var = tk.StringVar()
    _entry(row1, title_var, width=30)
    title_var.trace_add("write", lambda *_: _apply_filter_later())

    # Row 2 — fandom (multi-select dropdown) + date range
    row2 = tk.Frame(scroll_frame, bg=PANEL)
    row2.pack(fill="x", padx=PAD_OUTER, pady=(0, 4))

    _lbl(row2, "fandom:").pack(side="left", padx=(0, 4))

    _selected_fandoms: list[str] = []
    _fandom_dd: list[tk.Toplevel | None] = [None]

    # Search entry
    fandom_search_var = tk.StringVar()
    fandom_border = tk.Frame(row2, bg=BORDER, padx=1, pady=1)
    fandom_border.pack(side="left", padx=(0, 8))
    fandom_entry = tk.Entry(fandom_border, textvariable=fandom_search_var,
                            bg=ENTRY_BG, fg=FG, insertbackground=FG,
                            relief="flat", font=FONT_MONO, width=20)
    fandom_entry.pack(padx=4, pady=3)

    # Chip row below
    chip_row = tk.Frame(scroll_frame, bg=PANEL)
    chip_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 8))
    _lbl(chip_row, "").pack(side="left", padx=(0, 4))   # alignment spacer
    fandom_chip_frame = tk.Frame(chip_row, bg=PANEL)
    fandom_chip_frame.pack(side="left", fill="x")

    def _fandoms_all() -> list[str]:
        return sorted({f.fandom for f in _all_fics})

    def _fandoms_matching(q: str) -> list[str]:
        names = _fandoms_all()
        if not q:
            return names
        return [n for n in names if q.lower() in n.lower()]

    def _rebuild_fandom_chips() -> None:
        for w in fandom_chip_frame.winfo_children():
            w.destroy()
        for name in _selected_fandoms:
            chip = tk.Frame(fandom_chip_frame, bg=ACCENT, padx=4, pady=1,
                            cursor="hand2")
            chip.pack(side="left", padx=(0, 3))
            lbl = tk.Label(chip, text=f"{name}  ×", bg=ACCENT, fg=BTN_FG,
                           font=FONT_TAGS, cursor="hand2")
            lbl.pack()
            for w in (chip, lbl):
                w.bind("<Button-1>", lambda _e, n=name: _toggle_fandom(n))

    def _toggle_fandom(name: str) -> None:
        if name in _selected_fandoms:
            _selected_fandoms.remove(name)
        else:
            _selected_fandoms.append(name)
        _rebuild_fandom_chips()
        _apply_filter_later()
        _rebuild_fandom_dd_list()

    def _close_fandom_dd(*_) -> None:
        if _fandom_dd[0]:
            try:
                _fandom_dd[0].destroy()
            except Exception:
                pass
            _fandom_dd[0] = None

    _fandom_dd_list_frame: list[tk.Frame | None] = [None]
    _fandom_dd_canvas:     list[tk.Canvas | None] = [None]

    def _open_fandom_dd() -> None:
        if _fandom_dd[0] and _fandom_dd[0].winfo_exists():
            _rebuild_fandom_dd_list()
            return
        _close_fandom_dd()

        root = scroll_frame.winfo_toplevel()
        dd = tk.Toplevel(root)
        dd.overrideredirect(True)
        dd.configure(bg=BORDER)
        _fandom_dd[0] = dd

        inner = tk.Frame(dd, bg=ENTRY_BG)
        inner.pack(fill="both", padx=1, pady=1)

        max_rows = 8
        row_h    = 24
        dd_h     = max_rows * row_h

        dd_canvas = tk.Canvas(inner, bg=ENTRY_BG, highlightthickness=0,
                              height=dd_h, width=220)
        _fandom_dd_canvas[0] = dd_canvas
        dd_sb = ttk.Scrollbar(inner, orient="vertical", command=dd_canvas.yview)
        dd_canvas.configure(yscrollcommand=dd_sb.set)
        dd_sb.pack(side="right", fill="y")
        dd_canvas.pack(side="left", fill="both", expand=True)
        register_scroll_canvas(dd_canvas)

        lf = tk.Frame(dd_canvas, bg=ENTRY_BG)
        _fandom_dd_list_frame[0] = lf
        lw = dd_canvas.create_window((0, 0), window=lf, anchor="nw")
        lf.bind("<Configure>",
            lambda _e: dd_canvas.configure(scrollregion=dd_canvas.bbox("all")))
        dd_canvas.bind("<Configure>",
            lambda e: dd_canvas.itemconfig(lw, width=e.width))

        fandom_entry.update_idletasks()
        x = fandom_entry.winfo_rootx()
        y = fandom_entry.winfo_rooty() + fandom_entry.winfo_height() + 4
        dd.geometry(f"+{x}+{y}")
        dd.lift()

        _rebuild_fandom_dd_list()

        dd.bind("<FocusOut>",
            lambda _e: scroll_frame.after(120, _maybe_close_fandom_dd))

    def _rebuild_fandom_dd_list() -> None:
        lf = _fandom_dd_list_frame[0]
        if not lf or not lf.winfo_exists():
            return
        for w in lf.winfo_children():
            w.destroy()

        q       = fandom_search_var.get().strip().lower()
        matches = _fandoms_matching(q)

        if not matches:
            tk.Label(lf, text="  no fandoms match", bg=ENTRY_BG, fg=FG_DIM,
                     font=FONT_MONO).pack(anchor="w", padx=8, pady=6)
            return

        for name in matches:
            active = name in _selected_fandoms
            bg_c   = SEL_BG if active else ENTRY_BG
            fg_c   = ACCENT if active else FG_DIM
            row    = tk.Frame(lf, bg=bg_c, cursor="hand2")
            row.pack(fill="x")
            lbl    = tk.Label(row, text=("✔  " if active else "   ") + name,
                              bg=bg_c, fg=fg_c, font=FONT_MONO, anchor="w",
                              cursor="hand2")
            lbl.pack(fill="x", padx=6, pady=2)
            for w in (row, lbl):
                w.bind("<Button-1>", lambda _e, n=name: _toggle_fandom(n))
                w.bind("<Enter>",    lambda _e, r=row: r.config(bg=BORDER))
                w.bind("<Leave>",    lambda _e, r=row, b=bg_c: r.config(bg=b))

        # Resize canvas height to content, capped at max
        lf.update_idletasks()
        content_h = lf.winfo_reqheight()
        max_h     = 8 * 24
        if _fandom_dd_canvas[0] and _fandom_dd_canvas[0].winfo_exists():
            _fandom_dd_canvas[0].config(height=min(content_h, max_h))

    def _maybe_close_fandom_dd() -> None:
        if not (_fandom_dd[0] and _fandom_dd[0].winfo_exists()):
            return
        focused = scroll_frame.winfo_toplevel().focus_get()
        if focused and str(focused).startswith(str(_fandom_dd[0])):
            return
        _close_fandom_dd()

    fandom_search_var.trace_add("write", lambda *_: (
        _rebuild_fandom_dd_list() if _fandom_dd[0] and _fandom_dd[0].winfo_exists()
        else _open_fandom_dd()
    ))
    fandom_entry.bind("<FocusIn>",  lambda _e: _open_fandom_dd())
    fandom_entry.bind("<FocusOut>",
        lambda _e: scroll_frame.after(150, _maybe_close_fandom_dd))
    fandom_entry.bind("<Escape>",   lambda _e: _close_fandom_dd())
    fandom_entry.bind("<Return>",   lambda _e: _close_fandom_dd())

    # ── Date picker helper ────────────────────────────────────────────────────

    def _make_date_picker(parent: tk.Frame, var: tk.StringVar) -> tk.Entry:
        """
        Create a date entry with a calendar popup.
        Clicking the entry or the 📅 button opens the calendar.
        Selecting a day writes yyyy-mm-dd into var.
        """
        import calendar as _cal
        from datetime import date as _date, datetime as _dt

        _cal_dd: list[tk.Toplevel | None] = [None]

        wrapper = tk.Frame(parent, bg=PANEL)
        wrapper.pack(side="left", padx=(0, 12))

        border = tk.Frame(wrapper, bg=BORDER, padx=1, pady=1)
        border.pack(side="left")
        entry = tk.Entry(border, textvariable=var, bg=ENTRY_BG, fg=FG,
                         insertbackground=FG, relief="flat",
                         font=FONT_MONO, width=11)
        entry.pack(padx=4, pady=3)

        cal_btn = tk.Label(wrapper, text="📅", bg=PANEL, fg=FG_DIM,
                           font=FONT_MONO, cursor="hand2")
        cal_btn.pack(side="left", padx=(3, 0))

        _cal_open = [False]   # tracks whether calendar is open

        def _close_cal(*_) -> None:
            _cal_open[0] = False
            if _cal_dd[0]:
                try:
                    _cal_dd[0].destroy()
                except Exception:
                    pass
                _cal_dd[0] = None

        def _open_cal(*_) -> None:
            if _cal_dd[0] and _cal_dd[0].winfo_exists():
                return
            _close_cal()
            _cal_open[0] = True

            try:
                current = _dt.strptime(var.get().strip(), "%Y-%m-%d").date()
            except ValueError:
                current = _date.today()

            # Mutable state
            _view  = [current.year, current.month]  # [year, month]
            _mode  = ["day"]   # "day" | "month" | "year"
            _ydec  = [current.year // 10 * 10]  # decade start for year grid

            CAL_W = 224
            CELL  = 28
            MCELL = 52   # month cell size
            YCELL = 44   # year cell size

            root = scroll_frame.winfo_toplevel()
            dd = tk.Toplevel(root)
            dd.overrideredirect(True)
            dd.configure(bg=BORDER)
            _cal_dd[0] = dd

            frame = tk.Frame(dd, bg=ENTRY_BG)
            frame.pack(padx=1, pady=1)

            # ── Nav header (shared across modes) ─────────────────────────────
            nav = tk.Frame(frame, bg=ENTRY_BG)
            nav.pack(fill="x", pady=(6, 2))

            prev_btn = tk.Label(nav, text="◀", bg=ENTRY_BG, fg=ACCENT,
                                font=FONT_BOLD, cursor="hand2", padx=8)
            prev_btn.pack(side="left")

            nav_lbl = tk.Label(nav, text="", bg=ENTRY_BG, fg=FG,
                               font=FONT_BOLD, cursor="hand2", anchor="center")
            nav_lbl.pack(side="left", expand=True, fill="x")

            next_btn = tk.Label(nav, text="▶", bg=ENTRY_BG, fg=ACCENT,
                                font=FONT_BOLD, cursor="hand2", padx=8)
            next_btn.pack(side="right")

            # ── Day-of-week row ───────────────────────────────────────────────
            dow_row = tk.Frame(frame, bg=ENTRY_BG)
            sep     = tk.Frame(frame, bg=BORDER, height=1)

            for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"):
                tk.Label(dow_row, text=d, bg=ENTRY_BG, fg=FG_DIM,
                         font=FONT_TAGS, width=3, anchor="center",
                         ).pack(side="left", padx=1)

            # ── Main canvas ───────────────────────────────────────────────────
            cal_canvas = tk.Canvas(frame, bg=ENTRY_BG, highlightthickness=0,
                                   width=CAL_W)
            cal_canvas.pack(padx=4, pady=(0, 6))

            # ── Drawing functions ─────────────────────────────────────────────

            def _show_day_mode() -> None:
                dow_row.pack(fill="x")
                sep.pack(fill="x", pady=2)
                _draw_days()

            def _show_month_mode() -> None:
                dow_row.pack_forget()
                sep.pack_forget()
                _draw_months()

            def _show_year_mode() -> None:
                dow_row.pack_forget()
                sep.pack_forget()
                _draw_years()

            def _draw_days() -> None:
                cal_canvas.delete("all")
                year, month = _view
                nav_lbl.config(
                    text=f"{_cal.month_name[month]} {year}")

                weeks = _cal.monthcalendar(year, month)
                cal_canvas.config(height=CELL * len(weeks))
                CW = CAL_W // 7

                for r, week in enumerate(weeks):
                    for c, day in enumerate(week):
                        if day == 0:
                            continue
                        x0, y0 = c * CW, r * CELL
                        x1, y1 = x0 + CW, y0 + CELL

                        d_obj    = _date(year, month, day)
                        is_sel   = var.get().strip() == d_obj.strftime("%Y-%m-%d")
                        is_today = d_obj == _date.today()

                        bg_c = ACCENT if is_sel else (SEL_BG if is_today else ENTRY_BG)
                        fg_c = BTN_FG if is_sel else (ACCENT if is_today else FG)
                        hov_c = BTN_HOV if is_sel else BORDER

                        tc = f"cell_{r}_{c}"
                        tr = f"rect_{r}_{c}"

                        cal_canvas.create_rectangle(
                            x0+1, y0+1, x1-1, y1-1,
                            fill=bg_c, outline="", tags=(tc, tr))
                        cal_canvas.create_text(
                            (x0+x1)//2, (y0+y1)//2,
                            text=str(day), fill=fg_c,
                            font=FONT_TAGS, tags=tc)

                        cal_canvas.tag_bind(tc, "<Button-1>",
                            lambda _e, d=d_obj: _pick_date(d))
                        cal_canvas.tag_bind(tc, "<Enter>",
                            lambda _e, r=tr, h=hov_c: cal_canvas.itemconfig(r, fill=h))
                        cal_canvas.tag_bind(tc, "<Leave>",
                            lambda _e, r=tr, b=bg_c: cal_canvas.itemconfig(r, fill=b))

            def _draw_months() -> None:
                cal_canvas.delete("all")
                year = _view[0]
                nav_lbl.config(text=str(year))
                COLS, ROWS = 3, 4
                CW = CAL_W // COLS
                cal_canvas.config(height=MCELL * ROWS)

                for i, name in enumerate(_cal.month_abbr[1:], 0):
                    r, c = divmod(i, COLS)
                    x0, y0 = c * CW, r * MCELL
                    x1, y1 = x0 + CW, y0 + MCELL
                    is_cur = (i + 1) == _view[1]
                    bg_c = ACCENT if is_cur else ENTRY_BG
                    fg_c = BTN_FG if is_cur else FG

                    tc = f"mc_{r}_{c}"
                    tr = f"mr_{r}_{c}"
                    cal_canvas.create_rectangle(
                        x0+2, y0+2, x1-2, y1-2,
                        fill=bg_c, outline="", tags=(tc, tr))
                    cal_canvas.create_text(
                        (x0+x1)//2, (y0+y1)//2,
                        text=name, fill=fg_c, font=FONT_TAGS, tags=tc)

                    month_num = i + 1
                    cal_canvas.tag_bind(tc, "<Button-1>",
                        lambda _e, m=month_num: _pick_month(m))
                    cal_canvas.tag_bind(tc, "<Enter>",
                        lambda _e, r=tr, b=bg_c: cal_canvas.itemconfig(r,
                            fill=BORDER if b == ENTRY_BG else BTN_HOV))
                    cal_canvas.tag_bind(tc, "<Leave>",
                        lambda _e, r=tr, b=bg_c: cal_canvas.itemconfig(r, fill=b))

            def _draw_years() -> None:
                cal_canvas.delete("all")
                start = _ydec[0]
                nav_lbl.config(text=f"{start}–{start+11}")
                COLS, ROWS = 3, 4
                CW = CAL_W // COLS
                cal_canvas.config(height=YCELL * ROWS)

                years = list(range(start, start + 12))
                for i, yr in enumerate(years):
                    r, c = divmod(i, COLS)
                    x0, y0 = c * CW, r * YCELL
                    x1, y1 = x0 + CW, y0 + YCELL
                    is_cur = yr == _view[0]
                    bg_c = ACCENT if is_cur else ENTRY_BG
                    fg_c = BTN_FG if is_cur else FG

                    tc = f"yc_{r}_{c}"
                    tr = f"yr_{r}_{c}"
                    cal_canvas.create_rectangle(
                        x0+2, y0+2, x1-2, y1-2,
                        fill=bg_c, outline="", tags=(tc, tr))
                    cal_canvas.create_text(
                        (x0+x1)//2, (y0+y1)//2,
                        text=str(yr), fill=fg_c, font=FONT_TAGS, tags=tc)

                    cal_canvas.tag_bind(tc, "<Button-1>",
                        lambda _e, y=yr: _pick_year(y))
                    cal_canvas.tag_bind(tc, "<Enter>",
                        lambda _e, r=tr, b=bg_c: cal_canvas.itemconfig(r,
                            fill=BORDER if b == ENTRY_BG else BTN_HOV))
                    cal_canvas.tag_bind(tc, "<Leave>",
                        lambda _e, r=tr, b=bg_c: cal_canvas.itemconfig(r, fill=b))

            # ── Mode transitions ──────────────────────────────────────────────

            def _on_nav_label_click() -> None:
                if _mode[0] == "day":
                    _mode[0] = "month"
                    _show_month_mode()
                elif _mode[0] == "month":
                    _mode[0] = "year"
                    _show_year_mode()
                # clicking in year mode does nothing (top level)

            def _on_prev() -> None:
                if _mode[0] == "day":
                    _view[1] -= 1
                    if _view[1] < 1:
                        _view[1] = 12; _view[0] -= 1
                    _draw_days()
                elif _mode[0] == "month":
                    _view[0] -= 1
                    _draw_months()
                elif _mode[0] == "year":
                    _ydec[0] -= 12
                    _draw_years()

            def _on_next() -> None:
                if _mode[0] == "day":
                    _view[1] += 1
                    if _view[1] > 12:
                        _view[1] = 1; _view[0] += 1
                    _draw_days()
                elif _mode[0] == "month":
                    _view[0] += 1
                    _draw_months()
                elif _mode[0] == "year":
                    _ydec[0] += 12
                    _draw_years()

            def _pick_month(month: int) -> None:
                _view[1] = month
                _mode[0] = "day"
                _show_day_mode()

            def _pick_year(year: int) -> None:
                _view[0] = year
                _ydec[0] = year // 10 * 10
                _mode[0] = "month"
                _show_month_mode()

            def _pick_date(d: "_date") -> None:
                var.set(d.strftime("%Y-%m-%d"))
                _close_cal()

            prev_btn.bind("<Button-1>", lambda _e: _on_prev())
            next_btn.bind("<Button-1>", lambda _e: _on_next())
            nav_lbl.bind("<Button-1>",  lambda _e: _on_nav_label_click())

            # Position and show
            entry.update_idletasks()
            x = entry.winfo_rootx()
            y = entry.winfo_rooty() + entry.winfo_height() + 4
            dd.geometry(f"+{x}+{y}")
            dd.lift()

            # Close on click outside — check coordinates, not focus
            def _on_root_click(event: tk.Event) -> None:
                if not _cal_open[0]:
                    return
                if not (_cal_dd[0] and _cal_dd[0].winfo_exists()):
                    _cal_open[0] = False
                    return
                dx = _cal_dd[0].winfo_rootx()
                dy = _cal_dd[0].winfo_rooty()
                dw = _cal_dd[0].winfo_width()
                dh = _cal_dd[0].winfo_height()
                ex = entry.winfo_rootx()
                ey = entry.winfo_rooty()
                ew = entry.winfo_width()
                eh = entry.winfo_height()
                bx = cal_btn.winfo_rootx()
                by = cal_btn.winfo_rooty()
                bw = cal_btn.winfo_width()
                bh = cal_btn.winfo_height()
                inside_dd    = dx <= event.x_root < dx+dw and dy <= event.y_root < dy+dh
                inside_entry = ex <= event.x_root < ex+ew and ey <= event.y_root < ey+eh
                inside_btn   = bx <= event.x_root < bx+bw and by <= event.y_root < by+bh
                if not (inside_dd or inside_entry or inside_btn):
                    _close_cal()

            root.bind("<Button-1>", _on_root_click, add="+")

            # Start in day mode
            _show_day_mode()

        def _maybe_close_cal() -> None:
            pass   # no-op — handled by root click binding now

        entry.bind("<FocusIn>",  lambda _e: _open_cal())
        entry.bind("<Escape>",   lambda _e: _close_cal())
        cal_btn.bind("<Button-1>", lambda _e: (_open_cal()
                                    if not (_cal_dd[0] and _cal_dd[0].winfo_exists())
                                    else _close_cal()))
        return entry

    # ── Date range ────────────────────────────────────────────────────────────
    _lbl(row2, "from:").pack(side="left", padx=(0, 4))
    date_from_var = tk.StringVar()
    _make_date_picker(row2, date_from_var)
    date_from_var.trace_add("write", lambda *_: _apply_filter_later())

    _lbl(row2, "to:").pack(side="left", padx=(0, 4))
    date_to_var = tk.StringVar()
    _make_date_picker(row2, date_to_var)
    date_to_var.trace_add("write", lambda *_: _apply_filter_later())

    # Row 3 - download state, work type, and numeric ranges
    row3 = tk.Frame(scroll_frame, bg=PANEL)
    row3.pack(fill="x", padx=PAD_OUTER, pady=(0, 8))

    _lbl(row3, "dl:").pack(side="left", padx=(0, 4))
    DL_OPTIONS = [
        ("All", "All"),
        ("Enabled", "Enabled"),
        ("Disabled", "Disabled"),
    ]
    dl_status_var = tk.StringVar(value="All")
    _dropdown(row3, dl_status_var, DL_OPTIONS)
    dl_status_var.trace_add("write", lambda *_: _apply_filter_later())

    _lbl(row3, "type:").pack(side="left", padx=(0, 4))
    TYPE_OPTIONS = [
        ("All", "All"),
        ("Single", "Single"),
        ("Series", "Series"),
    ]
    work_type_var = tk.StringVar(value="All")
    _dropdown(row3, work_type_var, TYPE_OPTIONS)
    work_type_var.trace_add("write", lambda *_: _apply_filter_later())

    _lbl(row3, "wc:").pack(side="left", padx=(0, 4))
    wc_min_var = tk.StringVar()
    wc_max_var = tk.StringVar()
    _entry(row3, wc_min_var, width=8)
    tk.Label(row3, text="to", bg=PANEL, fg=FG_DIM, font=FONT_MONO).pack(side="left", padx=(0, 8))
    _entry(row3, wc_max_var, width=8)

    _lbl(row3, "ch:").pack(side="left", padx=(0, 4))
    ch_min_var = tk.StringVar()
    ch_max_var = tk.StringVar()
    _entry(row3, ch_min_var, width=6)
    tk.Label(row3, text="to", bg=PANEL, fg=FG_DIM, font=FONT_MONO).pack(side="left", padx=(0, 8))
    _entry(row3, ch_max_var, width=6)

    wc_min_var.trace_add("write", lambda *_: _apply_filter_later())
    wc_max_var.trace_add("write", lambda *_: _apply_filter_later())
    ch_min_var.trace_add("write", lambda *_: _apply_filter_later())
    ch_max_var.trace_add("write", lambda *_: _apply_filter_later())

    divider(scroll_frame)

    # ── Stats bar ─────────────────────────────────────────────────────────────
    stats_row = tk.Frame(scroll_frame, bg=PANEL)
    stats_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 4))
    stats_lbl = tk.Label(stats_row, text="", bg=PANEL, fg=FG_DIM,
                         font=FONT_MONO, anchor="w")
    stats_lbl.pack(side="left")

    # ── Table ─────────────────────────────────────────────────────────────────
    section_label(scroll_frame, "FICS")

    table_outer = tk.Frame(scroll_frame, bg=BORDER, padx=1, pady=1)
    table_outer.pack(fill="both", expand=True, padx=PAD_OUTER, pady=(0, 20))

    # Header canvas — drawn with the same column positions as the data rows
    header_canvas = tk.Canvas(table_outer, bg=ENTRY_BG, highlightthickness=0,
                              height=26)
    header_canvas.pack(fill="x")

    _sort_col:  list[str] = ["date"]
    _sort_asc:  list[bool] = [False]

    # Resize state
    _resize_col:   list[str | None] = [None]   # column being dragged
    _resize_x0:    list[int]        = [0]       # drag start x
    _resize_px0:   list[int]        = [0]       # column width at drag start
    HANDLE_W = 6    # px width of the invisible hit zone
    MIN_COL  = 20   # minimum column width in px

    def _draw_header() -> None:
        header_canvas.delete("all")
        w = header_canvas.winfo_width() or 900

        def _cx(col: str) -> int:
            return _COL_PX.get(col, 30)

        col_x: dict[str, int] = {}
        PAD = 8; x = PAD
        col_x["dl"]     = x + _cx("dl")     // 2;  x += _cx("dl")     + 4
        col_x["type"]   = x + _cx("type")   // 2;  x += _cx("type")   + 4
        col_x["status"] = x + _cx("status") // 2;  x += _cx("status") + 4
        col_x["date"]   = x;                        x += _cx("date")   + 4
        col_x["fandom"] = x;                        x += _cx("fandom") + 4
        col_x["title"]  = x
        right = PAD
        right += _cx("upd") // 2
        col_x["upd"] = w - right;  right += _cx("upd") // 2 + 4
        right += _cx("ch") // 2
        col_x["ch"]  = w - right;  right += _cx("ch")  // 2 + 4
        right += _cx("wc") // 2
        col_x["wc"]  = w - right;  right += _cx("wc")  // 2 + 4
        col_x["title_r"] = w - right - 4

        LEFT_COLS = {"fandom", "title", "date"}
        ty = 13
        for col, label in HEADER_LABELS.items():
            cx   = col_x.get(col, 0)
            text = label
            if col == _sort_col[0]:
                text += "  ▲" if _sort_asc[0] else "  ▼"
            fg   = ACCENT if col == _sort_col[0] else FG_DIM
            anch = "w" if col in LEFT_COLS else "center"
            if col == "title":
                title_w = max(10, col_x.get("title_r", w - 120) - cx)
                header_canvas.create_text(cx, ty, text=text, anchor="w",
                                          fill=fg, font=FONT_BOLD,
                                          width=title_w, tags=f"hdr_{col}")
            else:
                header_canvas.create_text(cx, ty, text=text, anchor=anch,
                                          fill=fg, font=FONT_BOLD,
                                          tags=f"hdr_{col}")
            header_canvas.tag_bind(f"hdr_{col}", "<Button-1>",
                                   lambda _e, c=col: _on_header_click(_e, c))

        # ── Resize handles ────────────────────────────────────────────────────
        # Place a handle at the right edge of each resizable left-anchored col.
        # For right-anchored cols (wc, ch, upd) place it at their left edge.
        resizable_left  = ["dl", "type", "status", "date", "fandom"]
        resizable_right = ["wc", "ch"]   # upd is too small to bother

        for col in resizable_left:
            # right edge = col centre + half width + gap/2
            edge = col_x[col] + _cx(col) // 2 + _cx(col) // 2 + 2
            tag  = f"hdl_{col}"
            header_canvas.create_rectangle(
                edge - HANDLE_W // 2, 0, edge + HANDLE_W // 2, 26,
                fill="", outline="", tags=tag)
            header_canvas.tag_bind(tag, "<Enter>",
                lambda _e: header_canvas.config(cursor="sb_h_double_arrow"))
            header_canvas.tag_bind(tag, "<Leave>",
                lambda _e: header_canvas.config(cursor=""))
            header_canvas.tag_bind(tag, "<ButtonPress-1>",
                lambda _e, c=col: _on_handle_press(_e, c))

        for col in resizable_right:
            # left edge of this right-anchored col
            edge = col_x[col] - _cx(col) // 2 - 2
            tag  = f"hdl_{col}"
            header_canvas.create_rectangle(
                edge - HANDLE_W // 2, 0, edge + HANDLE_W // 2, 26,
                fill="", outline="", tags=tag)
            header_canvas.tag_bind(tag, "<Enter>",
                lambda _e: header_canvas.config(cursor="sb_h_double_arrow"))
            header_canvas.tag_bind(tag, "<Leave>",
                lambda _e: header_canvas.config(cursor=""))
            header_canvas.tag_bind(tag, "<ButtonPress-1>",
                lambda _e, c=col: _on_handle_press(_e, c))

    def _on_header_click(event: tk.Event, col: str) -> None:
        """Only sort if we're not on a resize handle."""
        if _resize_col[0] is None:
            _on_sort(col)

    def _on_handle_press(event: tk.Event, col: str) -> None:
        _resize_col[0]  = col
        _resize_x0[0]   = event.x_root
        _resize_px0[0]  = _COL_PX.get(col, 30)

    def _on_header_drag(event: tk.Event) -> None:
        col = _resize_col[0]
        if col is None:
            return
        dx      = event.x_root - _resize_x0[0]
        new_w   = max(MIN_COL, _resize_px0[0] + dx)
        _COL_PX[col] = new_w
        _draw_header()
        _draw()

    def _on_header_release(event: tk.Event) -> None:
        _resize_col[0] = None
        header_canvas.config(cursor="")

    header_canvas.bind("<Configure>",    lambda _e: _draw_header())
    header_canvas.bind("<B1-Motion>",    _on_header_drag)
    header_canvas.bind("<ButtonRelease-1>", _on_header_release)

    tk.Frame(table_outer, bg=BORDER, height=1).pack(fill="x")

    # ── Virtual list canvas ───────────────────────────────────────────────────
    ROW_H    = 24   # px per row
    TBL_H    = 500  # visible height cap

    tbl_canvas = tk.Canvas(table_outer, bg=ENTRY_BG, highlightthickness=0,
                           height=TBL_H)
    def _sb_command(*args):
        tbl_canvas.yview(*args)
        tbl_canvas.after_idle(_draw)
    tbl_sb     = ttk.Scrollbar(table_outer, orient="vertical",
                               command=_sb_command)
    tbl_canvas.configure(yscrollcommand=tbl_sb.set)
    tbl_sb.pack(side="right", fill="y")
    tbl_canvas.pack(side="left", fill="both", expand=True)
    register_scroll_canvas(tbl_canvas)

    # Intercept scroll wheel directly so it works regardless of outer canvas
    def _tbl_scroll(event: tk.Event) -> str:
        if event.num == 4:
            tbl_canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            tbl_canvas.yview_scroll(1, "units")
        else:
            tbl_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"
    tbl_canvas.bind("<MouseWheel>", _tbl_scroll)
    tbl_canvas.bind("<Button-4>",   _tbl_scroll)
    tbl_canvas.bind("<Button-5>",   _tbl_scroll)

    # ── State ─────────────────────────────────────────────────────────────────
    _all_fics:      list[Fic]        = []
    _fic_file:      list[FicFile]    = [FicFile()]   # mutable cell
    _visible_fics:  list[Fic]        = []
    _filter_id:     list[str] = [""]
    _hovered_row:   list[int] = [-1]
    _hovered_col:   list[str] = [""]
    _selected_rows: set[int]  = set()
    _tooltip_win:   list[tk.Toplevel | None] = [None]
    _tooltip_after: list[str] = [""]

    # Column x positions — computed once on first draw, updated on resize
    # Layout: status | date | fandom | title (flex) | wc | ch
    _COL_X:    dict[str, int] = {}
    _COL_PX:   dict[str, int] = {}   # computed pixel width per column

    def _measure_cols() -> None:
        """Measure max content width (px) for auto-sized columns."""
        from tkinter import font as tkfont
        try:
            f = tkfont.Font(font=FONT_MONO)
        except Exception:
            return

        def _w(text: str) -> int:
            return f.measure(text)

        # Fixed columns — use header label as minimum
        _COL_PX["dl"]     = _w("⬇") + 10
        _COL_PX["type"]   = _w("📚") + 10
        _COL_PX["status"] = _w("🟠") + 10
        _COL_PX["upd"]    = _w("↻") + 10

        # Auto-sized columns — max of header and all visible content
        _COL_PX["date"]   = max(_w("Updated"), max((_w(f.date)   for f in _visible_fics if f.date),   default=0)) + 12
        _COL_PX["fandom"] = max(_w("Fandom"),  max((_w(f.fandom) for f in _visible_fics if f.fandom), default=0)) + 12
        _COL_PX["wc"]     = max(_w("Words"),   max((_w(_fmt_wc(f.word_count)) for f in _visible_fics if f.word_count), default=0)) + 12
        _COL_PX["ch"]     = max(_w("Ch."),     max((_w(f.chapters)   for f in _visible_fics if f.chapters),   default=0)) + 12

    def _compute_cols(w: int) -> None:
        PAD = 8
        x   = PAD

        def _cx(col: str) -> int:
            return _COL_PX.get(col, 30)

        _COL_X["dl"]     = x + _cx("dl")     // 2;  x += _cx("dl")     + 4
        _COL_X["type"]   = x + _cx("type")   // 2;  x += _cx("type")   + 4
        _COL_X["status"] = x + _cx("status") // 2;  x += _cx("status") + 4
        _COL_X["date"]   = x;                        x += _cx("date")   + 4
        _COL_X["fandom"] = x;                        x += _cx("fandom") + 4
        _COL_X["title"]  = x

        right = PAD
        right += _cx("upd") // 2
        _COL_X["upd"] = w - right;  right += _cx("upd") // 2 + 4
        right += _cx("ch") // 2
        _COL_X["ch"]  = w - right;  right += _cx("ch")  // 2 + 4
        right += _cx("wc") // 2
        _COL_X["wc"]  = w - right;  right += _cx("wc")  // 2 + 4
        _COL_X["title_r"] = w - right - 4

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _draw(*_) -> None:
        tbl_canvas.delete("all")
        fics = _visible_fics
        if not fics:
            tbl_canvas.create_text(
                16, 12, text="No fics match the current filters.",
                anchor="nw", fill=FG_DIM, font=FONT_MONO,
            )
            return

        cw = tbl_canvas.winfo_width() or 900
        _compute_cols(cw)

        # Which rows are in view?
        view_top    = tbl_canvas.canvasy(0)
        view_bottom = tbl_canvas.canvasy(TBL_H)
        first = max(0, int(view_top  // ROW_H))
        last  = min(len(fics), int(view_bottom // ROW_H) + 1)

        for i in range(first, last):
            fic = fics[i]
            y0  = i * ROW_H
            y1  = y0 + ROW_H
            is_sel    = i in _selected_rows
            bg  = SEL_BG if is_sel else (ROW_ALT if i % 2 else ENTRY_BG)
            hl  = BORDER  if i == _hovered_row[0] else bg
            sc  = STATUS_COLOR.get(fic.status, FG_DIM)
            is_dl_hov  = (_hovered_col[0] == "dl"    and i == _hovered_row[0])
            is_ti_hov  = (_hovered_col[0] == "title"  and i == _hovered_row[0])
            is_upd_hov = (_hovered_col[0] == "upd"    and i == _hovered_row[0])

            # Row background
            tbl_canvas.create_rectangle(
                0, y0, cw, y1, fill=hl, outline="", tags=f"row{i}")

            ty = y0 + ROW_H // 2

            # Centre x for centred columns — stored directly in _COL_X
            # (already computed as left_edge + half_width in _compute_cols)
            dl_cx  = _COL_X["dl"]
            ty_cx  = _COL_X["type"]
            st_cx  = _COL_X["status"]
            upd_cx = _COL_X["upd"]
            wc_cx  = _COL_X["wc"]
            ch_cx  = _COL_X["ch"]

            dl_fill = (BTN_HOV if is_dl_hov else
                       (COLOR_OK if fic.enabled else COLOR_FAIL))
            tbl_canvas.create_text(
                dl_cx, ty,
                text="⬇" if fic.enabled else "✗",
                anchor="center", fill=dl_fill,
                font=FONT_EMOJI, tags=f"row{i}")

            tbl_canvas.create_text(
                ty_cx, ty,
                text="📚" if fic.is_series else "📖",
                anchor="center", fill=FG_DIM, font=FONT_EMOJI, tags=f"row{i}")

            tbl_canvas.create_text(
                st_cx, ty, text=fic.status,
                anchor="center", fill=sc, font=FONT_EMOJI, tags=f"row{i}")

            # Date — left aligned
            tbl_canvas.create_text(
                _COL_X["date"], ty, text=fic.date,
                anchor="w", fill=FG_DIM, font=FONT_MONO, tags=f"row{i}")

            # Fandom — left aligned
            tbl_canvas.create_text(
                _COL_X["fandom"], ty, text=fic.fandom,
                anchor="w", fill=ACCENT2, font=FONT_MONO, tags=f"row{i}")

            # Title — left aligned, expands
            title_w  = max(10, _COL_X.get("title_r", cw - 120) - _COL_X["title"])
            title_fg = ACCENT if is_ti_hov else FG
            tbl_canvas.create_text(
                _COL_X["title"], ty, text=fic.title,
                anchor="w", fill=title_fg, font=FONT_MONO,
                tags=f"row{i}", width=title_w)

            tbl_canvas.create_text(
                wc_cx, ty, text=_fmt_wc(fic.word_count),
                anchor="center", fill=FG_DIM, font=FONT_MONO, tags=f"row{i}")

            tbl_canvas.create_text(
                ch_cx, ty, text=fic.chapters,
                anchor="center", fill=FG_DIM, font=FONT_MONO, tags=f"row{i}")

            # Per-row update button — centred
            upd_fill = ACCENT if is_upd_hov else FG_DIM
            tbl_canvas.create_text(
                upd_cx, ty, text="↻",
                anchor="center", fill=upd_fill,
                font=FONT_MONO, tags=f"row{i}")

    def _col_at(x: int) -> str:
        if x < _COL_X.get("type", 999):
            return "dl"
        if x < _COL_X.get("status", 999):
            return "type"
        if x >= _COL_X.get("title", 0) and x < _COL_X.get("wc", 9999):
            return "title"
        # upd is rightmost — anything beyond the wc column
        if x > _COL_X.get("wc", 9999):
            return "upd"
        return "other"

    def _row_at(y_canvas: int) -> int:
        return int(y_canvas // ROW_H)

    def _hide_tooltip() -> None:
        if _tooltip_after[0]:
            try:
                tbl_canvas.after_cancel(_tooltip_after[0])
            except Exception:
                pass
            _tooltip_after[0] = ""
        if _tooltip_win[0] and _tooltip_win[0].winfo_exists():
            _tooltip_win[0].destroy()
        _tooltip_win[0] = None

    def _show_tooltip(fic: Fic, rx: int, ry: int) -> None:
        _hide_tooltip()

        root_w = tbl_canvas.winfo_toplevel()
        TIP_W  = 340
        TIP_H  = 340

        tip = tk.Toplevel(root_w)
        tip.overrideredirect(True)
        tip.configure(bg=BORDER)
        tip.attributes("-topmost", True)
        tip.resizable(False, False)
        _tooltip_win[0] = tip

        tip_canvas = tk.Canvas(tip, bg=ENTRY_BG, highlightthickness=0,
                               width=TIP_W, height=TIP_H)
        tip_sb = ttk.Scrollbar(tip, orient="vertical", command=tip_canvas.yview,
                               style="Thin.Vertical.TScrollbar" if False else "Vertical.TScrollbar")
        tip_canvas.configure(yscrollcommand=tip_sb.set)
        tip_sb.pack(side="right", fill="y")
        tip_canvas.pack(side="left", fill="both", expand=True)

        content = tk.Frame(tip_canvas, bg=ENTRY_BG, padx=8, pady=6)
        cw_id = tip_canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_resize(_e=None):
            tip_canvas.configure(scrollregion=tip_canvas.bbox("all"))
            tip_canvas.itemconfig(cw_id, width=TIP_W)
        content.bind("<Configure>", _on_resize)

        def _tip_scroll(e: tk.Event) -> str:
            if e.num == 4:   tip_canvas.yview_scroll(-1, "units")
            elif e.num == 5: tip_canvas.yview_scroll(1,  "units")
            else:            tip_canvas.yview_scroll(int(-1*(e.delta/120)), "units")
            return "break"
        tip_canvas.bind("<MouseWheel>", _tip_scroll)
        tip_canvas.bind("<Button-4>",   _tip_scroll)
        tip_canvas.bind("<Button-5>",   _tip_scroll)

        def _bind_scroll(w) -> None:
            w.bind("<MouseWheel>", _tip_scroll)
            w.bind("<Button-4>",   _tip_scroll)
            w.bind("<Button-5>",   _tip_scroll)
            for ch in w.winfo_children():
                _bind_scroll(ch)

        W   = TIP_W - 16
        F   = FONT_MONO          # normal mono font
        FS  = FONT_MONO          # compact mono font for metadata

        def _div():
            tk.Frame(content, bg=BORDER, height=1).pack(fill="x", pady=3)

        # Title
        tk.Label(content, text=fic.title,
                 bg=ENTRY_BG, fg=ACCENT, font=FONT_BOLD,
                 anchor="w", justify="left", wraplength=W
                 ).pack(fill="x", pady=(0, 2))

        # Link directly below title
        if fic.url:
            lnk = tk.Label(content, text=fic.url,
                           bg=ENTRY_BG, fg=ACCENT2, font=FONT_TAGS,
                           anchor="w", cursor="hand2", wraplength=W)
            lnk.pack(fill="x", pady=(0, 2))
            lnk.bind("<Button-1>", lambda _e: webbrowser.open(fic.url))

        # Meta: category · fandom · rating
        meta_parts = []
        if fic.categories:   meta_parts.append("  ".join(fic.categories))
        if fic.fandoms_list: meta_parts.append(fic.fandoms_list[0])
        if fic.rating:       meta_parts.append(fic.rating)
        if meta_parts:
            tk.Label(content, text="  ·  ".join(meta_parts),
                     bg=ENTRY_BG, fg=ACCENT2, font=FS,
                     anchor="w", wraplength=W).pack(fill="x")

        if fic.authors:
            tk.Label(content, text="by " + ", ".join(fic.authors),
                     bg=ENTRY_BG, fg=FG_DIM, font=FS,
                     anchor="w", wraplength=W).pack(fill="x", pady=(0, 2))

        # Summary
        if fic.summary:
            _div()
            tk.Label(content, text=fic.summary,
                     bg=ENTRY_BG, fg=FG, font=FS,
                     anchor="nw", justify="left", wraplength=W
                     ).pack(fill="x")

        # Tag groups
        def _chips(label, items, fg_col):
            if not items: return
            _div()
            tk.Label(content, text=label, bg=ENTRY_BG, fg=FG_DIM,
                     font=FONT_BOLD, anchor="w"
                     ).pack(fill="x")
            wrap = tk.Frame(content, bg=ENTRY_BG)
            wrap.pack(fill="x")
            row = tk.Frame(wrap, bg=ENTRY_BG)
            row.pack(fill="x", anchor="w")
            row_w = 0
            for item in items:
                # Measure text width before creating chip
                char_w = 7   # approx px per char at size 9
                est_w  = len(item) * char_w + 12
                if row_w + est_w > W and row_w > 0:
                    row = tk.Frame(wrap, bg=ENTRY_BG)
                    row.pack(fill="x", anchor="w")
                    row_w = 0
                tk.Label(row, text=item, bg=BORDER, fg=fg_col,
                         font=FS, padx=4, pady=2
                         ).pack(side="left", padx=(0, 3), pady=1)
                row_w += est_w + 3

        _chips("CW",           fic.warnings,      "#e09050")
        _chips("Fandoms",      fic.fandoms_list,   ACCENT2)
        _chips("Relationship", fic.relationships,  "#c084fc")
        _chips("Characters",   fic.characters,     FG)
        _chips("Tags",         fic.tags,           FG_DIM)

        # Hover-to-keep-open
        def _bind_tip(w, ec, lc):
            try: w.bind("<Enter>", ec); w.bind("<Leave>", lc)
            except Exception: pass
            for ch in w.winfo_children(): _bind_tip(ch, ec, lc)

        def _tip_enter(_e):
            if _tooltip_after[0]:
                try: tbl_canvas.after_cancel(_tooltip_after[0])
                except Exception: pass
                _tooltip_after[0] = ""

        def _tip_leave(_e):
            tbl_canvas.after(120, _ctl)

        def _ctl():
            if not (_tooltip_win[0] and _tooltip_win[0].winfo_exists()): return
            px = tip.winfo_pointerx(); py = tip.winfo_pointery()
            tx = tip.winfo_rootx();    ty = tip.winfo_rooty()
            tw = tip.winfo_width();    th = tip.winfo_height()
            if not (tx <= px < tx+tw and ty <= py < ty+th):
                _hide_tooltip()

        # Position
        tip.update_idletasks()
        _bind_scroll(content)   # bind scroll on all content widgets
        sw = root_w.winfo_screenwidth(); sh = root_w.winfo_screenheight()
        sb_w = tip_sb.winfo_reqwidth()
        total_w = TIP_W + sb_w
        x = rx + 16; y = ry + 8
        if x + total_w > sw - 10: x = rx - total_w - 8
        if y + TIP_H   > sh - 10: y = sh - TIP_H - 10
        tip.geometry(f"{total_w}x{TIP_H}+{x}+{y}")
        _bind_tip(tip, _tip_enter, _tip_leave)

    def _schedule_tooltip(fic: Fic, rx: int, ry: int) -> None:
        if _tooltip_after[0]:
            try:
                tbl_canvas.after_cancel(_tooltip_after[0])
            except Exception:
                pass
        _tooltip_after[0] = tbl_canvas.after(
            600, lambda: _show_tooltip(fic, rx, ry))

    def _on_canvas_motion(event: tk.Event) -> None:
        y   = tbl_canvas.canvasy(event.y)
        x   = event.x
        idx = _row_at(y)
        col = _col_at(x)

        changed = (idx != _hovered_row[0] or col != _hovered_col[0])
        _hovered_row[0] = idx
        _hovered_col[0] = col

        if col in ("dl", "upd") and 0 <= idx < len(_visible_fics):
            tbl_canvas.config(cursor="hand2")
        elif col == "title" and 0 <= idx < len(_visible_fics):
            tbl_canvas.config(cursor="hand2")
        else:
            tbl_canvas.config(cursor="")

        # Tooltip on title hover
        if col == "title" and 0 <= idx < len(_visible_fics):
            fic = _visible_fics[idx]
            _schedule_tooltip(fic,
                              event.x_root, event.y_root)
        else:
            _hide_tooltip()

        if changed:
            _draw()

    def _on_canvas_leave(event: tk.Event) -> None:
        if _hovered_row[0] != -1 or _hovered_col[0] != "":
            _hovered_row[0] = -1
            _hovered_col[0] = ""
            tbl_canvas.config(cursor="")
            _draw()
        # Delay hide — user may be moving toward the tooltip
        if _tooltip_after[0]:
            try:
                tbl_canvas.after_cancel(_tooltip_after[0])
            except Exception:
                pass
            _tooltip_after[0] = ""
        _tooltip_after[0] = tbl_canvas.after(150, _check_canvas_leave)

    def _check_canvas_leave() -> None:
        _tooltip_after[0] = ""
        if not (_tooltip_win[0] and _tooltip_win[0].winfo_exists()):
            return
        tip = _tooltip_win[0]
        px = tip.winfo_pointerx()
        py = tip.winfo_pointery()
        tx = tip.winfo_rootx();  ty2 = tip.winfo_rooty()
        tw = tip.winfo_width();  th  = tip.winfo_height()
        if not (tx <= px < tx + tw and ty2 <= py < ty2 + th):
            _hide_tooltip()

    def _on_canvas_click(event: tk.Event) -> None:
        y   = tbl_canvas.canvasy(event.y)
        x   = event.x
        idx = _row_at(y)
        if not (0 <= idx < len(_visible_fics)):
            return
        fic = _visible_fics[idx]
        col = _col_at(x)

        if col == "dl":
            toggle_fic_enabled(_fic_file[0], fic)
        elif col == "upd":
            _run_update([fic], force=True)
            return
        else:
            # Any other column (including title) — toggle row selection
            if idx in _selected_rows:
                _selected_rows.discard(idx)
            else:
                _selected_rows.add(idx)
        _draw()

    def _on_canvas_ctrl_click(event: tk.Event) -> None:
        """Ctrl+click on the title column opens the work in the browser."""
        y   = tbl_canvas.canvasy(event.y)
        x   = event.x
        idx = _row_at(y)
        if not (0 <= idx < len(_visible_fics)):
            return
        col = _col_at(x)
        if col == "title":
            webbrowser.open(_visible_fics[idx].url)

    tbl_canvas.bind("<Motion>",          _on_canvas_motion)
    tbl_canvas.bind("<Leave>",           _on_canvas_leave)
    tbl_canvas.bind("<Button-1>",        _on_canvas_click)
    tbl_canvas.bind("<Control-Button-1>", _on_canvas_ctrl_click)
    tbl_canvas.bind("<Configure>",       lambda _e: _draw())
    tbl_canvas.bind("<<ScrollUpdate>>",  lambda _e: _draw())

    def _on_tbl_scroll(*_) -> None:
        _draw()

    # Patch yview to also redraw
    _orig_yview = tbl_canvas.yview
    def _patched_yview(*args):
        result = _orig_yview(*args) if args else _orig_yview()
        _draw()
        return result

    tbl_canvas.configure(yscrollcommand=lambda *a: (tbl_sb.set(*a), tbl_canvas.after_idle(_draw)))

    # ── Render helper ─────────────────────────────────────────────────────────

    def _render(fics: list[Fic]) -> None:
        _visible_fics.clear()
        _visible_fics.extend(fics)
        _hovered_row[0] = -1
        _measure_cols()
        _draw_header()
        total_h = max(len(fics) * ROW_H, 1)

        if total_h <= TBL_H:
            tbl_canvas.config(height=max(total_h, 40))
            tbl_canvas.configure(scrollregion=(0, 0, 0, max(total_h, 40)))
            set_scroll_enabled(tbl_canvas, False)
        else:
            tbl_canvas.config(height=TBL_H)
            tbl_canvas.configure(scrollregion=(0, 0, 0, total_h))
            set_scroll_enabled(tbl_canvas, True)

        tbl_canvas.yview_moveto(0)
        _draw()

    # ── Sorting ───────────────────────────────────────────────────────────────

    def _toggle_select_all() -> None:
        if len(_selected_rows) == len(_visible_fics):
            _selected_rows.clear()
        else:
            _selected_rows.update(range(len(_visible_fics)))
        _draw()

    def _on_sort(col: str) -> None:
        if _sort_col[0] == col:
            _sort_asc[0] = not _sort_asc[0]
        else:
            _sort_col[0] = col
            _sort_asc[0] = True
        _draw_header()
        _apply_filter()

    # ── Filtering ─────────────────────────────────────────────────────────────

    def _apply_filter_later() -> None:
        """Debounce: wait 80 ms after last keystroke before filtering."""
        if _filter_id[0]:
            try:
                scroll_frame.after_cancel(_filter_id[0])
            except Exception:
                pass
        _filter_id[0] = scroll_frame.after(80, _apply_filter)

    def _apply_filter() -> None:
        import re as _re

        st_filter  = status_var.get()
        ti_filter  = title_var.get().strip().lower()
        fa_filter  = _selected_fandoms
        df_filter  = date_from_var.get().strip()
        dt_filter  = date_to_var.get().strip()
        dl_filter  = dl_status_var.get()
        tp_filter  = work_type_var.get()

        def _parse_int(raw: str) -> int | None:
            s = (raw or "").strip().replace(",", "").replace("_", "")
            if not s:
                return None
            try:
                return int(s)
            except ValueError:
                return None

        def _parse_wc(raw: str) -> int | None:
            s = (raw or "").strip().lower()
            s = s.replace(",", "").replace("_", "").replace("\u202f", "")
            if not s:
                return None
            if s.endswith("w"):
                s = s[:-1]
            mult = 1
            if s.endswith("k"):
                mult = 1000
                s = s[:-1]
            try:
                if "." in s:
                    return int(float(s) * mult)
                return int(s) * mult
            except ValueError:
                return None

        def _parse_chapters(raw: str) -> int | None:
            s = (raw or "").strip().lower()
            if not s:
                return None
            m = _re.match(r"(\d+)\s*/", s)
            if m:
                return int(m.group(1))
            m = _re.search(r"\((\d+)\s*ch", s)
            if m:
                return int(m.group(1))
            m = _re.match(r"(\d+)\b", s)
            if m:
                return int(m.group(1))
            return None

        wc_min = _parse_int(wc_min_var.get())
        wc_max = _parse_int(wc_max_var.get())
        ch_min = _parse_int(ch_min_var.get())
        ch_max = _parse_int(ch_max_var.get())

        def _matches(fic: Fic) -> bool:
            if st_filter != "All" and fic.status != st_filter:
                return False
            if dl_filter == "Enabled" and not fic.enabled:
                return False
            if dl_filter == "Disabled" and fic.enabled:
                return False
            if tp_filter == "Single" and fic.is_series:
                return False
            if tp_filter == "Series" and not fic.is_series:
                return False
            if ti_filter and ti_filter not in fic.title.lower():
                return False
            if fa_filter and fic.fandom not in fa_filter:
                return False
            if df_filter and fic.date < df_filter:
                return False
            if dt_filter and fic.date > dt_filter:
                return False

            wc_val = _parse_wc(fic.word_count)
            if wc_min is not None and (wc_val is None or wc_val < wc_min):
                return False
            if wc_max is not None and (wc_val is None or wc_val > wc_max):
                return False

            ch_val = _parse_chapters(fic.chapters)
            if ch_min is not None and (ch_val is None or ch_val < ch_min):
                return False
            if ch_max is not None and (ch_val is None or ch_val > ch_max):
                return False
            return True

        filtered = [f for f in _all_fics if _matches(f)]
        _selected_rows.clear()

        # Sort
        key_map = {
            "status": lambda f: f.status,
            "date":   lambda f: f.date,
            "fandom": lambda f: f.fandom.lower(),
            "title":  lambda f: f.title.lower(),
            "wc":     lambda f: f.word_count,
            "ch":     lambda f: f.chapters,
        }
        key = key_map.get(_sort_col[0], lambda f: f.date)
        filtered.sort(key=key, reverse=not _sort_asc[0])

        # Stats
        total    = len(_all_fics)
        shown    = len(filtered)
        finished = sum(1 for f in filtered if f.status == "🟢")
        ongoing  = sum(1 for f in filtered if f.status == "🔴")
        dropped  = sum(1 for f in filtered if f.status == "🟡")
        stale    = sum(1 for f in filtered if f.status == "🟠")
        suffix   = f"  ({shown} shown)" if shown != total else ""
        stale_str = f"  🟠 {stale}" if stale else ""
        stats_lbl.config(
            text=f"{total} fics{suffix}   "
                 f"🟢 {finished}  🔴 {ongoing}  🟡 {dropped}{stale_str}"
        )

        _render(filtered)

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load() -> None:
        path = file_entry.get().strip()
        if not path:
            return
        try:
            fic_file = parse_fic_file(path)
        except FileNotFoundError:
            messagebox.showerror("File not found", f"Cannot open:\n{path}")
            return
        except Exception as e:
            messagebox.showerror("Parse error", str(e))
            return

        _all_fics.clear()
        _all_fics.extend(fic_file.all_fics)
        _fic_file[0] = fic_file
        _refresh_update_all_label()
        _apply_filter()

    # Auto-load default file if it exists
    if Path(DEFAULT_FIC_FILE).exists():
        _load()