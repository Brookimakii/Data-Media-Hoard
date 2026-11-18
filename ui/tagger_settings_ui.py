"""
ui/tagger_settings_ui.py
------------------------
Body of the Tagger's ⚙ settings dialog: which AI model suggests tags.

    ( ) Z3D-E621-ConvNeXt   e621 tags       ✓ installed
    (•) WD SwinV2 Tagger v3 Danbooru tags   not downloaded · 467 MB   [Download]
    ( ) Custom model        your own ONNX file (folder, file names, preprocessing)

Picking a model saves at once; the Tagger switches to it within a second. Scores
are cached per model, so switching back never repeats work.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from core import ai_tagger, tagger_settings as ts
from ui.booru_settings_ui import _button, _field
from ui.theme import (
    BORDER, COLOR_FAIL, COLOR_OK, ENTRY_BG, FG, FG_DIM, FONT_BTN, FONT_SUB, PANEL,
)

_UI = FONT_BTN[0]


def _radio(parent: tk.Misc, text: str, var: tk.StringVar, value: str, command) -> tk.Radiobutton:
    return tk.Radiobutton(parent, text=text, variable=var, value=value, command=command,
                          bg=PANEL, fg=FG, selectcolor=ENTRY_BG, activebackground=PANEL,
                          activeforeground=FG, font=(_UI, 11, "bold"), highlightthickness=0,
                          bd=0, anchor="w")


def build_tagger_settings(body: tk.Frame) -> None:
    cur = ts.load()
    v_model = tk.StringVar(value=ts.active_spec(cur).key if cur.model != ts.CUSTOM_KEY
                           else ts.CUSTOM_KEY)
    status = tk.Label(body, text="", bg=PANEL, fg=COLOR_OK, font=FONT_SUB, anchor="w",
                      justify="left", wraplength=480)
    downloads: dict[str, dict] = {}                 # key -> {"label", "button", "queue"}

    def say(text: str, ok: bool = True) -> None:
        status.config(text=text, fg=COLOR_OK if ok else COLOR_FAIL)

    def choose() -> None:
        s = ts.load()
        s.model = v_model.get()
        if s.model == ts.CUSTOM_KEY and not s.custom.usable:
            say("Fill in the custom model below and press Save custom model.", ok=False)
            return
        ts.save(s)
        say(f"Using {ts.active_spec(s).title}. The Tagger switches to it automatically.")

    tk.Label(body, text="AI model for tag suggestions", bg=PANEL, fg=FG_DIM, font=(_UI, 9),
             anchor="w").pack(fill="x")

    for spec in ai_tagger.MODELS.values():
        block = tk.Frame(body, bg=PANEL)
        block.pack(fill="x", pady=(8, 0))
        row = tk.Frame(block, bg=PANEL)
        row.pack(fill="x")
        _radio(row, f"{spec.title}   ·   {spec.vocabulary}", v_model, spec.key, choose
               ).pack(side="left")
        state = tk.Label(row, text="", bg=PANEL, fg=FG_DIM, font=FONT_SUB)
        state.pack(side="right")
        btn = _button(row, "Download", lambda s=spec: start_download(s))
        downloads[spec.key] = {"label": state, "button": btn, "queue": None}
        tk.Label(block, text=spec.note, bg=PANEL, fg=FG_DIM, font=FONT_SUB, anchor="w",
                 justify="left", wraplength=480).pack(fill="x", padx=(22, 0))

    # ── Custom model ──────────────────────────────────────────────────────────
    tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=12)
    _radio(body, "Custom model   ·   your own ONNX tagger", v_model, ts.CUSTOM_KEY, choose
           ).pack(fill="x")
    c = cur.custom
    v_folder, v_model_file = tk.StringVar(value=c.folder), tk.StringVar(value=c.model_file)
    v_tags_file, v_name = tk.StringVar(value=c.tags_file), tk.StringVar(value=c.name_col)
    v_size, v_bgr = tk.StringVar(value=str(c.input_size)), tk.BooleanVar(value=c.bgr)
    v_unit = tk.BooleanVar(value=c.unit_scale)

    frow = tk.Frame(body, bg=PANEL)
    frow.pack(fill="x", pady=(6, 0))
    tk.Label(frow, text="Folder holding the model and its tag list", bg=PANEL, fg=FG_DIM,
             font=(_UI, 9), anchor="w").pack(fill="x")
    line = tk.Frame(frow, bg=PANEL)
    line.pack(fill="x", pady=(0, 6))
    tk.Entry(line, textvariable=v_folder, bg=ENTRY_BG, fg=FG, insertbackground=FG,
             relief="flat", highlightthickness=1, highlightbackground=BORDER,
             font=(_UI, 10)).pack(side="left", fill="x", expand=True, ipady=4)
    _button(line, "Browse…", lambda: v_folder.set(
        filedialog.askdirectory(initialdir=v_folder.get() or str(Path.home())) or v_folder.get())
    ).pack(side="left", padx=(6, 0))
    pair = tk.Frame(body, bg=PANEL)
    pair.pack(fill="x")
    for caption, var in (("Model file (.onnx)", v_model_file), ("Tag list (.csv)", v_tags_file)):
        col = tk.Frame(pair, bg=PANEL)
        col.pack(side="left", fill="x", expand=True, padx=(0, 8))
        _field(col, caption, var)
    pair2 = tk.Frame(body, bg=PANEL)
    pair2.pack(fill="x")
    for caption, var in (("Tag column in the CSV", v_name), ("Input size (px)", v_size)):
        col = tk.Frame(pair2, bg=PANEL)
        col.pack(side="left", fill="x", expand=True, padx=(0, 8))
        _field(col, caption, var)
    for text, var in (("Colour order is BGR (untick for RGB)", v_bgr),
                      ("Pixel values are 0-1 (untick for 0-255)", v_unit)):
        tk.Checkbutton(body, text=text, variable=var, bg=PANEL, fg=FG, selectcolor=ENTRY_BG,
                       activebackground=PANEL, activeforeground=FG, font=(_UI, 10),
                       highlightthickness=0, bd=0, anchor="w").pack(fill="x")

    def save_custom() -> None:
        try:
            size = int(v_size.get().strip())
        except ValueError:
            say("Input size must be a number, e.g. 448.", ok=False)
            return
        s = ts.load()
        s.custom = ts.CustomModel(v_folder.get().strip(), v_model_file.get().strip(),
                                  v_tags_file.get().strip(), v_name.get().strip() or "name",
                                  max(16, min(2048, size)), v_bgr.get(), v_unit.get())
        if not s.custom.usable:
            say("Folder, model file and tag list are required.", ok=False)
            return
        s.model = ts.CUSTOM_KEY
        v_model.set(ts.CUSTOM_KEY)
        ts.save(s)
        spec = ts.custom_spec(s.custom)
        missing = [f for f in spec.files if not (ai_tagger.model_dir(spec) / f).is_file()]
        if missing:
            say("Saved, but not found in that folder: " + ", ".join(missing), ok=False)
        else:
            say("Custom model saved and selected.")

    _button(body, "Save custom model", save_custom, primary=True).pack(anchor="w", pady=(8, 0))
    status.pack(fill="x", pady=(10, 0))

    # ── Download ──────────────────────────────────────────────────────────────
    def refresh_states() -> None:
        for spec in ai_tagger.MODELS.values():
            d = downloads[spec.key]
            if d["queue"] is not None:
                continue
            if ai_tagger.installed(spec):
                d["label"].config(text="✓ installed", fg=COLOR_OK)
                d["button"].pack_forget()
            else:
                d["label"].config(text=f"not downloaded · {spec.size_mb} MB", fg=FG_DIM)
                if not d["button"].winfo_manager():
                    d["button"].pack(side="right", padx=(0, 8))

    def start_download(spec: ai_tagger.ModelSpec) -> None:
        d = downloads[spec.key]
        if d["queue"] is not None:
            return
        q: queue.Queue = queue.Queue()
        d["queue"] = q
        d["button"].pack_forget()
        d["label"].config(text="starting…", fg=FG_DIM)

        def work() -> None:
            try:
                ai_tagger.download(spec, progress=lambda a, b, n: q.put(("p", a, b, n)))
                q.put(("ok",))
            except ai_tagger.ModelError as e:
                q.put(("err", str(e)))
            except Exception as e:
                q.put(("err", f"Unexpected error: {e}"))
        threading.Thread(target=work, name="model-download", daemon=True).start()

    def poll() -> None:
        if not body.winfo_exists():
            return
        for spec in ai_tagger.MODELS.values():
            d = downloads[spec.key]
            q = d["queue"]
            while q is not None:
                try:
                    m = q.get_nowait()
                except queue.Empty:
                    break
                if m[0] == "p":
                    tot = f" / {m[2] / 1e6:.0f}" if m[2] else ""
                    d["label"].config(text=f"{m[3]}: {m[1] / 1e6:.0f}{tot} MB", fg=FG_DIM)
                else:
                    d["queue"] = q = None
                    if m[0] == "ok":
                        say(f"{spec.title} downloaded.")
                    else:
                        say(m[1], ok=False)
                    refresh_states()
        body.after(250, poll)

    refresh_states()
    body.after(250, poll)
