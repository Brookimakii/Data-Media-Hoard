"""
ui/uploader_tab.py
------------------
Wizard-style uploader tab.

Flow
----
1. User sets server, credentials, upload folder.
2. "Load Queue" scans the folder, skipping uploaded/DNU files.
3. For each image, the wizard shows:
   - Image preview
   - Auto-populated tags (from .txt sidecar)
   - Character tag picker
   - Copyright tag picker
   - Rating selector
   - Source (derived from path, editable)
   - Description field
4. User clicks Upload, Skip, or DNU.
5. On upload success the file is marked in uploaded_dnu.json.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from core.catalogue import ConfigError, load_config
from core.upload import RATING_OPTIONS, rating_label_to_code, upload_image
from core.upload_data import (
    derive_source_url,
    expected_tag_path,
    fetch_description,
    find_tag_file,
    derive_source_from_path,
    load_character_tags,
    load_copyright_tags,
    read_tag_file,
    scan_upload_folder,
    UploadedDNU,
)
from core.file_db import get_db
from ui.scroll import register_scroll_canvas
from ui.theme import (
    ACCENT, ACCENT2, BORDER, ENTRY_BG, FG, FG_DIM,
    FONT_BODY, FONT_BOLD, FONT_HEAD, FONT_MONO, FONT_SUB, FONT_TAGS,
    PANEL, PAD_OUTER,
    COLOR_OK, COLOR_FAIL, COLOR_RUNNING,
)
from ui.widgets import (
    divider, section_label, styled_button, styled_entry,
)

SCRIPT_DIR     = Path(__file__).parent.parent
DEFAULT_CONFIG = str(SCRIPT_DIR / "config.yaml")


# ── Tag picker widget ──────────────────────────────────────────────────────────

class TagPicker(tk.Frame):
    """
    Compact widget for picking tags from a known list and adding new ones.
    Selected tags are shown as removable chips below the input.
    """

    def __init__(self, parent: tk.Widget, label: str, **kwargs) -> None:
        super().__init__(parent, bg=PANEL, **kwargs)
        self._selected: list[str] = []
        self._known:    list[str] = []
        self._label = label
        self._build()

    def _build(self) -> None:
        tk.Label(self, text=self._label, bg=PANEL, fg=ACCENT2,
                 font=FONT_BOLD, anchor="w"
                 ).pack(fill="x", pady=(6, 2))

        input_row = tk.Frame(self, bg=PANEL)
        input_row.pack(fill="x")

        self._var = tk.StringVar()
        self._entry = tk.Entry(
            input_row, textvariable=self._var,
            bg=ENTRY_BG, fg=FG, insertbackground=ACCENT,
            relief="flat", font=FONT_BODY,
        )
        self._entry.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=4)
        self._entry.bind("<Return>",     lambda _e: self._add_current())
        self._entry.bind("<KeyRelease>", self._on_key)

        styled_button(input_row, "+", command=self._add_current,
                      padx=8, pady=4).pack(side="left")

        # Dropdown suggestion list (hidden until typing)
        self._listbox_frame = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        self._listbox = tk.Listbox(
            self._listbox_frame,
            bg=ENTRY_BG, fg=FG, selectbackground=ACCENT,
            relief="flat", font=FONT_MONO, height=4,
            activestyle="none",
        )
        self._listbox.pack(fill="both", expand=True)
        self._listbox.bind("<Double-Button-1>", self._pick_from_list)
        self._listbox.bind("<Return>",          self._pick_from_list)

        # Chips area — Text widget as wrapping container
        self._chips_frame = tk.Frame(self, bg=PANEL)
        self._chips_frame.pack(fill="x", pady=(4, 0))

    # ── public API ─────────────────────────────────────────────────────────────

    def set_known(self, tags: list[str]) -> None:
        self._known = tags

    def get_selected(self) -> list[str]:
        return list(self._selected)

    def set_selected(self, tags: list[str]) -> None:
        self._selected = [t.strip().strip(",") for t in tags if t.strip().strip(",")]
        self._refresh_chips()

    def clear(self) -> None:
        self._selected = []
        self._var.set("")
        self._hide_listbox()
        self._refresh_chips()

    # ── internals ──────────────────────────────────────────────────────────────

    def _on_key(self, _e: tk.Event) -> None:
        text = self._var.get().strip()
        if text:
            self._refresh_listbox(text)
            self._show_listbox()
        else:
            self._hide_listbox()

    def _refresh_listbox(self, prefix: str) -> None:
        self._listbox.delete(0, "end")
        for tag in self._known:
            if prefix.lower() in tag.lower() and tag not in self._selected:
                self._listbox.insert("end", tag)

    def _show_listbox(self) -> None:
        if not self._listbox_frame.winfo_ismapped():
            self._listbox_frame.pack(fill="x", pady=(2, 0))

    def _hide_listbox(self) -> None:
        self._listbox_frame.pack_forget()

    def _add_current(self) -> None:
        tag = self._var.get().strip().strip(",")
        if tag and tag not in self._selected:
            self._selected.append(tag)
            self._refresh_chips()
        self._var.set("")
        self._hide_listbox()

    def _pick_from_list(self, _e: tk.Event | None = None) -> None:
        sel = self._listbox.curselection()
        if sel:
            tag = self._listbox.get(sel[0])
            if tag not in self._selected:
                self._selected.append(tag)
                self._refresh_chips()
            self._var.set("")
            self._hide_listbox()

    def _refresh_chips(self) -> None:
        for w in self._chips_frame.winfo_children():
            w.destroy()
        if not self._selected:
            return

        # Use a Text widget as the chip container so tkinter handles
        # word-wrapping natively — no manual width measuring, no re-entrancy.
        txt = tk.Text(
            self._chips_frame,
            bg=PANEL, relief="flat", cursor="arrow",
            font=FONT_MONO, height=1, wrap="word",
            state="normal", padx=0, pady=2,
            highlightthickness=0, borderwidth=0,
        )
        txt.pack(fill="x")

        for tag in self._selected:
            tag = tag.strip().strip(",")
            if not tag:
                continue
            chip = tk.Frame(txt, bg="#2a2040", padx=4, pady=2)
            tk.Button(chip, text="x", bg="#2a2040", fg=FG_DIM,
                      relief="flat", font=FONT_MONO, cursor="hand2",
                      command=lambda t=tag: self._remove(t)).pack(side="left")
            tk.Label(chip, text=tag, bg="#2a2040", fg=ACCENT,
                     font=FONT_MONO).pack(side="left")
            txt.window_create("end", window=chip, padx=2, pady=2)

        txt.config(state="disabled")

    def _remove(self, tag: str) -> None:
        if tag in self._selected:
            self._selected.remove(tag)
            self._refresh_chips()


# ── Uploader tab ───────────────────────────────────────────────────────────────

def build_uploader(parent: tk.Frame) -> None:
    """Mount the full uploader UI onto *parent*."""

    # ── Mutable state ─────────────────────────────────────────────────────────
    queue:        list[Path] = []
    queue_index:  list[int]  = [0]
    dnu_reg:      list[Optional[UploadedDNU]] = [None]
    char_cat:     list       = [None]
    copy_cat:     list       = [None]
    base_path:    list[Optional[Path]] = [None]   # download root (for artist/site derivation)
    scan_path:    list[Optional[Path]] = [None]   # folder actually scanned for images

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
    tk.Label(scroll_frame, text="IMAGE UPLOADER", bg=PANEL, fg=FG,
             font=FONT_HEAD, anchor="w").pack(fill="x", padx=PAD_OUTER)
    tk.Label(scroll_frame, text="push images to your e621ng instance",
             bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w",
             ).pack(fill="x", padx=PAD_OUTER + 2, pady=(0, 10))
    divider(scroll_frame)

    # ── Server / auth ─────────────────────────────────────────────────────────
    section_label(scroll_frame, "BOORU SERVER")
    sf, srv_entry = styled_entry(scroll_frame, "http://localhost:3000", width=50)
    sf.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))

    section_label(scroll_frame, "AUTHENTICATION")
    auth_row = tk.Frame(scroll_frame, bg=PANEL)
    auth_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 6))
    uf, usr_entry = styled_entry(auth_row, "username", width=22)
    uf.pack(side="left", padx=(0, 8))
    pf, api_entry = styled_entry(auth_row, "api_key", width=28)
    pf.pack(side="left")

    # ── Scan folder ───────────────────────────────────────────────────────────
    section_label(scroll_frame, "SCAN FOLDER  (folder to scan for images to upload)")
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

    # ── Queue load row ────────────────────────────────────────────────────────
    load_row = tk.Frame(scroll_frame, bg=PANEL)
    load_row.pack(fill="x", padx=PAD_OUTER, pady=(0, 10))
    queue_lbl = tk.Label(load_row, text="No queue loaded.",
                         bg=PANEL, fg=FG_DIM, font=FONT_MONO)
    queue_lbl.pack(side="left")

    def _load_queue() -> None:
        # Load download_dir from config.yaml — always use it as base
        try:
            cfg  = load_config(DEFAULT_CONFIG)
            base = (SCRIPT_DIR / cfg.get("download_dir", "./download")).resolve()
        except (ConfigError, Exception) as e:
            print(f"[uploader] config error: {e}")
            base = (SCRIPT_DIR / "download").resolve()

        folder = Path(folder_entry.get().strip() or str(base))
        base.mkdir(parents=True, exist_ok=True)
        folder.mkdir(parents=True, exist_ok=True)

        base_path[0] = base
        scan_path[0] = folder
        print(f"[uploader] base={base}  scan={folder}")

        dnu_reg[0] = get_db() or UploadedDNU(base)
        char_cat[0] = load_character_tags(base)
        copy_cat[0] = load_copyright_tags(base)

        char_picker.set_known(char_cat[0].tags)
        copy_picker.set_known(copy_cat[0].tags)

        pending = scan_upload_folder(folder, dnu_reg[0])
        queue.clear()
        queue.extend(pending)
        queue_index[0] = 0

        if not pending:
            queue_lbl.config(text="Queue empty — nothing pending.", fg=FG_DIM)
            _clear_wizard()
        else:
            queue_lbl.config(text=f"{len(pending)} image(s) pending.", fg=COLOR_OK)
            _show_current()

    styled_button(load_row, "Load Queue", command=_load_queue,
                  bg="#2a2a38", hov="#3a3a50").pack(side="right")

    # ── Wizard: two-column layout ─────────────────────────────────────────────
    wizard = tk.Frame(scroll_frame, bg=PANEL)
    wizard.pack(fill="both", expand=True, padx=PAD_OUTER)
    wizard.columnconfigure(0, weight=0)
    wizard.columnconfigure(1, weight=1)

    # Progress
    progress_lbl = tk.Label(wizard, text="", bg=PANEL, fg=FG_DIM,
                             font=FONT_MONO, anchor="w")
    progress_lbl.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

    # Left: preview
    preview_outer = tk.Frame(wizard, bg=BORDER, padx=1, pady=1, width=322, height=322)
    preview_outer.grid(row=1, column=0, sticky="n", padx=(0, 20))
    preview_outer.pack_propagate(False)
    preview_inner = tk.Frame(preview_outer, bg=ENTRY_BG, width=320, height=320)
    preview_inner.pack(fill="both", expand=True)
    preview_inner.pack_propagate(False)
    preview_lbl = tk.Label(preview_inner, bg=ENTRY_BG, fg=FG_DIM,
                           text="No image loaded", font=FONT_MONO)
    preview_lbl.place(relx=0.5, rely=0.5, anchor="center")

    filename_lbl = tk.Label(wizard, text="", bg=PANEL, fg=FG_DIM,
                            font=FONT_TAGS, anchor="w", wraplength=320)
    filename_lbl.grid(row=2, column=0, sticky="nw", pady=(4, 0))

    # Right: form
    form = tk.Frame(wizard, bg=PANEL)
    form.grid(row=1, column=1, sticky="nsew")

    # Tags from sidecar — plain resizable text field, one tag per line or space-separated
    tk.Label(form, text="TAGS  (from .txt sidecar)", bg=PANEL, fg=ACCENT2,
             font=FONT_BOLD, anchor="w").pack(fill="x")
    tags_border = tk.Frame(form, bg=BORDER, padx=1, pady=1)
    tags_border.pack(fill="x", pady=(2, 8))
    tags_text = tk.Text(
        tags_border, bg=ENTRY_BG, fg=FG, insertbackground=ACCENT,
        relief="flat", font=FONT_MONO, height=4, wrap="word",
    )
    tags_text.pack(fill="both", expand=True, padx=4, pady=4)

    # Character + copyright pickers
    char_picker = TagPicker(form, "CHARACTER TAGS")
    char_picker.pack(fill="x", pady=(0, 6))

    copy_picker = TagPicker(form, "COPYRIGHT TAGS")
    copy_picker.pack(fill="x", pady=(0, 10))

    # Rating
    tk.Label(form, text="RATING", bg=PANEL, fg=ACCENT2,
             font=FONT_BOLD, anchor="w").pack(fill="x")
    rating_var = tk.StringVar(value="Safe")
    rating_row = tk.Frame(form, bg=PANEL)
    rating_row.pack(fill="x", pady=(2, 10))
    for lbl, _ in RATING_OPTIONS:
        tk.Radiobutton(
            rating_row, text=lbl, variable=rating_var, value=lbl,
            bg=PANEL, fg=FG, selectcolor="#2a2040",
            activebackground=PANEL, activeforeground=ACCENT,
            font=FONT_BODY,
        ).pack(side="left", padx=(0, 16))

    # Source (URL)
    tk.Label(form, text="SOURCE  (url)", bg=PANEL, fg=ACCENT2,
             font=FONT_BOLD, anchor="w").pack(fill="x")
    src_border = tk.Frame(form, bg=BORDER, padx=1, pady=1)
    src_border.pack(fill="x", pady=(2, 10))
    src_entry = tk.Entry(src_border, bg=ENTRY_BG, fg=FG,
                         insertbackground=ACCENT, relief="flat", font=FONT_BODY)
    src_entry.pack(fill="x", padx=4, pady=4)

    # Description (caption fetched from source)
    tk.Label(form, text="DESCRIPTION  (caption from source)", bg=PANEL, fg=ACCENT2,
             font=FONT_BOLD, anchor="w").pack(fill="x")
    desc_border = tk.Frame(form, bg=BORDER, padx=1, pady=1)
    desc_border.pack(fill="x", pady=(2, 10))
    desc_text = tk.Text(desc_border, bg=ENTRY_BG, fg=FG, relief="flat",
                        font=FONT_MONO, height=3, wrap="word",
                        insertbackground=ACCENT)
    desc_text.pack(fill="x", padx=4, pady=4)

    # Status line
    status_lbl = tk.Label(form, text="", bg=PANEL, fg=FG_DIM,
                          font=FONT_MONO, anchor="w", wraplength=450)
    status_lbl.pack(fill="x", pady=(0, 4))

    # ── Action buttons ────────────────────────────────────────────────────────
    btn_row = tk.Frame(scroll_frame, bg=PANEL)
    btn_row.pack(fill="x", padx=PAD_OUTER, pady=(12, 24))

    upload_btn = styled_button(btn_row, "  Upload")
    upload_btn.pack(side="left", padx=(0, 10))

    skip_btn = styled_button(btn_row, "  Skip",
                             bg="#2a2a38", hov="#3a3a50")
    skip_btn.pack(side="left", padx=(0, 10))

    dnu_btn = styled_button(btn_row, "x  Do Not Upload",
                            bg="#3a2a2a", hov="#5a3a3a")
    dnu_btn.pack(side="left")

    # ── Wizard logic ──────────────────────────────────────────────────────────

    def _expected_tag_path(img_path: Path, base: Path) -> str:
        """Return the expected tag file path as a readable string."""
        try:
            parts  = img_path.relative_to(base).parts
            artist = parts[0]
            site   = parts[1]
            return str(base / artist / ".tags" / site / img_path.with_suffix(".txt").name)
        except (ValueError, IndexError):
            return "(unknown)"

    _photo_ref: list = [None]

    def _clear_wizard() -> None:
        preview_lbl.config(image="", text="No image loaded")
        _photo_ref[0] = None
        filename_lbl.config(text="")
        progress_lbl.config(text="")
        tags_text.delete("1.0", "end")
        char_picker.clear()
        copy_picker.clear()
        src_entry.delete(0, "end")
        desc_text.delete("1.0", "end")
        rating_var.set("Safe")
        status_lbl.config(text="", fg=FG_DIM)
        for btn in (upload_btn, skip_btn, dnu_btn):
            btn.config(state="disabled")

    def _show_current() -> None:
        idx   = queue_index[0]
        total = len(queue)

        if not queue or idx >= total:
            _clear_wizard()
            queue_lbl.config(text="All done! Queue exhausted.", fg=COLOR_OK)
            return

        img_path = queue[idx]
        progress_lbl.config(text=f"Image {idx + 1} of {total}")
        filename_lbl.config(text=str(img_path))
        status_lbl.config(text="", fg=FG_DIM)

        _load_preview(img_path)

        # Sidecar tags
        print(f"[uploader_tab] _show_current: img={img_path} base={base_path[0]}")
        sidecar_tags = read_tag_file(img_path, base_path[0])
        tags_text.delete("1.0", "end")
        tags_text.insert("end", " ".join(sidecar_tags))
        tag_file = find_tag_file(img_path, base_path[0])
        if tag_file is None:
            expected = expected_tag_path(img_path, base_path[0])
            status_lbl.config(
                text=f"No tag file found — expected: {expected}",
                fg=COLOR_FAIL,
            )
        elif len(sidecar_tags) <= 1:
            status_lbl.config(text="Tag file found but is empty.", fg=COLOR_RUNNING)

        # Source URL
        src = derive_source_url(img_path, base_path[0])
        src_entry.delete(0, "end")
        src_entry.insert(0, src)

        # Description — check sidecar first, then fetch from URL; always in background
        # Prefer gallery-dl sidecar metadata if present (fast, no network)
        desc_text.config(state="normal")
        desc_text.delete("1.0", "end")
        try:
            from core.upload_data import _description_from_sidecar
            side_desc = _description_from_sidecar(img_path)
        except Exception:
            side_desc = None

        if side_desc:
            desc_text.insert("end", side_desc)
            desc_text.config(state="disabled")
        else:
            desc_text.insert("end", "Fetching description...")
            desc_text.config(state="disabled")

            def _fetch_desc(url: str, path: Path) -> None:
                desc = fetch_description(url, image_path=path)
                parent.after(0, lambda d=desc: _set_desc(d))

            def _set_desc(desc: str) -> None:
                desc_text.config(state="normal")
                desc_text.delete("1.0", "end")
                desc_text.insert("end", desc)

            threading.Thread(target=_fetch_desc, args=(src, img_path), daemon=True).start()

        char_picker.clear()
        copy_picker.clear()
        rating_var.set("Safe")

        for btn in (upload_btn, skip_btn, dnu_btn):
            btn.config(state="normal")

    def _load_preview(img_path: Path) -> None:
        try:
            from PIL import Image, ImageTk
            img = Image.open(img_path)
            img.thumbnail((318, 318))
            photo = ImageTk.PhotoImage(img)
            _photo_ref[0] = photo
            preview_lbl.config(image=photo, text="")
        except ImportError:
            preview_lbl.config(image="", text="Install Pillow\nfor previews")
            _photo_ref[0] = None
        except Exception as exc:
            preview_lbl.config(image="", text=f"Preview error:\n{exc}")
            _photo_ref[0] = None

    def _advance() -> None:
        queue_index[0] += 1
        _show_current()

    def _collect_tags() -> list[str]:
        sidecar = tags_text.get("1.0", "end").split()
        all_tags = sidecar + char_picker.get_selected() + copy_picker.get_selected()
        return list(dict.fromkeys(t.strip().strip(",") for t in all_tags if t.strip().strip(",")))

    # ── Button callbacks ──────────────────────────────────────────────────────

    def _on_skip() -> None:
        _advance()

    def _on_dnu() -> None:
        if not queue or dnu_reg[0] is None or base_path[0] is None:
            return
        img_path = queue[queue_index[0]]
        dnu_reg[0].mark_dnu(base_path[0], img_path)
        dnu_reg[0].save()
        status_lbl.config(text="Marked as Do Not Upload.", fg=FG_DIM)
        _advance()

    def _on_upload() -> None:
        if not queue or dnu_reg[0] is None or base_path[0] is None:
            return

        server   = srv_entry.get().strip()
        username = usr_entry.get().strip()
        api_key  = api_entry.get().strip()

        if not server:
            status_lbl.config(text="Server URL is required.", fg=COLOR_FAIL)
            return
        if not username or not api_key:
            status_lbl.config(text="Username and API key are required.", fg=COLOR_FAIL)
            return

        img_path    = queue[queue_index[0]]
        tags        = _collect_tags()
        rating_code = rating_label_to_code(rating_var.get())
        source      = src_entry.get().strip()
        description = desc_text.get("1.0", "end").strip()

        # Persist any newly added tags to the catalogues
        if char_cat[0]:
            for t in char_picker.get_selected():
                char_cat[0].add(t)
            char_cat[0].save()
        if copy_cat[0]:
            for t in copy_picker.get_selected():
                copy_cat[0].add(t)
            copy_cat[0].save()

        status_lbl.config(text="Uploading...", fg=COLOR_RUNNING)
        for btn in (upload_btn, skip_btn, dnu_btn):
            btn.config(state="disabled")

        def _do() -> None:
            result = upload_image(
                server=server,
                username=username,
                api_key=api_key,
                image_path=img_path,
                tags=tags,
                rating=rating_code,
                source=source,
                description=description,
            )
            parent.after(0, lambda r=result: _upload_done(r, img_path))

        threading.Thread(target=_do, daemon=True).start()

    def _upload_done(result, img_path: Path) -> None:
        if result.success:
            dnu_reg[0].mark_uploaded(base_path[0], img_path)
            dnu_reg[0].save()
            status_lbl.config(text=result.message, fg=COLOR_OK)
            parent.after(800, _advance)
        else:
            status_lbl.config(text=f"Failed: {result.message}", fg=COLOR_FAIL)
            for btn in (upload_btn, skip_btn, dnu_btn):
                btn.config(state="normal")

    upload_btn.config(command=_on_upload)
    skip_btn.config(  command=_on_skip)
    dnu_btn.config(   command=_on_dnu)

    _clear_wizard()