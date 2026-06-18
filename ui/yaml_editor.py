"""
ui/yaml_editor.py
-----------------
In-app YAML editor for artists.yaml and config.yaml.

Artist editor features:
  • Search/filter bar — live-filters cards by name or tag
  • Reorder — ▲ / ▼ buttons on each card move it up or down
  • Disable, not delete — toggle switch greys the card and sets enabled: false
  • Click header to expand / collapse fields
  • Media links always sorted alphabetically (by category then site key)

Entry point:
    open_yaml_editor(root, catalogue_path, config_path, on_saved)
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path
from typing import Any, Callable

import yaml

from ui.theme import (
    ACCENT, ACCENT2, BG, BORDER, ENTRY_BG, FG, FG_DIM,
    FONT_BODY, FONT_BOLD, FONT_BTN, FONT_HEAD, FONT_MONO,
    FONT_SUB, PAD_OUTER, PANEL, BTN_BG, BTN_HOV, BTN_FG,
    COLOR_FAIL, COLOR_OK, COLOR_RUNNING, ROW_ALT,
)
from ui.widgets import divider, section_label, styled_button, styled_entry
from ui.scroll import suspend_global_scroll
from utils.yaml_io import read_yaml, write_yaml, YAMLError

# ── constants ──────────────────────────────────────────────────────────────────

_WIN_W, _WIN_H = 960, 720
_KNOWN_CATS = ["aggregators", "socials", "paid", "contacts", "streaming"]

_DL_SITES = {
    "pixiv", "danbooru", "artstation", "twitter", "bluesky",
    "furaffinity", "tumblr", "deviantart", "kemono",
}

_DISABLED_OVERLAY = "#1a1a24"   # slightly darker bg for disabled cards
_DISABLED_FG      = "#44445a"


# ── tiny helpers ───────────────────────────────────────────────────────────────

def _btn(parent: tk.Widget, text: str, cmd: Callable,
         bg: str = BTN_BG, hov: str = BTN_HOV, **kw) -> tk.Button:
    kw.setdefault("padx", 10)
    kw.setdefault("pady", 4)
    b = tk.Button(parent, text=text, command=cmd,
                  bg=bg, fg=BTN_FG, relief="flat",
                  font=FONT_BTN, cursor="hand2",
                  activebackground=hov, activeforeground=BTN_FG, **kw)
    b.bind("<Enter>", lambda _: b.config(bg=hov))
    b.bind("<Leave>", lambda _: b.config(bg=bg))
    return b


def _entry(parent: tk.Widget, value: str = "", width: int = 36) -> tk.Entry:
    e = tk.Entry(parent, bg=ENTRY_BG, fg=FG, insertbackground=ACCENT,
                 relief="flat", font=FONT_BODY, width=width,
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT)
    e.insert(0, value)
    return e


def _lbl(parent: tk.Widget, text: str, fg: str = FG_DIM,
         font=None, **kw) -> tk.Label:
    return tk.Label(parent, text=text, bg=ENTRY_BG, fg=fg,
                    font=font or FONT_MONO, **kw)


def _scrolled_frame(
    parent: tk.Widget,
    lock: "_ScrollLock | None" = None,
) -> tuple[tk.Canvas, tk.Frame, ttk.Scrollbar]:
    """Return (canvas, inner_frame, scrollbar); caller packs canvas."""
    canvas = tk.Canvas(parent, bg=ENTRY_BG, highlightthickness=0)
    sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=ENTRY_BG)
    win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    inner.bind("<Configure>",
               lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfig(win_id, width=e.width))
    canvas.configure(yscrollcommand=sb.set)

    sb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    handler = _make_scroll_handler(canvas, lock)
    for w in (canvas, inner):
        w.bind("<MouseWheel>", handler)
        w.bind("<Button-4>",   handler)
        w.bind("<Button-5>",   handler)

    return canvas, inner, sb


class _ScrollLock:
    """
    Single global switch for the YAML editor window.

    While locked, every wheel handler created via _make_scroll_handler()
    swallows the event (returns "break") instead of scrolling — this is
    simpler and far more reliable than guarding each binding site
    individually, since it only needs to be flipped in exactly two places
    (card expand / collapse) and every handler automatically respects it.
    """
    def __init__(self) -> None:
        self.locked = False


def _make_scroll_handler(
    canvas: tk.Canvas,
    lock: "_ScrollLock | None" = None,
) -> Callable[[tk.Event], str]:
    """
    Build a wheel-event handler bound to *canvas* that:
      - does nothing while *lock* is held (lock.locked is True)
      - otherwise scrolls only when content actually overflows the
        visible area
      - always returns "break" so the event never falls through to
        whatever canvas/dialog happens to be underneath
    """
    def _overflows() -> bool:
        bbox = canvas.bbox("all")
        if not bbox:
            return False
        return (bbox[3] - bbox[1]) > canvas.winfo_height()

    def _handler(ev: tk.Event) -> str:
        if lock is not None and lock.locked:
            return "break"
        if _overflows():
            if ev.num == 4:
                canvas.yview_scroll(-1, "units")
            elif ev.num == 5:
                canvas.yview_scroll(1, "units")
            else:
                canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")
        return "break"
    return _handler


def _sort_media(media: dict) -> dict:
    """Return media dict with categories sorted A-Z and sites sorted A-Z within each."""
    return {
        cat: dict(sorted(sites.items()))
        for cat, sites in sorted(media.items())
        if isinstance(sites, dict)
    }


# ── Artist card ────────────────────────────────────────────────────────────────

class _ArtistCard(tk.Frame):
    """
    One artist row.

    Public API
    ----------
    .get_data()     → dict  (YAML-ready, media sorted)
    .matches(q)     → bool  (True if name/tags contain search query q)
    .expand()       — force open
    .collapse()     — force closed
    .enabled        → bool  property
    """

    def __init__(self, parent: tk.Widget, artist: dict,
                 on_move_up: Callable, on_move_down: Callable,
                 canvas: tk.Canvas, outer_sb: ttk.Scrollbar,
                 lock: "_ScrollLock", **kw) -> None:
        super().__init__(parent, bg=BORDER, padx=1, pady=1, **kw)
        self._artist       = dict(artist)
        self._on_move_up   = on_move_up
        self._on_move_down = on_move_down
        self._canvas       = canvas
        self._outer_sb     = outer_sb
        self._lock         = lock
        self._expanded     = False
        self._resize_bind_id: str | None = None
        self._link_rows: list[dict] = []
        self._build()

    # ── build ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self._inner = tk.Frame(self, bg=ENTRY_BG)
        self._inner.pack(fill="both", expand=True)
        self._build_header()
        # _body_canvas / _body_inner created fresh on each expand
        self._body_canvas: tk.Canvas | None = None
        self._body_inner:  tk.Frame  | None = None
        self._apply_enabled()

    def _build_header(self) -> None:
        hdr = tk.Frame(self._inner, bg=ENTRY_BG, cursor="hand2")
        hdr.pack(fill="x", padx=6, pady=(5, 3))
        self._hdr = hdr

        # ▲ ▼ reorder buttons
        reorder = tk.Frame(hdr, bg=ENTRY_BG)
        reorder.pack(side="left", padx=(0, 6))
        tk.Button(reorder, text="▲", command=self._on_move_up,
                  bg=ENTRY_BG, fg=FG_DIM, relief="flat",
                  font=(FONT_MONO[0], 7), cursor="hand2", padx=2, pady=0,
                  activebackground=BORDER, activeforeground=FG,
                  ).pack()
        tk.Button(reorder, text="▼", command=self._on_move_down,
                  bg=ENTRY_BG, fg=FG_DIM, relief="flat",
                  font=(FONT_MONO[0], 7), cursor="hand2", padx=2, pady=0,
                  activebackground=BORDER, activeforeground=FG,
                  ).pack()

        # Expand toggle arrow
        self._arrow = tk.Label(hdr, text="▶", bg=ENTRY_BG, fg=ACCENT,
                               font=FONT_BOLD, cursor="hand2")
        self._arrow.pack(side="left", padx=(0, 6))

        # Name label (read-only in header; editable inside body)
        self._name_var = tk.StringVar(value=self._artist.get("name", ""))
        self._name_lbl = tk.Label(hdr, textvariable=self._name_var,
                                  bg=ENTRY_BG, fg=FG,
                                  font=FONT_BOLD, anchor="w", cursor="hand2")
        self._name_lbl.pack(side="left", fill="x", expand=True)

        # Enabled toggle
        self._enabled_var = tk.BooleanVar(
            value=self._artist.get("enabled", True) is not False
        )
        self._toggle_lbl = tk.Label(hdr, text="", bg=ENTRY_BG,
                                    font=FONT_MONO, cursor="hand2")
        self._toggle_lbl.pack(side="right", padx=(6, 4))
        self._toggle_lbl.bind("<Button-1>", lambda _: self._toggle_enabled())
        self._update_toggle_label()

        # Bind entire header to expand/collapse
        for w in (hdr, self._arrow, self._name_lbl):
            w.bind("<Button-1>", lambda _: self._toggle_expand())

    def _build_body(self, body: tk.Frame) -> None:
        """Populate *body* (the inner scrollable frame) with form fields."""

        # ── Name field (editable) ──────────────────────────────────────────
        name_row = tk.Frame(body, bg=ENTRY_BG)
        name_row.pack(fill="x", padx=8, pady=(6, 3))
        _lbl(name_row, "name:", width=8, anchor="w").pack(side="left")
        self._name_entry = _entry(name_row, self._artist.get("name", ""), width=36)
        self._name_entry.pack(side="left", fill="x", expand=True)
        # Keep header label in sync
        self._name_entry.bind(
            "<KeyRelease>",
            lambda _: self._name_var.set(self._name_entry.get())
        )

        # ── Tags ──────────────────────────────────────────────────────────
        tags_row = tk.Frame(body, bg=ENTRY_BG)
        tags_row.pack(fill="x", padx=8, pady=(0, 3))
        _lbl(tags_row, "tags:", width=8, anchor="w").pack(side="left")
        raw_tags = self._artist.get("tags", []) or []
        self._tags_entry = _entry(tags_row, ", ".join(raw_tags), width=50)
        self._tags_entry.pack(side="left", fill="x", expand=True)
        _lbl(tags_row, " comma-separated", width=18).pack(side="left")

        # ── Notes ─────────────────────────────────────────────────────────
        notes_row = tk.Frame(body, bg=ENTRY_BG)
        notes_row.pack(fill="x", padx=8, pady=(0, 4))
        _lbl(notes_row, "notes:", width=8, anchor="nw").pack(side="left", anchor="n")
        self._notes_text = tk.Text(
            notes_row, bg=ENTRY_BG, fg=FG, insertbackground=ACCENT,
            relief="flat", font=FONT_MONO, height=2, width=58,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, wrap="word",
        )
        self._notes_text.insert("1.0", self._artist.get("notes", "") or "")
        self._notes_text.pack(side="left", fill="x", expand=True)

        # ── Media links ────────────────────────────────────────────────────
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", padx=8, pady=(5, 4))
        lnk_hdr = tk.Frame(body, bg=ENTRY_BG)
        lnk_hdr.pack(fill="x", padx=8, pady=(0, 3))
        _lbl(lnk_hdr, "MEDIA LINKS", fg=ACCENT2, font=FONT_BOLD).pack(side="left")
        _lbl(lnk_hdr, " — sorted A-Z automatically", fg=FG_DIM).pack(side="left")
        _btn(lnk_hdr, "+ link", self._add_link_row,
             bg="#1a2a1a", hov="#2a3a2a", padx=8, pady=2).pack(side="right")

        # Column headers
        col_hdr = tk.Frame(body, bg=ENTRY_BG)
        col_hdr.pack(fill="x", padx=8, pady=(0, 2))
        _lbl(col_hdr, "category",  width=13, anchor="w").pack(side="left", padx=(0, 4))
        _lbl(col_hdr, "site/key",  width=14, anchor="w").pack(side="left", padx=(0, 4))
        _lbl(col_hdr, "url",       anchor="w").pack(side="left")

        self._links_frame = tk.Frame(body, bg=ENTRY_BG)
        self._links_frame.pack(fill="x", padx=8, pady=(0, 8))

        # Populate from YAML — sorted
        media = _sort_media(self._artist.get("media") or {})
        for cat, entries in media.items():
            for site, url_val in entries.items():
                if isinstance(url_val, list):
                    for u in url_val:
                        self._add_link_row(cat=cat, site=site, url=str(u))
                else:
                    self._add_link_row(cat=cat, site=site, url=str(url_val or ""))

    def _add_link_row(self, cat: str = "socials",
                      site: str = "", url: str = "") -> None:
        row = tk.Frame(self._links_frame, bg=ENTRY_BG)
        row.pack(fill="x", pady=1)

        cat_var = tk.StringVar(value=cat)
        cat_cb = ttk.Combobox(row, textvariable=cat_var,
                               values=_KNOWN_CATS, width=11, font=FONT_MONO)
        cat_cb.pack(side="left", padx=(0, 4))

        site_e = _entry(row, site, width=13)
        site_e.pack(side="left", padx=(0, 4))

        def _colour(*_) -> None:
            site_e.config(fg=ACCENT if site_e.get().strip().lower() in _DL_SITES else FG)

        site_e.bind("<KeyRelease>", _colour)
        _colour()

        url_e = _entry(row, url, width=44)
        url_e.pack(side="left", padx=(0, 4), fill="x", expand=True)

        rd = {"cat": cat_var, "site": site_e, "url": url_e, "frame": row}

        def _remove(r=rd) -> None:
            r["frame"].destroy()
            self._link_rows.remove(r)

        _btn(row, "✕", _remove, bg="#2a1a1a", hov="#4a2a2a",
             padx=5, pady=1).pack(side="left")
        self._link_rows.append(rd)

    # ── scroll helpers ─────────────────────────────────────────────────────

    def _bind_scroll_tree(self, widget: tk.Widget, target: tk.Canvas,
                           respect_lock: bool = True) -> None:
        """
        Recursively forward scroll events from *widget* tree to *target* canvas.

        respect_lock=True  → forwarding goes inert while the shared lock is
                              held (used for the collapsed header, which must
                              not move the outer list while another card —
                              or this same card — is open for editing).
        respect_lock=False → always forwards regardless of lock state (used
                              for the body of *this* card while it's open,
                              since that's the one place scrolling must keep
                              working even though everything else is locked).
        """
        handler = _make_scroll_handler(target, self._lock if respect_lock else None)
        widget.bind("<MouseWheel>", handler, add=True)
        widget.bind("<Button-4>",   handler, add=True)
        widget.bind("<Button-5>",   handler, add=True)
        for child in widget.winfo_children():
            self._bind_scroll_tree(child, target, respect_lock)

    def _lock_outer(self) -> None:
        """Flip the shared lock so every scroll handler in the editor goes inert."""
        self._lock.locked = True
        self._outer_sb.state(["disabled"])   # also blocks dragging the scrollbar thumb

    def _unlock_outer(self) -> None:
        self._lock.locked = False
        self._outer_sb.state(["!disabled"])

    # ── expand / collapse ──────────────────────────────────────────────────

    def _toggle_expand(self) -> None:
        if self._expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self) -> None:
        if self._expanded:
            return
        self._expanded = True
        self._arrow.config(text="▼")
        self._lock_outer()

        # ── Measure available height in the outer canvas viewport ──────────
        self._canvas.update_idletasks()
        viewport_h = self._canvas.winfo_height()
        hdr_h      = self._hdr.winfo_reqheight() + 8   # header + padding

        # Card fills the viewport: header stays visible, body takes the rest
        body_h = max(viewport_h - hdr_h, 200)

        # ── Build inner scroll canvas for the body ─────────────────────────
        body_canvas = tk.Canvas(self._inner, bg=ENTRY_BG,
                                highlightthickness=0, height=body_h)
        body_sb     = ttk.Scrollbar(self._inner, orient="vertical",
                                    command=body_canvas.yview)
        body_inner  = tk.Frame(body_canvas, bg=ENTRY_BG)
        win_id = body_canvas.create_window((0, 0), window=body_inner, anchor="nw")

        body_inner.bind(
            "<Configure>",
            lambda _: body_canvas.configure(
                scrollregion=body_canvas.bbox("all")))
        body_canvas.bind(
            "<Configure>",
            lambda e: body_canvas.itemconfig(win_id, width=e.width))
        body_canvas.configure(yscrollcommand=body_sb.set)

        # Pack scrollbar first so it sits on the right
        body_sb.pack(side="right", fill="y", after=self._hdr)
        body_canvas.pack(side="left", fill="both", expand=True, after=self._hdr)

        self._body_canvas = body_canvas
        self._body_inner  = body_inner

        # Populate form fields into body_inner
        self._link_rows.clear()
        self._build_body(body_inner)

        # Route all wheel events inside the card to the body canvas.
        # respect_lock=False: this body is the one place that must keep
        # scrolling even though the outer list and other headers are locked.
        self._bind_scroll_tree(body_inner, body_canvas, respect_lock=False)

        # Also handle wheel on the body canvas itself (never locked either)
        body_handler = _make_scroll_handler(body_canvas, lock=None)
        body_canvas.bind("<MouseWheel>", body_handler)
        body_canvas.bind("<Button-4>",   body_handler)
        body_canvas.bind("<Button-5>",   body_handler)

        # ── Keep body height pinned to "fill remaining viewport" on resize,
        #    and keep this card's header anchored at the top so the edited
        #    card never visibly drifts while the window is being resized ──
        def _resync(_e: tk.Event | None = None) -> None:
            if self._body_canvas is None or not self._body_canvas.winfo_exists():
                return
            self._canvas.update_idletasks()
            new_viewport_h = self._canvas.winfo_height()
            new_hdr_h      = self._hdr.winfo_reqheight() + 8
            new_body_h     = max(new_viewport_h - new_hdr_h, 200)
            self._body_canvas.configure(height=new_body_h)
            self._anchor_to_top()

        # Outer canvas resizes whenever the editor window/pane is resized
        self._resize_bind_id = self._canvas.bind("<Configure>", _resync, add=True)
        # Run once immediately in case sizes changed since the initial measure
        self._canvas.after(10, _resync)

        self._anchor_to_top()

    def _anchor_to_top(self) -> None:
        """Scroll the outer canvas so this card's header sits at the top of the viewport."""
        self._canvas.update_idletasks()
        card_y = self.winfo_y()
        total  = self._canvas.bbox("all")
        if total and total[3] > 0:
            self._canvas.yview_moveto(card_y / total[3])

    def _sync_artist(self) -> None:
        """Flush the live form values back into self._artist before destroying widgets."""
        if not self._expanded or self._body_inner is None:
            return
        self._artist = self.get_data()
        # Keep name_var in sync with whatever was typed
        self._name_var.set(self._artist.get("name", ""))

    def collapse(self) -> None:
        if not self._expanded:
            return
        # Save edits before destroying form widgets
        self._sync_artist()
        self._expanded = False
        self._arrow.config(text="▶")

        # Stop tracking outer canvas resizes for this card
        if self._resize_bind_id is not None:
            try:
                self._canvas.unbind("<Configure>", self._resize_bind_id)
            except tk.TclError:
                pass
            self._resize_bind_id = None

        # Destroy the inner scroll canvas + its scrollbar
        if self._body_canvas is not None:
            for child in list(self._inner.winfo_children()):
                if child is not self._hdr:
                    child.destroy()
            self._body_canvas = None
            self._body_inner  = None
            self._link_rows.clear()

        self._unlock_outer()

    # ── enable / disable ───────────────────────────────────────────────────

    def _toggle_enabled(self) -> None:
        self._enabled_var.set(not self._enabled_var.get())
        self._apply_enabled()

    def _update_toggle_label(self) -> None:
        if self._enabled_var.get():
            self._toggle_lbl.config(text="● ON ", fg=COLOR_OK)
        else:
            self._toggle_lbl.config(text="○ OFF", fg=_DISABLED_FG)

    def _apply_enabled(self) -> None:
        self._update_toggle_label()
        enabled = self._enabled_var.get()
        card_bg = ENTRY_BG if enabled else _DISABLED_OVERLAY
        name_fg = FG       if enabled else _DISABLED_FG
        self._inner.config(bg=card_bg)
        self._hdr.config(bg=card_bg)
        self._name_lbl.config(fg=name_fg, bg=card_bg)
        self._arrow.config(bg=card_bg)

    @property
    def enabled(self) -> bool:
        return self._enabled_var.get()

    # ── search matching ────────────────────────────────────────────────────

    def matches(self, query: str) -> bool:
        """Return True if name or any tag contains query (case-insensitive)."""
        if not query:
            return True
        q = query.lower()
        if q in self._name_var.get().lower():
            return True
        raw = self._tags_entry.get() if hasattr(self, "_tags_entry") else ""
        return any(q in t.lower() for t in raw.split(",") if t.strip())

    # ── serialise ──────────────────────────────────────────────────────────

    def get_data(self) -> dict:
        """
        Serialise card to dict.
        When collapsed the form widgets don't exist; fall back to self._artist
        (which is kept in sync on collapse via _sync_artist).
        """
        if not self._expanded:
            # Return the cached data unchanged (enabled state always live)
            entry = dict(self._artist)
            entry["enabled"] = self._enabled_var.get()
            return entry

        name  = self._name_entry.get().strip()
        notes = self._notes_text.get("1.0", "end").strip()
        tags  = [t.strip() for t in self._tags_entry.get().split(",") if t.strip()]

        # Build media dict, then sort it
        raw_media: dict[str, dict[str, Any]] = {}
        for rd in self._link_rows:
            cat  = rd["cat"].get().strip() or "socials"
            site = rd["site"].get().strip()
            url  = rd["url"].get().strip()
            if not site or not url:
                continue
            cat_d = raw_media.setdefault(cat, {})
            existing = cat_d.get(site)
            if existing is None:
                cat_d[site] = url
            elif isinstance(existing, list):
                existing.append(url)
            else:
                cat_d[site] = [existing, url]

        entry: dict[str, Any] = {"name": name, "enabled": self._enabled_var.get()}
        if tags:
            entry["tags"] = tags
        if notes:
            entry["notes"] = notes
        if raw_media:
            entry["media"] = _sort_media(raw_media)
        return entry


# ── Artist editor (scrollable list of cards) ───────────────────────────────────

class ArtistEditor(tk.Frame):
    """
    Full artist catalogue editor.

    Public API: .load(path), .save(path) → bool
    """

    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, bg=ENTRY_BG, **kw)
        self._path: Path | None = None
        self._cards: list[_ArtistCard] = []
        self._lock = _ScrollLock()    # shared by every card's header + outer canvas
        self._search_query = tk.StringVar()
        self._search_query.trace_add("write", lambda *_: self._apply_filter())
        self._build()

    # ── layout ─────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── Toolbar ────────────────────────────────────────────────────────
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x")

        _btn(bar, "+ Add artist", self._add_artist,
             bg="#1a2040", hov="#2a3060").pack(side="left", padx=(8, 4), pady=6)

        # Search box
        tk.Label(bar, text="Search:", bg=PANEL, fg=FG_DIM,
                 font=FONT_MONO).pack(side="left", padx=(8, 2))
        search_e = tk.Entry(bar, textvariable=self._search_query,
                            bg=ENTRY_BG, fg=FG, insertbackground=ACCENT,
                            relief="flat", font=FONT_BODY, width=22,
                            highlightthickness=1, highlightbackground=BORDER,
                            highlightcolor=ACCENT)
        search_e.pack(side="left", pady=6)

        _btn(bar, "✕", lambda: self._search_query.set(""),
             bg=PANEL, hov=ENTRY_BG, padx=4, pady=4,
             ).pack(side="left", padx=(2, 12))

        # Sort buttons
        tk.Label(bar, text="Sort:", bg=PANEL, fg=FG_DIM,
                 font=FONT_MONO).pack(side="left", padx=(0, 4))
        _btn(bar, "A→Z", lambda: self._sort_cards(reverse=False),
             bg="#1e1e2a", hov="#2a2a3a", padx=8, pady=4,
             ).pack(side="left", padx=(0, 4))
        _btn(bar, "Z→A", lambda: self._sort_cards(reverse=True),
             bg="#1e1e2a", hov="#2a2a3a", padx=8, pady=4,
             ).pack(side="left", padx=(0, 12))

        self._status_lbl = tk.Label(bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
        self._status_lbl.pack(side="left")

        # ── Scrollable card list ────────────────────────────────────────────
        scroll_outer = tk.Frame(self, bg=ENTRY_BG)
        scroll_outer.pack(fill="both", expand=True)
        self._canvas, self._inner, self._sb = _scrolled_frame(scroll_outer, self._lock)

    # ── data ───────────────────────────────────────────────────────────────

    def load(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            data = read_yaml(self._path)
        except YAMLError as e:
            messagebox.showerror("Load error", str(e))
            return
        artists = []
        if isinstance(data, dict):
            artists = data.get("artists", []) or []
        self._render(artists)
        self._status(f"Loaded {len(artists)} artists from {self._path.name}")

    def _render(self, artists: list[dict]) -> None:
        for w in self._inner.winfo_children():
            w.destroy()
        self._cards.clear()
        for a in artists:
            self._make_card(a)
        self._apply_filter()

    def _make_card(self, artist: dict) -> _ArtistCard:
        idx = len(self._cards)

        def _up(i: int = idx) -> None:
            if i == 0:
                return
            self._swap(i, i - 1)

        def _down(i: int = idx) -> None:
            if i >= len(self._cards) - 1:
                return
            self._swap(i, i + 1)

        card = _ArtistCard(self._inner, artist,
                           on_move_up=_up, on_move_down=_down,
                           canvas=self._canvas, outer_sb=self._sb,
                           lock=self._lock)
        card.pack(fill="x", padx=8, pady=3)
        # Forward scroll from the card's header only (visible while collapsed)
        # to the outer canvas. respect_lock=True (default) means this goes
        # inert the instant ANY card is expanded — including this one —
        # because all cards share the same _ScrollLock instance.
        # The body — built fresh on each expand() — is wired separately with
        # respect_lock=False and must never share this binding.
        card._bind_scroll_tree(card._hdr, self._canvas)
        self._cards.append(card)
        return card

    def _swap(self, i: int, j: int) -> None:
        """Swap two cards by re-rendering with their data exchanged."""
        data = [c.get_data() for c in self._cards]
        data[i], data[j] = data[j], data[i]
        # Preserve expanded state
        expanded = [c._expanded for c in self._cards]
        expanded[i], expanded[j] = expanded[j], expanded[i]
        self._render(data)
        for k, ex in enumerate(expanded):
            if ex and k < len(self._cards):
                self._cards[k].expand()
        self._status(f"{len(self._cards)} artists  (unsaved)")

    def _sort_cards(self, reverse: bool = False) -> None:
        data = [c.get_data() for c in self._cards]
        data.sort(key=lambda a: a.get("name", "").lower(), reverse=reverse)
        self._render(data)
        self._status(f"Sorted {'Z→A' if reverse else 'A→Z'} — {len(self._cards)} artists")

    def _add_artist(self) -> None:
        card = self._make_card({"name": "New Artist", "enabled": True})
        self._inner.update_idletasks()
        card.expand()
        self._canvas.yview_moveto(1.0)
        self._status(f"{len(self._cards)} artists  (unsaved)")

    # ── filter ─────────────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        q = self._search_query.get().strip()
        shown = 0
        for card in self._cards:
            if card.matches(q):
                card.pack(fill="x", padx=8, pady=3)
                shown += 1
            else:
                card.pack_forget()
        if q:
            self._status(f"Showing {shown} / {len(self._cards)} artists")
        else:
            self._status(f"{len(self._cards)} artists")

    # ── save ───────────────────────────────────────────────────────────────

    def save(self, path: str | Path | None = None) -> bool:
        target = Path(path) if path else self._path
        if target is None:
            messagebox.showerror("Save error", "No file path set.")
            return False
        artists = [c.get_data() for c in self._cards]
        try:
            write_yaml(target, {"artists": artists})
        except YAMLError as e:
            messagebox.showerror("Save error", str(e))
            return False
        self._path = target
        self._status(f"✓ Saved {len(artists)} artists → {target.name}")
        return True

    def _status(self, msg: str) -> None:
        self._status_lbl.config(text=msg)


# ── Raw YAML editor ────────────────────────────────────────────────────────────

class RawEditor(tk.Frame):
    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, bg=ENTRY_BG, **kw)
        self._path: Path | None = None
        self._build()

    def _build(self) -> None:
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x")
        _btn(bar, "⟳ Validate", self._validate,
             bg="#1e1e2a", hov="#2a2a3a").pack(side="left", padx=8, pady=6)
        self._status_lbl = tk.Label(bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
        self._status_lbl.pack(side="left", padx=(6, 0))

        txt_frame = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        txt_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self._text = tk.Text(
            txt_frame, bg="#0a0a10", fg=FG, insertbackground=ACCENT,
            relief="flat", font=FONT_MONO, wrap="none", highlightthickness=0,
        )
        xsb = ttk.Scrollbar(txt_frame, orient="horizontal", command=self._text.xview)
        ysb = ttk.Scrollbar(txt_frame, orient="vertical",   command=self._text.yview)
        self._text.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
        xsb.pack(side="bottom", fill="x")
        ysb.pack(side="right",  fill="y")
        self._text.pack(fill="both", expand=True)

    def load(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Load error", str(e))
            return
        self._text.delete("1.0", "end")
        self._text.insert("1.0", raw)
        self._status(f"Loaded {self._path.name}")

    def _validate(self) -> bool:
        try:
            yaml.safe_load(self._text.get("1.0", "end"))
            self._status("✓ Valid YAML", ok=True)
            return True
        except yaml.YAMLError as e:
            self._status(f"✗ {e}", ok=False)
            return False

    def save(self, path: str | Path | None = None) -> bool:
        if not self._validate():
            return False
        target = Path(path) if path else self._path
        if target is None:
            messagebox.showerror("Save error", "No file path set.")
            return False
        try:
            target.write_text(self._text.get("1.0", "end"), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Save error", str(e))
            return False
        self._path = target
        self._status(f"✓ Saved → {target.name}", ok=True)
        return True

    def _status(self, msg: str, ok: bool | None = None) -> None:
        color = COLOR_OK if ok is True else (COLOR_FAIL if ok is False else FG_DIM)
        self._status_lbl.config(text=msg, fg=color)


# ── Config editor ──────────────────────────────────────────────────────────────

class ConfigEditor(tk.Frame):
    _SITE_CHOICES = [
        "pixiv", "danbooru", "artstation", "twitter", "bluesky",
        "furaffinity", "tumblr", "deviantArt", "kemono",
    ]

    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, bg=ENTRY_BG, **kw)
        self._path: Path | None = None
        self._site_vars: dict[str, tk.BooleanVar] = {}
        self._path_entries: dict[str, tk.Entry] = {}
        self._ao3_entries:  dict[str, tk.Entry] = {}
        self._build()

    def _build(self) -> None:
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x")
        self._status_lbl = tk.Label(bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
        self._status_lbl.pack(side="left", padx=12, pady=6)
        outer = tk.Frame(self, bg=ENTRY_BG)
        outer.pack(fill="both", expand=True)
        _, self._inner, _ = _scrolled_frame(outer)
        self._form = tk.Frame(self._inner, bg=ENTRY_BG)
        self._form.pack(fill="x", padx=16, pady=8)

    def load(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            data = read_yaml(self._path) or {}
        except YAMLError as e:
            messagebox.showerror("Load error", str(e))
            return
        for w in self._form.winfo_children():
            w.destroy()
        self._build_form(data)
        self._status(f"Loaded {self._path.name}")

    def _row(self, label: str) -> tk.Frame:
        row = tk.Frame(self._form, bg=ENTRY_BG)
        row.pack(fill="x", pady=3)
        _lbl(row, label, width=22, anchor="w").pack(side="left")
        return row

    def _build_form(self, data: dict) -> None:
        def section(title: str) -> None:
            _lbl(self._form, title, fg=ACCENT2, font=FONT_BOLD
                 ).pack(anchor="w", pady=(12, 2))
            tk.Frame(self._form, bg=BORDER, height=1).pack(fill="x", pady=(0, 6))

        section("PATHS")
        for key, label in [
            ("gdl_config",   "Gallery-DL config"),
            ("download_dir", "Download directory"),
            ("archives_dir", "Archives directory"),
            ("log_dir",      "Log directory"),
            ("database",     "Database file"),
        ]:
            row = self._row(label)
            e = _entry(row, str(data.get(key, "") or ""), width=50)
            e.pack(side="left", fill="x", expand=True)
            self._path_entries[key] = e

        section("DOWNLOADABLE SITES")
        active = set(data.get("downloadable_sites", []) or [])
        self._site_vars = {}
        grid = tk.Frame(self._form, bg=ENTRY_BG)
        grid.pack(fill="x")
        for i, site in enumerate(self._SITE_CHOICES):
            var = tk.BooleanVar(value=site in active)
            self._site_vars[site] = var
            tk.Checkbutton(grid, text=site, variable=var,
                           bg=ENTRY_BG, fg=FG, selectcolor=ENTRY_BG,
                           activebackground=ENTRY_BG, activeforeground=ACCENT,
                           font=FONT_MONO,
                           ).grid(row=i // 3, column=i % 3, sticky="w", padx=8, pady=2)
        extra_row = tk.Frame(self._form, bg=ENTRY_BG)
        extra_row.pack(fill="x", pady=(4, 0))
        _lbl(extra_row, "extra sites:").pack(side="left", padx=(0, 6))
        extra = [s for s in active if s not in self._SITE_CHOICES]
        self._extra_sites_entry = _entry(extra_row, ", ".join(extra), width=40)
        self._extra_sites_entry.pack(side="left")
        _lbl(extra_row, " comma-separated").pack(side="left", padx=(4, 0))

        section("AO3")
        ao3 = data.get("ao3", {}) or {}
        self._ao3_entries = {}
        for key, label in [
            ("source_dir",   "AO3 source dir"),
            ("transfer_dir", "Transfer dir"),
            ("archive",      "Download archive"),
        ]:
            row = self._row(label)
            e = _entry(row, str(ao3.get(key, "") or ""), width=50)
            e.pack(side="left", fill="x", expand=True)
            self._ao3_entries[key] = e

        section("FIC TRACKER")
        fic = data.get("fic_tracker", {}) or {}
        row = self._row("Fic list file")
        self._fic_file_entry = _entry(row, str(fic.get("fic_file", "") or ""), width=50)
        self._fic_file_entry.pack(side="left", fill="x", expand=True)
        row2 = self._row("Stale months")
        self._stale_entry = _entry(row2, str(fic.get("stale_months", 6)), width=6)
        self._stale_entry.pack(side="left")

    def save(self, path: str | Path | None = None) -> bool:
        target = Path(path) if path else self._path
        if target is None:
            messagebox.showerror("Save error", "No file path set.")
            return False
        try:
            existing = read_yaml(target) or {}
        except YAMLError:
            existing = {}
        for key, e in self._path_entries.items():
            v = e.get().strip()
            if v:
                existing[key] = v
        sites = [s for s, v in self._site_vars.items() if v.get()]
        extra = [s.strip() for s in self._extra_sites_entry.get().split(",") if s.strip()]
        existing["downloadable_sites"] = sites + extra
        ao3 = existing.get("ao3", {}) or {}
        for key, e in self._ao3_entries.items():
            v = e.get().strip()
            if v:
                ao3[key] = v
        existing["ao3"] = ao3
        fic = existing.get("fic_tracker", {}) or {}
        ff = self._fic_file_entry.get().strip()
        if ff:
            fic["fic_file"] = ff
        try:
            fic["stale_months"] = int(self._stale_entry.get().strip() or "6")
        except ValueError:
            pass
        existing["fic_tracker"] = fic
        try:
            write_yaml(target, existing)
        except YAMLError as e:
            messagebox.showerror("Save error", str(e))
            return False
        self._path = target
        self._status(f"✓ Saved → {target.name}", ok=True)
        return True

    def _status(self, msg: str, ok: bool | None = None) -> None:
        color = COLOR_OK if ok is True else (COLOR_FAIL if ok is False else FG_DIM)
        self._status_lbl.config(text=msg, fg=color)


# ── Toplevel window ────────────────────────────────────────────────────────────

_OPEN_WINDOW: tk.Toplevel | None = None


def open_yaml_editor(
    root: tk.Tk,
    catalogue_path: str,
    config_path: str,
    on_saved: Callable[[str], None] | None = None,
) -> None:
    global _OPEN_WINDOW

    if _OPEN_WINDOW is not None and _OPEN_WINDOW.winfo_exists():
        _OPEN_WINDOW.lift()
        _OPEN_WINDOW.focus_set()
        return

    win = tk.Toplevel(root)
    win.title("YAML Editor")
    win.geometry(f"{_WIN_W}x{_WIN_H}")
    win.configure(bg=BG)
    win.minsize(760, 540)
    win.transient(root)     # stay on top of / tied to the main window
    win.grab_set()          # makes click/keyboard modal
    _OPEN_WINDOW = win

    # The main window routes ALL wheel events through a single app-wide
    # bind_all handler (ui.scroll._global_scroll) that picks whichever
    # registered canvas is geometrically under the pointer — it has no idea
    # this modal dialog is stacked on top. Suspending it for the duration
    # this editor is open is what actually stops the window underneath from
    # scrolling; grab_set() alone does not cover <MouseWheel>.
    suspend_global_scroll(True)

    def _on_close() -> None:
        global _OPEN_WINDOW
        win.grab_release()
        suspend_global_scroll(False)
        _OPEN_WINDOW = None
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _on_close)
    # Safety net: if the window is destroyed through any other path (not via
    # _on_close), make sure the main window's scroll never stays frozen.
    win.bind("<Destroy>", lambda _e: suspend_global_scroll(False), add=True)

    # Title bar
    hdr = tk.Frame(win, bg=BG, pady=10)
    hdr.pack(fill="x", padx=20)
    tk.Label(hdr, text="✦ YAML EDITOR",
             bg=BG, fg=ACCENT, font=FONT_HEAD).pack(side="left")
    tk.Label(hdr, text="edit artists & config in-app",
             bg=BG, fg=FG_DIM, font=FONT_SUB).pack(side="left", padx=(10, 0))
    tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

    # Notebook styles
    style = ttk.Style(win)
    style.configure("Editor.TNotebook", background=BG, borderwidth=0)
    style.configure("Editor.TNotebook.Tab", background=BG, foreground=FG_DIM,
                    font=FONT_BOLD, padding=[16, 8])
    style.map("Editor.TNotebook.Tab",
              background=[("selected", PANEL), ("active", ENTRY_BG)],
              foreground=[("selected", ACCENT), ("active", FG)])

    nb = ttk.Notebook(win, style="Editor.TNotebook")
    nb.pack(fill="both", expand=True)

    # ── ARTISTS tab ────────────────────────────────────────────────────────
    artists_outer = tk.Frame(nb, bg=PANEL)
    nb.add(artists_outer, text="  👤  Artists  ")

    inner_nb = ttk.Notebook(artists_outer, style="Editor.TNotebook")
    inner_nb.pack(fill="both", expand=True)

    art_form_frame = tk.Frame(inner_nb, bg=ENTRY_BG)
    art_raw_frame  = tk.Frame(inner_nb, bg=ENTRY_BG)
    inner_nb.add(art_form_frame, text="  Form view  ")
    inner_nb.add(art_raw_frame,  text="  Raw YAML  ")

    artist_editor = ArtistEditor(art_form_frame)
    artist_editor.pack(fill="both", expand=True)
    artist_editor.load(catalogue_path)

    art_raw_editor = RawEditor(art_raw_frame)
    art_raw_editor.pack(fill="both", expand=True)
    art_raw_editor.load(catalogue_path)

    def _on_inner_tab(_e: tk.Event) -> None:
        if inner_nb.index(inner_nb.select()) == 0:
            artist_editor.load(catalogue_path)

    inner_nb.bind("<<NotebookTabChanged>>", _on_inner_tab)

    # Save bar
    art_save_bar = tk.Frame(artists_outer, bg=PANEL, pady=6)
    art_save_bar.pack(fill="x", padx=16)
    art_status = tk.Label(art_save_bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    art_status.pack(side="left")

    def _save_artists() -> None:
        tab = inner_nb.index(inner_nb.select())
        ok = artist_editor.save(catalogue_path) if tab == 0 else art_raw_editor.save(catalogue_path)
        if ok and on_saved:
            on_saved("catalogue")
        art_status.config(text="✓ Saved" if ok else "✗ Save failed",
                          fg=COLOR_OK if ok else COLOR_FAIL)

    _btn(art_save_bar, "💾  Save artists.yaml", _save_artists,
         bg="#2a1a4a", hov="#3a2a6a").pack(side="right")

    # ── CONFIG tab ─────────────────────────────────────────────────────────
    config_outer = tk.Frame(nb, bg=PANEL)
    nb.add(config_outer, text="  ⚙  Config  ")

    cfg_inner_nb = ttk.Notebook(config_outer, style="Editor.TNotebook")
    cfg_inner_nb.pack(fill="both", expand=True)

    cfg_form_frame = tk.Frame(cfg_inner_nb, bg=ENTRY_BG)
    cfg_raw_frame  = tk.Frame(cfg_inner_nb, bg=ENTRY_BG)
    cfg_inner_nb.add(cfg_form_frame, text="  Form view  ")
    cfg_inner_nb.add(cfg_raw_frame,  text="  Raw YAML  ")

    config_editor = ConfigEditor(cfg_form_frame)
    config_editor.pack(fill="both", expand=True)
    config_editor.load(config_path)

    cfg_raw_editor = RawEditor(cfg_raw_frame)
    cfg_raw_editor.pack(fill="both", expand=True)
    cfg_raw_editor.load(config_path)

    cfg_save_bar = tk.Frame(config_outer, bg=PANEL, pady=6)
    cfg_save_bar.pack(fill="x", padx=16)
    cfg_status = tk.Label(cfg_save_bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    cfg_status.pack(side="left")

    def _save_config() -> None:
        tab = cfg_inner_nb.index(cfg_inner_nb.select())
        ok = config_editor.save(config_path) if tab == 0 else cfg_raw_editor.save(config_path)
        if ok and on_saved:
            on_saved("config")
        cfg_status.config(text="✓ Saved" if ok else "✗ Save failed",
                          fg=COLOR_OK if ok else COLOR_FAIL)

    _btn(cfg_save_bar, "💾  Save config.yaml", _save_config,
         bg="#1a2a1a", hov="#2a3a2a").pack(side="right")