"""
ui/widgets.py
-------------
All reusable tkinter widgets and UI primitives.

Contents
--------
  Primitives (functions)
    styled_button()   — themed tk.Button with hover effect
    styled_entry()    — themed tk.Entry with placeholder support
    section_label()   — dimmed uppercase section header
    divider()         — thin horizontal rule

  Composite widgets (classes)
    Tooltip           — dark floating tooltip on hover
    ArtistList        — scrollable columnar checklist of artists
    StatusPanel       — live download status tree with circle indicators
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from ui.theme import (
    ACCENT, ACCENT2, BG, BORDER, BTN_BG, BTN_FG, BTN_HOV,
    COLS, ENTRY_BG, FG, FG_DIM, FONT_BODY, FONT_BOLD, FONT_BTN,
    FONT_MONO, FONT_STRI, FONT_TAGS, PAD_OUTER, PANEL, ROW_ALT,
    SEL_BG, STATUS_CIRCLES,
)
from ui.scroll import register_scroll_canvas, set_scroll_enabled


# ── Primitives ─────────────────────────────────────────────────────────────────

def styled_button(
    parent: tk.Widget,
    text: str,
    command: Callable | None = None,
    bg: str = BTN_BG,
    hov: str = BTN_HOV,
    **kwargs,
) -> tk.Button:
    """Themed flat button with hover colour transition."""
    kwargs.setdefault("padx", 18)
    kwargs.setdefault("pady", 8)
    btn = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=BTN_FG, relief="flat",
        font=FONT_BTN, cursor="hand2",
        activebackground=hov, activeforeground=BTN_FG,
        **kwargs,
    )
    btn.bind("<Enter>", lambda _e: btn.config(bg=hov))
    btn.bind("<Leave>", lambda _e: btn.config(bg=bg))
    return btn


def styled_entry(
    parent: tk.Widget,
    placeholder: str = "",
    width: int = 40,
) -> tuple[tk.Frame, tk.Entry]:
    """
    Themed entry field with optional placeholder text.
    Returns (outer_frame, entry) so callers can pack/grid the frame.
    """
    frame = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
    entry = tk.Entry(
        frame, bg=ENTRY_BG, fg=FG,
        insertbackground=ACCENT,
        relief="flat", font=FONT_BODY, width=width,
    )
    entry.pack(padx=4, pady=4, fill="x")

    if placeholder:
        entry.insert(0, placeholder)
        entry.config(fg=FG_DIM)

        def _focus_in(_e: tk.Event) -> None:
            if entry.get() == placeholder:
                entry.delete(0, "end")
                entry.config(fg=FG)

        def _focus_out(_e: tk.Event) -> None:
            if not entry.get():
                entry.insert(0, placeholder)
                entry.config(fg=FG_DIM)

        entry.bind("<FocusIn>",  _focus_in)
        entry.bind("<FocusOut>", _focus_out)

    return frame, entry


def section_label(parent: tk.Widget, text: str) -> tk.Label:
    """Dimmed all-caps section header label."""
    lbl = tk.Label(
        parent, text=text, bg=PANEL, fg=ACCENT2,
        font=FONT_BOLD, anchor="w",
    )
    lbl.pack(fill="x", padx=PAD_OUTER, pady=(18, 4))
    return lbl


def divider(parent: tk.Widget) -> tk.Frame:
    """1 px horizontal rule."""
    rule = tk.Frame(parent, bg=BORDER, height=1)
    rule.pack(fill="x", padx=PAD_OUTER, pady=6)
    return rule


# ── Tooltip ────────────────────────────────────────────────────────────────────

class Tooltip:
    """
    Dark floating tooltip displayed below a widget on hover.

    Parameters
    ----------
    widget  : the widget that triggers the tooltip
    text_fn : zero-argument callable that returns the tooltip string.
              Evaluated lazily each time the tooltip appears.
    """

    def __init__(self, widget: tk.Widget, text_fn: Callable[[], str]) -> None:
        self._widget  = widget
        self._text_fn = text_fn
        self._win: tk.Toplevel | None = None
        widget.bind("<Enter>",   self._show, add="+")
        widget.bind("<Leave>",   self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _show(self, _e: tk.Event | None = None) -> None:
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
        tk.Label(
            tw, text=text, bg="#1a1a28", fg=FG,
            font=FONT_MONO, justify="left", padx=10, pady=6,
        ).pack()

    def _hide(self, _e: tk.Event | None = None) -> None:
        if self._win:
            self._win.destroy()
            self._win = None


# ── ArtistList ─────────────────────────────────────────────────────────────────

class ArtistList(tk.Frame):
    """
    Scrollable columnar grid of artist checkboxes.

    - Unchecked artists: dimmed + strikethrough
    - Checked artists:   bright + bold
    - Hover tooltip:     shows tags, notes, and all media links
    - Toolbar:           All / None / Invert  +  selection counter
    """

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, bg=PANEL, **kwargs)
        self._all_artists:  list[dict]        = []
        self._dl_sites:     set[str]          = set()
        self._check_state:  dict[str, bool]   = {}   # name → checked
        self.artists:       list[dict]        = []   # currently visible
        self.check_vars:    list[tk.BooleanVar]  = []
        self.check_btns:    list[tk.Checkbutton] = []
        self._filter_tags:  list[str]         = []   # active tag chips
        self._build()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── filter row: name + tag search entries side by side ────────────────
        filter_row = tk.Frame(self, bg=PANEL)
        filter_row.pack(fill="x", padx=PAD_OUTER, pady=(6, 2))

        tk.Label(filter_row, text="filter:", bg=PANEL, fg=FG_DIM,
                 font=FONT_MONO).pack(side="left", padx=(0, 6))

        # Name search
        name_border = tk.Frame(filter_row, bg=BORDER, padx=1, pady=1)
        name_border.pack(side="left", padx=(0, 6))
        self._name_var = tk.StringVar()
        self._name_var.trace_add("write", lambda *_: self._apply_filter())
        tk.Entry(name_border, textvariable=self._name_var,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG,
                 relief="flat", font=FONT_MONO, width=20,
                 ).pack(padx=4, pady=3)

        # Tag search — focusing it opens the dropdown
        tag_border = tk.Frame(filter_row, bg=BORDER, padx=1, pady=1)
        tag_border.pack(side="left", padx=(0, 6))
        self._tag_search_var = tk.StringVar()
        self._tag_search_var.trace_add("write", self._on_tag_search_change)
        self._tag_search_entry = tk.Entry(
            tag_border, textvariable=self._tag_search_var,
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            relief="flat", font=FONT_MONO, width=16,
        )
        self._tag_search_entry.pack(padx=4, pady=3)
        self._tag_search_entry.bind("<FocusIn>",  lambda _e: self._open_tag_dropdown())
        self._tag_search_entry.bind("<Escape>",   lambda _e: self._close_tag_dropdown())
        self._tag_search_entry.bind("<FocusOut>", self._on_tag_focus_out)

        # Dropdown panel (placed via place() when open)
        self._dropdown: tk.Frame | None = None

        # ── chip row ──────────────────────────────────────────────────────────
        chip_row = tk.Frame(self, bg=PANEL)
        chip_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 2))
        self._chip_frame = tk.Frame(chip_row, bg=PANEL)
        self._chip_frame.pack(side="left", fill="x")

        # ── toolbar ───────────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=PANEL)
        toolbar.pack(fill="x", padx=PAD_OUTER, pady=(2, 4))

        self.count_label = tk.Label(
            toolbar, text="0 / 0 selected",
            bg=PANEL, fg=FG_DIM, font=FONT_MONO, anchor="w",
        )
        self.count_label.pack(side="left")

        for label, cmd in [
            ("Invert", self._invert),
            ("None",   self._select_none),
            ("All",    self._select_all),
        ]:
            styled_button(
                toolbar, label, command=cmd,
                bg="#2a2a38", hov="#3a3a50", padx=10, pady=4,
            ).pack(side="right", padx=(4, 0))

        # ── scrollable grid ───────────────────────────────────────────────────
        outer = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="both", expand=True, padx=PAD_OUTER, pady=(0, 6))

        self.canvas = tk.Canvas(outer, bg=ENTRY_BG, highlightthickness=0, height=0)
        self._sb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._sb.set)
        self._sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=ENTRY_BG)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self._win_id, width=e.width),
        )
        register_scroll_canvas(self.canvas)

    # ── tag dropdown ──────────────────────────────────────────────────────────

    def _all_tags(self) -> list[str]:
        tags: set[str] = set()
        for a in self._all_artists:
            for t in (a.get("tags") or []):
                tags.add(t.lower())
        return sorted(tags)

    def _on_tag_search_change(self, *_) -> None:
        """Filter the dropdown list as the user types; open it if not already."""
        if self._dropdown and self._dropdown.winfo_exists():
            self._rebuild_dropdown_list()
        else:
            self._open_tag_dropdown()

    def _on_tag_focus_out(self, event: tk.Event) -> None:
        """Close only if focus moved outside the dropdown."""
        # Give tkinter a tick to update focus before deciding
        self.after(50, self._maybe_close_dropdown)

    def _maybe_close_dropdown(self) -> None:
        if not (self._dropdown and self._dropdown.winfo_exists()):
            return
        focused = self.focus_get()
        if focused is None:
            return
        # Keep open if focus is inside the dropdown
        try:
            if str(focused).startswith(str(self._dropdown)):
                return
        except Exception:
            pass
        self._close_tag_dropdown()

    def _open_tag_dropdown(self) -> None:
        if self._dropdown and self._dropdown.winfo_exists():
            self._rebuild_dropdown_list()
            return

        all_tags = self._all_tags()
        if not all_tags:
            return

        root = self.winfo_toplevel()
        dd = tk.Frame(root, bg=BORDER, padx=1, pady=1)
        self._dropdown = dd

        inner = tk.Frame(dd, bg=ENTRY_BG)
        inner.pack(fill="both", expand=True)

        # Scrollable tag list — height adjusted dynamically after each rebuild
        self._dd_list_canvas = tk.Canvas(inner, bg=ENTRY_BG, highlightthickness=0,
                                         height=0, width=140)
        self._dd_list_sb = ttk.Scrollbar(inner, orient="vertical",
                                         command=self._dd_list_canvas.yview)
        self._dd_list_canvas.configure(yscrollcommand=self._dd_list_sb.set)
        self._dd_list_canvas.pack(side="left", fill="both", expand=True, pady=2)
        register_scroll_canvas(self._dd_list_canvas)

        self._dd_list_frame = tk.Frame(self._dd_list_canvas, bg=ENTRY_BG)
        list_win = self._dd_list_canvas.create_window((0, 0), window=self._dd_list_frame,
                                                      anchor="nw")
        self._dd_list_frame.bind("<Configure>",
            lambda _e: self._dd_list_canvas.configure(
                scrollregion=self._dd_list_canvas.bbox("all")))
        self._dd_list_canvas.bind("<Configure>",
            lambda e: self._dd_list_canvas.itemconfig(list_win, width=e.width))

        # Position below the tag search entry
        self._tag_search_entry.update_idletasks()
        ex = self._tag_search_entry.winfo_rootx() - root.winfo_rootx()
        ey = (self._tag_search_entry.winfo_rooty() - root.winfo_rooty()
              + self._tag_search_entry.winfo_height() + 4)
        dd.place(x=ex, y=ey)
        dd.lift()

        self._rebuild_dropdown_list()

    _DD_MAX_H = 120  # cap before scrolling kicks in (~6 rows)

    def _rebuild_dropdown_list(self) -> None:
        if not (self._dropdown and self._dropdown.winfo_exists()):
            return
        for w in self._dd_list_frame.winfo_children():
            w.destroy()

        q     = self._tag_search_var.get().strip().lower()
        tags  = self._all_tags()
        shown = [t for t in tags if not q or q in t]

        if not shown:
            tk.Label(self._dd_list_frame, text="  no tags match",
                     bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO,
                     ).pack(anchor="w", padx=8, pady=4)
        else:
            for tag in shown:
                active = tag in self._filter_tags
                bg_c   = SEL_BG if active else ENTRY_BG
                fg_c   = ACCENT if active else FG_DIM
                row    = tk.Frame(self._dd_list_frame, bg=bg_c, cursor="hand2")
                row.pack(fill="x")
                lbl = tk.Label(row, text=("✔  " if active else "   ") + tag,
                               bg=bg_c, fg=fg_c, font=FONT_MONO, anchor="w",
                               cursor="hand2")
                lbl.pack(fill="x", padx=6, pady=1)
                for w in (row, lbl):
                    w.bind("<Button-1>", lambda _e, t=tag: self._on_dd_tag_click(t))
                    w.bind("<Enter>",    lambda _e, r=row: r.config(bg=BORDER))
                    w.bind("<Leave>",    lambda _e, r=row, b=bg_c: r.config(bg=b))

        # Resize canvas to content, capped at _DD_MAX_H; show scrollbar only if needed
        self._dd_list_frame.update_idletasks()
        content_h = self._dd_list_frame.winfo_reqheight()
        if content_h <= self._DD_MAX_H:
            self._dd_list_canvas.config(height=content_h)
            self._dd_list_sb.pack_forget()
        else:
            self._dd_list_canvas.config(height=self._DD_MAX_H)
            self._dd_list_sb.pack(side="right", fill="y", before=self._dd_list_canvas)

    def _on_dd_tag_click(self, tag: str) -> None:
        if tag in self._filter_tags:
            self._filter_tags.remove(tag)
        else:
            self._filter_tags.append(tag)
        self._rebuild_chips()
        self._apply_filter()
        self._rebuild_dropdown_list()
        # Keep focus on the search entry
        self._tag_search_entry.focus_set()

    def _close_tag_dropdown(self) -> None:
        if self._dropdown and self._dropdown.winfo_exists():
            self._dropdown.destroy()
        self._dropdown = None

    # ── tag chips ─────────────────────────────────────────────────────────────

    def _remove_tag_chip(self, tag: str) -> None:
        if tag in self._filter_tags:
            self._filter_tags.remove(tag)
        self._rebuild_chips()
        self._apply_filter()

    def _rebuild_chips(self) -> None:
        for w in self._chip_frame.winfo_children():
            w.destroy()
        for tag in self._filter_tags:
            chip = tk.Frame(self._chip_frame, bg=ACCENT, padx=4, pady=1,
                            cursor="hand2")
            chip.pack(side="left", padx=(0, 3))
            lbl = tk.Label(chip, text=f"{tag}  ×", bg=ACCENT, fg=BTN_FG,
                           font=FONT_TAGS, cursor="hand2")
            lbl.pack()
            chip.bind("<Button-1>", lambda _e, t=tag: self._remove_tag_chip(t))
            lbl.bind( "<Button-1>", lambda _e, t=tag: self._remove_tag_chip(t))

    # ── filtering ─────────────────────────────────────────────────────────────

    def _matches(self, artist: dict) -> bool:
        name_q = self._name_var.get().strip().lower()
        if name_q and name_q not in artist.get("name", "").lower():
            return False
        if self._filter_tags:
            atags = [t.lower() for t in (artist.get("tags") or [])]
            if not all(ft in atags for ft in self._filter_tags):
                return False
        return True

    def _apply_filter(self) -> None:
        """Re-render the grid with only matching artists, preserving check state."""
        # Save current check state
        for artist, var in zip(self.artists, self.check_vars):
            self._check_state[artist.get("name", "")] = var.get()

        filtered = [a for a in self._all_artists if self._matches(a)]
        self._render(filtered)

    # ── data loading ──────────────────────────────────────────────────────────

    def load(self, artists: list[dict], dl_sites: set[str] | None = None) -> None:
        """Repopulate the checklist with a new list of artists."""
        self._all_artists = artists
        self._dl_sites    = dl_sites or set()
        self._check_state = {}
        self._apply_filter()

    def _render(self, artists: list[dict]) -> None:
        """Rebuild the inner grid for a (possibly filtered) artist list."""
        for w in self.inner.winfo_children():
            w.destroy()
        self.artists    = artists
        self.check_vars = []
        self.check_btns = []

        if not artists:
            msg = ("  No artists match the filter." if self._all_artists
                   else "  No artists found — load a catalogue file.")
            tk.Label(self.inner, text=msg,
                     bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO,
                     ).pack(anchor="w", padx=16, pady=12)
            self._update_count()
            self.inner.after_idle(self._adjust_height)
            return

        for c in range(COLS):
            self.inner.columnconfigure(c, weight=1, uniform="col")

        for i, artist in enumerate(artists):
            name    = artist.get("name", "???")
            checked = self._check_state.get(name, False)
            var     = tk.BooleanVar(value=checked)
            self.check_vars.append(var)

            row, col = divmod(i, COLS)
            cell_bg  = ENTRY_BG if (row % 2 == 0) else ROW_ALT
            cell     = tk.Frame(self.inner, bg=cell_bg)
            cell.grid(row=row, column=col, sticky="ew", padx=1, pady=1)

            cb = tk.Checkbutton(
                cell,
                text=name,
                variable=var,
                bg=cell_bg, fg=FG if checked else FG_DIM,
                selectcolor=SEL_BG,
                activebackground=cell_bg, activeforeground=ACCENT,
                font=FONT_BOLD if checked else FONT_STRI,
                anchor="w", cursor="hand2",
            )
            cb.config(command=lambda v=var, b=cb, n=name: self._on_toggle(v, b, n))
            cb.pack(fill="x", padx=8, pady=5)
            self.check_btns.append(cb)

            Tooltip(cb, self._make_tooltip_fn(artist, self._dl_sites))

        self._update_count()
        self.inner.after_idle(self._adjust_height)

    # ── dynamic height ────────────────────────────────────────────────────────

    _MAX_HEIGHT = 400   # px — canvas is always this tall; scroll only if content overflows

    def _adjust_height(self) -> None:
        """
        Always keep the canvas at _MAX_HEIGHT.
        Only enable scrolling (and show the scrollbar) if content overflows.
        """
        self.inner.update_idletasks()
        content_h = self.inner.winfo_reqheight()
        self.canvas.config(height=self._MAX_HEIGHT)
        if content_h > self._MAX_HEIGHT:
            if not self._sb.winfo_ismapped():
                self._sb.pack(side="right", fill="y")
            set_scroll_enabled(self.canvas, True)
        else:
            self._sb.pack_forget()
            self.canvas.yview_moveto(0)
            set_scroll_enabled(self.canvas, False)

    @staticmethod
    def _make_tooltip_fn(artist: dict, dl_sites: set[str]) -> Callable[[], str]:
        def _text() -> str:
            lines: list[str] = []

            # Notes first — primary human info
            notes = (artist.get("notes") or artist.get("note") or "").strip()
            if notes:
                lines.append(notes)

            # Count total links and downloadable links
            media      = artist.get("media") or {}
            total_links = 0
            dl_links    = 0
            for cat_entries in media.values():
                if isinstance(cat_entries, dict):
                    for site, url in cat_entries.items():
                        urls = url if isinstance(url, list) else [url]
                        total_links += len(urls)
                        if site.lower() in dl_sites:
                            dl_links += len(urls)

            if total_links:
                if lines:
                    lines.append("")
                lines.append(f"links:  {total_links} total  /  {dl_links} downloadable")

            # Tags last
            tags = artist.get("tags", [])
            if tags:
                lines.append("tags:   " + "  ".join(f"[{t}]" for t in tags))

            return "\n".join(lines) if lines else artist.get("name", "")
        return _text

    # ── toggle styling ────────────────────────────────────────────────────────

    def _on_toggle(self, var: tk.BooleanVar, btn: tk.Checkbutton, name: str) -> None:
        checked = var.get()
        btn.config(
            fg=FG          if checked else FG_DIM,
            font=FONT_BOLD if checked else FONT_STRI,
        )
        self._check_state[name] = checked
        self._update_count()

    def _refresh_all_styles(self) -> None:
        for artist, var, btn in zip(self.artists, self.check_vars, self.check_btns):
            checked = var.get()
            btn.config(
                fg=FG          if checked else FG_DIM,
                font=FONT_BOLD if checked else FONT_STRI,
            )
            self._check_state[artist.get("name", "")] = checked

    # ── selection helpers ─────────────────────────────────────────────────────

    def _update_count(self) -> None:
        # Show selected / total across ALL artists, not just visible
        total    = len(self._all_artists)
        selected = sum(self._check_state.get(a.get("name", ""), False)
                       for a in self._all_artists)
        visible  = len(self.artists)
        suffix   = f"  ({visible} shown)" if visible != total else ""
        self.count_label.config(text=f"{selected} / {total} selected{suffix}")

    def _select_all(self) -> None:
        for v in self.check_vars:
            v.set(True)
        self._refresh_all_styles()
        self._update_count()

    def _select_none(self) -> None:
        for v in self.check_vars:
            v.set(False)
        self._refresh_all_styles()
        self._update_count()

    def _invert(self) -> None:
        for v in self.check_vars:
            v.set(not v.get())
        self._refresh_all_styles()
        self._update_count()

    def get_selected(self) -> list[dict]:
        """Return all checked artists across the full (unfiltered) list."""
        return [a for a in self._all_artists
                if self._check_state.get(a.get("name", ""), False)]


# ── StatusPanel ────────────────────────────────────────────────────────────────

def _dim_color(hex_color: str) -> str:
    """Return a lighter/dimmer version of a hex color for file count labels."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    # Blend toward background (ENTRY_BG = #1a1a2e) at 50%
    bg_r, bg_g, bg_b = 0x1a, 0x1a, 0x2e
    r = (r + bg_r) // 2
    g = (g + bg_g) // 2
    b = (b + bg_b) // 2
    return f"#{r:02x}{g:02x}{b:02x}"


class StatusPanel(tk.Frame):
    """
    Scrollable status tree showing per-artist and per-site download states.
    Each row has a coloured circle emoji reflecting its current status.
    File counts are shown live in a dimmed version of the status color.
    """

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, bg=ENTRY_BG, **kwargs)
        self._artist_rows:  dict[str, dict] = {}   # artist → {lbl, count_lbl, status, count}
        self._site_rows:    dict[tuple, dict] = {}  # key → {lbl, count_lbl, status, count}

        canvas = tk.Canvas(self, bg=ENTRY_BG, highlightthickness=0)
        sb     = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        # Worker panel placed above the scrollable status list
        self._workers_frame = tk.Frame(self, bg=ENTRY_BG)
        self._workers_frame.pack(fill="x", padx=6, pady=(6, 2))

        canvas.pack(side="left", fill="both", expand=True)
        register_scroll_canvas(canvas)

        self._inner = tk.Frame(canvas, bg=ENTRY_BG)
        win = canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

        # map worker index -> widgets
        self._worker_rows: dict[int, dict] = {}

    # ── population ────────────────────────────────────────────────────────────

    def populate(self, jobs: list[dict]) -> None:
        """Build the status tree from a flat job list."""
        for w in self._inner.winfo_children():
            w.destroy()
        self._artist_rows.clear()
        self._site_rows.clear()

        by_artist: dict[str, list[dict]] = {}
        for job in jobs:
            by_artist.setdefault(job["artist"], []).append(job)

        circle, _ = STATUS_CIRCLES["pending"]

        for artist_name, artist_jobs in by_artist.items():
            # Artist row: circle + name on left, count on right
            arow = tk.Frame(self._inner, bg=ENTRY_BG)
            arow.pack(fill="x", padx=6, pady=(6, 1))
            albl = tk.Label(arow, text=f"{circle}  {artist_name}",
                            bg=ENTRY_BG, fg=FG, font=FONT_BOLD, anchor="w")
            albl.pack(side="left")
            acount = tk.Label(arow, text="", bg=ENTRY_BG, fg=FG_DIM,
                              font=FONT_MONO, anchor="w")
            acount.pack(side="left", padx=(8, 0))
            self._artist_rows[artist_name] = {
                "lbl": albl, "count_lbl": acount,
                "status": "pending", "count": 0,
            }

            # Site rows
            for job in artist_jobs:
                key   = (job["artist"], job["site"], job["url"])
                srow  = tk.Frame(self._inner, bg=ENTRY_BG)
                srow.pack(fill="x", padx=6)
                slbl  = tk.Label(srow,
                                 text=f"    {circle}  {job['site']}  —  {job['url']}",
                                 bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO, anchor="w")
                slbl.pack(side="left")
                scount = tk.Label(srow, text="", bg=ENTRY_BG, fg=FG_DIM,
                                  font=FONT_MONO, anchor="w")
                scount.pack(side="left", padx=(8, 0))

                # Per-site mini progress bar (shows current session / DB total)
                prog_var = tk.IntVar(value=0)
                try:
                    from core.file_db import get_db
                    db = get_db()
                    total = db.total(artist=job["artist"], site=job["site"]) if db else 0
                except Exception:
                    total = 0
                prog_bar = ttk.Progressbar(
                    srow, variable=prog_var, maximum=max(total, 1),
                    length=120,
                    style="Download.Horizontal.TProgressbar",
                )
                prog_bar.pack(side="left", padx=(10, 0))
                total_lbl = tk.Label(srow,
                                     text=f"/ {total}" if total else "",
                                     bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO)
                total_lbl.pack(side="left", padx=(4, 0))

                self._site_rows[key] = {
                    "lbl": slbl, "count_lbl": scount,
                    "status": "pending", "count": 0,
                    "prog_bar": prog_bar, "prog_var": prog_var,
                    "total_lbl": total_lbl,
                }

    # ── worker rows ───────────────────────────────────────────────────────

    def set_worker_start(self, worker_idx: int, artist: str, site: str, url: str) -> None:
        """Create or update a worker row showing its current job and progress."""
        row = self._worker_rows.get(worker_idx)
        label_text = f"Worker {worker_idx}: {site} — {url}"
        if row:
            row["lbl"].config(text=label_text, fg=FG)
            row["prog_var"].set(0)
            row["total_lbl"].config(text="")
            row["status"] = "running"
            return

        wrow = tk.Frame(self._workers_frame, bg=ENTRY_BG)
        wrow.pack(fill="x", pady=(2, 0))
        wlbl = tk.Label(wrow, text=label_text, bg=ENTRY_BG, fg=FG, font=FONT_MONO, anchor="w")
        wlbl.pack(side="left")

        prog_var = tk.IntVar(value=0)
        prog_bar = ttk.Progressbar(wrow, variable=prog_var, maximum=1, length=160,
                                   style="Download.Horizontal.TProgressbar")
        prog_bar.pack(side="left", padx=(10, 0))
        total_lbl = tk.Label(wrow, text="", bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO)
        total_lbl.pack(side="left", padx=(4, 0))

        self._worker_rows[worker_idx] = {
            "frame": wrow, "lbl": wlbl, "prog_bar": prog_bar,
            "prog_var": prog_var, "total_lbl": total_lbl, "status": "running",
        }

    def set_worker_progress(self, worker_idx: int, artist: str, site: str, url: str, count: int, skipped: int) -> None:
        row = self._worker_rows.get(worker_idx)
        if not row:
            # lazily create if missing
            self.set_worker_start(worker_idx, artist, site, url)
            row = self._worker_rows.get(worker_idx)
        try:
            from core.file_db import get_db
            db = get_db()
            total = db.total(artist=artist, site=site) if db else 0
        except Exception:
            total = 0
        if total > 0:
            row["prog_var"].set(count)
            row["prog_bar"].config(maximum=max(total, count))
            row["total_lbl"].config(text=f"/ {total}")
        else:
            # no DB total — show count only as indeterminate-ish value
            row["prog_var"].set(count)
            row["prog_bar"].config(maximum=max(1, count))

    def set_worker_done(self, worker_idx: int, artist: str, site: str, url: str, success: bool | None) -> None:
        row = self._worker_rows.get(worker_idx)
        if not row:
            return
        status_text = "idle" if success is None else ("ok" if success else "fail")
        row["lbl"].config(text=f"Worker {worker_idx}: {status_text}", fg=FG_DIM)
        row["status"] = "idle"
        row["prog_var"].set(0)

    # ── status updates ────────────────────────────────────────────────────────

    def set_site_status(self, artist: str, site: str, url: str, status: str) -> None:
        key  = (artist, site, url)
        row  = self._site_rows.get(key)
        if not row:
            return
        circle, color = STATUS_CIRCLES[status]
        row["status"] = status
        row["lbl"].config(text=f"    {circle}  {site}  —  {url}", fg=color)
        # Refresh count label color to match new status
        if row["count"]:
            row["count_lbl"].config(fg=_dim_color(color))

    def set_artist_status(self, artist: str, status: str) -> None:
        row = self._artist_rows.get(artist)
        if not row:
            return
        circle, color = STATUS_CIRCLES[status]
        row["status"] = status
        row["lbl"].config(text=f"{circle}  {artist}", fg=color)
        if row["count"]:
            row["count_lbl"].config(fg=_dim_color(color))

    def set_site_file_count(self, artist: str, site: str, url: str, count: int, skipped: int) -> None:
        """Update the live file count for a site row and its progress bar."""
        key = (artist, site, url)
        row = self._site_rows.get(key)
        if not row:
            return
        row["count"]   = count
        row["skipped"] = skipped
        _, color = STATUS_CIRCLES[row["status"]]
        parts = []
        if count:   parts.append(f"+{count}")
        if skipped: parts.append(f"~{skipped} skipped")
        row["count_lbl"].config(text="  ".join(parts), fg=_dim_color(color))

        # Update progress bar using DB total for this site
        if "prog_bar" in row and "prog_var" in row:
            try:
                from core.file_db import get_db
                db = get_db()
                if db:
                    total = db.total(artist=artist, site=site)
                    if total > 0:
                        row["prog_var"].set(count)
                        row["prog_bar"].config(maximum=max(total, count))
            except Exception:
                pass

    def set_site_error_count(self, artist: str, site: str, url: str, count: int) -> None:
        """Show error/warning count on a site row in red."""
        key = (artist, site, url)
        row = self._site_rows.get(key)
        if not row:
            return
        if "error_lbl" not in row:
            # Create error label lazily on first error
            row["error_lbl"] = tk.Label(
                row["count_lbl"].master,
                text="", bg=ENTRY_BG,
                fg="#e05050", font=FONT_MONO, anchor="w")
            row["error_lbl"].pack(side="left", padx=(8, 0))
        row["error_lbl"].config(text=f"✗ {count} err" if count else "")

    def set_artist_file_count(self, artist: str, count: int, skipped: int, failed: int = 0) -> None:
        """Update the live file count for an artist row."""
        row = self._artist_rows.get(artist)
        if not row:
            return
        row["count"]   = count
        row["skipped"] = skipped
        row["failed"]  = failed
        _, color = STATUS_CIRCLES[row["status"]]
        parts = []
        if count:
            parts.append(f"+{count}")
        if skipped:
            parts.append(f"~{skipped} skipped")
        if failed:
            parts.append(f"{failed} failed")
        row["count_lbl"].config(text="  ".join(parts), fg=_dim_color(color))

    def clear(self) -> None:
        for w in self._inner.winfo_children():
            w.destroy()
        self._artist_rows.clear()
        self._site_rows.clear()