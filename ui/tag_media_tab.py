"""
ui/tag_media_tab.py
--------------------
Tag Media tab.

Browse every image under the configured download folder, view/edit its
tags, and save — writes to BOTH:
  - the .txt sidecar file (base/artist/.tags/site/image.txt) so the
    Uploader tab picks up the same tags unchanged, and
  - the artists_media table in hoard.db (tags column), so this tab and
    any future feature can read tags back without touching disk.

Layout mirrors the Uploader tab's wizard: folder picker + load button,
then a single-image view with thumbnail, tag editor, and prev/next nav.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.catalogue import ConfigError, load_config
from core.file_db import get_db
from core.media_scan import (
    iter_image_files, sync_tags, open_thumbnail_image, is_video, ffmpeg_available,
)
from core.upload_data import read_tag_file
from ui.scroll import register_scroll_canvas
from ui.theme import (
    ACCENT, ACCENT2, BORDER, ENTRY_BG, FG, FG_DIM,
    FONT_BODY, FONT_BOLD, FONT_HEAD, FONT_MONO, FONT_SUB,
    PANEL, PAD_OUTER, COLOR_OK, COLOR_FAIL,
)
from ui.widgets import divider, section_label, styled_button, styled_entry
from ui.uploader_tab import TagPicker

SCRIPT_DIR     = Path(__file__).parent.parent
DEFAULT_CONFIG = str(SCRIPT_DIR / "config.yaml")


def build_tag_media(parent: tk.Frame) -> None:
    """Mount the full Tag Media UI onto *parent*."""

    # ── Mutable state ─────────────────────────────────────────────────────────
    queue:       list[Path] = []
    queue_index: list[int]  = [0]
    base_path:   list[Path | None] = [None]
    photo_ref:   list = [None]   # keep a reference so PhotoImage isn't GC'd
    dirty:       list[bool] = [False]   # unsaved edits on the current image

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

    # ── Header ────────────────────────────────────────────────────────────────
    tk.Frame(scroll_frame, bg=PANEL, height=30).pack()
    tk.Label(scroll_frame, text="TAG MEDIA", bg=PANEL, fg=FG,
             font=FONT_HEAD, anchor="w").pack(fill="x", padx=PAD_OUTER)
    tk.Label(scroll_frame, text="browse downloaded images and videos, and edit their tags",
             bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w",
             ).pack(fill="x", padx=PAD_OUTER + 2, pady=(0, 10))
    divider(scroll_frame)

    # ── Scan folder ───────────────────────────────────────────────────────────
    section_label(scroll_frame, "SCAN FOLDER  (same folder the Uploader tab uses)")
    folder_row = tk.Frame(scroll_frame, bg=PANEL)
    folder_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    frf, folder_entry = styled_entry(folder_row, "./download", width=50)
    frf.pack(side="left", padx=(0, 8))

    def _browse_folder() -> None:
        path = filedialog.askdirectory(title="Select folder to scan")
        if path:
            folder_entry.config(fg=FG)
            folder_entry.delete(0, "end")
            folder_entry.insert(0, path)

    styled_button(folder_row, "Browse...", command=_browse_folder).pack(side="left")

    divider(scroll_frame)

    # ── Filter + load row ───────────────────────────────────────────────────────
    filter_row = tk.Frame(scroll_frame, bg=PANEL)
    filter_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    tk.Label(filter_row, text="Show:", bg=PANEL, fg=FG_DIM,
             font=FONT_MONO).pack(side="left", padx=(0, 6))
    filter_var = tk.StringVar(value="all")
    for value, label in [("all", "All"), ("untagged", "Untagged only"), ("tagged", "Tagged only")]:
        tk.Radiobutton(
            filter_row, text=label, variable=filter_var, value=value,
            bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
            activebackground=PANEL, activeforeground=ACCENT,
            font=FONT_MONO,
        ).pack(side="left", padx=(0, 10))

    load_row = tk.Frame(scroll_frame, bg=PANEL)
    load_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 10))
    queue_lbl = tk.Label(load_row, text="No media loaded.",
                         bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    queue_lbl.pack(side="left")

    def _has_tags(img: Path, base: Path) -> bool:
        db = get_db()
        if db is not None:
            cached = db.get_tags(str(img))
            if cached:
                return True
        # Fall back to sidecar check — read_tag_file always returns at
        # least [artist] when nothing is tagged, so >1 entry means tagged.
        return len(read_tag_file(img, base)) > 1

    def _load_images() -> None:
        try:
            cfg  = load_config(DEFAULT_CONFIG)
            base = (SCRIPT_DIR / cfg.get("download_dir", "./download")).resolve()
        except (ConfigError, Exception) as e:
            print(f"[tag_media] config error: {e}")
            base = (SCRIPT_DIR / "download").resolve()

        folder = Path(folder_entry.get().strip() or str(base))
        base.mkdir(parents=True, exist_ok=True)
        folder.mkdir(parents=True, exist_ok=True)
        base_path[0] = base

        all_images = iter_image_files(folder)
        mode = filter_var.get()
        if mode == "untagged":
            images = [p for p in all_images if not _has_tags(p, base)]
        elif mode == "tagged":
            images = [p for p in all_images if _has_tags(p, base)]
        else:
            images = all_images

        queue.clear()
        queue.extend(images)
        queue_index[0] = 0

        if not images:
            queue_lbl.config(text="No media found for this filter.", fg=FG_DIM)
            _clear_view()
        else:
            queue_lbl.config(text=f"{len(images)} file(s) loaded.", fg=COLOR_OK)
            _show_current()

    styled_button(load_row, "Load Media", command=_load_images,
                  bg="#2a2a38", hov="#3a3a50").pack(side="right")

    divider(scroll_frame)

    # ── Main view: two-column layout ────────────────────────────────────────────
    view = tk.Frame(scroll_frame, bg=PANEL)
    view.pack(fill="both", expand=True, padx=PAD_OUTER, pady=(0, 20))
    view.columnconfigure(0, weight=0)
    view.columnconfigure(1, weight=1)

    # Left: preview + filename + nav
    left = tk.Frame(view, bg=PANEL)
    left.grid(row=0, column=0, sticky="n", padx=(0, 24))

    preview_lbl = tk.Label(left, bg=ENTRY_BG, fg=FG_DIM, font=FONT_MONO,
                           text="Load media to begin", width=40, height=18)
    preview_lbl.pack()

    path_lbl = tk.Label(left, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO,
                        wraplength=320, justify="left")
    path_lbl.pack(fill="x", pady=(8, 0))

    nav_row = tk.Frame(left, bg=PANEL)
    nav_row.pack(fill="x", pady=(8, 0))
    prev_btn = styled_button(nav_row, "← Prev", bg="#2a2a38", hov="#3a3a50")
    prev_btn.pack(side="left")
    nav_lbl = tk.Label(nav_row, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    nav_lbl.pack(side="left", expand=True)
    next_btn = styled_button(nav_row, "Next →", bg="#2a2a38", hov="#3a3a50")
    next_btn.pack(side="right")

    # Right: tag editor
    right = tk.Frame(view, bg=PANEL)
    right.grid(row=0, column=1, sticky="nsew")

    tag_picker = TagPicker(right, "TAGS")
    tag_picker.pack(fill="x")

    status_lbl = tk.Label(right, text="", bg=PANEL, fg=FG_DIM, font=FONT_MONO,
                          anchor="w")
    status_lbl.pack(fill="x", pady=(10, 0))

    action_row = tk.Frame(right, bg=PANEL)
    action_row.pack(fill="x", pady=(10, 0))
    save_btn = styled_button(action_row, "💾 Save Tags", bg="#2a4a2a", hov="#3a5a3a")
    save_btn.pack(side="left")

    # ── View logic ────────────────────────────────────────────────────────────

    def _clear_view() -> None:
        preview_lbl.config(image="", text="Load media to begin")
        photo_ref[0] = None
        path_lbl.config(text="")
        nav_lbl.config(text="")
        tag_picker.clear()
        status_lbl.config(text="")
        dirty[0] = False

    def _load_preview(img_path: Path) -> None:
        try:
            from PIL import ImageTk
        except ImportError:
            preview_lbl.config(image="", text="Install Pillow\nfor previews")
            photo_ref[0] = None
            return

        thumb = open_thumbnail_image(img_path, size=(320, 320))
        if thumb is None:
            if is_video(img_path) and not ffmpeg_available():
                preview_lbl.config(
                    image="",
                    text="ffmpeg not found —\ncan't preview video\n(tagging still works)",
                )
            else:
                preview_lbl.config(image="", text="Preview unavailable\n(file may be corrupt)")
            photo_ref[0] = None
            return

        photo = ImageTk.PhotoImage(thumb)
        photo_ref[0] = photo
        preview_lbl.config(image=photo, text="")

    def _confirm_discard_if_dirty() -> bool:
        """True if it's OK to navigate away (no unsaved edits, or user confirmed)."""
        if not dirty[0]:
            return True
        return messagebox.askyesno(
            "Unsaved changes",
            "This file's tags haven't been saved. Discard changes?",
        )

    def _show_current() -> None:
        if not queue:
            _clear_view()
            return
        idx = queue_index[0]
        img_path = queue[idx]
        base = base_path[0] or img_path.parent

        _load_preview(img_path)
        try:
            rel = img_path.relative_to(base)
        except ValueError:
            rel = img_path
        prefix = "🎬 " if is_video(img_path) else ""
        path_lbl.config(text=f"{prefix}{rel}")
        nav_lbl.config(text=f"{idx + 1} / {len(queue)}")

        db = get_db()
        cached = db.get_tags(str(img_path)) if db else []
        tags = cached if cached else read_tag_file(img_path, base)
        tag_picker.set_selected(tags)
        status_lbl.config(text="")
        dirty[0] = False

        prev_btn.config(state="normal" if idx > 0 else "disabled")
        next_btn.config(state="normal" if idx < len(queue) - 1 else "disabled")

        # Mark dirty on any tag edit (typing in the picker's entry)
        tag_picker._entry.bind("<KeyRelease>", lambda _e: dirty.__setitem__(0, True), add=True)

    def _on_prev() -> None:
        if queue_index[0] <= 0:
            return
        if not _confirm_discard_if_dirty():
            return
        queue_index[0] -= 1
        _show_current()

    def _on_next() -> None:
        if queue_index[0] >= len(queue) - 1:
            return
        if not _confirm_discard_if_dirty():
            return
        queue_index[0] += 1
        _show_current()

    def _on_save() -> None:
        if not queue:
            return
        img_path = queue[queue_index[0]]
        base = base_path[0] or img_path.parent
        tags = tag_picker.get_selected()
        if not tags:
            messagebox.showwarning("No tags", "Add at least one tag before saving.")
            return

        db = get_db()
        try:
            if db is not None:
                sidecar = sync_tags(db, base, img_path, tags)
            else:
                from core.upload_data import write_tag_file
                sidecar = write_tag_file(img_path, base, tags)
        except OSError as e:
            status_lbl.config(text=f"✗ Save failed: {e}", fg=COLOR_FAIL)
            return

        dirty[0] = False
        status_lbl.config(text=f"✓ Saved to {sidecar.name}", fg=COLOR_OK)

    prev_btn.config(command=_on_prev)
    next_btn.config(command=_on_next)
    save_btn.config(command=_on_save)

    _clear_view()
