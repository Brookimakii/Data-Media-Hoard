import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import yaml
import os
import threading
import subprocess
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Palette ────────────────────────────────────────────────────────────────────
BG        = "#0f0f13"
PANEL     = "#16161d"
ACCENT    = "#7c6af7"
ACCENT2   = "#c084fc"
FG        = "#e8e8f0"
FG_DIM    = "#6b6b80"
BORDER    = "#2a2a38"
ENTRY_BG  = "#1e1e2a"
BTN_BG    = "#7c6af7"
BTN_FG    = "#ffffff"
BTN_HOV   = "#9580ff"
SEL_BG    = "#2a2040"

FONT_HEAD = ("Georgia", 22, "bold")
FONT_SUB  = ("Georgia", 11, "italic")
FONT_BODY = ("Courier New", 10)
FONT_MONO = ("Courier New", 9)
FONT_BTN  = ("Courier New", 10, "bold")
FONT_TAGS = ("Courier New", 8)

CIRCLE_GRAY   = "\U00002B55"   # pending
CIRCLE_ORANGE = "\U0001F7E0"   # in progress
CIRCLE_GREEN  = "\U0001F7E2"   # success
CIRCLE_RED    = "\U0001F534"   # failed

SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CATALOGUE = os.path.join(SCRIPT_DIR, "artists.yaml")
DEFAULT_CONFIG    = os.path.join(SCRIPT_DIR, "config.yaml")

# ── Global scroll router ───────────────────────────────────────────────────────
_hovered_canvas = None

def _on_widget_enter(canvas):
    global _hovered_canvas
    _hovered_canvas = canvas

def _on_widget_leave(canvas):
    global _hovered_canvas
    if _hovered_canvas is canvas:
        _hovered_canvas = None

def _global_scroll(event):
    if _hovered_canvas is None:
        return
    if event.num == 4:
        _hovered_canvas.yview_scroll(-1, "units")
    elif event.num == 5:
        _hovered_canvas.yview_scroll(1, "units")
    else:
        _hovered_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

def register_scroll_canvas(canvas):
    canvas.bind("<Enter>", lambda e, c=canvas: _on_widget_enter(c), add="+")
    canvas.bind("<Leave>", lambda e, c=canvas: _on_widget_leave(c), add="+")


# ── Helpers ────────────────────────────────────────────────────────────────────

def styled_button(parent, text, command=None, bg=BTN_BG, hov=BTN_HOV, **kwargs):
    kwargs.setdefault("padx", 18)
    kwargs.setdefault("pady", 8)
    btn = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=BTN_FG, relief="flat",
        font=FONT_BTN, cursor="hand2",
        activebackground=hov, activeforeground=BTN_FG,
        **kwargs
    )
    btn.bind("<Enter>", lambda e: btn.config(bg=hov))
    btn.bind("<Leave>", lambda e: btn.config(bg=bg))
    return btn


def styled_entry(parent, placeholder="", width=40):
    frame = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
    entry = tk.Entry(
        frame, bg=ENTRY_BG, fg=FG, insertbackground=ACCENT,
        relief="flat", font=FONT_BODY, width=width,
    )
    entry.pack(padx=4, pady=4, fill="x")
    if placeholder:
        entry.insert(0, placeholder)
        entry.config(fg=FG_DIM)
        def on_focus_in(e):
            if entry.get() == placeholder:
                entry.delete(0, "end")
                entry.config(fg=FG)
        def on_focus_out(e):
            if not entry.get():
                entry.insert(0, placeholder)
                entry.config(fg=FG_DIM)
        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
    return frame, entry


def section_label(parent, text):
    tk.Label(
        parent, text=text, bg=PANEL, fg=ACCENT2,
        font=("Courier New", 9, "bold"), anchor="w"
    ).pack(fill="x", padx=30, pady=(18, 4))


def divider(parent):
    tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=30, pady=6)


# ── YAML loaders ───────────────────────────────────────────────────────────────

def load_artists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("artists", [])
    except FileNotFoundError:
        return []
    except Exception as e:
        messagebox.showerror("YAML Error", f"Failed to load catalogue:\n{e}")
        return []


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        messagebox.showerror("Config Error", f"Failed to load config:\n{e}")
        return {}


# ── Path builder ───────────────────────────────────────────────────────────────

def get_account_name(site, url):
    """
    Extract a human-readable account name from a URL.
    TODO: customise per-site logic as needed.
    Examples:
        pixiv   → last path segment of /users/XXXXX  → "XXXXX"
        twitter → last path segment                  → username
        default → last non-empty path segment
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    parts  = [p for p in parsed.path.split("/") if p]
    if not parts:
        return "unknown"
    # Skip common prefix segments like "users", "en", "profile"
    skip = {"users", "en", "user", "profile", "artist"}
    for p in reversed(parts):
        if p.lower() not in skip:
            return p
    return parts[-1]


def build_output_path(base_dir, artist_name, site, urls):
    """
    Return a list of (url, output_path) tuples.
    Single URL  → base/artist/site
    Multi  URL  → base/artist/site/account_name
    """
    safe_artist = artist_name.replace(" ", "_")
    if len(urls) == 1:
        return [(urls[0], os.path.join(base_dir, safe_artist, site))]
    return [
        (url, os.path.join(base_dir, safe_artist, site, get_account_name(site, url)))
        for url in urls
    ]


# ── Download job builder ───────────────────────────────────────────────────────

def build_jobs(artists, downloadable_sites, base_dir):
    """
    Returns a flat list of dicts:
      { artist, site, url, output_path }
    Only includes sites present in downloadable_sites.
    """
    jobs = []
    for artist in artists:
        artist_name = artist.get("name", "unknown")
        for category, entries in (artist.get("media") or {}).items():
            if not entries:
                continue
            for site, raw_url in entries.items():
                if site not in downloadable_sites:
                    continue
                urls = raw_url if isinstance(raw_url, list) else [raw_url]
                for url, out_path in build_output_path(base_dir, artist_name, site, urls):
                    jobs.append({
                        "artist": artist_name,
                        "site":   site,
                        "url":    url,
                        "output": out_path,
                    })
    return jobs


# ── gallery-dl runner ──────────────────────────────────────────────────────────

def run_gallery_dl(job, log_q):
    """Run gallery-dl for one job. Posts log lines to log_q. Returns success bool."""
    url     = job["url"]
    out     = job["output"]
    os.makedirs(out, exist_ok=True)
    cmd = ["gallery-dl", "--dest", out, url]
    log_q.put(("log", f"[START] {job['artist']} / {job['site']}  →  {url}\n"))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            log_q.put(("log", line))
        proc.wait()
        success = proc.returncode == 0
        status  = "OK" if success else f"EXIT {proc.returncode}"
        log_q.put(("log", f"[{status}] {job['artist']} / {job['site']}\n"))
        return success
    except FileNotFoundError:
        log_q.put(("log", "[ERROR] gallery-dl not found — is it installed and on PATH?\n"))
        return False
    except Exception as e:
        log_q.put(("log", f"[ERROR] {e}\n"))
        return False


# ── Tooltip ────────────────────────────────────────────────────────────────────

class Tooltip:
    def __init__(self, widget, text_fn):
        self._widget  = widget
        self._text_fn = text_fn
        self._win     = None
        widget.bind("<Enter>",   self._show, add="+")
        widget.bind("<Leave>",   self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _show(self, e=None):
        if self._win:
            return
        text = self._text_fn()
        if not text:
            return
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._win = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=BORDER)
        tk.Label(tw, text=text, bg="#1a1a28", fg=FG,
                 font=FONT_MONO, justify="left", padx=10, pady=6).pack()

    def _hide(self, e=None):
        if self._win:
            self._win.destroy()
            self._win = None


# ── Artist list widget ─────────────────────────────────────────────────────────

COLS = 3

class ArtistList(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=PANEL, **kwargs)
        self.artists    = []
        self.check_vars = []
        self.check_btns = []
        self._build()

    def _build(self):
        toolbar = tk.Frame(self, bg=PANEL)
        toolbar.pack(fill="x", padx=30, pady=(6, 4))

        self.count_label = tk.Label(
            toolbar, text="0 / 0 selected",
            bg=PANEL, fg=FG_DIM, font=FONT_MONO, anchor="w"
        )
        self.count_label.pack(side="left")

        for label, cmd in [("Invert", self._invert), ("None", self._select_none), ("All", self._select_all)]:
            styled_button(toolbar, label, command=cmd,
                          bg="#2a2a38", hov="#3a3a50", padx=10, pady=4
                          ).pack(side="right", padx=(4, 0))

        outer = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="both", expand=True, padx=30, pady=(0, 6))

        self.canvas = tk.Canvas(outer, bg=ENTRY_BG, highlightthickness=0, height=220)
        sb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=ENTRY_BG)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self._win_id, width=e.width))
        register_scroll_canvas(self.canvas)

    def load(self, artists):
        for w in self.inner.winfo_children():
            w.destroy()
        self.artists    = artists
        self.check_vars = []
        self.check_btns = []

        if not artists:
            tk.Label(self.inner, text="  No artists found — load a catalogue file.",
                     bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO
                     ).pack(anchor="w", padx=16, pady=12)
            self._update_count()
            return

        for c in range(COLS):
            self.inner.columnconfigure(c, weight=1, uniform="col")

        for i, artist in enumerate(artists):
            var = tk.BooleanVar(value=False)
            self.check_vars.append(var)

            row, col = divmod(i, COLS)
            cell_bg  = ENTRY_BG if (row % 2 == 0) else "#181826"
            cell     = tk.Frame(self.inner, bg=cell_bg)
            cell.grid(row=row, column=col, sticky="ew", padx=1, pady=1)

            cb = tk.Checkbutton(
                cell,
                text=artist.get("name", "???"),
                variable=var,
                bg=cell_bg, fg=FG_DIM,
                selectcolor=SEL_BG,
                activebackground=cell_bg, activeforeground=ACCENT,
                font=("Courier New", 10, "overstrike"),
                anchor="w", cursor="hand2",
            )
            cb.config(command=lambda v=var, b=cb: self._on_toggle(v, b))
            cb.pack(fill="x", padx=8, pady=5)
            self.check_btns.append(cb)

            def make_tooltip_text(a=artist):
                lines = []
                tags  = a.get("tags", [])
                notes = a.get("notes", "")
                if tags:
                    lines.append("tags:   " + "  ".join(f"[{t}]" for t in tags))
                if notes:
                    lines.append("notes:  " + notes)
                media = a.get("media") or {}
                for cat, entries in media.items():
                    if entries:
                        lines.append(f"── {cat} ──")
                        for site, url in entries.items():
                            urls = url if isinstance(url, list) else [url]
                            for u in urls:
                                lines.append(f"  {site}: {u}")
                return "\n".join(lines) if lines else ""

            Tooltip(cb, make_tooltip_text)

        self._update_count()

    def _on_toggle(self, var, btn):
        if var.get():
            btn.config(fg=FG,     font=("Courier New", 10, "bold"))
        else:
            btn.config(fg=FG_DIM, font=("Courier New", 10, "overstrike"))
        self._update_count()

    def _refresh_all_styles(self):
        for var, btn in zip(self.check_vars, self.check_btns):
            if var.get():
                btn.config(fg=FG,     font=("Courier New", 10, "bold"))
            else:
                btn.config(fg=FG_DIM, font=("Courier New", 10, "overstrike"))

    def _update_count(self):
        total    = len(self.check_vars)
        selected = sum(v.get() for v in self.check_vars)
        self.count_label.config(text=f"{selected} / {total} selected")

    def _select_all(self):
        for v in self.check_vars: v.set(True)
        self._refresh_all_styles(); self._update_count()

    def _select_none(self):
        for v in self.check_vars: v.set(False)
        self._refresh_all_styles(); self._update_count()

    def _invert(self):
        for v in self.check_vars: v.set(not v.get())
        self._refresh_all_styles(); self._update_count()

    def get_selected(self):
        return [a for a, v in zip(self.artists, self.check_vars) if v.get()]


# ── Status panel ───────────────────────────────────────────────────────────────

class StatusPanel(tk.Frame):
    """
    Scrollable list showing each artist and their per-site download status
    using coloured circle emoji.
    """
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=ENTRY_BG, **kwargs)
        self._artist_labels = {}   # artist_name → tk.Label (the artist row)
        self._site_labels   = {}   # (artist_name, site, url) → tk.Label

        canvas = tk.Canvas(self, bg=ENTRY_BG, highlightthickness=0)
        sb     = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        register_scroll_canvas(canvas)

        self._inner = tk.Frame(canvas, bg=ENTRY_BG)
        win = canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

    def populate(self, jobs):
        """Build the status tree from a list of jobs."""
        for w in self._inner.winfo_children():
            w.destroy()
        self._artist_labels.clear()
        self._site_labels.clear()

        # Group jobs by artist
        from collections import OrderedDict
        by_artist = OrderedDict()
        for job in jobs:
            by_artist.setdefault(job["artist"], []).append(job)

        for artist_name, artist_jobs in by_artist.items():
            # Artist row
            arow = tk.Frame(self._inner, bg=ENTRY_BG)
            arow.pack(fill="x", padx=6, pady=(6, 1))
            lbl = tk.Label(arow,
                           text=f"{CIRCLE_GRAY}  {artist_name}",
                           bg=ENTRY_BG, fg=FG,
                           font=("Courier New", 10, "bold"),
                           anchor="w")
            lbl.pack(fill="x")
            self._artist_labels[artist_name] = lbl

            # Site rows (indented)
            for job in artist_jobs:
                key  = (job["artist"], job["site"], job["url"])
                label_text = f"    {CIRCLE_GRAY}  {job['site']}  —  {job['url']}"
                slbl = tk.Label(self._inner,
                                text=label_text,
                                bg=ENTRY_BG, fg=FG_DIM,
                                font=FONT_MONO, anchor="w")
                slbl.pack(fill="x", padx=6)
                self._site_labels[key] = slbl

    def set_site_status(self, artist, site, url, status):
        """status: 'pending' | 'running' | 'ok' | 'fail'"""
        key = (artist, site, url)
        lbl = self._site_labels.get(key)
        if not lbl:
            return
        circle, color = {
            "pending": (CIRCLE_GRAY,   FG_DIM),
            "running": (CIRCLE_ORANGE, "#f5a623"),
            "ok":      (CIRCLE_GREEN,  "#7ec87e"),
            "fail":    (CIRCLE_RED,    "#e06c6c"),
        }[status]
        lbl.config(text=f"    {circle}  {site}  —  {url}", fg=color)

    def set_artist_status(self, artist, status):
        lbl = self._artist_labels.get(artist)
        if not lbl:
            return
        circle, color = {
            "pending": (CIRCLE_GRAY,   FG),
            "running": (CIRCLE_ORANGE, "#f5a623"),
            "ok":      (CIRCLE_GREEN,  "#7ec87e"),
            "fail":    (CIRCLE_RED,    "#e06c6c"),
        }[status]
        lbl.config(text=f"{circle}  {artist}", fg=color)

    def clear(self):
        for w in self._inner.winfo_children():
            w.destroy()
        self._artist_labels.clear()
        self._site_labels.clear()


# ── Download controller ────────────────────────────────────────────────────────

class DownloadController:
    """
    Manages running gallery-dl jobs, updating the UI via the tkinter event loop.
    All UI mutations are posted through root.after() so they're thread-safe.
    """
    def __init__(self, root, log_widget, status_panel, progress_var,
                 progress_label, start_btn, stop_btn):
        self.root           = root
        self.log_widget     = log_widget
        self.status_panel   = status_panel
        self.progress_var   = progress_var
        self.progress_label = progress_label
        self.start_btn      = start_btn
        self.stop_btn       = stop_btn

        self._log_q    = queue.Queue()
        self._stop_evt = threading.Event()
        self._thread   = None

    # ── public API ─────────────────────────────────────────────────────────────

    def start(self, jobs, parallel):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._set_ui_running(True)
        self.progress_var.set(0)
        self.progress_label.config(text=f"0 / {len(jobs)}")

        self._thread = threading.Thread(
            target=self._run, args=(jobs, parallel), daemon=True
        )
        self._thread.start()
        self.root.after(50, self._poll_log)

    def stop(self):
        self._stop_evt.set()

    # ── internals ──────────────────────────────────────────────────────────────

    def _run(self, jobs, parallel):
        total = len(jobs)
        done  = [0]  # mutable counter shared across threads

        # Track per-artist fail counts
        artist_results = {}   # artist → {"ok": int, "fail": int}
        for job in jobs:
            artist_results.setdefault(job["artist"], {"ok": 0, "fail": 0})

        def run_one(job):
            if self._stop_evt.is_set():
                return job, None   # cancelled

            artist = job["artist"]
            site   = job["site"]
            url    = job["url"]

            self._log_q.put(("status_site",   (artist, site, url, "running")))
            self._log_q.put(("status_artist", (artist, "running")))

            success = run_gallery_dl(job, self._log_q)

            site_status   = "ok"   if success else "fail"
            self._log_q.put(("status_site", (artist, site, url, site_status)))

            done[0] += 1
            self._log_q.put(("progress", (done[0], total)))

            return job, success

        if parallel:
            with ThreadPoolExecutor() as executor:
                futures = {executor.submit(run_one, job): job for job in jobs}
                for future in as_completed(futures):
                    if self._stop_evt.is_set():
                        break
                    job, success = future.result()
                    if success is not None:
                        if success:
                            artist_results[job["artist"]]["ok"]   += 1
                        else:
                            artist_results[job["artist"]]["fail"] += 1
        else:
            for job in jobs:
                if self._stop_evt.is_set():
                    break
                job, success = run_one(job)
                if success is not None:
                    if success:
                        artist_results[job["artist"]]["ok"]   += 1
                    else:
                        artist_results[job["artist"]]["fail"] += 1

        # Final artist statuses
        for artist, counts in artist_results.items():
            if counts["fail"] > 0:
                self._log_q.put(("status_artist", (artist, "fail")))
            elif counts["ok"] > 0:
                self._log_q.put(("status_artist", (artist, "ok")))

        self._log_q.put(("done", None))

    def _poll_log(self):
        try:
            while True:
                msg_type, payload = self._log_q.get_nowait()

                if msg_type == "log":
                    self._append_log(payload)

                elif msg_type == "status_site":
                    artist, site, url, status = payload
                    self.status_panel.set_site_status(artist, site, url, status)

                elif msg_type == "status_artist":
                    artist, status = payload
                    self.status_panel.set_artist_status(artist, status)

                elif msg_type == "progress":
                    done, total = payload
                    pct = int(done / total * 100) if total else 0
                    self.progress_var.set(pct)
                    self.progress_label.config(text=f"{done} / {total}")

                elif msg_type == "done":
                    self._set_ui_running(False)
                    self._append_log("\n── All downloads finished ──\n")
                    return   # stop polling

        except queue.Empty:
            pass

        self.root.after(50, self._poll_log)

    def _append_log(self, text):
        self.log_widget.config(state="normal")
        self.log_widget.insert("end", text)
        self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    def _set_ui_running(self, running):
        if running:
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
        else:
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")


# ── Tab 1 — Downloader ─────────────────────────────────────────────────────────

def build_downloader(parent):
    canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    scroll_frame = tk.Frame(canvas, bg=PANEL)

    scroll_frame.bind("<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    register_scroll_canvas(canvas)

    # ── Header ──
    tk.Frame(scroll_frame, bg=PANEL, height=30).pack()
    tk.Label(scroll_frame, text="IMAGE DOWNLOADER", bg=PANEL, fg=FG,
             font=FONT_HEAD, anchor="w").pack(fill="x", padx=30)
    tk.Label(scroll_frame, text="fetch sources via gallery-dl",
             bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w"
             ).pack(fill="x", padx=32, pady=(0, 10))
    divider(scroll_frame)

    # ── Config file ──
    section_label(scroll_frame, "CONFIG FILE")
    cfg_row = tk.Frame(scroll_frame, bg=PANEL)
    cfg_row.pack(fill="x", padx=30, pady=(0, 6))
    cfg_frame, cfg_entry = styled_entry(cfg_row, DEFAULT_CONFIG, width=52)
    cfg_frame.pack(side="left", padx=(0, 8), fill="x", expand=True)

    def browse_config():
        path = filedialog.askopenfilename(
            title="Select config file",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")]
        )
        if path:
            cfg_entry.config(fg=FG)
            cfg_entry.delete(0, "end")
            cfg_entry.insert(0, path)

    styled_button(cfg_row, "Browse…", command=browse_config).pack(side="left")

    # ── Catalogue file ──
    section_label(scroll_frame, "ARTIST CATALOGUE")
    cat_row = tk.Frame(scroll_frame, bg=PANEL)
    cat_row.pack(fill="x", padx=30, pady=(0, 6))
    cat_frame, cat_entry = styled_entry(cat_row, DEFAULT_CATALOGUE, width=52)
    cat_frame.pack(side="left", padx=(0, 8), fill="x", expand=True)

    # Artist list
    section_label(scroll_frame, "ARTISTS")
    artist_list = ArtistList(scroll_frame)
    artist_list.pack(fill="both", expand=True, pady=(0, 4))

    def browse_catalogue():
        path = filedialog.askopenfilename(
            title="Select artists catalogue",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")]
        )
        if path:
            cat_entry.config(fg=FG)
            cat_entry.delete(0, "end")
            cat_entry.insert(0, path)
            artist_list.load(load_artists(path))

    def reload_catalogue():
        artist_list.load(load_artists(cat_entry.get()))

    styled_button(cat_row, "Browse…", command=browse_catalogue).pack(side="left", padx=(0, 6))
    styled_button(cat_row, "↺ Reload", command=reload_catalogue,
                  bg="#2a2a38", hov="#3a3a50").pack(side="left")

    if os.path.exists(DEFAULT_CATALOGUE):
        artist_list.load(load_artists(DEFAULT_CATALOGUE))

    # ── Output directory ──
    section_label(scroll_frame, "OUTPUT DIRECTORY")
    dir_row = tk.Frame(scroll_frame, bg=PANEL)
    dir_row.pack(fill="x", padx=30, pady=(0, 6))
    dir_frame, dir_entry = styled_entry(dir_row, "./download", width=50)
    dir_frame.pack(side="left", padx=(0, 8))

    def browse_dir():
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            dir_entry.config(fg=FG)
            dir_entry.delete(0, "end")
            dir_entry.insert(0, path)

    styled_button(dir_row, "Browse…", command=browse_dir).pack(side="left")

    # ── Execution mode toggle ──
    section_label(scroll_frame, "EXECUTION MODE")
    mode_row = tk.Frame(scroll_frame, bg=PANEL)
    mode_row.pack(fill="x", padx=30, pady=(0, 6))
    parallel_var = tk.BooleanVar(value=False)

    def update_mode_label():
        if parallel_var.get():
            mode_lbl.config(text="Parallel  (all jobs run simultaneously)", fg=ACCENT)
        else:
            mode_lbl.config(text="Sequential  (one job at a time)", fg=FG_DIM)

    seq_btn = tk.Radiobutton(mode_row, text="Sequential", variable=parallel_var, value=False,
                              bg=PANEL, fg=FG, selectcolor=SEL_BG,
                              activebackground=PANEL, activeforeground=ACCENT,
                              font=FONT_BODY, command=update_mode_label)
    par_btn = tk.Radiobutton(mode_row, text="Parallel",   variable=parallel_var, value=True,
                              bg=PANEL, fg=FG, selectcolor=SEL_BG,
                              activebackground=PANEL, activeforeground=ACCENT,
                              font=FONT_BODY, command=update_mode_label)
    seq_btn.pack(side="left", padx=(0, 20))
    par_btn.pack(side="left")
    mode_lbl = tk.Label(mode_row, text="Sequential  (one job at a time)",
                        bg=PANEL, fg=FG_DIM, font=("Courier New", 9, "italic"))
    mode_lbl.pack(side="left", padx=(20, 0))

    # ── Progress bar ──
    section_label(scroll_frame, "PROGRESS")
    prog_outer = tk.Frame(scroll_frame, bg=PANEL)
    prog_outer.pack(fill="x", padx=30, pady=(0, 6))

    progress_var = tk.IntVar(value=0)
    style_name   = "Download.Horizontal.TProgressbar"

    prog_bar = ttk.Progressbar(prog_outer, variable=progress_var,
                                maximum=100, length=400,
                                style=style_name)
    prog_bar.pack(side="left", fill="x", expand=True, padx=(0, 12))
    prog_lbl = tk.Label(prog_outer, text="0 / 0",
                        bg=PANEL, fg=FG_DIM, font=FONT_MONO, width=10)
    prog_lbl.pack(side="left")

    divider(scroll_frame)

    # ── Bottom: status panel (left) + log (right) ──
    bottom = tk.Frame(scroll_frame, bg=PANEL)
    bottom.pack(fill="both", expand=True, padx=30, pady=(0, 10))
    bottom.columnconfigure(0, weight=1)
    bottom.columnconfigure(1, weight=2)

    # Status panel
    tk.Label(bottom, text="DOWNLOAD STATUS", bg=PANEL, fg=ACCENT2,
             font=("Courier New", 9, "bold"), anchor="w"
             ).grid(row=0, column=0, sticky="w", pady=(10, 4))
    status_panel = StatusPanel(bottom)
    status_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
    bottom.rowconfigure(1, weight=1)

    # Log
    tk.Label(bottom, text="OUTPUT LOG", bg=PANEL, fg=ACCENT2,
             font=("Courier New", 9, "bold"), anchor="w"
             ).grid(row=0, column=1, sticky="w", pady=(10, 4))
    log_frame = tk.Frame(bottom, bg=BORDER, padx=1, pady=1)
    log_frame.grid(row=1, column=1, sticky="nsew")
    log_text = tk.Text(log_frame, bg="#0a0a10", fg=FG_DIM, insertbackground=ACCENT,
                       relief="flat", font=FONT_MONO, height=16,
                       wrap="word", state="disabled")
    log_sb = ttk.Scrollbar(log_frame, command=log_text.yview)
    log_text.configure(yscrollcommand=log_sb.set)
    log_sb.pack(side="right", fill="y")
    log_text.pack(fill="both", expand=True, padx=4, pady=4)

    # ── Action buttons ──
    action_row = tk.Frame(scroll_frame, bg=PANEL)
    action_row.pack(fill="x", padx=30, pady=(10, 20))

    # Forward declarations so controller can reference buttons
    start_btn_holder = [None]
    stop_btn_holder  = [None]

    controller_holder = [None]

    def get_root(widget):
        return widget.winfo_toplevel()

    def on_start(select_all=False):
        if select_all:
            artist_list._select_all()
        selected = artist_list.get_selected()
        if not selected:
            messagebox.showwarning("No selection", "Please select at least one artist.")
            return
        cfg       = load_config(cfg_entry.get())
        dl_sites  = set(cfg.get("downloadable_sites", []))
        if not dl_sites:
            messagebox.showwarning("No sites", "No downloadable_sites configured in config.yaml.")
            return
        base_dir  = dir_entry.get().strip() or "./download"
        jobs      = build_jobs(selected, dl_sites, base_dir)
        if not jobs:
            messagebox.showinfo("Nothing to do",
                "No matching downloadable sites found for the selected artists.")
            return
        status_panel.populate(jobs)
        progress_var.set(0)
        controller_holder[0].start(jobs, parallel=parallel_var.get())

    def on_stop():
        if controller_holder[0]:
            controller_holder[0].stop()

    start_btn = styled_button(action_row, "▶  Download Selected",
                               command=lambda: on_start(False))
    start_btn.pack(side="left", padx=(0, 10))

    all_btn = styled_button(action_row, "⬇  Download All",
                             command=lambda: on_start(True),
                             bg="#4a3a8a", hov="#5a4a9a")
    all_btn.pack(side="left", padx=(0, 10))

    stop_btn = styled_button(action_row, "■  Stop",
                              command=on_stop,
                              bg="#3a2a2a", hov="#5a3a3a")
    stop_btn.config(state="disabled")
    stop_btn.pack(side="left")

    start_btn_holder[0] = start_btn
    stop_btn_holder[0]  = stop_btn

    # Wire up the controller now that all widgets exist
    root_win = parent.winfo_toplevel()
    controller_holder[0] = DownloadController(
        root=root_win,
        log_widget=log_text,
        status_panel=status_panel,
        progress_var=progress_var,
        progress_label=prog_lbl,
        start_btn=start_btn,
        stop_btn=stop_btn,
    )


# ── Tab 2 — Uploader ───────────────────────────────────────────────────────────

def build_uploader(parent):
    canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    scroll_frame = tk.Frame(canvas, bg=PANEL)

    scroll_frame.bind("<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    register_scroll_canvas(canvas)

    tk.Frame(scroll_frame, bg=PANEL, height=30).pack()
    tk.Label(scroll_frame, text="IMAGE UPLOADER", bg=PANEL, fg=FG,
             font=FONT_HEAD, anchor="w").pack(fill="x", padx=30)
    tk.Label(scroll_frame, text="push images to your booru docker instance",
             bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w"
             ).pack(fill="x", padx=32, pady=(0, 10))
    divider(scroll_frame)

    section_label(scroll_frame, "BOORU SERVER")
    ef, _ = styled_entry(scroll_frame, "http://localhost:3000", width=50)
    ef.pack(fill="x", padx=30, pady=(0, 6))

    section_label(scroll_frame, "AUTHENTICATION")
    auth_row = tk.Frame(scroll_frame, bg=PANEL)
    auth_row.pack(fill="x", padx=30, pady=(0, 6))
    ef_u, _ = styled_entry(auth_row, "username", width=22)
    ef_u.pack(side="left", padx=(0, 8))
    ef_p, _ = styled_entry(auth_row, "api_key / password", width=28)
    ef_p.pack(side="left")

    section_label(scroll_frame, "IMAGE SOURCE")
    src_row = tk.Frame(scroll_frame, bg=PANEL)
    src_row.pack(fill="x", padx=30, pady=(0, 6))
    ef2, _ = styled_entry(src_row, "/home/user/images/", width=50)
    ef2.pack(side="left", padx=(0, 8))
    styled_button(src_row, "Browse…").pack(side="left")

    section_label(scroll_frame, "DEFAULT TAGS")
    ef3, _ = styled_entry(scroll_frame, "tag1 tag2 tag3 ...", width=60)
    ef3.pack(fill="x", padx=30, pady=(0, 6))

    section_label(scroll_frame, "OPTIONS")
    opts_frame = tk.Frame(scroll_frame, bg=PANEL)
    opts_frame.pack(fill="x", padx=30, pady=(0, 6))
    for i, (label, val) in enumerate([
        ("Skip duplicates", True), ("Upload recursively", False),
        ("Auto-tag via AI", False), ("Set rating: safe", True),
    ]):
        tk.Checkbutton(
            opts_frame, text=label, variable=tk.BooleanVar(value=val),
            bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
            activebackground=PANEL, activeforeground=ACCENT, font=FONT_BODY
        ).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 30), pady=3)

    divider(scroll_frame)

    status_frame = tk.Frame(scroll_frame, bg=ENTRY_BG)
    status_frame.pack(fill="x", padx=30, pady=(10, 6))
    tk.Label(status_frame,
             text="  0 / 0 images uploaded   |   0 skipped   |   0 failed",
             bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO, anchor="w"
             ).pack(side="left", padx=8, pady=6)

    action_row = tk.Frame(scroll_frame, bg=PANEL)
    action_row.pack(fill="x", padx=30, pady=(10, 6))
    styled_button(action_row, "▶  Start Upload").pack(side="left", padx=(0, 10))
    styled_button(action_row, "■  Stop", bg="#3a2a2a", hov="#5a3a3a").pack(side="left")

    section_label(scroll_frame, "OUTPUT LOG")
    log_frame = tk.Frame(scroll_frame, bg=BORDER, padx=1, pady=1)
    log_frame.pack(fill="both", expand=True, padx=30, pady=(4, 20))
    log_text = tk.Text(log_frame, bg="#0a0a10", fg=FG_DIM, insertbackground=ACCENT,
                       relief="flat", font=FONT_MONO, height=8,
                       wrap="word", state="disabled")
    log_sb = ttk.Scrollbar(log_frame, command=log_text.yview)
    log_text.configure(yscrollcommand=log_sb.set)
    log_sb.pack(side="right", fill="y")
    log_text.pack(fill="both", expand=True, padx=4, pady=4)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    root.title("Booru Manager")
    root.geometry("980x780")
    root.configure(bg=BG)
    root.minsize(720, 560)

    style = ttk.Style(root)
    style.theme_use("default")
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=[0, 0, 0, 0])
    style.configure("TNotebook.Tab", background=BG, foreground=FG_DIM,
                    font=("Courier New", 10, "bold"), padding=[20, 10], borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", PANEL), ("active", ENTRY_BG)],
              foreground=[("selected", ACCENT), ("active", FG)])
    style.configure("TScrollbar", background=BORDER, troughcolor=PANEL,
                    borderwidth=0, arrowsize=12)
    # Progress bar style
    style.configure("Download.Horizontal.TProgressbar",
                    troughcolor=ENTRY_BG, background=ACCENT,
                    borderwidth=0, thickness=14)

    title_bar = tk.Frame(root, bg=BG, pady=12)
    title_bar.pack(fill="x", padx=30)
    tk.Label(title_bar, text="✦ BOORU MANAGER",
             bg=BG, fg=ACCENT, font=("Courier New", 13, "bold")).pack(side="left")
    tk.Label(title_bar, text="personal image database toolkit",
             bg=BG, fg=FG_DIM, font=("Courier New", 9, "italic")
             ).pack(side="left", padx=(12, 0))

    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    tab_dl = tk.Frame(nb, bg=PANEL)
    tab_up = tk.Frame(nb, bg=PANEL)
    nb.add(tab_dl, text="  ↓  Downloader  ")
    nb.add(tab_up, text="  ↑  Uploader  ")

    build_downloader(tab_dl)
    build_uploader(tab_up)

    root.bind_all("<MouseWheel>", _global_scroll)
    root.bind_all("<Button-4>",   _global_scroll)
    root.bind_all("<Button-5>",   _global_scroll)

    root.mainloop()


if __name__ == "__main__":
    main()