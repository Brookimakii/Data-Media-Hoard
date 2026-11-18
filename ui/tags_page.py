"""
ui/tags_page.py
---------------
Tags management, three screens inside one page (same layout as the Artists page):

    LIST      search · type chips · sort · ＋ Add tag · Undo/Redo      (paged, no scroll)
      │ click a tag                         │ ＋ Add tag
      ▼                                     ▼
    DETAIL    type · synonyms · implies · implied by · wiki      FORM  create / edit
              Edit · Delete · History

Tag names in the detail screen are links: click one to jump to that tag.
All data goes through core.tag_store (local DB, history, undo/redo).
Every change is pushed to the booru server automatically (core.tag_sync); each tag shows
its state: ✓ synced · ● pending · ⟳ syncing · ✕ failed (with a Retry button). "⇩ Fetch"
pulls tags (with synonyms, implications and wiki) from the server. The login lives in the
⚙ settings. The Implies field of the form completes tag names as you type.
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from core.file_db import get_db
from core.tag_store import CATEGORIES, SORTS, TagError, TagStore
from core.tag_sync import TagSync
from ui import back_stack
from ui.autocomplete import Autocomplete
from ui.artists_page import _Button, _caption, _entry, _local_time, _text
from ui.theme import (
    ACCENT, ACCENT2, BORDER, COLOR_FAIL, COLOR_OK, ENTRY_BG, FG, FG_DIM, FONT_BTN,
    FONT_SUB, PANEL, SEL_BG,
)

_UI = FONT_BTN[0]
_ROW_H = 40
_ROW_GAP = 4
_HIST_ROWS = 6

# Column layout of the list (fractions of the row width)
_COL_NAME = (0.00, 0.25)
_COL_TYPE = (0.25, 0.11)
_COL_SYN = (0.36, 0.22)
_COL_IMP = (0.58, 0.23)
_COL_SYNC = (0.81, 0.19)

_AMBER = "#d9a441"
_POLL_MS = 400

# status -> (short text, long text, colour)
_STATUS = {
    "synced": ("✓ synced", "In sync with the server", COLOR_OK),
    "pending": ("● pending", "Waiting to be pushed to the server", _AMBER),
    "syncing": ("⟳ syncing…", "Pushing to the server…", ACCENT),
    "failed": ("✕ failed", "Push failed", COLOR_FAIL),
}

# e621-style colours for the tag types
TYPE_COLOR = {
    "general": "#b4c7d9", "artist": "#f2ac08", "copyright": "#dd00dd",
    "character": "#00aa00", "species": "#ed5d1f", "meta": "#e8e8f0", "lore": "#6cc26c",
}


def _summary(items: list[str], limit: int = 2) -> str:
    if not items:
        return "—"
    shown = ", ".join(items[:limit])
    return shown + (f"  +{len(items) - limit}" if len(items) > limit else "")


class TagsPage(tk.Frame):
    def __init__(self, parent: tk.Misc, store: TagStore, sync: TagSync | None = None) -> None:
        super().__init__(parent, bg=PANEL)
        self._store = store
        self._sync = sync
        self._last_busy: set[int] = set()
        self._ticks = 0
        self._cfg_seen: tuple | None = None
        self._selected: int | None = None
        self._page_no = 0
        self._page_size = 10
        self._category = "all"
        self._sort = "name"
        self._desc = False
        self._loaded_raw: tuple = ()
        self._dirty = False
        self._msg_job: str | None = None
        self._view_name = "list"
        self._trail: list[int] = []          # tags visited through links (for Back)

        self._q = tk.StringVar()
        self._v_name = tk.StringVar()
        self._v_type = tk.StringVar(value="general")
        self._v_alias = tk.StringVar()
        self._v_implies = tk.StringVar()
        self._v_sort = tk.StringVar(value=SORTS["name"])

        self._build_status_bar()
        self._list_view = tk.Frame(self, bg=PANEL)
        self._detail_view = tk.Frame(self, bg=PANEL)
        self._form_view = tk.Frame(self, bg=PANEL)
        self._build_list_view(self._list_view)
        self._build_detail_view(self._detail_view)
        self._build_form_view(self._form_view)

        self._q.trace_add("write", lambda *_: self._on_search())
        for var in (self._v_name, self._v_type, self._v_alias, self._v_implies):
            var.trace_add("write", lambda *_: self._check_dirty())

        self._refresh_list()
        self._show("list")
        back_stack.register(self.handle_back)
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
        self._trail.clear()
        if focus_id is not None:
            self._page_no = self._store.rank(
                focus_id, self._q.get(), self._category, self._sort, self._desc
            ) // self._page_size
        self._refresh_list()
        self._show("list")

    def handle_back(self) -> bool:
        """Mouse back / Alt+Left: form -> detail, linked tag -> previous tag, detail -> list."""
        if not self.winfo_ismapped():
            return False
        if self._view_name == "form":
            self._cancel()
            return True
        if self._view_name == "detail":
            self._back()
            return True
        return False

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

    # ── Sync status, polling, retry, fetch ────────────────────────────────────

    def _status_of(self, tag: dict, busy: set[int]) -> tuple[str, str, str]:
        key = "syncing" if tag["id"] in busy else tag["sync_status"]
        return _STATUS.get(key, _STATUS["pending"])

    def _kick(self) -> None:
        """Push pending tags (when auto-push is on) and refresh the sync widgets."""
        if self._sync is not None:
            self._sync.request_pending()
        self._update_sync_ui()

    def _update_sync_ui(self) -> None:
        if self._sync is None:
            return
        failed = self._store.sync_summary()["failed"]
        if failed:
            self._btn_retry_all.config(text=f"⟳  Retry {failed} failed")
            if not self._btn_retry_all.winfo_ismapped():
                self._btn_retry_all.pack(side="right", padx=(0, 8))
        else:
            self._btn_retry_all.pack_forget()

    def _retry_all(self) -> None:
        if self._sync is not None and self._sync.retry_failed():
            self._say("Retrying failed pushes…")
        self._refresh_list()
        self._update_sync_ui()

    def _retry_one(self) -> None:
        if self._sync is not None and self._selected is not None:
            self._sync.retry(self._selected)
            self._render_sync_status(self._store.get(self._selected))
            self._update_sync_ui()

    def _poll(self) -> None:
        """Repaint what the background worker finished; runs every few hundred ms."""
        if not self.winfo_exists():
            return
        if self._sync is not None:
            self._ticks += 1
            done = self._sync.poll()
            if done:
                self._refresh_list()
                if self._view_name == "detail" and self._selected is not None:
                    self._refresh_detail_status()
                self._update_sync_ui()
                bad = [r for r in done if not r.ok]
                if bad:
                    self._say(f"Push failed: {bad[-1].message}", error=True)
            elif self._sync.syncing_ids() != self._last_busy:
                self._refresh_list()
            self._last_busy = self._sync.syncing_ids()
            self._poll_fetch()
            cfg = self._sync.settings()
            seen = (cfg.configured, cfg.auto_push)
            if seen != self._cfg_seen:                 # ⚙ settings were changed
                self._cfg_seen = seen
                self._update_hint()
                self._kick()
        self.after(_POLL_MS, self._poll)

    def _poll_fetch(self) -> None:
        for r in self._sync.poll_fetch():
            self._btn_fetch.set_enabled(True)
            self._page_no = 0
            self._refresh_list()
            self._say(r.message, error=not r.ok)
        if self._sync.fetching:
            done, total = self._sync.fetch_progress()
            self._btn_fetch.set_enabled(False)
            self._say(f"Fetching tags… {done}/{total}" if total else "Fetching tags…")

    def _fetch(self) -> None:
        if self._sync is None:
            return
        _FetchDialog(self, self._start_fetch, self._q.get().strip())

    def _start_fetch(self, pattern: str, limit: int) -> None:
        if self._sync.fetch(pattern, limit):
            self._btn_fetch.set_enabled(False)
            self._say("Fetching tags…")
        else:
            self._poll_fetch()                         # shows "login not set" / "already running"

    # ══ LIST screen ═══════════════════════════════════════════════════════════

    def _build_list_view(self, view: tk.Frame) -> None:
        bar = tk.Frame(view, bg=PANEL)
        bar.pack(side="top", fill="x", padx=24, pady=(18, 8))

        box = tk.Frame(bar, bg=ENTRY_BG, highlightthickness=1, highlightbackground=BORDER)
        box.pack(side="left")
        tk.Label(box, text="🔍", bg=ENTRY_BG, fg=FG_DIM, font=(_UI, 10)).pack(side="left", padx=(8, 0))
        tk.Entry(box, textvariable=self._q, width=26, bg=ENTRY_BG, fg=FG, insertbackground=FG,
                 relief="flat", font=(_UI, 10)).pack(side="left", padx=6, pady=6)

        self._btn_retry_all = _Button(bar, "", self._retry_all, "danger")   # shown only when needed
        _Button(bar, "＋  Add tag", self._add, "primary").pack(side="right")
        self._btn_fetch = _Button(bar, "⇩  Fetch", self._fetch)
        self._btn_fetch.pack(side="right", padx=(0, 8))
        self._btn_redo = _Button(bar, "↷ Redo", self._redo)
        self._btn_redo.pack(side="right", padx=(0, 8))
        self._btn_undo = _Button(bar, "↶ Undo", self._undo)
        self._btn_undo.pack(side="right", padx=(0, 8))

        # Sort
        sort = tk.Frame(bar, bg=PANEL)
        sort.pack(side="right", padx=(0, 16))
        tk.Label(sort, text="Sort by", bg=PANEL, fg=FG_DIM, font=(_UI, 9)).pack(side="left", padx=(0, 6))
        menu = tk.OptionMenu(sort, self._v_sort, *SORTS.values(), command=lambda _v: self._on_sort())
        menu.config(bg=ENTRY_BG, fg=FG, activebackground=ACCENT, activeforeground=FG,
                    highlightthickness=0, relief="flat", font=(_UI, 9), width=13, anchor="w")
        menu.pack(side="left")
        self._btn_dir = _Button(sort, "▲", self._flip_dir)
        self._btn_dir.pack(side="left", padx=(6, 0))

        # Type chips
        self._chips_bar = tk.Frame(view, bg=PANEL)
        self._chips_bar.pack(side="top", fill="x", padx=24, pady=(0, 8))
        self._chips: dict[str, tk.Label] = {}
        for key in ("all", *CATEGORIES):
            chip = tk.Label(self._chips_bar, text=key, bg=ENTRY_BG, fg=FG, font=(_UI, 9, "bold"),
                            padx=10, pady=4, cursor="hand2")
            chip.pack(side="left", padx=(0, 6))
            chip.bind("<Button-1>", lambda _e, k=key: self._set_category(k))
            self._chips[key] = chip

        pager = tk.Frame(view, bg=PANEL)
        pager.pack(side="bottom", fill="x", padx=24, pady=(8, 8))
        self._prev = _Button(pager, "‹", lambda: self._flip(-1))
        self._prev.pack(side="left")
        self._next = _Button(pager, "›", lambda: self._flip(1))
        self._next.pack(side="right")
        self._pager = tk.Label(pager, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 10))
        self._pager.pack(expand=True)

        head = tk.Frame(view, bg=PANEL, height=22)
        head.pack(side="top", fill="x", padx=24)
        for title, (x, w) in (("NAME", _COL_NAME), ("TYPE", _COL_TYPE),
                              ("SYNONYMS", _COL_SYN), ("IMPLIES", _COL_IMP),
                              ("SERVER", _COL_SYNC)):
            tk.Label(head, text=title, bg=PANEL, fg=FG_DIM, font=(_UI, 8, "bold"), anchor="w",
                     padx=14).place(relx=x, rely=0, relwidth=w, relheight=1)
        self._count = tk.Label(head, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 8), anchor="e")
        self._count.place(relx=0.88, rely=0, relwidth=0.12, relheight=1)

        self._rows = tk.Frame(view, bg=PANEL)
        self._rows.pack(side="top", fill="both", expand=True, padx=24)
        self._rows.pack_propagate(False)
        self._rows.bind("<Configure>", self._on_rows_resize)

    def _on_rows_resize(self, e: tk.Event) -> None:
        size = max(3, e.height // (_ROW_H + _ROW_GAP))
        if size != self._page_size:
            self._page_size = size
            self.after_idle(self._refresh_list)

    def _refresh_chips(self) -> None:
        counts = self._store.category_counts()
        total = sum(counts.values())
        for key, chip in self._chips.items():
            n = total if key == "all" else counts.get(key, 0)
            on = key == self._category
            color = FG if key == "all" else TYPE_COLOR[key]
            chip.config(text=f"{key}  {n}", bg=SEL_BG if on else ENTRY_BG,
                        fg=color if (on or n) else FG_DIM,
                        highlightthickness=1, highlightbackground=color if on else ENTRY_BG)

    def _refresh_list(self) -> None:
        query = self._q.get()
        total = self._store.count(query, self._category)
        pages = max(1, math.ceil(total / self._page_size))
        self._page_no = max(0, min(self._page_no, pages - 1))
        rows = self._store.search(query, self._category, self._sort, self._desc,
                                  self._page_size, self._page_no * self._page_size)
        for w in self._rows.winfo_children():
            w.destroy()
        busy = self._sync.syncing_ids() if self._sync is not None else set()
        for tag in rows:
            self._add_row(tag, busy)
        if not rows:
            msg = ("No match." if query.strip() or self._category != "all" else
                   "No tags yet.\nClick “＋ Add tag”.")
            tk.Label(self._rows, text=msg, bg=PANEL, fg=FG_DIM, font=FONT_SUB,
                     justify="center").pack(pady=40)
        self._refresh_chips()
        self._count.config(text=f"{total} tag{'' if total == 1 else 's'}")
        self._pager.config(text=f"{self._page_no + 1} / {pages}")
        self._prev.set_enabled(self._page_no > 0)
        self._next.set_enabled(self._page_no < pages - 1)
        self._btn_dir.config(text="▼" if self._desc else "▲")
        self._update_hint()

    def _add_row(self, t: dict, busy: set[int]) -> None:
        row = tk.Frame(self._rows, bg=ENTRY_BG, height=_ROW_H, cursor="hand2",
                       highlightthickness=1, highlightbackground=BORDER)
        row.pack(fill="x", pady=(0, _ROW_GAP))
        cells = [
            (t["name"], _COL_NAME, TYPE_COLOR[t["category"]], (_UI, 11, "bold")),
            (t["category"], _COL_TYPE, TYPE_COLOR[t["category"]], (_UI, 10)),
            (_summary(t["aliases"]), _COL_SYN, FG_DIM, (_UI, 10)),
            (_summary(t["implies"]), _COL_IMP, FG_DIM, (_UI, 10)),
        ]
        if self._sync is not None:
            st_text, _long, st_color = self._status_of(t, busy)
            cells.append((st_text, _COL_SYNC, st_color, (_UI, 10, "bold")))
        labels = []
        for text, (x, w), fg, font in cells:
            lbl = tk.Label(row, text=text, bg=ENTRY_BG, fg=fg, font=font, anchor="w", padx=14)
            lbl.place(relx=x, rely=0, relwidth=w, relheight=1)
            labels.append(lbl)
        widgets = (row, *labels)
        for w in widgets:
            w.bind("<Button-1>", lambda _e, i=t["id"]: self._open_detail(i))
            w.bind("<Enter>", lambda _e, ws=widgets: [x.config(bg=SEL_BG) for x in ws])
            w.bind("<Leave>", lambda _e, ws=widgets: [x.config(bg=ENTRY_BG) for x in ws])

    def _flip(self, step: int) -> None:
        self._page_no += step
        self._refresh_list()

    def _on_search(self) -> None:
        self._page_no = 0
        self._refresh_list()

    def _set_category(self, key: str) -> None:
        self._category = key
        self._page_no = 0
        self._refresh_list()

    def _on_sort(self) -> None:
        label = self._v_sort.get()
        self._sort = next((k for k, v in SORTS.items() if v == label), "name")
        self._desc = self._sort in ("implies", "aliases", "updated")   # biggest / newest first
        self._page_no = 0
        self._refresh_list()

    def _flip_dir(self) -> None:
        self._desc = not self._desc
        self._page_no = 0
        self._refresh_list()

    # ── List actions ──────────────────────────────────────────────────────────

    def _add(self, name: str = "") -> None:
        self._selected = None
        self._f_title.config(text="New tag")
        self._btn_save.config(text="Create")
        self._fill(name=name, type_=self._category if self._category != "all" else "general")
        self._show("form")
        self._e_name.focus_set()

    def _undo(self) -> None:
        self._run_history(self._store.undo, "Undid", self._store.undo_label())

    def _redo(self) -> None:
        self._run_history(self._store.redo, "Redid", self._store.redo_label())

    def _run_history(self, action: Callable[[], list[int]], verb: str, label: str) -> None:
        try:
            ids = action()
        except TagError as e:
            self._say(str(e), error=True)
            return
        if ids:
            self._kick()
            self._refresh_list()
            self._say(f"{verb}: {label}.")

    # ══ DETAIL screen ═════════════════════════════════════════════════════════

    def _build_detail_view(self, view: tk.Frame) -> None:
        head = tk.Frame(view, bg=PANEL)
        head.pack(side="top", fill="x", padx=24, pady=(18, 10))
        self._btn_back = _Button(head, "‹  Tags", self._back)
        self._btn_back.pack(side="left")
        _Button(head, "Delete", self._delete, "danger").pack(side="right")
        _Button(head, "✎  Edit", self._edit, "primary").pack(side="right", padx=(0, 8))
        _Button(head, "History", self._open_history).pack(side="right", padx=(0, 8))

        body = tk.Frame(view, bg=PANEL)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        self._title = tk.Label(body, text="", bg=PANEL, fg=FG, font=(_UI, 18, "bold"), anchor="w")
        self._title.pack(fill="x")
        self._badge = tk.Label(body, text="", bg=PANEL, fg=FG, font=(_UI, 10, "bold"), anchor="w")
        self._badge.pack(fill="x", pady=(2, 4))
        state = tk.Frame(body, bg=PANEL)
        state.pack(fill="x")
        self._sync_lbl = tk.Label(state, text="", bg=PANEL, fg=COLOR_OK, font=(_UI, 10, "bold"),
                                  anchor="w")
        self._sync_lbl.pack(side="left")
        self._btn_retry = _Button(state, "⟳  Retry push", self._retry_one)
        self._sync_err = tk.Label(body, text="", bg=PANEL, fg=COLOR_FAIL, font=FONT_SUB,
                                  anchor="w", justify="left")
        self._sync_err.bind("<Configure>",
                            lambda e: self._sync_err.config(wraplength=max(100, e.width)))
        self._sync_err.pack(fill="x", pady=(0, 8))

        _caption(body, "Synonyms  (other names that mean this tag)").pack(fill="x")
        self._r_alias = self._links_box(body)
        _caption(body, "Implies  (every post with this tag also gets these)").pack(fill="x")
        self._r_implies = self._links_box(body)
        _caption(body, "Also implied, through the tags above").pack(fill="x")
        self._r_chain = self._links_box(body)
        _caption(body, "Implied by  (these tags imply this one)").pack(fill="x")
        self._r_by = self._links_box(body)

        _caption(body, "Wiki").pack(fill="x")
        wiki_row = tk.Frame(body, bg=PANEL)
        wiki_row.pack(fill="both", expand=True)
        self._r_wiki = tk.Text(wiki_row, bg=ENTRY_BG, fg=FG, relief="flat", wrap="word",
                               highlightthickness=1, highlightbackground=BORDER,
                               font=(_UI, 10), padx=8, pady=6, cursor="arrow")
        bar = ttk.Scrollbar(wiki_row, orient="vertical", command=self._r_wiki.yview)
        self._r_wiki.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")          # the one allowed scrollbar
        self._r_wiki.pack(side="left", fill="both", expand=True)

    @staticmethod
    def _links_box(parent: tk.Misc) -> tk.Text:
        box = tk.Text(parent, height=2, bg=ENTRY_BG, fg=FG, relief="flat", wrap="word",
                      highlightthickness=1, highlightbackground=BORDER, font=(_UI, 10),
                      padx=8, pady=5, cursor="arrow")
        box.pack(fill="x", pady=(0, 8))
        return box

    def _fill_links(self, box: tk.Text, names: list[str]) -> None:
        """Names as links: known tags open on click, unknown ones are dimmed."""
        box.config(state="normal")
        box.delete("1.0", "end")
        for tag in box.tag_names():
            if tag.startswith("l_"):
                box.tag_delete(tag)
        if not names:
            box.insert("end", "—", ("dim",))
            box.tag_configure("dim", foreground=FG_DIM)
        for i, name in enumerate(names):
            found = self._store.find(name)
            tag = f"l_{i}"
            if found:
                box.tag_configure(tag, foreground=TYPE_COLOR[found["category"]], underline=True)
                box.tag_bind(tag, "<Button-1>", lambda _e, t=found["id"]: self._follow(t))
                box.tag_bind(tag, "<Enter>", lambda _e, b=box: b.config(cursor="hand2"))
                box.tag_bind(tag, "<Leave>", lambda _e, b=box: b.config(cursor="arrow"))
            else:
                box.tag_configure(tag, foreground=FG_DIM)
            box.insert("end", name, (tag,))
            if i < len(names) - 1:
                box.insert("end", "   ")
        box.config(state="disabled")

    def _open_detail(self, tag_id: int) -> None:
        try:
            t = self._store.get(tag_id)
        except TagError as e:
            self._say(str(e), error=True)
            return
        self._selected = tag_id
        self._title.config(text=t["name"], fg=TYPE_COLOR[t["category"]])
        self._badge.config(text=t["category"].upper(), fg=TYPE_COLOR[t["category"]])
        self._fill_links(self._r_alias, t["aliases"])
        self._fill_links(self._r_implies, t["implies"])
        chain = [n for n in self._store.implication_closure(t["name"]) if n not in t["implies"]]
        self._fill_links(self._r_chain, chain)
        self._fill_links(self._r_by, self._store.implied_by(t["name"]))
        self._r_wiki.config(state="normal")
        self._r_wiki.delete("1.0", "end")
        self._r_wiki.insert("1.0", t["wiki"] or "—")
        self._r_wiki.config(state="disabled")
        self._btn_back.config(text="‹  Back" if self._trail else "‹  Tags")
        self._render_sync_status(t)
        self._show("detail")

    def _render_sync_status(self, t: dict) -> None:
        if self._sync is None:
            self._sync_lbl.config(text="")
            self._sync_err.config(text="")
            self._btn_retry.pack_forget()
            return
        busy = self._sync.syncing_ids()
        text, long_text, color = self._status_of(t, busy)
        remote = f" · on server as #{t['remote_id']}" if t.get("remote_id") else ""
        self._sync_lbl.config(text=f"{text}  ·  {long_text}{remote}", fg=color)
        failed = t["sync_status"] == "failed" and t["id"] not in busy
        self._sync_err.config(text=(t.get("sync_error") or "") if failed else "")
        if failed:
            self._btn_retry.pack(side="left", padx=(12, 0))
        else:
            self._btn_retry.pack_forget()

    def _refresh_detail_status(self) -> None:
        try:
            self._render_sync_status(self._store.get(self._selected))
        except TagError:
            pass

    def _follow(self, tag_id: int) -> None:
        if self._selected is not None:
            self._trail.append(self._selected)
        self._open_detail(tag_id)

    # ── Detail actions ────────────────────────────────────────────────────────

    def _back(self) -> None:
        if self._trail:
            self._open_detail(self._trail.pop())
        else:
            self._go_list(self._selected)

    def _edit(self) -> None:
        if self._selected is None:
            return
        t = self._store.get(self._selected)
        self._f_title.config(text=f"Edit  ·  {t['name']}")
        self._btn_save.config(text="Save")
        self._fill(t["name"], t["category"], ", ".join(t["aliases"]),
                   ", ".join(t["implies"]), t["wiki"])
        self._show("form")
        self._e_name.focus_set()

    def _delete(self) -> None:
        if self._selected is None:
            return
        t = self._store.get(self._selected)
        by = self._store.implied_by(t["name"])
        extra = (f"\n\n{len(by)} other tag(s) still imply it: {', '.join(by[:5])}"
                 f"{'…' if len(by) > 5 else ''}.") if by else ""
        if not messagebox.askyesno(
                "Delete tag",
                f"Delete '{t['name']}'?{extra}\n\nIt stays in the history and Undo brings it back."
                + ("\n\nOn the server its synonyms and implications are removed; the tag "
                   "itself and its wiki stay (the API cannot delete them)."
                   if self._sync is not None and t.get("remote_id") is not None else ""),
                parent=self):
            return
        try:
            self._store.delete(self._selected)
        except TagError as e:
            self._say(str(e), error=True)
            return
        self._selected = None
        self._kick()
        self._go_list()
        self._say(f"Deleted '{t['name']}'.")

    def _open_history(self) -> None:
        if self._selected is not None:
            _HistoryDialog(self, self._store, self._selected, self._after_restore)

    def _after_restore(self, tag_id: int) -> None:
        self._kick()
        self._open_detail(tag_id)
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

        row = tk.Frame(body, bg=PANEL)
        row.pack(fill="x", pady=(0, 8))
        left = tk.Frame(row, bg=PANEL)
        left.pack(side="left", fill="x", expand=True)
        _caption(left, "Name  (lowercase, spaces become _)").pack(fill="x")
        self._e_name = _entry(left, self._v_name)
        self._e_name.pack(fill="x", ipady=4)
        right = tk.Frame(row, bg=PANEL)
        right.pack(side="left", padx=(12, 0))
        _caption(right, "Type").pack(fill="x")
        type_menu = tk.OptionMenu(right, self._v_type, *CATEGORIES)
        type_menu.config(bg=ENTRY_BG, fg=FG, activebackground=ACCENT, activeforeground=FG,
                         highlightthickness=1, highlightbackground=BORDER, relief="flat",
                         font=(_UI, 10), width=11, anchor="w")
        type_menu.pack(pady=(0, 0))

        _caption(body, "Synonyms (comma separated)").pack(fill="x")
        self._e_alias = _entry(body, self._v_alias)
        self._e_alias.pack(fill="x", pady=(0, 8), ipady=4)

        _caption(body, "Implies (comma separated: tags that this tag always brings with it)"
                 ).pack(fill="x")
        self._e_implies = _entry(body, self._v_implies)
        self._e_implies.pack(fill="x", pady=(0, 8), ipady=4)

        _caption(body, "Wiki").pack(fill="x")
        wiki_row = tk.Frame(body, bg=PANEL)
        wiki_row.pack(fill="both", expand=True)
        self._t_wiki = _text(wiki_row)
        bar = ttk.Scrollbar(wiki_row, orient="vertical", command=self._t_wiki.yview)
        self._t_wiki.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")          # the one allowed scrollbar
        self._t_wiki.pack(side="left", fill="both", expand=True)

        # created before the Escape binding below so that Esc closes the list first
        self._complete = Autocomplete(self._e_implies, self._suggest, TYPE_COLOR,
                                      on_change=self._check_dirty)

        for seq in ("<KeyRelease>", "<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
            self._t_wiki.bind(seq, lambda _e: self.after_idle(self._check_dirty), add="+")
        for w in (self._e_name, self._e_alias, self._e_implies, self._t_wiki):
            w.bind("<Control-s>", lambda _e: (self._save(), "break")[1], add="+")
            w.bind("<Escape>", lambda _e: (self._cancel(), "break")[1], add="+")

    def _suggest(self, prefix: str, others: list[str]) -> list[dict]:
        """Tags to offer in the Implies field: not itself, not already listed."""
        skip = [*others, self._v_name.get()]
        if self._selected is not None:                  # nor one of its own synonyms
            skip += self._store.get(self._selected)["aliases"]
        return self._store.suggest(prefix, 8, skip)

    def _raw(self) -> tuple:
        return (self._v_name.get(), self._v_type.get(), self._v_alias.get(),
                self._v_implies.get(), self._t_wiki.get("1.0", "end-1c"))

    def _collect(self) -> dict:
        name, type_, alias, implies, wiki = self._raw()
        return {"name": name, "category": type_, "aliases": alias,
                "implies": implies, "wiki": wiki}

    def _fill(self, name="", type_="general", aliases="", implies="", wiki="") -> None:
        self._v_name.set(name)
        self._v_type.set(type_)
        self._v_alias.set(aliases)
        self._v_implies.set(implies)
        self._t_wiki.delete("1.0", "end")
        self._t_wiki.insert("1.0", wiki)
        self._t_wiki.edit_reset()
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
                tag_id = self._store.create(self._collect())
                note = "Created."
            else:
                tag_id = self._selected
                note = "Saved." if self._store.edit(tag_id, self._collect()) else "No changes."
        except TagError as e:
            self._say(str(e), error=True)
            return False
        self._dirty = False
        self._trail.clear()
        self._kick()
        self._open_detail(tag_id)
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


# ── Fetch dialog ──────────────────────────────────────────────────────────────

class _FetchDialog(tk.Toplevel):
    """Ask which tags to fetch: a name pattern (* = wildcard) and how many."""

    def __init__(self, page: TagsPage, on_fetch: Callable[[str, int], None],
                 initial: str = "") -> None:
        super().__init__(page)
        self._on_fetch = on_fetch
        self.title("Fetch tags from the server")
        self.configure(bg=PANEL)
        self.transient(page.winfo_toplevel())
        self.bind("<Escape>", lambda _e: self.destroy())

        tk.Label(self, text="Fetch tags from the server", bg=PANEL, fg=FG,
                 font=(_UI, 14, "bold"), anchor="w").pack(fill="x", padx=20, pady=(16, 2))
        tk.Label(self, text="Brings their type, synonyms, implications and wiki. Tags you have\n"
                            "edited but not pushed yet are left alone. One Undo reverts a fetch.",
                 bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w", justify="left"
                 ).pack(fill="x", padx=20, pady=(0, 10))

        self._v_pattern = tk.StringVar(value=(initial + "*") if initial else "*")
        self._v_limit = tk.StringVar(value="50")
        _caption(self, "Name pattern  (e.g.  canine*   *_ears   *  for the most used tags)"
                 ).pack(fill="x", padx=20)
        e = _entry(self, self._v_pattern)
        e.pack(fill="x", padx=20, pady=(0, 8), ipady=4)
        _caption(self, "How many tags (most used first, at most 320)").pack(fill="x", padx=20)
        _entry(self, self._v_limit).pack(fill="x", padx=20, pady=(0, 12), ipady=4)

        foot = tk.Frame(self, bg=PANEL)
        foot.pack(fill="x", padx=20, pady=(0, 16))
        _Button(foot, "⇩  Fetch", self._go, "primary").pack(side="left")
        _Button(foot, "Cancel", self.destroy).pack(side="left", padx=(8, 0))
        e.bind("<Return>", lambda _e: self._go())
        e.focus_set()

        self.update_idletasks()
        top = page.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _go(self) -> None:
        try:
            limit = max(1, min(320, int(self._v_limit.get().strip() or "50")))
        except ValueError:
            limit = 50
        pattern = self._v_pattern.get().strip() or "*"
        self.destroy()
        self._on_fetch(pattern, limit)


# ── History dialog ────────────────────────────────────────────────────────────

class _HistoryDialog(tk.Toplevel):
    """Per-tag version list (paged). 'Restore' brings a version back as a new edit."""

    def __init__(self, page: TagsPage, store: TagStore, tag_id: int,
                 on_restored: Callable[[int], None]) -> None:
        super().__init__(page)
        self._store = store
        self._tag_id = tag_id
        self._on_restored = on_restored
        self._page_no = 0

        name = store.get(tag_id)["name"]
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
        total = self._store.history_count(self._tag_id)
        pages = max(1, math.ceil(total / _HIST_ROWS))
        self._page_no = max(0, min(self._page_no, pages - 1))
        entries = self._store.history(self._tag_id, _HIST_ROWS, self._page_no * _HIST_ROWS)
        current = self._store.get(self._tag_id)
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
        if dim:
            detail = f"[{h['state']}]  {detail}"
        tk.Label(left, text=detail, bg=ENTRY_BG, fg=ACCENT2 if not dim else FG_DIM,
                 font=(_UI, 9), anchor="w").pack(fill="x")
        after = h["after"]
        same = after is not None and all(
            after.get(k) == current.get(k) for k in ("name", "category", "wiki", "aliases", "implies"))
        can = after is not None and not after.get("deleted") and not same \
            and not current["deleted"]
        btn = _Button(row, "Restore", lambda i=h["id"]: self._restore(i))
        btn.pack(side="right", padx=10)
        btn.set_enabled(can)

    def _restore(self, history_id: int) -> None:
        try:
            self._store.restore_version(history_id)
        except TagError as e:
            messagebox.showerror("Cannot restore", str(e), parent=self)
            return
        self._on_restored(self._tag_id)
        self._refresh()


# ── Builder used by ui/features.py ────────────────────────────────────────────

def build_tags(parent: tk.Frame) -> None:
    db = get_db()
    if db is None:
        tk.Label(parent, text="Database not initialised.", bg=PANEL, fg=COLOR_FAIL,
                 font=(_UI, 12)).pack(expand=True)
        return
    store = TagStore(db.connection)
    sync = TagSync(store)
    sync.start()
    TagsPage(parent, store, sync).pack(fill="both", expand=True)

