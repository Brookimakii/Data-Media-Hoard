"""
ui/downloader_tab.py
--------------------
Builds the Downloader tab and wires it to the core layer.

The only imports from core are:
    catalogue  — load_artists, load_config, get_downloadable_sites
    jobs       — build_jobs
    download   — DownloadController

All UI state (entries, checkboxes, labels) is local to build_downloader().
The DownloadController receives a queue.Queue.put as its on_event callback
and the UI polls that queue via root.after().
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from core.catalogue import (
    CatalogueError, ConfigError,
    load_artists, load_config, get_downloadable_sites,
)
from core.jobs import build_jobs, build_run_summary
from core.download import DownloadController
from core.file_db import get_db
from utils.logger import SessionLogger

from ui.theme import (
    ACCENT, ACCENT2, BG, BORDER, COLOR_FAIL, ENTRY_BG, FG, FG_DIM,
    FONT_BODY, FONT_BOLD, FONT_HEAD, FONT_MONO, FONT_SUB, PAD_OUTER, PANEL,
)
from ui.widgets import (
    ArtistList, StatusPanel,
    divider, section_label, styled_button, styled_entry,
)
from ui.scroll import register_scroll_canvas
from ui.taskbar import (
    clear_taskbar_progress,
    set_taskbar_error,
    set_taskbar_indeterminate,
    set_taskbar_paused,
    set_taskbar_progress,
)

SCRIPT_DIR        = Path(__file__).parent.parent
DEFAULT_CATALOGUE = str(SCRIPT_DIR / "artists.yaml")
DEFAULT_CONFIG    = str(SCRIPT_DIR / "config.yaml")


def build_downloader(parent: tk.Frame) -> None:
    """Mount the full downloader UI onto *parent*."""

    # ── Outer scrollable canvas ───────────────────────────────────────────────
    canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    scroll_frame = tk.Frame(canvas, bg=PANEL)

    win_id = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    scroll_frame.bind(
        "<Configure>",
        lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
    )
    canvas.bind(
        "<Configure>",
        lambda e: canvas.itemconfig(win_id, width=e.width),
    )

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    register_scroll_canvas(canvas)

    # ── Header ────────────────────────────────────────────────────────────────
    tk.Frame(scroll_frame, bg=PANEL, height=30).pack()
    tk.Label(scroll_frame, text="IMAGE DOWNLOADER", bg=PANEL, fg=FG,
             font=FONT_HEAD, anchor="w").pack(fill="x", padx=PAD_OUTER)
    tk.Label(scroll_frame, text="fetch sources via gallery-dl",
             bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w",
             ).pack(fill="x", padx=PAD_OUTER + 2, pady=(0, 10))
    divider(scroll_frame)

    # ── Config file ───────────────────────────────────────────────────────────
    section_label(scroll_frame, "CONFIG FILE")
    cfg_row = tk.Frame(scroll_frame, bg=PANEL)
    cfg_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    cfg_frame, cfg_entry = styled_entry(cfg_row, DEFAULT_CONFIG, width=52)
    cfg_frame.pack(side="left", padx=(0, 8), fill="x", expand=True)

    styled_button(cfg_row, "Browse…", command=lambda: _browse_config()).pack(side="left")

    # ── Artist catalogue ──────────────────────────────────────────────────────
    section_label(scroll_frame, "ARTIST CATALOGUE")
    cat_row = tk.Frame(scroll_frame, bg=PANEL)
    cat_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    cat_frame, cat_entry = styled_entry(cat_row, DEFAULT_CATALOGUE, width=52)
    cat_frame.pack(side="left", padx=(0, 8), fill="x", expand=True)

    section_label(scroll_frame, "ARTISTS")
    artist_list = ArtistList(scroll_frame)
    artist_list.pack(fill="both", expand=True, pady=(0, 4))

    _last_artists:  list[dict] = []
    _last_dl_sites: set[str]   = set()

    def _reload_list() -> None:
        artist_list.load(_last_artists, _last_dl_sites)

    def _load_artists(path: str) -> None:
        nonlocal _last_artists
        try:
            _last_artists = load_artists(path)
            _reload_list()
        except CatalogueError as e:
            messagebox.showerror("Catalogue Error", str(e))

    def browse_config() -> None:
        path = filedialog.askopenfilename(
            title="Select config file",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if path:
            cfg_entry.config(fg=FG)
            cfg_entry.delete(0, "end")
            cfg_entry.insert(0, path)
            _refresh_dl_sites()

    def _refresh_dl_sites() -> None:
        nonlocal _last_dl_sites
        try:
            cfg = load_config(cfg_entry.get())
            _last_dl_sites = get_downloadable_sites(cfg)
        except Exception:
            _last_dl_sites = set()
        _reload_list()

    def _browse_config() -> None:
        path = filedialog.askopenfilename(
            title="Select config file",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if path:
            cfg_entry.config(fg=FG)
            cfg_entry.delete(0, "end")
            cfg_entry.insert(0, path)
            _refresh_dl_sites()

    def browse_catalogue() -> None:
        path = filedialog.askopenfilename(
            title="Select artists catalogue",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if path:
            cat_entry.config(fg=FG)
            cat_entry.delete(0, "end")
            cat_entry.insert(0, path)
            _load_artists(path)

    def reload_catalogue() -> None:
        _load_artists(cat_entry.get())

    styled_button(cat_row, "Browse…", command=browse_catalogue).pack(side="left", padx=(0, 6))
    styled_button(cat_row, "↺ Reload", command=reload_catalogue,
                  bg="#2a2a38", hov="#3a3a50").pack(side="left")

    if Path(DEFAULT_CATALOGUE).exists():
        _refresh_dl_sites()   # loads config → dl_sites, then reloads artist list
        _load_artists(DEFAULT_CATALOGUE)

    # ── Output directory ──────────────────────────────────────────────────────
    section_label(scroll_frame, "OUTPUT DIRECTORY")
    dir_row = tk.Frame(scroll_frame, bg=PANEL)
    dir_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    dir_frame, dir_entry = styled_entry(dir_row, "./download", width=50)
    dir_frame.pack(side="left", padx=(0, 8))

    def browse_dir() -> None:
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            dir_entry.config(fg=FG)
            dir_entry.delete(0, "end")
            dir_entry.insert(0, path)

    styled_button(dir_row, "Browse…", command=browse_dir).pack(side="left")

    # ── Execution mode ────────────────────────────────────────────────────────
    section_label(scroll_frame, "EXECUTION MODE")
    mode_row = tk.Frame(scroll_frame, bg=PANEL)
    mode_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    parallel_var = tk.BooleanVar(value=False)
    max_parallel_var = tk.StringVar(value="4")

    mode_lbl = tk.Label(
        mode_row, text="Sequential  (one job at a time)",
        bg=PANEL, fg=FG_DIM, font=FONT_SUB,
    )

    def _update_mode_label() -> None:
        if parallel_var.get():
            mode_lbl.config(text="Parallel  (limited concurrent jobs)", fg=ACCENT)
            max_parallel_spin.config(state="normal")
        else:
            mode_lbl.config(text="Sequential  (one job at a time)", fg=FG_DIM)
            max_parallel_spin.config(state="disabled")

    for text, value in [("Sequential", False), ("Parallel", True)]:
        tk.Radiobutton(
            mode_row, text=text, variable=parallel_var, value=value,
            bg=PANEL, fg=FG, selectcolor="#2a2040",
            activebackground=PANEL, activeforeground=ACCENT,
            font=FONT_BODY,
            command=_update_mode_label,
        ).pack(side="left", padx=(0, 20))

    tk.Label(mode_row, text="Max concurrent:", bg=PANEL, fg=FG_DIM, font=FONT_SUB).pack(
        side="left", padx=(8, 6)
    )
    max_parallel_spin = tk.Spinbox(
        mode_row,
        from_=1,
        to=32,
        width=4,
        textvariable=max_parallel_var,
        bg=ENTRY_BG,
        fg=FG,
        insertbackground=FG,
        relief="flat",
        highlightthickness=0,
        disabledbackground=ENTRY_BG,
        disabledforeground=FG_DIM,
    )
    max_parallel_spin.pack(side="left")

    mode_lbl.pack(side="left", padx=(10, 0))
    _update_mode_label()

    # ── Progress bar ──────────────────────────────────────────────────────────
    section_label(scroll_frame, "PROGRESS")

    prog_bar_row = tk.Frame(scroll_frame, bg=PANEL)
    prog_bar_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 4))

    progress_var = tk.IntVar(value=0)
    prog_bar = ttk.Progressbar(
        prog_bar_row, variable=progress_var, maximum=100,
        style="Download.Horizontal.TProgressbar",
    )
    prog_bar.pack(fill="x")

    # Current-site progress (processed in this run vs DB total for that site)
    site_prog_var = tk.IntVar(value=0)
    site_prog_max = tk.IntVar(value=1)
    site_prog_bar = ttk.Progressbar(
        prog_bar_row, variable=site_prog_var, maximum=1,
        style="Download.Horizontal.TProgressbar",
    )
    site_prog_bar.pack(fill="x", pady=(6, 0))

    prog_stats_row = tk.Frame(scroll_frame, bg=PANEL)
    prog_stats_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))

    prog_lbl = tk.Label(prog_stats_row, text="0 / 0",
                        bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    prog_lbl.pack(side="left")
    file_lbl = tk.Label(prog_stats_row, text="0 file(s) downloaded",
                        bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    file_lbl.pack(side="left", padx=(16, 0))
    site_prog_lbl = tk.Label(prog_stats_row, text="site: -",
                             bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    site_prog_lbl.pack(side="left", padx=(16, 0))
    fail_lbl = tk.Label(prog_stats_row, text="",
                        bg=PANEL, fg=COLOR_FAIL, font=FONT_MONO)
    fail_lbl.pack(side="left", padx=(16, 0))

    divider(scroll_frame)

    # ── Output log ────────────────────────────────────────────────────────────
    section_label(scroll_frame, "OUTPUT LOG")
    log_frame = tk.Frame(scroll_frame, bg=BORDER, padx=1, pady=1)
    log_frame.pack(fill="both", expand=True, padx=PAD_OUTER, pady=(0, 6))
    log_text = tk.Text(
        log_frame, bg="#0a0a10", fg=FG_DIM, insertbackground=ACCENT,
        relief="flat", font=FONT_MONO, height=12,
        wrap="word", state="disabled",
    )
    log_sb = ttk.Scrollbar(log_frame, command=log_text.yview)
    log_text.configure(yscrollcommand=log_sb.set)
    log_sb.pack(side="right", fill="y")
    log_text.pack(fill="both", expand=True, padx=4, pady=4)

    # Intercept scroll wheel on the log so it scrolls the Text,
    # not the outer tab canvas.
    def _log_scroll(event: tk.Event) -> str:
        if event.num == 4:
            log_text.yview_scroll(-1, "units")
        elif event.num == 5:
            log_text.yview_scroll(1, "units")
        else:
            log_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    log_text.bind("<MouseWheel>", _log_scroll)
    log_text.bind("<Button-4>",   _log_scroll)
    log_text.bind("<Button-5>",   _log_scroll)

    # ── Download status ───────────────────────────────────────────────────────
    section_label(scroll_frame, "DOWNLOAD STATUS")
    status_panel = StatusPanel(scroll_frame)
    status_panel.pack(fill="both", expand=True, padx=PAD_OUTER, pady=(0, 10))

    # ── Event queue + controller ──────────────────────────────────────────────
    event_queue: queue.Queue = queue.Queue()
    artist_counts: dict = {}
    _current_site_key: list[tuple[str, str, str] | None] = [None]

    def _set_site_progress(key: tuple[str, str, str] | None,
                           downloaded: int = 0, skipped: int = 0) -> None:
        """Update second progress bar: current site processed vs DB total."""
        _current_site_key[0] = key
        if key is None:
            site_prog_var.set(0)
            site_prog_max.set(1)
            site_prog_bar.config(maximum=1)
            site_prog_lbl.config(text="site: -")
            return

        artist, site, _url = key
        processed = max(0, downloaded + skipped)
        total = 0
        try:
            db = get_db()
            if db:
                total = db.total(artist=artist, site=site)
        except Exception:
            total = 0

        maximum = max(1, total, processed)
        site_prog_max.set(maximum)
        site_prog_var.set(min(processed, maximum))
        site_prog_bar.config(maximum=maximum)
        site_prog_lbl.config(text=f"site: {artist}/{site}  {processed}/{maximum}")

    logger = SessionLogger(log_dir="./logs")

    controller = DownloadController(
        on_event=event_queue.put,
        logger=logger,
    )

    # ── Action buttons ────────────────────────────────────────────────────────
    action_row = tk.Frame(scroll_frame, bg=PANEL)
    action_row.pack(fill="x", padx=PAD_OUTER, pady=(10, 20))

    start_btn = styled_button(action_row, "▶  Download Selected")
    start_btn.pack(side="left", padx=(0, 10))

    all_btn = styled_button(action_row, "⬇  Download All",
                            bg="#4a3a8a", hov="#5a4a9a")
    all_btn.pack(side="left", padx=(0, 10))

    stop_btn = styled_button(action_row, "■  Stop", bg="#3a2a2a", hov="#5a3a3a")
    stop_btn.config(state="disabled")
    stop_btn.pack(side="left", padx=(0, 10))

    pause_btn = styled_button(action_row, "⏸  Pause", bg="#2a2a1a", hov="#3a3a2a")
    pause_btn.config(state="disabled")
    pause_btn.pack(side="left")

    # ── Queue polling ─────────────────────────────────────────────────────────
    def _append_log(text: str) -> None:
        log_text.config(state="normal")
        log_text.insert("end", text)
        log_text.see("end")
        log_text.config(state="disabled")

    def _set_running(running: bool) -> None:
        start_btn.config(state="disabled" if running else "normal")
        all_btn.config(  state="disabled" if running else "normal")
        stop_btn.config( state="normal"   if running else "disabled")
        pause_btn.config(state="normal"   if running else "disabled")
        if not running:
            # Reset pause button label when done
            pause_btn.config(text="⏸  Pause", bg="#2a2a1a")

    def _toggle_pause() -> None:
        if controller.is_paused:
            controller.resume()
            pause_btn.config(text="⏸  Pause", bg="#2a2a1a")
            set_taskbar_indeterminate(parent.winfo_toplevel())
        else:
            controller.pause()
            pause_btn.config(text="▶  Resume", bg="#1a2a1a")
            set_taskbar_paused(parent.winfo_toplevel())

    def _poll() -> None:
        try:
            while True:
                event_type, payload = event_queue.get_nowait()

                if event_type == "log":
                    _append_log(payload)

                elif event_type == "status_site":
                    artist, site, url, status = payload
                    status_panel.set_site_status(artist, site, url, status)
                    if status == "running":
                        _set_site_progress((artist, site, url), 0, 0)

                elif event_type == "worker_start":
                    worker_idx, (artist, site, url) = payload
                    try:
                        status_panel.set_worker_start(worker_idx, artist, site, url)
                    except Exception:
                        pass

                elif event_type == "worker_file_count":
                    worker_idx, (artist, site, url, count, skipped) = payload
                    try:
                        status_panel.set_worker_progress(worker_idx, artist, site, url, count, skipped)
                    except Exception:
                        pass

                elif event_type == "worker_done":
                    worker_idx, (artist, site, url), success = payload
                    try:
                        status_panel.set_worker_done(worker_idx, artist, site, url, success)
                    except Exception:
                        pass

                elif event_type == "status_artist":
                    artist, status = payload
                    status_panel.set_artist_status(artist, status)

                elif event_type == "progress":
                    done, total = payload
                    pct = int(done / total * 100) if total else 0
                    progress_var.set(pct)
                    prog_lbl.config(text=f"{done} / {total} jobs")
                    set_taskbar_progress(parent.winfo_toplevel(), done, total)

                elif event_type == "error_count":
                    artist, site, url, count = payload
                    status_panel.set_site_error_count(artist, site, url, count)

                elif event_type == "file_count":
                    artist, site, url, count, skipped = payload
                    status_panel.set_site_file_count(artist, site, url, count, skipped)
                    if _current_site_key[0] == (artist, site, url):
                        _set_site_progress((artist, site, url), count, skipped)
                    # Accumulate per-job totals keyed by (artist, site, url)
                    artist_counts[(artist, site, url)] = (count, skipped)
                    a_total   = sum(c for (a, s, u), (c, sk) in artist_counts.items() if a == artist)
                    a_skipped = sum(sk for (a, s, u), (c, sk) in artist_counts.items() if a == artist)
                    status_panel.set_artist_file_count(artist, a_total, a_skipped)
                    g_total   = sum(c  for (c, sk) in artist_counts.values())
                    g_skipped = sum(sk for (c, sk) in artist_counts.values())
                    file_lbl.config(text=f"{g_total} downloaded  ~{g_skipped} skipped")

                elif event_type == "done":
                    # Drain any remaining events before stopping the poll loop
                    try:
                        while True:
                            et, pl = event_queue.get_nowait()
                            if et == "log":
                                _append_log(pl)
                    except queue.Empty:
                        pass
                    _set_running(False)
                    _append_log("\n── All downloads finished ──\n")
                    # Unpack recap from payload before any use
                    errors_recap = payload or []
                    failed_count = sum(1 for _, _, errs in errors_recap if errs)
                    if failed_count:
                        fail_lbl.config(text=f"✗ {failed_count} failed")
                    else:
                        fail_lbl.config(text="")
                    # Taskbar: red if any failures, clear otherwise
                    if any(errs for _, _, errs in errors_recap):
                        set_taskbar_error(parent.winfo_toplevel())
                    else:
                        clear_taskbar_progress(parent.winfo_toplevel())
                    # Print error recap at the very end
                    if errors_recap:
                        sep = "═" * 60
                        lines = [f"\n{sep}", "  ERROR / WARNING RECAP", sep]
                        for label, cmd_str, errs in errors_recap:
                            lines.append(f"\n  {label}")
                            lines.append(f"  $ {cmd_str}")
                            for e in errs:
                                lines.append(f"    {e}")
                        lines.append(sep + "\n")
                        _append_log("\n".join(lines))
                    else:
                        _append_log("\n── No errors or warnings ──\n")
                    _set_site_progress(None)
                    return   # stop polling

        except queue.Empty:
            pass

        parent.after(50, _poll)

    # ── Start logic ───────────────────────────────────────────────────────────
    def _start(select_all: bool = False) -> None:
        if select_all:
            artist_list._select_all()

        selected = artist_list.get_selected()
        if not selected:
            messagebox.showwarning("No selection", "Please select at least one artist.")
            return

        try:
            cfg = load_config(cfg_entry.get())
        except ConfigError as e:
            messagebox.showerror("Config Error", str(e))
            return

        dl_sites = get_downloadable_sites(cfg)
        if not dl_sites:
            messagebox.showwarning("No sites",
                "No downloadable_sites configured in config.yaml.")
            return

        base_dir   = dir_entry.get().strip() or "./download"
        gdl_config = cfg.get("gdl_config", "./config.json")
        jobs       = build_jobs(selected, dl_sites, base_dir, gdl_config)

        try:
            max_parallel = max(1, int(max_parallel_var.get().strip() or "1"))
        except ValueError:
            messagebox.showwarning("Invalid limit", "Max concurrent must be a whole number.")
            return

        if not jobs:
            messagebox.showinfo("Nothing to do",
                "No matching downloadable sites found for the selected artists.")
            return

        status_panel.populate(jobs)
        progress_var.set(0)
        prog_lbl.config(text=f"0 / {len(jobs)} jobs")
        file_lbl.config(text="0 file(s) downloaded")
        _set_site_progress(None)
        fail_lbl.config(text="")
        artist_counts.clear()

        # Print config + command list to log before starting
        summary = build_run_summary(
            jobs,
            cfg,
            parallel_var.get(),
            max_parallel=max_parallel,
        )
        _append_log(summary)

        _set_running(True)
        set_taskbar_indeterminate(parent.winfo_toplevel())
        controller.start(
            jobs,
            parallel=parallel_var.get(),
            max_parallel=max_parallel,
        )
        parent.after(50, _poll)

    start_btn.config(command=lambda: _start(False))
    all_btn.config(  command=lambda: _start(True))
    stop_btn.config( command=controller.stop)
    pause_btn.config(command=_toggle_pause)