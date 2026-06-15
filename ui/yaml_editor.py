"""
ui/yaml_editor.py
-----------------
In-app YAML editor for artists.yaml and config.yaml.

Provides two views, switchable via tabs in a Toplevel:

  ArtistEditor  — structured form: list of artists with name, notes, tags,
                  and a per-category media link table.  Add / remove artists
                  and individual links without touching raw YAML.

  RawEditor     — plain-text editor with syntax-check on save.

Entry point:
    open_yaml_editor(root, catalogue_path, config_path, on_saved)
        Opens the Toplevel (singleton per parent).
        on_saved(kind)  is called with "catalogue" or "config" after a write.
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
    COLOR_FAIL, COLOR_OK,
)
from ui.widgets import divider, section_label, styled_button, styled_entry
from utils.yaml_io import read_yaml, write_yaml, YAMLError

# ── constants ──────────────────────────────────────────────────────────────────

_WIN_W, _WIN_H = 900, 700
_KNOWN_CATS = ["aggregators", "socials", "paid", "contacts", "streaming"]

# All known site names (used for autocomplete / colour hints)
_DL_SITES = {
    "pixiv", "danbooru", "artstation", "twitter", "bluesky",
    "furaffinity", "tumblr", "deviantart", "kemono",
}


# ── helpers ────────────────────────────────────────────────────────────────────

def _btn(parent: tk.Widget, text: str, cmd: Callable,
         bg: str = BTN_BG, hov: str = BTN_HOV,
         **kw) -> tk.Button:
    kw.setdefault("padx", 14)
    kw.setdefault("pady", 6)
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


def _label(parent: tk.Widget, text: str, fg: str = FG_DIM,
           font=None, **kw) -> tk.Label:
    return tk.Label(parent, text=text, bg=ENTRY_BG, fg=fg,
                    font=font or FONT_MONO, **kw)


def _scrolled_frame(parent: tk.Widget) -> tuple[tk.Canvas, tk.Frame]:
    """Return (canvas, inner_frame) — pack canvas yourself."""
    canvas = tk.Canvas(parent, bg=ENTRY_BG, highlightthickness=0)
    sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=ENTRY_BG)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_configure(_e: tk.Event) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(e: tk.Event) -> None:
        canvas.itemconfig(win, width=e.width)

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    canvas.configure(yscrollcommand=sb.set)

    sb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    def _scroll(event: tk.Event) -> None:
        if event.num == 4:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            canvas.yview_scroll(1, "units")
        else:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind("<MouseWheel>", _scroll)
    canvas.bind("<Button-4>", _scroll)
    canvas.bind("<Button-5>", _scroll)
    inner.bind("<MouseWheel>", _scroll)
    inner.bind("<Button-4>", _scroll)
    inner.bind("<Button-5>", _scroll)

    return canvas, inner


# ── Artist structured editor ───────────────────────────────────────────────────

class _ArtistCard(tk.Frame):
    """
    Collapsible card for one artist entry.

    Exposes .get_data() → dict (same shape as artists.yaml entry).
    """

    def __init__(self, parent: tk.Widget, artist: dict,
                 on_delete: Callable, **kw) -> None:
        super().__init__(parent, bg=BORDER, padx=1, pady=1, **kw)
        self._artist = artist
        self._on_delete = on_delete
        self._expanded = tk.BooleanVar(value=False)
        self._link_rows: list[dict] = []   # {cat, site_entry, url_entry, frame}
        self._build()

    # ── build ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        inner = tk.Frame(self, bg=ENTRY_BG)
        inner.pack(fill="both", expand=True)

        # ── header row ─────────────────────────────────────────────────────
        hdr = tk.Frame(inner, bg=ENTRY_BG)
        hdr.pack(fill="x", padx=8, pady=(6, 4))

        self._toggle_btn = tk.Label(
            hdr, text="▶", bg=ENTRY_BG, fg=ACCENT,
            font=FONT_BOLD, cursor="hand2",
        )
        self._toggle_btn.pack(side="left", padx=(0, 6))
        self._toggle_btn.bind("<Button-1>", lambda _: self._toggle())

        self._name_entry = _entry(hdr, self._artist.get("name", ""), width=28)
        self._name_entry.pack(side="left", padx=(0, 8))

        _label(hdr, "name", fg=FG_DIM).pack(side="left", padx=(0, 16))

        _btn(hdr, "✕ Remove", self._on_delete,
             bg="#3a1a1a", hov="#5a2a2a", padx=10, pady=4,
             ).pack(side="right")

        # ── collapsible body ───────────────────────────────────────────────
        self._body = tk.Frame(inner, bg=ENTRY_BG)
        # not packed until expanded

        self._build_body()

    def _build_body(self) -> None:
        body = self._body

        # Notes
        notes_row = tk.Frame(body, bg=ENTRY_BG)
        notes_row.pack(fill="x", padx=8, pady=(0, 4))
        _label(notes_row, "notes:", anchor="nw").pack(side="left", padx=(0, 6))
        self._notes_text = tk.Text(
            notes_row, bg=ENTRY_BG, fg=FG, insertbackground=ACCENT,
            relief="flat", font=FONT_MONO, height=3, width=60,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, wrap="word",
        )
        self._notes_text.insert("1.0", self._artist.get("notes", "") or "")
        self._notes_text.pack(side="left", fill="x", expand=True)

        # Tags
        tags_row = tk.Frame(body, bg=ENTRY_BG)
        tags_row.pack(fill="x", padx=8, pady=(0, 4))
        _label(tags_row, "tags:", anchor="w").pack(side="left", padx=(0, 6))
        raw_tags = self._artist.get("tags", []) or []
        self._tags_entry = _entry(tags_row, ", ".join(raw_tags), width=60)
        self._tags_entry.pack(side="left", fill="x", expand=True)
        _label(tags_row, " (comma-separated)").pack(side="left", padx=(4, 0))

        # Media links table
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", padx=8, pady=(6, 4))
        lnk_hdr = tk.Frame(body, bg=ENTRY_BG)
        lnk_hdr.pack(fill="x", padx=8, pady=(0, 4))
        _label(lnk_hdr, "MEDIA LINKS", fg=ACCENT2, font=FONT_BOLD).pack(side="left")
        _btn(lnk_hdr, "+ Add link", self._add_link_row,
             bg="#1a2a1a", hov="#2a3a2a", padx=10, pady=3,
             ).pack(side="right")

        # Column headers
        col_hdr = tk.Frame(body, bg=ENTRY_BG)
        col_hdr.pack(fill="x", padx=8, pady=(0, 2))
        _label(col_hdr, "category",   fg=FG_DIM, width=12, anchor="w").pack(side="left", padx=(0, 4))
        _label(col_hdr, "site/key",   fg=FG_DIM, width=14, anchor="w").pack(side="left", padx=(0, 4))
        _label(col_hdr, "url(s)",     fg=FG_DIM, anchor="w").pack(side="left")

        # Links container
        self._links_frame = tk.Frame(body, bg=ENTRY_BG)
        self._links_frame.pack(fill="x", padx=8, pady=(0, 6))

        # Populate from existing data
        media = self._artist.get("media") or {}
        for cat, entries in media.items():
            if isinstance(entries, dict):
                for site, url_val in entries.items():
                    if isinstance(url_val, list):
                        for u in url_val:
                            self._add_link_row(cat=cat, site=site, url=str(u))
                    else:
                        self._add_link_row(cat=cat, site=site, url=str(url_val or ""))

    def _add_link_row(self, cat: str = "socials", site: str = "", url: str = "") -> None:
        row = tk.Frame(self._links_frame, bg=ENTRY_BG)
        row.pack(fill="x", pady=1)

        # Category combobox
        cat_var = tk.StringVar(value=cat)
        cat_cb = ttk.Combobox(
            row, textvariable=cat_var, values=_KNOWN_CATS,
            width=11, font=FONT_MONO,
        )
        cat_cb.pack(side="left", padx=(0, 4))

        site_e = _entry(row, site, width=13)
        site_e.pack(side="left", padx=(0, 4))

        # Colour hint when site is downloadable
        def _update_site_colour(*_) -> None:
            v = site_e.get().strip().lower()
            site_e.config(fg=ACCENT if v in _DL_SITES else FG)
        site_e.bind("<KeyRelease>", lambda _: _update_site_colour())
        _update_site_colour()

        url_e = _entry(row, url, width=42)
        url_e.pack(side="left", padx=(0, 4), fill="x", expand=True)

        row_data = {"cat": cat_var, "site": site_e, "url": url_e, "frame": row}

        def _remove(rd=row_data) -> None:
            rd["frame"].destroy()
            self._link_rows.remove(rd)

        _btn(row, "✕", _remove, bg="#2a1a1a", hov="#4a2a2a",
             padx=6, pady=2).pack(side="left")

        self._link_rows.append(row_data)

    # ── toggle ─────────────────────────────────────────────────────────────

    def _toggle(self) -> None:
        if self._expanded.get():
            self._body.pack_forget()
            self._toggle_btn.config(text="▶")
            self._expanded.set(False)
        else:
            self._body.pack(fill="x", padx=0, pady=(0, 6))
            self._toggle_btn.config(text="▼")
            self._expanded.set(True)

    def expand(self) -> None:
        if not self._expanded.get():
            self._toggle()

    # ── serialise ──────────────────────────────────────────────────────────

    def get_data(self) -> dict:
        name = self._name_entry.get().strip()
        notes = self._notes_text.get("1.0", "end").strip()
        raw_tags = self._tags_entry.get()
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        # Rebuild media dict: group by cat → site → list|str
        media: dict[str, dict[str, Any]] = {}
        for rd in self._link_rows:
            cat  = rd["cat"].get().strip() or "socials"
            site = rd["site"].get().strip()
            url  = rd["url"].get().strip()
            if not site or not url:
                continue
            cat_d = media.setdefault(cat, {})
            existing = cat_d.get(site)
            if existing is None:
                cat_d[site] = url
            elif isinstance(existing, list):
                existing.append(url)
            else:
                cat_d[site] = [existing, url]

        entry: dict[str, Any] = {"name": name}
        if tags:
            entry["tags"] = tags
        if notes:
            entry["notes"] = notes
        if media:
            entry["media"] = media
        return entry


class ArtistEditor(tk.Frame):
    """
    Scrollable list of _ArtistCard widgets.
    Call .load(path) to populate; .save(path) to write back.
    """

    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, bg=ENTRY_BG, **kw)
        self._path: Path | None = None
        self._cards: list[_ArtistCard] = []
        self._build()

    def _build(self) -> None:
        # Toolbar
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x", padx=0, pady=0)
        _btn(bar, "+ Add artist", self._add_artist,
             bg="#1a2040", hov="#2a3060",
             ).pack(side="left", padx=8, pady=6)
        self._status_lbl = tk.Label(bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
        self._status_lbl.pack(side="left", padx=(8, 0))

        # Scrollable area
        scroll_outer = tk.Frame(self, bg=ENTRY_BG)
        scroll_outer.pack(fill="both", expand=True)
        _, self._inner = _scrolled_frame(scroll_outer)

    # ── data ───────────────────────────────────────────────────────────────

    def load(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            data = read_yaml(self._path)
        except YAMLError as e:
            messagebox.showerror("Load error", str(e))
            return
        artists = (data or {}).get("artists", []) if isinstance(data, dict) else []
        self._render(artists)
        self._status(f"Loaded {len(artists)} artists from {self._path.name}")

    def _render(self, artists: list[dict]) -> None:
        for w in self._inner.winfo_children():
            w.destroy()
        self._cards.clear()
        for a in artists:
            self._append_card(a)

    def _append_card(self, artist: dict) -> None:
        idx = len(self._cards)

        def _remove(i: int = idx) -> None:
            card = self._cards[i]
            card.destroy()
            self._cards.pop(i)
            # Re-index removers (rebuild _on_delete closures) — simplest: re-render
            artists = [c.get_data() for c in self._cards]
            self._render(artists)
            self._status(f"{len(self._cards)} artists")

        card = _ArtistCard(self._inner, artist, on_delete=_remove)
        card.pack(fill="x", padx=8, pady=4)
        self._cards.append(card)

    def _add_artist(self) -> None:
        new: dict = {"name": "New Artist", "media": {}}
        self._append_card(new)
        # Scroll to bottom & auto-expand
        self._inner.update_idletasks()
        if self._cards:
            self._cards[-1].expand()
        self._status(f"{len(self._cards)} artists  (unsaved)")

    def save(self, path: str | Path | None = None) -> bool:
        target = Path(path) if path else self._path
        if target is None:
            messagebox.showerror("Save error", "No file path set.")
            return False
        artists = [c.get_data() for c in self._cards]
        data = {"artists": artists}
        try:
            write_yaml(target, data)
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
    """Plain Text widget with load / validate / save."""

    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, bg=ENTRY_BG, **kw)
        self._path: Path | None = None
        self._build()

    def _build(self) -> None:
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x")
        _btn(bar, "⟳ Validate YAML", self._validate,
             bg="#1e1e2a", hov="#2a2a3a").pack(side="left", padx=8, pady=6)
        self._status_lbl = tk.Label(bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
        self._status_lbl.pack(side="left", padx=(6, 0))

        txt_frame = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        txt_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self._text = tk.Text(
            txt_frame, bg="#0a0a10", fg=FG, insertbackground=ACCENT,
            relief="flat", font=FONT_MONO, wrap="none",
            highlightthickness=0,
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
        raw = self._text.get("1.0", "end")
        try:
            yaml.safe_load(raw)
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
        raw = self._text.get("1.0", "end")
        try:
            target.write_text(raw, encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Save error", str(e))
            return False
        self._path = target
        self._status(f"✓ Saved → {target.name}", ok=True)
        return True

    def get_raw(self) -> str:
        return self._text.get("1.0", "end")

    def _status(self, msg: str, ok: bool | None = None) -> None:
        color = COLOR_OK if ok is True else (COLOR_FAIL if ok is False else FG_DIM)
        self._status_lbl.config(text=msg, fg=color)


# ── Config editor (key-value form for config.yaml) ─────────────────────────────

class ConfigEditor(tk.Frame):
    """
    Structured editor for the flat-ish config.yaml.
    Only handles the common top-level scalar/list keys; a Raw tab is
    available for advanced edits.
    """

    _SITE_CHOICES = [
        "pixiv", "danbooru", "artstation", "twitter", "bluesky",
        "furaffinity", "tumblr", "deviantArt", "kemono",
    ]

    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, bg=ENTRY_BG, **kw)
        self._path: Path | None = None
        self._site_vars: dict[str, tk.BooleanVar] = {}
        self._build()

    def _build(self) -> None:
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x")
        self._status_lbl = tk.Label(bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
        self._status_lbl.pack(side="left", padx=12, pady=6)

        outer = tk.Frame(self, bg=ENTRY_BG)
        outer.pack(fill="both", expand=True)
        _, self._inner = _scrolled_frame(outer)
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

    def _row(self, label: str) -> tuple[tk.Frame, tk.Label]:
        row = tk.Frame(self._form, bg=ENTRY_BG)
        row.pack(fill="x", pady=3)
        lbl = _label(row, label, width=22, anchor="w")
        lbl.pack(side="left")
        return row, lbl

    def _build_form(self, data: dict) -> None:
        # ── Paths ──────────────────────────────────────────────────────────
        _label(self._form, "PATHS", fg=ACCENT2, font=FONT_BOLD
               ).pack(anchor="w", pady=(4, 2))
        tk.Frame(self._form, bg=BORDER, height=1).pack(fill="x", pady=(0, 6))

        path_keys = [
            ("gdl_config",   "Gallery-DL config"),
            ("download_dir", "Download directory"),
            ("archives_dir", "Archives directory"),
            ("log_dir",      "Log directory"),
            ("database",     "Database file"),
        ]
        self._path_entries: dict[str, tk.Entry] = {}
        for key, label in path_keys:
            row, _ = self._row(label)
            e = _entry(row, str(data.get(key, "") or ""), width=50)
            e.pack(side="left", fill="x", expand=True)
            self._path_entries[key] = e

        # ── Downloadable sites ─────────────────────────────────────────────
        _label(self._form, "DOWNLOADABLE SITES", fg=ACCENT2, font=FONT_BOLD
               ).pack(anchor="w", pady=(14, 2))
        tk.Frame(self._form, bg=BORDER, height=1).pack(fill="x", pady=(0, 6))

        active = set(data.get("downloadable_sites", []) or [])
        self._site_vars = {}
        grid = tk.Frame(self._form, bg=ENTRY_BG)
        grid.pack(fill="x")
        for i, site in enumerate(self._SITE_CHOICES):
            var = tk.BooleanVar(value=(site in active))
            self._site_vars[site] = var
            cb = tk.Checkbutton(
                grid, text=site, variable=var,
                bg=ENTRY_BG, fg=FG, selectcolor=ENTRY_BG,
                activebackground=ENTRY_BG, activeforeground=ACCENT,
                font=FONT_MONO,
            )
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=8, pady=2)

        # Custom site entry
        custom_row = tk.Frame(self._form, bg=ENTRY_BG)
        custom_row.pack(fill="x", pady=(4, 0))
        _label(custom_row, "extra sites:").pack(side="left", padx=(0, 6))
        extra = [s for s in active if s not in self._SITE_CHOICES]
        self._extra_sites_entry = _entry(custom_row, ", ".join(extra), width=40)
        self._extra_sites_entry.pack(side="left")
        _label(custom_row, " (comma-separated)").pack(side="left", padx=(4, 0))

        # ── AO3 settings ───────────────────────────────────────────────────
        _label(self._form, "AO3", fg=ACCENT2, font=FONT_BOLD
               ).pack(anchor="w", pady=(14, 2))
        tk.Frame(self._form, bg=BORDER, height=1).pack(fill="x", pady=(0, 6))

        ao3 = data.get("ao3", {}) or {}
        ao3_keys = [
            ("source_dir",   "AO3 source dir"),
            ("transfer_dir", "Transfer dir"),
            ("archive",      "Download archive"),
        ]
        self._ao3_entries: dict[str, tk.Entry] = {}
        for key, label in ao3_keys:
            row, _ = self._row(label)
            e = _entry(row, str(ao3.get(key, "") or ""), width=50)
            e.pack(side="left", fill="x", expand=True)
            self._ao3_entries[key] = e

        # ── Fic tracker ────────────────────────────────────────────────────
        _label(self._form, "FIC TRACKER", fg=ACCENT2, font=FONT_BOLD
               ).pack(anchor="w", pady=(14, 2))
        tk.Frame(self._form, bg=BORDER, height=1).pack(fill="x", pady=(0, 6))

        fic = data.get("fic_tracker", {}) or {}
        fic_row, _ = self._row("Fic list file")
        self._fic_file_entry = _entry(fic_row, str(fic.get("fic_file", "") or ""), width=50)
        self._fic_file_entry.pack(side="left", fill="x", expand=True)

        stale_row, _ = self._row("Stale months")
        self._stale_entry = _entry(stale_row, str(fic.get("stale_months", 6)), width=6)
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
            val = e.get().strip()
            if val:
                existing[key] = val

        sites = [s for s, v in self._site_vars.items() if v.get()]
        extra = [s.strip() for s in self._extra_sites_entry.get().split(",") if s.strip()]
        existing["downloadable_sites"] = sites + extra

        ao3 = existing.get("ao3", {}) or {}
        for key, e in self._ao3_entries.items():
            val = e.get().strip()
            if val:
                ao3[key] = val
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
    """
    Open (or raise) the YAML editor Toplevel.

    Parameters
    ----------
    root            : parent tk.Tk window
    catalogue_path  : path to artists.yaml
    config_path     : path to config.yaml
    on_saved        : called with "catalogue" or "config" after each save
    """
    global _OPEN_WINDOW

    if _OPEN_WINDOW is not None and _OPEN_WINDOW.winfo_exists():
        _OPEN_WINDOW.lift()
        _OPEN_WINDOW.focus_set()
        return

    win = tk.Toplevel(root)
    win.title("YAML Editor")
    win.geometry(f"{_WIN_W}x{_WIN_H}")
    win.configure(bg=BG)
    win.minsize(720, 520)
    _OPEN_WINDOW = win

    def _on_close() -> None:
        global _OPEN_WINDOW
        _OPEN_WINDOW = None
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _on_close)

    # ── title bar ──────────────────────────────────────────────────────────
    hdr = tk.Frame(win, bg=BG, pady=10)
    hdr.pack(fill="x", padx=20)
    tk.Label(hdr, text="✦ YAML EDITOR",
             bg=BG, fg=ACCENT, font=FONT_HEAD).pack(side="left")
    tk.Label(hdr, text="edit artists & config in-app",
             bg=BG, fg=FG_DIM, font=FONT_SUB).pack(side="left", padx=(10, 0))
    tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

    # ── outer notebook: Artists / Config ───────────────────────────────────
    style = ttk.Style(win)
    style.configure("Editor.TNotebook", background=BG, borderwidth=0)
    style.configure("Editor.TNotebook.Tab", background=BG, foreground=FG_DIM,
                    font=FONT_BOLD, padding=[16, 8])
    style.map("Editor.TNotebook.Tab",
              background=[("selected", PANEL), ("active", ENTRY_BG)],
              foreground=[("selected", ACCENT), ("active", FG)])

    nb = ttk.Notebook(win, style="Editor.TNotebook")
    nb.pack(fill="both", expand=True, padx=0, pady=0)

    # ── ARTISTS tab ────────────────────────────────────────────────────────
    artists_outer = tk.Frame(nb, bg=PANEL)
    nb.add(artists_outer, text="  👤  Artists  ")

    # inner notebook: Form / Raw
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

    # Sync raw → form when switching to form tab
    def _on_inner_tab_change(_e: tk.Event) -> None:
        sel = inner_nb.select()
        tab_idx = inner_nb.index(sel)
        if tab_idx == 0:
            # Switching to form: re-load from file (safest)
            artist_editor.load(catalogue_path)

    inner_nb.bind("<<NotebookTabChanged>>", _on_inner_tab_change)

    # Save bar for artists
    art_save_bar = tk.Frame(artists_outer, bg=PANEL, pady=6)
    art_save_bar.pack(fill="x", padx=16)
    art_status = tk.Label(art_save_bar, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    art_status.pack(side="left")

    def _save_artists() -> None:
        sel = inner_nb.select()
        tab_idx = inner_nb.index(sel)
        ok: bool
        if tab_idx == 0:
            ok = artist_editor.save(catalogue_path)
        else:
            ok = art_raw_editor.save(catalogue_path)
        if ok and on_saved:
            on_saved("catalogue")
        art_status.config(
            text="✓ Saved" if ok else "✗ Save failed",
            fg=COLOR_OK if ok else COLOR_FAIL,
        )

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
        sel = cfg_inner_nb.select()
        tab_idx = cfg_inner_nb.index(sel)
        ok: bool
        if tab_idx == 0:
            ok = config_editor.save(config_path)
        else:
            ok = cfg_raw_editor.save(config_path)
        if ok and on_saved:
            on_saved("config")
        cfg_status.config(
            text="✓ Saved" if ok else "✗ Save failed",
            fg=COLOR_OK if ok else COLOR_FAIL,
        )

    _btn(cfg_save_bar, "💾  Save config.yaml", _save_config,
         bg="#1a2a1a", hov="#2a3a2a").pack(side="right")