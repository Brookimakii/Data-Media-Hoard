"""
ui/artists_page.py
------------------
Artists management, as three screens inside one page:

    LIST      search · ＋ Add artist · Undo/Redo · Import        (paged, no scroll)
      │ click an artist                         │ ＋ Add artist
      ▼                                         ▼
    DETAIL    read-only card + Edit · Delete · History · Copy   FORM  create / edit
      │ Edit ───────────────────────────────────▶ │ Save / Cancel
      ◀──────────────────────────────────────────┘

No scrollbars, with one exception: the notes box (the artist's wiki text).
The list is paged and sizes itself to the window height.

All data goes through core.artist_store (local DB, history, undo/redo).
Every change is pushed to the booru server automatically (core.artist_sync); each
artist shows its state: ✓ synced · ● pending · ⟳ syncing · ✕ failed (with a Retry
button). The server login lives in the ⚙ settings (ui/booru_settings_ui.py).
"""

from __future__ import annotations

import logging
import math
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable
from urllib.parse import urlparse

from core.artist_store import ArtistError, ArtistStore, artists_from_yaml
from core.artist_sync import ArtistSync
from core.file_db import get_db
from ui.theme import (
    ACCENT, ACCENT2, BORDER, BTN_BG, BTN_FG, BTN_HOV, COLOR_FAIL, COLOR_OK,
    ENTRY_BG, FG, FG_DIM, FONT_BTN, FONT_SUB, PANEL, SEL_BG,
)

log = logging.getLogger(__name__)

_UI = FONT_BTN[0]
_ROW_H = 40
_ROW_GAP = 4
_HIST_ROWS = 6

# Column layout of the list (fractions of the row width)
_COL_NAME = (0.00, 0.37)
_COL_GROUP = (0.37, 0.25)
_COL_LINKS = (0.62, 0.17)
_COL_SYNC = (0.79, 0.21)

_AMBER = "#d9a441"
_POLL_MS = 400

# status -> (short text, long text, colour)
_STATUS = {
    "synced": ("✓ synced", "In sync with the server", COLOR_OK),
    "pending": ("● pending", "Waiting to be pushed to the server", _AMBER),
    "syncing": ("⟳ syncing…", "Pushing to the server…", ACCENT),
    "failed": ("✕ failed", "Push failed", COLOR_FAIL),
}


# ── Small widgets ──────────────────────────────────────────────────────────────

class _Button(tk.Label):
    _KINDS = {
        "primary": (BTN_BG, BTN_HOV, BTN_FG),
        "ghost": (ENTRY_BG, SEL_BG, FG),
        "danger": (ENTRY_BG, "#5a2530", COLOR_FAIL),
    }

    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None],
                 kind: str = "ghost") -> None:
        self._c_bg, self._c_hover, self._c_fg = self._KINDS[kind]
        super().__init__(parent, text=text, bg=self._c_bg, fg=self._c_fg, font=FONT_BTN,
                         padx=14, pady=6, cursor="hand2")
        self._cb = command
        self._on = True
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda _e: self._on and self.config(bg=self._c_hover))
        self.bind("<Leave>", lambda _e: self.config(bg=self._c_bg))

    def _click(self, _e: tk.Event) -> None:
        if self._on:
            self._cb()

    def set_enabled(self, on: bool) -> None:
        self._on = on
        self.config(fg=self._c_fg if on else FG_DIM, bg=self._c_bg,
                    cursor="hand2" if on else "arrow")


def _entry(parent: tk.Misc, var: tk.StringVar) -> tk.Entry:
    return tk.Entry(parent, textvariable=var, bg=ENTRY_BG, fg=FG, insertbackground=FG,
                    relief="flat", highlightthickness=1, highlightbackground=BORDER,
                    highlightcolor=ACCENT, font=(_UI, 10))


def _text(parent: tk.Misc, height: int | None = None) -> tk.Text:
    kw = {"height": height} if height else {}
    return tk.Text(parent, bg=ENTRY_BG, fg=FG, insertbackground=FG, relief="flat",
                   highlightthickness=1, highlightbackground=BORDER,
                   highlightcolor=ACCENT, font=(_UI, 10), wrap="word", undo=True,
                   padx=6, pady=4, **kw)


def _caption(parent: tk.Misc, text: str) -> tk.Label:
    return tk.Label(parent, text=text, bg=PANEL, fg=FG_DIM, font=(_UI, 9), anchor="w")


def _host(url: str) -> str:
    host = urlparse(url if "//" in url else "//" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else (host or url[:24])


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


# ── Page ───────────────────────────────────────────────────────────────────────

class ArtistsPage(tk.Frame):
    def __init__(self, parent: tk.Misc, store: ArtistStore, yaml_path: Path,
                 sync: ArtistSync | None = None) -> None:
        super().__init__(parent, bg=PANEL)
        self._store = store
        self._sync = sync
        self._last_busy: set[int] = set()
        self._ticks = 0
        self._cfg_seen: tuple | None = None
        self._yaml_path = Path(yaml_path)
        self._selected: int | None = None
        self._page_no = 0
        self._page_size = 10
        self._loaded_raw: tuple = ()
        self._dirty = False
        self._msg_job: str | None = None
        self._view_name = "list"
        self._link_tags: list[str] = []

        self._q = tk.StringVar()
        self._v_name = tk.StringVar()
        self._v_other = tk.StringVar()
        self._v_group = tk.StringVar()
        self._v_active = tk.BooleanVar(value=True)

        self._build_status_bar()
        self._list_view = tk.Frame(self, bg=PANEL)
        self._detail_view = tk.Frame(self, bg=PANEL)
        self._form_view = tk.Frame(self, bg=PANEL)
        self._build_list_view(self._list_view)
        self._build_detail_view(self._detail_view)
        self._build_form_view(self._form_view)

        self._q.trace_add("write", lambda *_: self._on_search())
        for var in (self._v_name, self._v_other, self._v_group, self._v_active):
            var.trace_add("write", lambda *_: self._check_dirty())

        self._refresh_list()
        self._show("list")
        self._update_sync_ui()
        self._kick()                       # push whatever was left pending last time
        self.after(_POLL_MS, self._poll)

    # ── Screen switching ──────────────────────────────────────────────────────

    def _show(self, name: str) -> None:
        views = {"list": self._list_view, "detail": self._detail_view, "form": self._form_view}
        for v in views.values():
            v.pack_forget()
        views[name].pack(fill="both", expand=True)
        self._view_name = name
        self._update_hint()

    def _go_list(self, focus_id: int | None = None) -> None:
        """Back to the list; when `focus_id` is given, show the page that holds it."""
        if focus_id is not None and not self._q.get().strip():
            try:
                self._page_no = self._store.rank(focus_id) // self._page_size
            except ArtistError:
                pass
        self._refresh_list()
        self._show("list")

    # ── Shared bottom bar ─────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(side="bottom", fill="x", padx=24, pady=(0, 12))
        self._hint = tk.Label(bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w")
        self._hint.pack(side="left")
        self._status = tk.Label(bar, text="", bg=PANEL, fg=COLOR_OK, font=(_UI, 10), anchor="e")
        self._status.pack(side="right")

    def _update_hint(self) -> None:
        undo, redo = self._store.undo_label(), self._store.redo_label()
        self._btn_undo.set_enabled(bool(undo))
        self._btn_redo.set_enabled(bool(redo))
        parts = []
        if self._view_name == "list":
            if undo:
                parts.append(f"Undo: {undo}")
            if redo:
                parts.append(f"Redo: {redo}")
            if self._sync is not None:
                cfg = self._sync.settings()
                if not cfg.configured:
                    parts.append("Push is off: set the server login in ⚙ (top right).")
                elif not cfg.auto_push:
                    parts.append("Auto-push is off.")
        self._hint.config(text="    ".join(parts))

    def _say(self, text: str, error: bool = False) -> None:
        self._status.config(text=text, fg=COLOR_FAIL if error else COLOR_OK)
        if self._msg_job is not None:
            self.after_cancel(self._msg_job)
        self._msg_job = self.after(6000, lambda: self._status.config(text=""))

    # ── Sync status, polling, retry ───────────────────────────────────────────

    def _status_of(self, artist: dict, busy: set[int]) -> tuple[str, str, str]:
        key = "syncing" if artist["id"] in busy else artist["sync_status"]
        return _STATUS.get(key, _STATUS["pending"])

    def _kick(self) -> None:
        """Ask the sync engine to push whatever is pending (call after every change)."""
        if self._sync is not None:
            self._sync.request_pending()
            self._update_sync_ui()

    def _update_sync_ui(self) -> None:
        """Toolbar retry button + footer hint."""
        self._update_hint()
        if self._sync is None:
            return
        summary = self._store.sync_summary()
        cfg = self._sync.settings()
        if summary["failed"]:
            text, self._retry_mode = f"⟳  Retry failed ({summary['failed']})", "retry"
        elif summary["pending"] and cfg.configured and not cfg.auto_push:
            text, self._retry_mode = f"⟳  Push pending ({summary['pending']})", "push"
        else:
            text, self._retry_mode = "", ""
        if text:
            self._btn_retry_all.config(text=text)
            if not self._btn_retry_all.winfo_ismapped():
                self._btn_retry_all.pack(side="right", padx=(0, 8), before=self._btn_undo)
        else:
            self._btn_retry_all.pack_forget()

    def _retry_all(self) -> None:
        if self._sync is None:
            return
        if self._retry_mode == "retry":
            self._sync.retry_failed()
        else:
            self._sync.push_now()
        self._update_sync_ui()
        self._repaint_sync()

    def _retry_one(self) -> None:
        if self._sync is None or self._selected is None:
            return
        if self._store.get(self._selected)["sync_status"] == "failed":
            self._sync.retry(self._selected)
        else:
            self._sync.push_now()
        self._repaint_sync()

    def _repaint_sync(self) -> None:
        if self._view_name == "list":
            self._refresh_list()
        elif self._view_name == "detail":
            self._refresh_detail_status()

    def _poll(self) -> None:
        """UI-thread timer: pick up finished pushes and repaint."""
        if not self.winfo_exists():
            return
        try:
            if self._sync is not None:
                self._ticks += 1
                if self._ticks % 5 == 0:                  # ~2s: did the login settings change?
                    cfg = self._sync.settings()
                    seen = (cfg.configured, cfg.auto_push, cfg.server)
                    if self._cfg_seen is not None and seen != self._cfg_seen:
                        self._kick()
                    self._cfg_seen = seen
                results = self._sync.poll()
                busy = self._sync.syncing_ids()
                if results or busy != self._last_busy:
                    self._last_busy = busy
                    self._repaint_sync()
                    self._update_sync_ui()
                    failed = [r for r in results if not r.ok]
                    if failed:
                        self._say(f"Push failed: {failed[-1].message}", error=True)
        except Exception:                                  # never kill the timer
            log.exception("artists page: poll failed")
        finally:
            self.after(_POLL_MS, self._poll)

    # ══ LIST screen ═══════════════════════════════════════════════════════════

    def _build_list_view(self, view: tk.Frame) -> None:
        bar = tk.Frame(view, bg=PANEL)
        bar.pack(side="top", fill="x", padx=24, pady=(18, 12))

        box = tk.Frame(bar, bg=ENTRY_BG, highlightthickness=1, highlightbackground=BORDER)
        box.pack(side="left")
        tk.Label(box, text="🔍", bg=ENTRY_BG, fg=FG_DIM, font=(_UI, 10)).pack(side="left", padx=(8, 0))
        tk.Entry(box, textvariable=self._q, width=30, bg=ENTRY_BG, fg=FG, insertbackground=FG,
                 relief="flat", font=(_UI, 10)).pack(side="left", padx=6, pady=6)

        self._retry_mode = ""
        self._btn_retry_all = _Button(bar, "", self._retry_all, "danger")   # shown only when needed
        _Button(bar, "＋  Add artist", self._add, "primary").pack(side="right")
        self._btn_import = _Button(bar, "Import artists.yaml", self._import_yaml)
        self._btn_import.pack(side="right", padx=(0, 8))
        self._btn_redo = _Button(bar, "↷ Redo", self._redo)
        self._btn_redo.pack(side="right", padx=(0, 8))
        self._btn_undo = _Button(bar, "↶ Undo", self._undo)
        self._btn_undo.pack(side="right", padx=(0, 8))

        pager = tk.Frame(view, bg=PANEL)
        pager.pack(side="bottom", fill="x", padx=24, pady=(8, 8))
        self._prev = _Button(pager, "‹", lambda: self._flip(-1))
        self._prev.pack(side="left")
        self._next = _Button(pager, "›", lambda: self._flip(1))
        self._next.pack(side="right")
        self._pager = tk.Label(pager, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 10))
        self._pager.pack(expand=True)

        # Column captions
        head = tk.Frame(view, bg=PANEL, height=22)
        head.pack(side="top", fill="x", padx=24)
        for title, (x, w) in (("NAME", _COL_NAME), ("GROUP", _COL_GROUP),
                              ("LINKS", _COL_LINKS), ("SERVER", _COL_SYNC)):
            tk.Label(head, text=title, bg=PANEL, fg=FG_DIM, font=(_UI, 8, "bold"), anchor="w",
                     padx=14).place(relx=x, rely=0, relwidth=w, relheight=1)
        self._count = tk.Label(head, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 8), anchor="e")
        self._count.place(relx=0.4, rely=0, relwidth=0.2, relheight=1)

        self._rows = tk.Frame(view, bg=PANEL)
        self._rows.pack(side="top", fill="both", expand=True, padx=24)
        self._rows.pack_propagate(False)
        self._rows.bind("<Configure>", self._on_rows_resize)

    def _on_rows_resize(self, e: tk.Event) -> None:
        size = max(3, e.height // (_ROW_H + _ROW_GAP))
        if size != self._page_size:
            self._page_size = size
            self.after_idle(self._refresh_list)

    def _refresh_list(self) -> None:
        query = self._q.get()
        total = self._store.count(query)
        pages = max(1, math.ceil(total / self._page_size))
        self._page_no = max(0, min(self._page_no, pages - 1))
        rows = self._store.search(query, self._page_size, self._page_no * self._page_size)

        for w in self._rows.winfo_children():
            w.destroy()
        busy = self._sync.syncing_ids() if self._sync is not None else set()
        for artist in rows:
            self._add_row(artist, busy)
        if not rows:
            msg = ("No match." if query.strip() else
                   "No artists yet.\nClick “＋ Add artist”, or import your artists.yaml.")
            tk.Label(self._rows, text=msg, bg=PANEL, fg=FG_DIM, font=FONT_SUB,
                     justify="center").pack(pady=40)

        self._count.config(text=f"{total} artist{'' if total == 1 else 's'}")
        self._pager.config(text=f"{self._page_no + 1} / {pages}")
        self._prev.set_enabled(self._page_no > 0)
        self._next.set_enabled(self._page_no < pages - 1)

    def _add_row(self, a: dict, busy: set[int]) -> None:
        row = tk.Frame(self._rows, bg=ENTRY_BG, height=_ROW_H, cursor="hand2",
                       highlightthickness=1, highlightbackground=BORDER)
        row.pack(fill="x", pady=(0, _ROW_GAP))

        n = len(a["urls"])
        st_text, _long, st_color = self._status_of(a, busy)
        cells = [
            (a["name"], _COL_NAME, FG, (_UI, 11, "bold")),
            (a["group_name"] or "—", _COL_GROUP, FG_DIM, (_UI, 10)),
            (f"{n} link{'' if n == 1 else 's'}" + ("" if a["is_active"] else " · off"),
             _COL_LINKS, FG_DIM, (_UI, 10)),
            (st_text, _COL_SYNC, st_color, (_UI, 10, "bold")),
        ]
        labels = []
        for text, (x, w), fg, font in cells:
            lbl = tk.Label(row, text=text, bg=ENTRY_BG, fg=fg, font=font, anchor="w", padx=14)
            lbl.place(relx=x, rely=0, relwidth=w, relheight=1)
            labels.append(lbl)

        widgets = (row, *labels)
        for w in widgets:
            w.bind("<Button-1>", lambda _e, i=a["id"]: self._open_detail(i))
            w.bind("<Enter>", lambda _e, ws=widgets: [x.config(bg=SEL_BG) for x in ws])
            w.bind("<Leave>", lambda _e, ws=widgets: [x.config(bg=ENTRY_BG) for x in ws])

    def _flip(self, step: int) -> None:
        self._page_no += step
        self._refresh_list()

    def _on_search(self) -> None:
        self._page_no = 0
        self._refresh_list()

    # ── List actions ──────────────────────────────────────────────────────────

    def _add(self) -> None:
        self._selected = None
        self._f_title.config(text="New artist")
        self._btn_save.config(text="Create")
        self._fill()
        self._show("form")
        self._e_name.focus_set()

    def _undo(self) -> None:
        self._run_history(self._store.undo, "Undid", self._store.undo_label())

    def _redo(self) -> None:
        self._run_history(self._store.redo, "Redid", self._store.redo_label())

    def _run_history(self, action: Callable[[], list[int]], verb: str, label: str) -> None:
        try:
            ids = action()
        except ArtistError as e:
            self._say(str(e), error=True)
            return
        if ids:
            self._refresh_list()
            self._kick()
            self._say(f"{verb}: {label}.")

    def _import_yaml(self) -> None:
        if not self._yaml_path.exists():
            self._say(f"{self._yaml_path.name} not found next to the app.", error=True)
            return
        try:
            items = artists_from_yaml(self._yaml_path)
        except Exception as e:                   # YAMLError or malformed content
            self._say(f"Could not read {self._yaml_path.name}: {e}", error=True)
            return
        new = sum(1 for i in items if not self._store.name_exists(str(i["name"]).strip()))
        if new == 0:
            self._say(f"Nothing to import: all {len(items)} artists already exist.")
            return
        if not messagebox.askokcancel(
                "Import artists",
                f"Import {new} new artist(s) from {self._yaml_path.name}?\n"
                f"{len(items) - new} already exist and are skipped.\n\n"
                "One Undo reverts the whole import.", parent=self):
            return
        added, skipped = self._store.import_many(items)
        self._page_no = 0
        self._kick()
        self._refresh_list()
        self._say(f"Imported {added} artist(s), skipped {skipped}.")

    # ══ DETAIL screen ═════════════════════════════════════════════════════════

    def _build_detail_view(self, view: tk.Frame) -> None:
        head = tk.Frame(view, bg=PANEL)
        head.pack(side="top", fill="x", padx=24, pady=(18, 10))
        _Button(head, "‹  Artists", self._back).pack(side="left")
        self._btn_delete = _Button(head, "Delete", self._delete, "danger")
        self._btn_delete.pack(side="right")
        _Button(head, "✎  Edit", self._edit, "primary").pack(side="right", padx=(0, 8))
        _Button(head, "History", self._open_history).pack(side="right", padx=(0, 8))
        self._btn_copy = _Button(head, "Copy links", self._copy_links)
        self._btn_copy.pack(side="right", padx=(0, 8))

        body = tk.Frame(view, bg=PANEL)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        self._title = tk.Label(body, text="", bg=PANEL, fg=FG, font=(_UI, 18, "bold"), anchor="w")
        self._title.pack(fill="x")
        self._sub = tk.Label(body, text="", bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w")
        self._sub.pack(fill="x")

        state = tk.Frame(body, bg=PANEL)
        state.pack(fill="x", pady=(8, 0))
        self._sync_lbl = tk.Label(state, text="", bg=PANEL, fg=COLOR_OK, font=(_UI, 11, "bold"), anchor="w")
        self._sync_lbl.pack(side="left")
        self._btn_retry = _Button(state, "⟳  Retry push", self._retry_one)
        self._sync_err = tk.Label(body, text="", bg=PANEL, fg=COLOR_FAIL, font=FONT_SUB,
                                  anchor="w", justify="left")
        self._sync_err.bind("<Configure>", lambda e: self._sync_err.config(wraplength=max(100, e.width)))
        self._sync_err.pack(fill="x")

        self._badge = tk.Label(body, text="", bg=PANEL, fg=COLOR_OK, font=(_UI, 9, "bold"), anchor="w")
        self._badge.pack(fill="x", pady=(6, 10))

        _caption(body, "Other names").pack(fill="x")
        self._r_other = self._wrapped_label(body)
        self._r_other.pack(fill="x", pady=(0, 8))

        _caption(body, "Group").pack(fill="x")
        self._r_group = self._wrapped_label(body)
        self._r_group.pack(fill="x", pady=(0, 8))

        _caption(body, "Links  (click to open, right-click to copy)").pack(fill="x")
        self._r_links = tk.Text(body, height=7, bg=ENTRY_BG, fg=FG, relief="flat", wrap="none",
                                highlightthickness=1, highlightbackground=BORDER,
                                font=(_UI, 10), padx=8, pady=6, cursor="arrow", tabs=("120p",))
        self._r_links.tag_configure("host", foreground=FG_DIM)
        self._r_links.tag_configure("link", foreground=ACCENT, underline=True)
        self._r_links.tag_configure("dim", foreground=FG_DIM)
        self._r_links.pack(fill="x", pady=(0, 8))

        _caption(body, "Notes").pack(fill="x")
        notes_row = tk.Frame(body, bg=PANEL)
        notes_row.pack(fill="both", expand=True)
        self._r_notes = tk.Text(notes_row, bg=ENTRY_BG, fg=FG, relief="flat", wrap="word",
                                highlightthickness=1, highlightbackground=BORDER,
                                font=(_UI, 10), padx=8, pady=6, cursor="arrow")
        bar = ttk.Scrollbar(notes_row, orient="vertical", command=self._r_notes.yview)
        self._r_notes.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")          # the one allowed scrollbar
        self._r_notes.pack(side="left", fill="both", expand=True)

    @staticmethod
    def _wrapped_label(parent: tk.Misc) -> tk.Label:
        lbl = tk.Label(parent, text="", bg=PANEL, fg=FG, font=(_UI, 11), anchor="w", justify="left")
        lbl.bind("<Configure>", lambda e: lbl.config(wraplength=max(100, e.width)))
        return lbl

    def _open_detail(self, artist_id: int) -> None:
        try:
            a = self._store.get(artist_id)
        except ArtistError as e:
            self._say(str(e), error=True)
            return
        self._selected = artist_id
        self._title.config(text=a["name"])
        self._render_sync_status(a)
        self._badge.config(text="● ACTIVE" if a["is_active"] else "○ INACTIVE",
                           fg=COLOR_OK if a["is_active"] else FG_DIM)
        self._r_other.config(text=", ".join(a["other_names"]) or "—")
        self._r_group.config(text=a["group_name"] or "—")
        self._render_links(a["urls"])
        self._set_readonly(self._r_notes, a["notes"] or "—")
        self._btn_copy.set_enabled(bool(a["urls"]))
        self._show("detail")

    def _render_sync_status(self, a: dict) -> None:
        busy = self._sync.syncing_ids() if self._sync is not None else set()
        text, long_text, color = self._status_of(a, busy)
        remote = f" · on server as #{a['remote_id']}" if a.get("remote_id") else ""
        self._sub.config(text=f"#{a['id']}{remote}")
        self._sync_lbl.config(text=f"{text}   {long_text}", fg=color)
        failed = a["sync_status"] == "failed" and a["id"] not in busy
        self._sync_err.config(text=a.get("sync_error") or "" if failed else "")
        can_push = self._sync is not None and a["id"] not in busy and a["sync_status"] in ("failed", "pending")
        if can_push:
            self._btn_retry.config(text="⟳  Retry push" if failed else "Push now")
            if not self._btn_retry.winfo_ismapped():
                self._btn_retry.pack(side="left", padx=(12, 0))
        else:
            self._btn_retry.pack_forget()

    def _refresh_detail_status(self) -> None:
        if self._selected is None:
            return
        try:
            self._render_sync_status(self._store.get(self._selected))
        except ArtistError:
            pass

    @staticmethod
    def _set_readonly(widget: tk.Text, text: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _render_links(self, urls: list[str]) -> None:
        t = self._r_links
        t.config(state="normal")
        t.delete("1.0", "end")
        for tag in self._link_tags:
            t.tag_delete(tag)
        self._link_tags = []
        if not urls:
            t.insert("end", "No links yet.", "dim")
        for i, url in enumerate(urls):
            tag = f"url{i}"
            self._link_tags.append(tag)
            t.insert("end", _host(url) + "\t", "host")
            t.insert("end", url, ("link", tag))
            t.insert("end", "\n")
            t.tag_bind(tag, "<Button-1>", lambda _e, u=url: webbrowser.open(u))
            t.tag_bind(tag, "<Button-3>", lambda _e, u=url: self._copy([u]))
            t.tag_bind(tag, "<Enter>", lambda _e: t.config(cursor="hand2"))
            t.tag_bind(tag, "<Leave>", lambda _e: t.config(cursor="arrow"))
        t.config(state="disabled")

    def _copy(self, urls: list[str]) -> None:
        self.clipboard_clear()
        self.clipboard_append("\n".join(urls))
        self._say(f"Copied {len(urls)} link{'' if len(urls) == 1 else 's'}.")

    def _copy_links(self) -> None:
        if self._selected is not None:
            urls = self._store.get(self._selected)["urls"]
            if urls:
                self._copy(urls)

    # ── Detail actions ────────────────────────────────────────────────────────

    def _back(self) -> None:
        self._go_list(self._selected)

    def _edit(self) -> None:
        if self._selected is None:
            return
        a = self._store.get(self._selected)
        self._f_title.config(text=f"Edit  ·  {a['name']}")
        self._btn_save.config(text="Save")
        self._fill(a["name"], ", ".join(a["other_names"]), a["group_name"],
                   "\n".join(a["urls"]), a["notes"], a["is_active"])
        self._show("form")
        self._e_name.focus_set()

    def _delete(self) -> None:
        if self._selected is None:
            return
        name = self._store.get(self._selected)["name"]
        if not messagebox.askyesno(
                "Delete artist",
                f"Delete '{name}'?\n\nIt stays in the history and Undo brings it back.",
                parent=self):
            return
        try:
            self._store.delete(self._selected)
        except ArtistError as e:
            self._say(str(e), error=True)
            return
        self._selected = None
        self._kick()
        self._go_list()
        self._say(f"Deleted '{name}'.")

    def _open_history(self) -> None:
        if self._selected is not None:
            _HistoryDialog(self, self._store, self._selected, self._after_restore)

    def _after_restore(self, artist_id: int) -> None:
        self._kick()
        self._open_detail(artist_id)
        self._say("Version restored.")

    # ══ FORM screen (create + edit) ═══════════════════════════════════════════

    def _build_form_view(self, view: tk.Frame) -> None:
        head = tk.Frame(view, bg=PANEL)
        head.pack(side="top", fill="x", padx=24, pady=(18, 10))
        _Button(head, "‹  Cancel", self._cancel).pack(side="left")

        buttons = tk.Frame(view, bg=PANEL)
        buttons.pack(side="bottom", fill="x", padx=24, pady=(10, 8))
        self._btn_save = _Button(buttons, "Create", self._save, "primary")
        self._btn_save.pack(side="left")
        _Button(buttons, "Cancel", self._cancel).pack(side="left", padx=(8, 0))

        body = tk.Frame(view, bg=PANEL)
        body.pack(fill="both", expand=True, padx=24)
        self._f_title = tk.Label(body, text="", bg=PANEL, fg=FG, font=(_UI, 18, "bold"), anchor="w")
        self._f_title.pack(fill="x", pady=(0, 10))

        _caption(body, "Name").pack(fill="x")
        self._e_name = _entry(body, self._v_name)
        self._e_name.pack(fill="x", pady=(0, 8), ipady=4)

        _caption(body, "Other names (comma separated)").pack(fill="x")
        self._e_other = _entry(body, self._v_other)
        self._e_other.pack(fill="x", pady=(0, 8), ipady=4)

        row = tk.Frame(body, bg=PANEL)
        row.pack(fill="x", pady=(0, 8))
        grp = tk.Frame(row, bg=PANEL)
        grp.pack(side="left", fill="x", expand=True)
        _caption(grp, "Group name").pack(fill="x")
        self._e_group = _entry(grp, self._v_group)
        self._e_group.pack(fill="x", ipady=4)
        tk.Checkbutton(row, text="Active", variable=self._v_active, bg=PANEL, fg=FG,
                       selectcolor=ENTRY_BG, activebackground=PANEL, activeforeground=FG,
                       font=(_UI, 10), highlightthickness=0, bd=0
                       ).pack(side="left", padx=(16, 0), anchor="s", pady=(0, 4))

        _caption(body, "URLs (one per line)").pack(fill="x")
        self._t_urls = _text(body, height=7)
        self._t_urls.pack(fill="x", pady=(0, 8))

        _caption(body, "Notes").pack(fill="x")
        notes_row = tk.Frame(body, bg=PANEL)
        notes_row.pack(fill="both", expand=True)
        self._t_notes = _text(notes_row)
        bar = ttk.Scrollbar(notes_row, orient="vertical", command=self._t_notes.yview)
        self._t_notes.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")          # the one allowed scrollbar
        self._t_notes.pack(side="left", fill="both", expand=True)

        for w in (self._t_urls, self._t_notes):
            for seq in ("<KeyRelease>", "<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
                w.bind(seq, lambda _e: self.after_idle(self._check_dirty), add="+")
        for w in (self._e_name, self._e_other, self._e_group, self._t_urls, self._t_notes):
            w.bind("<Control-s>", lambda _e: (self._save(), "break")[1])
            w.bind("<Escape>", lambda _e: (self._cancel(), "break")[1])

    def _raw(self) -> tuple:
        return (self._v_name.get(), self._v_other.get(), self._v_group.get(),
                self._t_urls.get("1.0", "end-1c"), self._t_notes.get("1.0", "end-1c"),
                bool(self._v_active.get()))

    def _collect(self) -> dict:
        name, other, group, urls, notes, active = self._raw()
        return {"name": name, "other_names": other, "group_name": group,
                "urls": urls, "notes": notes, "is_active": active}

    def _fill(self, name="", other="", group="", urls="", notes="", active=True) -> None:
        self._v_name.set(name)
        self._v_other.set(other)
        self._v_group.set(group)
        self._v_active.set(active)
        for widget, value in ((self._t_urls, urls), (self._t_notes, notes)):
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
            widget.edit_reset()
        self._loaded_raw = self._raw()
        self._check_dirty()

    def _check_dirty(self) -> None:
        self._dirty = self._raw() != self._loaded_raw
        self._btn_save.set_enabled(self._dirty)

    def _save(self) -> bool:
        if not self._dirty and self._selected is not None:
            self._open_detail(self._selected)
            return True
        try:
            if self._selected is None:
                artist_id = self._store.create(self._collect())
                note = "Created."
            else:
                artist_id = self._selected
                note = "Saved." if self._store.edit(artist_id, self._collect()) else "No changes."
        except ArtistError as e:
            self._say(str(e), error=True)
            return False
        self._kick()
        self._open_detail(artist_id)
        self._say(note)
        return True

    def _cancel(self) -> None:
        if self._dirty and not messagebox.askyesno(
                "Discard changes", "Discard your changes?", parent=self):
            return
        self._dirty = False
        if self._selected is not None:
            self._open_detail(self._selected)
        else:
            self._go_list()


# ── History dialog ─────────────────────────────────────────────────────────────

class _HistoryDialog(tk.Toplevel):
    """Per-artist version list (paged). 'Restore' brings back a version as a new edit."""

    def __init__(self, page: ArtistsPage, store: ArtistStore, artist_id: int,
                 on_restored: Callable[[int], None]) -> None:
        super().__init__(page)
        self._store = store
        self._artist_id = artist_id
        self._on_restored = on_restored
        self._page_no = 0

        name = store.get(artist_id)["name"]
        self.title(f"History — {name}")
        self.configure(bg=PANEL)
        self.transient(page.winfo_toplevel())
        self.minsize(520, 420)
        self.bind("<Escape>", lambda _e: self.destroy())

        tk.Label(self, text=f"History of '{name}'", bg=PANEL, fg=FG,
                 font=(_UI, 14, "bold"), anchor="w").pack(fill="x", padx=20, pady=(16, 2))
        tk.Label(self, text="Nothing is ever erased. Restoring adds a new entry you can undo.",
                 bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w").pack(fill="x", padx=20, pady=(0, 10))

        foot = tk.Frame(self, bg=PANEL)
        foot.pack(side="bottom", fill="x", padx=20, pady=12)
        self._prev = _Button(foot, "‹", lambda: self._flip(-1))
        self._prev.pack(side="left")
        self._next = _Button(foot, "›", lambda: self._flip(1))
        self._next.pack(side="left", padx=(8, 0))
        self._label = tk.Label(foot, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 10))
        self._label.pack(side="left", padx=12)
        _Button(foot, "Close", self.destroy, "primary").pack(side="right")

        self._rows = tk.Frame(self, bg=PANEL)
        self._rows.pack(fill="both", expand=True, padx=20)
        self._refresh()

        self.update_idletasks()
        top = page.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _flip(self, step: int) -> None:
        self._page_no += step
        self._refresh()

    def _refresh(self) -> None:
        total = self._store.history_count(self._artist_id)
        pages = max(1, math.ceil(total / _HIST_ROWS))
        self._page_no = max(0, min(self._page_no, pages - 1))
        entries = self._store.history(self._artist_id, _HIST_ROWS, self._page_no * _HIST_ROWS)
        current = self._store.get(self._artist_id)

        for w in self._rows.winfo_children():
            w.destroy()
        for h in entries:
            self._add_row(h, current)

        self._label.config(text=f"{self._page_no + 1} / {pages}   ({total} entries)")
        self._prev.set_enabled(self._page_no > 0)
        self._next.set_enabled(self._page_no < pages - 1)

    def _add_row(self, h: dict, current: dict) -> None:
        row = tk.Frame(self._rows, bg=ENTRY_BG, highlightthickness=1, highlightbackground=BORDER)
        row.pack(fill="x", pady=3)
        left = tk.Frame(row, bg=ENTRY_BG)
        left.pack(side="left", fill="x", expand=True, padx=12, pady=7)

        dim = h["state"] != "applied"
        tk.Label(left, text=f"{_local_time(h['at'])}   {h['action']}", bg=ENTRY_BG,
                 fg=FG_DIM if dim else FG, font=(_UI, 10, "bold"), anchor="w").pack(fill="x")
        detail = ", ".join(h["changed"]) if h["changed"] else (
            "initial version" if h["action"] in ("create", "import") else "")
        if h["state"] != "applied":
            detail = f"[{h['state']}]  {detail}"
        tk.Label(left, text=detail, bg=ENTRY_BG, fg=ACCENT2 if not dim else FG_DIM,
                 font=(_UI, 9), anchor="w").pack(fill="x")

        after = h["after"]
        same = after is not None and all(
            after.get(k) == current.get(k)
            for k in ("name", "other_names", "group_name", "urls", "notes", "is_active"))
        can = after is not None and not after.get("deleted") and not same \
            and not current["deleted"]
        btn = _Button(row, "Restore", lambda i=h["id"]: self._restore(i))
        btn.pack(side="right", padx=10)
        btn.set_enabled(can)

    def _restore(self, history_id: int) -> None:
        try:
            self._store.restore_version(history_id)
        except ArtistError as e:
            messagebox.showerror("Cannot restore", str(e), parent=self)
            return
        self._on_restored(self._artist_id)
        self._refresh()


# ── Builder used by ui/features.py ─────────────────────────────────────────────

def build_artists(parent: tk.Frame) -> None:
    db = get_db()
    if db is None:
        tk.Label(parent, text="Database not initialised.", bg=PANEL, fg=COLOR_FAIL,
                 font=(_UI, 12)).pack(expand=True)
        return
    yaml_path = Path(__file__).resolve().parent.parent / "artists.yaml"
    store = ArtistStore(db.connection)
    sync = ArtistSync(store)
    sync.start()
    ArtistsPage(parent, store, yaml_path, sync).pack(fill="both", expand=True)
