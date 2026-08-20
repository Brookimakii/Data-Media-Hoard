"""
ui/duplicates_tab.py
---------------------
Find Duplicates tab.

Scans the configured download folder, hashes every image (sha256 for
exact matches, perceptual hash for near-duplicates — resizes, recompresses,
minor edits), and groups them for review. Hashing runs on a background
thread since it can take a while on a large hoard; results are cached in
hoard.db (artists_media.sha256 / .phash) so re-scans only hash new or
changed files.

For each duplicate group, the user sees thumbnails of every file in the
group and can delete all but one with a single click.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

log = logging.getLogger(__name__)

from core.catalogue import ConfigError, load_config
from core.file_db import get_db
from core.media_scan import (
    DEFAULT_PHASH_THRESHOLD,
    DuplicateGroup,
    find_all_duplicate_groups,
    get_video_duration,
    hash_and_store,
    iter_image_files,
    needs_hashing,
    open_thumbnail_image,
    is_video,
    ffmpeg_available,
    imagehash_available,
)
from ui.scroll import register_scroll_canvas
from ui.theme import (
    ACCENT, ACCENT2, BORDER, ENTRY_BG, FG, FG_DIM,
    FONT_BODY, FONT_BOLD, FONT_HEAD, FONT_MONO, FONT_SUB,
    PANEL, PAD_OUTER, COLOR_OK, COLOR_FAIL, COLOR_RUNNING,
)
from ui.widgets import divider, section_label, styled_button, styled_entry

SCRIPT_DIR     = Path(__file__).parent.parent
DEFAULT_CONFIG = str(SCRIPT_DIR / "config.yaml")

THUMB_SIZE = 96

# Cap how many duplicate groups' thumbnails are rendered onto the canvas at
# once. On Windows, every PhotoImage backs onto a native GDI bitmap handle,
# and Tk/Tcl can fail to allocate further bitmaps well before Python's own
# memory limits are a concern ("Fail to allocate bitmap") if a scan turns up
# many groups and every thumbnail in every group is rendered simultaneously.
# Groups beyond this cap are listed by name only, with a "show more" control.
MAX_RENDERED_GROUPS = 25


def _resample_filter():
    """Best available high-quality resize filter across Pillow versions."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None
    return getattr(PILImage, "LANCZOS", getattr(PILImage, "BICUBIC", None))


def _format_size(num_bytes: int) -> str:
    if num_bytes >= 1 << 20:
        return f"{num_bytes / (1 << 20):.1f} MB"
    return f"{num_bytes / 1024:.1f} KB"


@dataclass
class _MetaField:
    label: str
    value: str          # display text, e.g. "1.2 MB" or "Pixiv"
    numeric: float | None = None   # set for fields that should be yellow/orange compared


def _file_metadata_fields(path: Path) -> list[_MetaField]:
    """
    Structured facts about *path* for the comparator's middle panel.

    Fields with `numeric` set get the yellow(higher)/orange(lower) treatment
    when they differ; all other fields get plain green(same)/red(different).
    """
    fields = [_MetaField("Name", path.name)]
    fields.append(_MetaField("Path", str(path.parent)))

    try:
        stat = path.stat()
        fields.append(_MetaField("Size", _format_size(stat.st_size), numeric=float(stat.st_size)))
    except OSError:
        fields.append(_MetaField("Size", "(file missing)"))
        return fields

    db = get_db()
    artist = site = ""
    if db is not None:
        row = db.get(str(path))
        if row:
            artist = row["artist"] or ""
            site   = row["site"] or ""
    fields.append(_MetaField("Artist", artist or "-"))
    fields.append(_MetaField("Site", site or "-"))

    if is_video(path):
        fields.append(_MetaField("Format", "video"))
        dur = get_video_duration(path)
        if dur > 0:
            fields.append(_MetaField("Duration", f"{dur:.1f}s", numeric=dur))
    else:
        try:
            from PIL import Image
            with Image.open(path) as img:
                w, h = img.width, img.height
                fields.append(_MetaField("Format", img.format or "-"))
                fields.append(_MetaField("Dimensions", f"{w}×{h}", numeric=float(w * h)))
        except Exception:
            fields.append(_MetaField("Format", "-"))

    return fields


# Colors for the metadata comparison grid. Identical values are green;
# different non-numeric values are red; different numeric values are
# yellow (the higher one) / orange (the lower one) so it's obvious at a
# glance which file "wins" on size, dimensions, duration, etc.
_META_SAME      = COLOR_OK
_META_DIFF      = COLOR_FAIL
_META_HIGHER    = "#f5d742"   # yellow
_META_LOWER     = "#e08a3c"   # orange


def _build_metadata_grid(parent: tk.Widget, fields_a: list, fields_b: list) -> None:
    """
    Render one row per field:
        FIELD LABEL   |  LEFT value   |  RIGHT value
    Colors: green=same, red=diff, yellow=higher numeric, orange=lower numeric.
    """
    for w in parent.winfo_children():
        w.destroy()

    # Compute colors once.
    colors: list[tuple[str, str]] = []
    for fa, fb in zip(fields_a, fields_b):
        if fa.value == fb.value:
            colors.append((_META_SAME, _META_SAME))
        elif fa.numeric is not None and fb.numeric is not None and fa.numeric != fb.numeric:
            if fa.numeric > fb.numeric:
                colors.append((_META_HIGHER, _META_LOWER))
            else:
                colors.append((_META_LOWER, _META_HIGHER))
        else:
            colors.append((_META_DIFF, _META_DIFF))

    grid = tk.Frame(parent, bg=ENTRY_BG)
    grid.pack(fill="both", expand=True)
    grid.columnconfigure(0, weight=0, minsize=80)   # field label
    grid.columnconfigure(1, weight=1)               # left value
    grid.columnconfigure(2, weight=1)               # right value

    # Header row
    tk.Label(grid, text="", bg=ENTRY_BG).grid(row=0, column=0, sticky="w")
    tk.Label(grid, text="LEFT", bg=ENTRY_BG, fg=ACCENT2, font=FONT_BOLD,
             anchor="w").grid(row=0, column=1, sticky="w", pady=(0, 6))
    tk.Label(grid, text="RIGHT", bg=ENTRY_BG, fg=ACCENT2, font=FONT_BOLD,
             anchor="w").grid(row=0, column=2, sticky="w", pady=(0, 6))

    for row_i, (fa, fb, (ca, cb)) in enumerate(zip(fields_a, fields_b, colors), start=1):
        tk.Label(grid, text=fa.label.upper(), bg=ENTRY_BG, fg=FG_DIM,
                 font=FONT_BOLD, anchor="w",
                 ).grid(row=row_i, column=0, sticky="w", pady=2, padx=(0, 8))
        tk.Label(grid, text=fa.value, bg=ENTRY_BG, fg=ca, font=FONT_MONO,
                 anchor="w", justify="left", wraplength=160,
                 ).grid(row=row_i, column=1, sticky="w", pady=2)
        tk.Label(grid, text=fb.value, bg=ENTRY_BG, fg=cb, font=FONT_MONO,
                 anchor="w", justify="left", wraplength=160,
                 ).grid(row=row_i, column=2, sticky="w", pady=2, padx=(16, 0))


class _ComparePane(tk.Frame):
    """
    One half of the compare dialog: a single image view.

    Holds its own Pillow Image at full resolution (loaded once); the
    actual zoom level is owned by the parent _CompareWindow and pushed
    into every pane together via set_zoom(), so both sides always show
    the same scale. Re-renders a PhotoImage on demand — zooming never
    re-decodes from disk, so it stays responsive even for large images
    or video frames.
    """

    def __init__(self, parent: tk.Widget, on_user_zoom, on_user_pan, **kw) -> None:
        super().__init__(parent, bg="#000000", **kw)
        self.path: Path | None = None
        self._pil_image = None
        self._photo = None
        self._zoom = 1.0
        self._pan_x = 0
        self._pan_y = 0
        self._drag_start: tuple[int, int] | None = None
        self._on_user_zoom = on_user_zoom   # called on wheel: (factor) -> None
        self._on_user_pan  = on_user_pan    # called on drag: (dx, dy) -> None
        self._build()

    def _build(self) -> None:
        hdr = tk.Frame(self, bg=ENTRY_BG)
        hdr.pack(fill="x")
        self._name_lbl = tk.Label(hdr, text="", bg=ENTRY_BG, fg=FG,
                                  font=FONT_MONO, anchor="w")
        self._name_lbl.pack(side="left", padx=8, pady=4, fill="x", expand=True)

        self.canvas = tk.Canvas(self, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", self._on_wheel)   # Linux scroll up
        self.canvas.bind("<Button-5>", self._on_wheel)   # Linux scroll down
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

    def load(self, path: Path) -> None:
        """Switch this pane to show a different file. Resets pan, keeps zoom."""
        self.path = path
        self._name_lbl.config(text=path.name)
        self._pil_image = None
        self._photo = None
        self._last_zw = self._last_zh = None
        self._pan_x = 0
        self._pan_y = 0
        self.canvas.delete("all")

        img = open_thumbnail_image(path, size=(8000, 8000))
        if img is None:
            self.canvas.create_text(
                10, 10, anchor="nw", fill=FG_DIM, font=FONT_MONO,
                text="Preview unavailable\n(corrupt file, or ffmpeg\nmissing for video)",
            )
            return
        self._pil_image = img
        self._redraw()

    def fit_zoom(self) -> float:
        """Compute (but don't apply) the zoom that fits this pane's image to its canvas."""
        if self._pil_image is None:
            return 1.0
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        iw, ih = self._pil_image.size
        return min(cw / iw, ch / ih, 1.0)

    def set_zoom(self, zoom: float, pan_x: int = 0, pan_y: int = 0) -> None:
        self._zoom = zoom
        self._pan_x = pan_x
        self._pan_y = pan_y
        self._redraw()

    def _redraw(self) -> None:
        if self._pil_image is None:
            return
        from PIL import ImageTk

        iw, ih = self._pil_image.size
        zw, zh = max(1, int(iw * self._zoom)), max(1, int(ih * self._zoom))

        if getattr(self, "_last_zw", None) != zw or getattr(self, "_last_zh", None) != zh:
            self._resized = self._pil_image.resize((zw, zh), _resample_filter())
            self._last_zw, self._last_zh = zw, zh

        self._photo = ImageTk.PhotoImage(self._resized)
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        x = cw // 2 + self._pan_x
        y = ch // 2 + self._pan_y
        self.canvas.create_image(x, y, anchor="center", image=self._photo)

    def _on_wheel(self, event: tk.Event) -> str:
        factor = 0.9 if (event.num == 5 or getattr(event, "delta", 0) < 0) else 1.1
        self._on_user_zoom(factor)
        return "break"

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_start = (event.x, event.y)

    def _on_drag_move(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        dx = event.x - self._drag_start[0]
        dy = event.y - self._drag_start[1]
        self._drag_start = (event.x, event.y)
        self._on_user_pan(dx, dy)


class _CompareWindow:
    """
    Singleton-style comparator Toplevel: one window, reused for every
    group. Call .show_group(group, all_groups) to load a (possibly new)
    group's first two files into it; if the window isn't open yet this
    creates it, otherwise it just swaps the content in place.

    Zoom and pan are shared: both panes always show the same scale and
    offset, driven from a single set of values owned by this class.

    on_files_changed(group_list) is called after a delete/ignore action
    so the caller (build_duplicates) can refresh the card list and the
    checkbox-selection state to match.
    """

    def __init__(self, root: tk.Widget, on_files_changed=None) -> None:
        self._root = root
        self._on_files_changed = on_files_changed
        self._win: tk.Toplevel | None = None
        self._pane_a: _ComparePane | None = None
        self._pane_b: _ComparePane | None = None
        self._meta_frame: tk.Frame | None = None
        self._cb_a: ttk.Combobox | None = None
        self._cb_b: ttk.Combobox | None = None
        self._var_a: tk.StringVar | None = None
        self._var_b: tk.StringVar | None = None
        self._group: DuplicateGroup | None = None
        self._all_groups: list[DuplicateGroup] = []
        self._group_index: int = 0
        self._group_lbl: tk.Label | None = None
        self._zoom = 1.0
        self._pan_x = 0
        self._pan_y = 0

    @property
    def is_open(self) -> bool:
        return self._win is not None and self._win.winfo_exists()

    def _ensure_window(self) -> None:
        if self.is_open:
            return

        win = tk.Toplevel(self._root)
        win.title("Compare duplicates")
        win.geometry("1300x760")
        win.configure(bg=PANEL)
        win.transient(self._root.winfo_toplevel())
        self._win = win

        def _on_close() -> None:
            win.destroy()
            self._win = None
        win.protocol("WM_DELETE_WINDOW", _on_close)

        bar = tk.Frame(win, bg=PANEL)
        bar.pack(fill="x", padx=10, pady=8)
        tk.Label(bar, text="Scroll to zoom · drag to pan — applies to both sides",
                 bg=PANEL, fg=FG_DIM, font=FONT_MONO).pack(side="left")

        # ── Group navigation ─────────────────────────────────────────────────
        nav_row = tk.Frame(win, bg=PANEL)
        nav_row.pack(fill="x", padx=10, pady=(0, 6))
        styled_button(nav_row, "← Previous group", self._prev_group,
                     bg="#2a2a38", hov="#3a3a50", padx=10, pady=4).pack(side="left")
        self._group_lbl = tk.Label(nav_row, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
        self._group_lbl.pack(side="left", padx=10)
        styled_button(nav_row, "Next group →", self._next_group,
                     bg="#2a2a38", hov="#3a3a50", padx=10, pady=4).pack(side="left")

        body = tk.Frame(win, bg=PANEL)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        # Weight ratio 2:1:2 -> middle column gets 1/5 = 20% of body width,
        # the two image panes split the remaining 80% evenly between them.
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=1, minsize=200)
        body.columnconfigure(2, weight=2)
        body.rowconfigure(0, weight=1)

        self._pane_a = _ComparePane(body, on_user_zoom=lambda f: self._user_zoom(f),
                                    on_user_pan=lambda dx, dy: self._user_pan(dx, dy))
        self._pane_a.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        meta_outer = tk.Frame(body, bg=ENTRY_BG)
        meta_outer.grid(row=0, column=1, sticky="nsew", padx=4)

        meta_canvas = tk.Canvas(meta_outer, bg=ENTRY_BG, highlightthickness=0)
        meta_sb     = ttk.Scrollbar(meta_outer, orient="vertical", command=meta_canvas.yview)
        meta_canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        meta_sb.pack(side="right", fill="y")
        meta_canvas.configure(yscrollcommand=meta_sb.set)

        self._meta_frame = tk.Frame(meta_canvas, bg=ENTRY_BG, padx=0, pady=0)
        meta_win_id = meta_canvas.create_window((0, 0), window=self._meta_frame, anchor="nw")
        self._meta_frame.bind(
            "<Configure>",
            lambda _e: meta_canvas.configure(scrollregion=meta_canvas.bbox("all")),
        )
        meta_canvas.bind(
            "<Configure>",
            lambda e: meta_canvas.itemconfig(meta_win_id, width=e.width),
        )

        def _meta_scroll(event: tk.Event) -> str:
            if event.num == 5 or getattr(event, "delta", 0) < 0:
                meta_canvas.yview_scroll(1, "units")
            else:
                meta_canvas.yview_scroll(-1, "units")
            return "break"
        meta_canvas.bind("<MouseWheel>", _meta_scroll)
        meta_canvas.bind("<Button-4>", _meta_scroll)
        meta_canvas.bind("<Button-5>", _meta_scroll)

        self._pane_b = _ComparePane(body, on_user_zoom=lambda f: self._user_zoom(f),
                                    on_user_pan=lambda dx, dy: self._user_pan(dx, dy))
        self._pane_b.grid(row=0, column=2, sticky="nsew", padx=(4, 0))

        # ── File pickers (shown/hidden per group depending on file count) ───
        # Each combobox takes ~50% of the row so both are equally prominent.
        self._picker_row = tk.Frame(win, bg=PANEL)
        self._picker_row.pack(fill="x", padx=10, pady=(4, 4))
        self._picker_row.columnconfigure(0, weight=0)
        self._picker_row.columnconfigure(1, weight=1)
        self._picker_row.columnconfigure(2, weight=0)
        self._picker_row.columnconfigure(3, weight=1)

        self._var_a = tk.StringVar()
        self._var_b = tk.StringVar()
        tk.Label(self._picker_row, text="Left:", bg=PANEL, fg=FG_DIM,
                 font=FONT_MONO).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._cb_a = ttk.Combobox(self._picker_row, textvariable=self._var_a, state="readonly")
        self._cb_a.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        self._cb_a.bind("<<ComboboxSelected>>", lambda _e: self._swap_pane("a"))

        tk.Label(self._picker_row, text="Right:", bg=PANEL, fg=FG_DIM,
                 font=FONT_MONO).grid(row=0, column=2, sticky="w", padx=(0, 6))
        self._cb_b = ttk.Combobox(self._picker_row, textvariable=self._var_b, state="readonly")
        self._cb_b.grid(row=0, column=3, sticky="ew")
        self._cb_b.bind("<<ComboboxSelected>>", lambda _e: self._swap_pane("b"))

        # ── Shared zoom controls ──────────────────────────────────────────────
        zoom_row = tk.Frame(win, bg=PANEL)
        zoom_row.pack(fill="x", padx=10, pady=(0, 6))
        styled_button(zoom_row, "− Zoom out", lambda: self._user_zoom(0.8),
                     bg="#2a2a38", hov="#3a3a50", padx=12, pady=5).pack(side="left", padx=(0, 6))
        styled_button(zoom_row, "Reset / Fit", self._reset_zoom,
                     bg="#2a2a38", hov="#3a3a50", padx=12, pady=5).pack(side="left", padx=(0, 6))
        styled_button(zoom_row, "+ Zoom in", lambda: self._user_zoom(1.25),
                     bg="#2a2a38", hov="#3a3a50", padx=12, pady=5).pack(side="left")

        # ── File actions: delete one side, or mark ignored ───────────────────
        action_row = tk.Frame(win, bg=PANEL)
        action_row.pack(fill="x", padx=10, pady=(0, 10))
        styled_button(action_row, "🗑 Delete left", lambda: self._delete_one("a"),
                     bg="#4a2a2a", hov="#5a3a3a", padx=10, pady=5).pack(side="left", padx=(0, 6))
        styled_button(action_row, "🗑 Delete right", lambda: self._delete_one("b"),
                     bg="#4a2a2a", hov="#5a3a3a", padx=10, pady=5).pack(side="left", padx=(0, 16))
        styled_button(action_row, "🚫 Ignore left", lambda: self._mark_ignored("a"),
                     bg="#4a3a1a", hov="#5a4a2a", padx=10, pady=5).pack(side="left", padx=(0, 6))
        styled_button(action_row, "🚫 Ignore right", lambda: self._mark_ignored("b"),
                     bg="#4a3a1a", hov="#5a4a2a", padx=10, pady=5).pack(side="left")

    # ── shared zoom/pan, applied identically to both panes ──────────────────

    def _user_zoom(self, factor: float) -> None:
        self._zoom = max(0.05, min(self._zoom * factor, 8.0))
        self._apply_zoom()

    def _reset_zoom(self) -> None:
        fits = [p.fit_zoom() for p in (self._pane_a, self._pane_b) if p is not None]
        self._zoom = min(fits) if fits else 1.0
        self._pan_x = 0
        self._pan_y = 0
        self._apply_zoom()

    def _user_pan(self, dx: int, dy: int) -> None:
        self._pan_x += dx
        self._pan_y += dy
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        if self._pane_a is not None:
            self._pane_a.set_zoom(self._zoom, self._pan_x, self._pan_y)
        if self._pane_b is not None:
            self._pane_b.set_zoom(self._zoom, self._pan_x, self._pan_y)

    # ── content ──────────────────────────────────────────────────────────────

    def _refresh_metadata(self) -> None:
        path_a = Path(self._var_a.get())
        path_b = Path(self._var_b.get())
        fields_a = _file_metadata_fields(path_a)
        fields_b = _file_metadata_fields(path_b)
        _build_metadata_grid(self._meta_frame, fields_a, fields_b)

    def _swap_pane(self, which: str) -> None:
        var  = self._var_a if which == "a" else self._var_b
        pane = self._pane_a if which == "a" else self._pane_b
        path = Path(var.get())
        pane.load(path)
        self._refresh_metadata()
        self._apply_zoom()

    def show_group(self, group: DuplicateGroup, all_groups: list[DuplicateGroup] | None = None) -> None:
        """Load *group*'s first two files into the (re)opened comparator."""
        self._ensure_window()
        self._win.deiconify()
        self._win.lift()

        if all_groups is not None:
            self._all_groups = all_groups
        if group in self._all_groups:
            self._group_index = self._all_groups.index(group)
        self._group = group
        self._load_current_group()

    def _load_current_group(self) -> None:
        group = self._group
        if self._all_groups:
            n = len(self._all_groups)
            self._group_lbl.config(text=f"Group {self._group_index + 1} / {n}")
        else:
            self._group_lbl.config(text="")

        names = [str(p) for p in group.paths]
        self._cb_a.config(values=names)
        self._cb_b.config(values=names)
        self._var_a.set(names[0])
        self._var_b.set(names[1] if len(names) > 1 else names[0])

        # Hide the pickers entirely for simple 2-file groups — nothing to pick.
        if len(names) > 2:
            self._picker_row.pack(fill="x", padx=10, pady=(4, 4))
        else:
            self._picker_row.pack_forget()

        self._pane_a.load(group.paths[0])
        second = group.paths[1] if len(group.paths) > 1 else group.paths[0]
        self._pane_b.load(second)
        self._refresh_metadata()

        self._win.after(50, self._reset_zoom)

    def _prev_group(self) -> None:
        if not self._all_groups:
            return
        self._group_index = (self._group_index - 1) % len(self._all_groups)
        self._group = self._all_groups[self._group_index]
        self._load_current_group()

    def _next_group(self) -> None:
        if not self._all_groups:
            return
        self._group_index = (self._group_index + 1) % len(self._all_groups)
        self._group = self._all_groups[self._group_index]
        self._load_current_group()

    # ── per-file actions ─────────────────────────────────────────────────────

    def _current_path(self, which: str) -> Path:
        var = self._var_a if which == "a" else self._var_b
        return Path(var.get())

    def _delete_one(self, which: str) -> None:
        path = self._current_path(which)
        if not messagebox.askyesno("Delete file", f"Permanently delete {path.name}?"):
            return
        try:
            path.unlink()
        except OSError as e:
            messagebox.showerror("Delete failed", f"{path.name}: {e}")
            return
        db = get_db()
        if db is not None:
            db._con.execute("DELETE FROM artists_media WHERE filepath = ?", (str(path),))
            db._con.commit()
        self._remove_path_everywhere(path)

    def _mark_ignored(self, which: str) -> None:
        path = self._current_path(which)
        db = get_db()
        if db is None:
            messagebox.showerror("Database unavailable", "Can't update upload status.")
            return
        fp = str(path)
        if not db.get(fp):
            db.register(filename=path.name, filepath=fp)
        from core.file_db import STATUS_IGNORED
        db.set_status(fp, STATUS_IGNORED)
        messagebox.showinfo("Marked ignored", f"{path.name} is now marked as ignored for upload.")

    def _remove_path_everywhere(self, path: Path) -> None:
        """After deleting a file, drop it from the in-memory group and refresh."""
        if self._group is not None and path in self._group.paths:
            self._group.paths.remove(path)

        if self._group is not None and len(self._group.paths) <= 1:
            # This group no longer has duplicates — drop it and move on.
            if self._group in self._all_groups:
                self._all_groups.remove(self._group)
            if not self._all_groups:
                if self._on_files_changed:
                    self._on_files_changed(self._all_groups)
                self.close()
                return
            self._group_index = min(self._group_index, len(self._all_groups) - 1)
            self._group = self._all_groups[self._group_index]

        self._load_current_group()
        if self._on_files_changed:
            self._on_files_changed(self._all_groups)

    def close(self) -> None:
        if self.is_open:
            self._win.destroy()
        self._win = None


def build_duplicates(parent: tk.Frame) -> None:
    """Mount the full Find Duplicates UI onto *parent*."""

    photo_refs: list = []   # keep PhotoImage references alive across redraws
    scanning   = [False]
    selected_for_delete: set[str] = set()   # filepaths currently checked across all groups
    all_groups_ref: list[DuplicateGroup] = []   # most recent scan results, flat list

    def _on_compare_window_changed(remaining_groups: list[DuplicateGroup]) -> None:
        """
        Called by the comparator after it deletes a file on its own (the
        Delete left/right buttons). Keeps the card list, selection set, and
        delete-button count in sync with what the comparator just did.
        """
        all_groups_ref[:] = remaining_groups
        still_present = {p for g in remaining_groups for p in map(str, g.paths)}
        selected_for_delete.intersection_update(still_present)
        _refresh_delete_button()
        exact = [g for g in remaining_groups if g.kind == "exact"]
        near  = [g for g in remaining_groups if g.kind == "near"]
        _render_groups(exact, near)

    compare_win = _CompareWindow(parent, on_files_changed=_on_compare_window_changed)

    # ── Outer scrollable canvas ───────────────────────────────────────────────
    outer_canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
    outer_sb     = ttk.Scrollbar(parent, orient="vertical", command=outer_canvas.yview)
    scroll_frame = tk.Frame(outer_canvas, bg=PANEL)

    win_id = outer_canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
    outer_canvas.configure(yscrollcommand=outer_sb.set)

    scroll_frame.bind("<Configure>",
        lambda _e: outer_canvas.configure(scrollregion=outer_canvas.bbox("all")))
    outer_canvas.bind("<Configure>",
        lambda e: outer_canvas.itemconfig(win_id, width=e.width))

    outer_canvas.pack(side="left", fill="both", expand=True)
    outer_sb.pack(side="right", fill="y")
    register_scroll_canvas(outer_canvas)

    # ── Collapsible search-parameters section ───────────────────────────────
    # Everything from the title down through the scan status line lives in
    # one foldable block, so once a scan is done the controls can be tucked
    # away to leave more room for the results below.
    params_expanded = [True]

    params_toggle_row = tk.Frame(scroll_frame, bg=PANEL, cursor="hand2")
    params_toggle_row.pack(fill="x", padx=PAD_OUTER, pady=(20, 0))

    params_arrow = tk.Label(params_toggle_row, text="▼", bg=PANEL, fg=ACCENT,
                            font=FONT_BOLD, cursor="hand2")
    params_arrow.pack(side="left", padx=(0, 8))

    params_title_lbl = tk.Label(params_toggle_row, text="FIND DUPLICATES",
                                bg=PANEL, fg=FG, font=FONT_HEAD, cursor="hand2")
    params_title_lbl.pack(side="left")

    params_body = tk.Frame(scroll_frame, bg=PANEL)
    params_body.pack(fill="x", after=params_toggle_row)

    def _toggle_params(_e=None) -> None:
        if params_expanded[0]:
            params_body.pack_forget()
            params_arrow.config(text="▶")
            params_expanded[0] = False
        else:
            params_body.pack(fill="x", after=params_toggle_row)
            params_arrow.config(text="▼")
            params_expanded[0] = True

    for w in (params_toggle_row, params_arrow, params_title_lbl):
        w.bind("<Button-1>", _toggle_params)

    # ── Header ────────────────────────────────────────────────────────────────
    tk.Label(params_body,
             text="hash every image and video to find exact and near-duplicate copies",
             bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w",
             ).pack(fill="x", padx=PAD_OUTER + 2, pady=(2, 10))
    divider(params_body)

    # ── Scan folder ───────────────────────────────────────────────────────────
    section_label(params_body, "SCAN FOLDER  (same folder the Uploader tab uses)")
    folder_row = tk.Frame(params_body, bg=PANEL)
    folder_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    frf, folder_entry = styled_entry(folder_row, "./download", width=50)
    frf.pack(side="left", padx=(0, 8))

    def _browse_folder() -> None:
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Select folder to scan")
        if path:
            folder_entry.config(fg=FG)
            folder_entry.delete(0, "end")
            folder_entry.insert(0, path)

    styled_button(folder_row, "Browse...", command=_browse_folder).pack(side="left")

    # ── Near-duplicate sensitivity ──────────────────────────────────────────────
    section_label(
        params_body,
        "NEAR-DUPLICATE SENSITIVITY  "
        "(higher = catches more, but more false positives)",
    )
    thresh_row = tk.Frame(params_body, bg=PANEL)
    thresh_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    thresh_var = tk.IntVar(value=DEFAULT_PHASH_THRESHOLD)
    thresh_scale = tk.Scale(
        thresh_row, from_=0, to=20, orient="horizontal", variable=thresh_var,
        bg=PANEL, fg=FG, troughcolor=ENTRY_BG, highlightthickness=0,
        font=FONT_MONO, length=300, showvalue=True,
    )
    thresh_scale.pack(side="left")
    tk.Label(thresh_row, text="  0 = exact match only · 8 = default · 20 = loose",
             bg=PANEL, fg=FG_DIM, font=FONT_MONO).pack(side="left")

    if not ffmpeg_available():
        tk.Label(
            params_body,
            text="⚠ ffmpeg not found on this system — videos will still be checked "
                 "for exact duplicates, but not near-duplicates, and won't show "
                 "thumbnails. Install ffmpeg and restart to enable this.",
            bg=PANEL, fg=COLOR_FAIL, font=FONT_MONO, anchor="w", justify="left",
            wraplength=700,
        ).pack(fill="x", padx=PAD_OUTER, pady=(0, 6))

    if not imagehash_available():
        tk.Label(
            params_body,
            text="⚠ 'imagehash' package not installed — near-duplicate detection "
                 "will find NOTHING (exact duplicates still work fine). Run:  "
                 "pip install imagehash   then restart.",
            bg=PANEL, fg=COLOR_FAIL, font=FONT_MONO, anchor="w", justify="left",
            wraplength=700,
        ).pack(fill="x", padx=PAD_OUTER, pady=(0, 6))

    divider(params_body)

    # ── Scan controls ───────────────────────────────────────────────────────────
    scan_row = tk.Frame(params_body, bg=PANEL)
    scan_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    scan_btn = styled_button(scan_row, "🔍 Scan for Duplicates",
                             bg="#2a2a38", hov="#3a3a50")
    scan_btn.pack(side="left")

    progress = ttk.Progressbar(scan_row, orient="horizontal", length=300,
                               mode="determinate")
    progress.pack(side="left", padx=(12, 0))

    status_lbl = tk.Label(params_body, text="Click Scan to begin.",
                          bg=PANEL, fg=FG_DIM, font=FONT_MONO, anchor="w")
    status_lbl.pack(fill="x", padx=PAD_OUTER, pady=(0, 10))

    divider(scroll_frame)

    # ── Results toolbar ──────────────────────────────────────────────────────
    results_toolbar = tk.Frame(scroll_frame, bg=PANEL)
    results_toolbar.pack(fill="x", padx=PAD_OUTER, pady=(0, 10))

    def _open_comparator() -> None:
        if all_groups_ref:
            compare_win.show_group(all_groups_ref[0], all_groups_ref)
        else:
            messagebox.showinfo("No groups yet", "Run a scan first to find duplicate groups.")

    compare_open_btn = styled_button(
        results_toolbar, "🔍 Open Comparator", command=_open_comparator,
        bg="#2a2a48", hov="#3a3a60",
    )
    compare_open_btn.pack(side="left", padx=(0, 8))

    def _refresh_delete_button() -> None:
        n = len(selected_for_delete)
        delete_selected_btn.config(
            text=f"🗑 Delete selected ({n})",
            state="normal" if n else "disabled",
        )

    def _delete_selected() -> None:
        if not selected_for_delete:
            return
        paths = [Path(p) for p in selected_for_delete]
        names = "\n".join(f"  - {p.name}" for p in paths)
        if not messagebox.askyesno(
            "Delete selected files",
            f"Permanently delete {len(paths)} file(s)?\n\n{names}",
        ):
            return
        db = get_db()
        failed = []
        for p in paths:
            try:
                p.unlink()
                if db is not None:
                    db._con.execute("DELETE FROM artists_media WHERE filepath = ?", (str(p),))
                    db._con.commit()
                selected_for_delete.discard(str(p))
            except OSError as e:
                failed.append(f"{p.name}: {e}")
        _refresh_delete_button()
        # Easiest correct way to reflect deletions in the group cards: redraw
        # from the groups we already have, dropping files that no longer exist.
        for group in all_groups_ref:
            group.paths = [p for p in group.paths if p.exists()]
        remaining = [g for g in all_groups_ref if len(g.paths) > 1]
        all_groups_ref[:] = remaining
        exact = [g for g in remaining if g.kind == "exact"]
        near  = [g for g in remaining if g.kind == "near"]
        _render_groups(exact, near)
        if failed:
            messagebox.showerror("Some files could not be deleted", "\n".join(failed))

    delete_selected_btn = styled_button(
        results_toolbar, "🗑 Delete selected (0)", command=_delete_selected,
        bg="#4a2a2a", hov="#5a3a3a",
    )
    delete_selected_btn.config(state="disabled")
    delete_selected_btn.pack(side="left")

    # ── Results area ─────────────────────────────────────────────────────────
    results_frame = tk.Frame(scroll_frame, bg=PANEL)
    results_frame.pack(fill="both", expand=True, padx=PAD_OUTER, pady=(0, 30))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_base() -> Path:
        try:
            cfg  = load_config(DEFAULT_CONFIG)
            base = (SCRIPT_DIR / cfg.get("download_dir", "./download")).resolve()
        except (ConfigError, Exception) as e:
            print(f"[duplicates] config error: {e}")
            base = (SCRIPT_DIR / "download").resolve()
        return base

    def _make_thumb(path: Path):
        try:
            from PIL import ImageTk
        except ImportError:
            log.warning("_make_thumb: PIL.ImageTk not importable, can't render thumbnails")
            return None

        thumb = open_thumbnail_image(path, size=(THUMB_SIZE, THUMB_SIZE))
        if thumb is None:
            return None

        try:
            photo = ImageTk.PhotoImage(thumb)
        except Exception as e:
            # On Windows this is where "Fail to allocate bitmap" surfaces —
            # Tk/Tcl ran out of native bitmap handles (a GDI/desktop-heap
            # limit, not a Python memory issue). Treat it the same as any
            # other unavailable preview rather than crashing the render.
            log.warning("_make_thumb: could not create bitmap for %s: %s", path.name, e)
            return None

        photo_refs.append(photo)   # prevent garbage collection
        return photo

    def _release_photo_refs() -> None:
        """
        Explicitly tell Tcl to free each image's native bitmap handle,
        rather than relying solely on Python's refcounting/GC to get
        around to it. On Windows, PhotoImage backs onto a GDI bitmap —
        waiting for GC can leave old handles alive longer than expected
        and contributes directly to "Fail to allocate bitmap" once
        enough scans/redraws have happened in one session.
        """
        for photo in photo_refs:
            try:
                photo.__del__()   # Tkinter's own teardown: tk.call('image', 'delete', name)
            except Exception:
                pass
        photo_refs.clear()

    def _render_groups(exact_groups: list[DuplicateGroup],
                       near_groups: list[DuplicateGroup]) -> None:
        for w in results_frame.winfo_children():
            w.destroy()
        _release_photo_refs()

        all_groups_ref[:] = exact_groups + near_groups

        if not exact_groups and not near_groups:
            tk.Label(results_frame, text="No duplicates found.",
                     bg=PANEL, fg=COLOR_OK, font=FONT_BOLD).pack(anchor="w", pady=20)
            return

        all_groups = exact_groups + near_groups
        rendered_count = [0]

        def _render_batch(start: int) -> None:
            end = min(start + MAX_RENDERED_GROUPS, len(all_groups))
            last_kind = None
            for group in all_groups[start:end]:
                if group.kind != last_kind:
                    title = (f"EXACT DUPLICATES ({len(exact_groups)} group(s))" if group.kind == "exact"
                             else f"NEAR-DUPLICATES ({len(near_groups)} group(s))")
                    tk.Label(results_frame, text=title, bg=PANEL, fg=ACCENT2, font=FONT_BOLD,
                             ).pack(anchor="w", pady=(16, 6))
                    last_kind = group.kind
                _render_group(group)
            rendered_count[0] = end

            if end < len(all_groups):
                remaining = len(all_groups) - end
                more_btn = styled_button(
                    results_frame, f"Show {min(MAX_RENDERED_GROUPS, remaining)} more "
                                   f"({remaining} group(s) not yet shown)",
                    bg="#2a2a38", hov="#3a3a50",
                )
                def _show_more() -> None:
                    more_btn.destroy()
                    _render_batch(end)
                more_btn.config(command=_show_more)
                more_btn.pack(anchor="w", pady=10)

        log.debug("_render_groups: %d group(s) total, rendering in batches of %d",
                  len(all_groups), MAX_RENDERED_GROUPS)
        _render_batch(0)

    def _render_group(group: DuplicateGroup) -> None:
        card = tk.Frame(results_frame, bg=BORDER, padx=1, pady=1, cursor="hand2")
        card.pack(fill="x", pady=4)
        inner = tk.Frame(card, bg=ENTRY_BG, padx=10, pady=10, cursor="hand2")
        inner.pack(fill="x")

        def _load_into_comparator(_e=None, grp=group) -> None:
            compare_win.show_group(grp, all_groups_ref)

        hdr = tk.Frame(inner, bg=ENTRY_BG, cursor="hand2")
        hdr.pack(fill="x", pady=(0, 8))
        label = "Identical files" if group.kind == "exact" else f"Similar / mixed (distance {group.distance})"
        hdr_lbl = tk.Label(hdr, text=label, bg=ENTRY_BG, fg=FG, font=FONT_BOLD, cursor="hand2")
        hdr_lbl.pack(side="left")
        tk.Label(hdr, text="  (click to open in comparator)", bg=ENTRY_BG, fg=FG_DIM,
                 font=FONT_MONO, cursor="hand2").pack(side="left")

        row = tk.Frame(inner, bg=ENTRY_BG, cursor="hand2")
        row.pack(fill="x")

        # Clicking anywhere on the card (except a checkbox itself) loads this
        # group into the one shared comparator window.
        for w in (card, inner, hdr, hdr_lbl, row):
            w.bind("<Button-1>", _load_into_comparator)

        for path in group.paths:
            cell = tk.Frame(row, bg=ENTRY_BG, padx=8, cursor="hand2")
            cell.pack(side="left", anchor="n")
            cell.bind("<Button-1>", _load_into_comparator)

            thumb = _make_thumb(path)
            if thumb is not None:
                thumb_lbl = tk.Label(cell, image=thumb, bg=ENTRY_BG, cursor="hand2")
                thumb_lbl.pack()
                thumb_lbl.bind("<Button-1>", _load_into_comparator)
            elif is_video(path) and not ffmpeg_available():
                tk.Label(cell, text="(install ffmpeg\nfor video preview)", bg=ENTRY_BG,
                         fg=FG_DIM, font=FONT_MONO, width=16, height=8, cursor="hand2",
                         ).pack()
            else:
                tk.Label(cell, text="(preview\nunavailable)", bg=ENTRY_BG,
                         fg=FG_DIM, font=FONT_MONO, width=16, height=8, cursor="hand2",
                         ).pack()

            try:
                size_kb = path.stat().st_size // 1024
            except OSError:
                size_kb = 0
            name_text = ("🎬 " if is_video(path) else "") + path.name
            name_lbl = tk.Label(cell, text=name_text, bg=ENTRY_BG, fg=FG,
                                font=FONT_MONO, wraplength=THUMB_SIZE, cursor="hand2")
            name_lbl.pack(pady=(4, 0))
            name_lbl.bind("<Button-1>", _load_into_comparator)
            tk.Label(cell, text=f"{size_kb} KB", bg=ENTRY_BG, fg=FG_DIM,
                     font=FONT_MONO, cursor="hand2").pack()

            # Checkbox for deletion — independent of the comparator click, so
            # ticking a box never triggers the card's "open in comparator"
            # handler (no <Button-1> binding here, and Checkbutton consumes
            # its own click before it could bubble to a parent binding).
            path_str = str(path)
            chk_var = tk.BooleanVar(value=path_str in selected_for_delete)

            def _on_check_toggle(p=path_str, var=chk_var) -> None:
                if var.get():
                    selected_for_delete.add(p)
                else:
                    selected_for_delete.discard(p)
                _refresh_delete_button()

            tk.Checkbutton(
                cell, text="Select to delete", variable=chk_var,
                command=_on_check_toggle,
                bg=ENTRY_BG, fg=FG, selectcolor=PANEL,
                activebackground=ENTRY_BG, activeforeground=ACCENT,
                font=FONT_MONO,
            ).pack(pady=(4, 0))

    # ── Scan logic ────────────────────────────────────────────────────────────

    def _start_scan() -> None:
        if scanning[0]:
            return
        db = get_db()
        if db is None:
            messagebox.showerror("Database unavailable",
                                 "The media database isn't initialized.")
            return

        folder = Path(folder_entry.get().strip() or str(_resolve_base()))
        if not folder.exists():
            messagebox.showerror("Folder not found", f"{folder} does not exist.")
            return

        images = iter_image_files(folder)
        if not images:
            status_lbl.config(text="No media found in this folder.", fg=FG_DIM)
            _render_groups([], [])
            return

        scanning[0] = True
        scan_btn.config(state="disabled", text="Scanning...")
        progress.config(value=0, maximum=len(images))
        status_lbl.config(text=f"Hashing {len(images)} file(s)...", fg=COLOR_RUNNING)

        def _worker() -> None:
            # The DB and media-scan modules log one DEBUG line per write —
            # genuinely useful when tracing a single action, but a wall of
            # spam across a scan of hundreds of files. Raise their level for
            # just this loop and always restore it afterward, even on error.
            noisy_loggers = [logging.getLogger("core.file_db"),
                            logging.getLogger("core.media_scan")]
            previous_levels = [lg.level for lg in noisy_loggers]
            for lg in noisy_loggers:
                lg.setLevel(logging.INFO)

            try:
                to_hash = set(p for p in images if needs_hashing(db, p))
                done = 0
                hash_errors = 0
                for path in images:
                    if path in to_hash:
                        result = hash_and_store(db, path)
                        if result.error:
                            hash_errors += 1
                            log.debug("scan: %s failed to hash: %s", path.name, result.error)
                    done += 1
                    if done % 5 == 0 or done == len(images):
                        parent.after(0, lambda d=done: progress.config(value=d))

                log.debug(
                    "scan: hashed %d/%d file(s) (%d already cached, %d error(s))",
                    len(to_hash), len(images), len(images) - len(to_hash), hash_errors,
                )

                threshold = thresh_var.get()
                # threshold=0 on the slider means "exact match only" — pass
                # a negative distance so no phash edge can ever qualify,
                # which disables near-merging entirely while still using
                # the same unified function (so exact-in-exact collapsing
                # of same-real-file rows stays consistent either way).
                merge_threshold = threshold if threshold > 0 else -1
                all_groups = find_all_duplicate_groups(db, threshold=merge_threshold)
                exact_groups = [g for g in all_groups if g.kind == "exact"]
                near_groups  = [g for g in all_groups if g.kind == "near"]
            finally:
                for lg, lvl in zip(noisy_loggers, previous_levels):
                    lg.setLevel(lvl)

            def _finish() -> None:
                scanning[0] = False
                scan_btn.config(state="normal", text="🔍 Scan for Duplicates")
                total_dupes = sum(len(g.paths) for g in exact_groups + near_groups)
                if total_dupes:
                    status_lbl.config(
                        text=f"Found {len(exact_groups)} exact + "
                             f"{len(near_groups)} near-duplicate group(s) "
                             f"({total_dupes} files involved).",
                        fg=COLOR_OK,
                    )
                    if params_expanded[0]:
                        _toggle_params()   # fold the controls away to make room
                else:
                    status_lbl.config(text="No duplicates found.", fg=COLOR_OK)
                _render_groups(exact_groups, near_groups)

            parent.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

    scan_btn.config(command=_start_scan)