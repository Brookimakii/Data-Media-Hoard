"""
ui/booru_settings_ui.py
-----------------------
Body of the ⚙ settings dialog for modules that push to the booru server
(see SubFeature.settings in ui/features.py): server URL, login, auto-push,
and a Test connection button.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk

from core.booru_client import BooruClient, BooruError
from core.booru_settings import BooruSettings, load_settings, save_settings, settings_path
from ui.theme import (
    ACCENT, BORDER, BTN_BG, BTN_FG, BTN_HOV, COLOR_FAIL, COLOR_OK, ENTRY_BG, FG, FG_DIM,
    FONT_BTN, FONT_SUB, PANEL,
)

_UI = FONT_BTN[0]


def _field(parent: tk.Misc, caption: str, var: tk.StringVar, show: str = "") -> None:
    tk.Label(parent, text=caption, bg=PANEL, fg=FG_DIM, font=(_UI, 9), anchor="w").pack(fill="x")
    tk.Entry(parent, textvariable=var, show=show, bg=ENTRY_BG, fg=FG, insertbackground=FG,
             relief="flat", highlightthickness=1, highlightbackground=BORDER,
             highlightcolor=ACCENT, font=(_UI, 10), width=44
             ).pack(fill="x", pady=(0, 10), ipady=4)


def _button(parent: tk.Misc, text: str, command, primary: bool = False) -> tk.Label:
    bg, hover = (BTN_BG, BTN_HOV) if primary else (ENTRY_BG, BORDER)
    b = tk.Label(parent, text=text, bg=bg, fg=BTN_FG if primary else FG, font=FONT_BTN,
                 padx=14, pady=6, cursor="hand2")
    b.bind("<Button-1>", lambda _e: command())
    b.bind("<Enter>", lambda _e: b.config(bg=hover))
    b.bind("<Leave>", lambda _e: b.config(bg=bg))
    return b


def build_booru_settings(body: tk.Frame) -> None:
    cur = load_settings()
    v_server = tk.StringVar(value=cur.server)
    v_user = tk.StringVar(value=cur.username)
    v_key = tk.StringVar(value=cur.api_key)
    v_auto = tk.BooleanVar(value=cur.auto_push)

    _field(body, "Booru server URL", v_server)
    _field(body, "Username", v_user)
    _field(body, "API key", v_key, show="•")
    tk.Checkbutton(body, text="Push changes automatically", variable=v_auto, bg=PANEL, fg=FG,
                   selectcolor=ENTRY_BG, activebackground=PANEL, activeforeground=FG,
                   font=(_UI, 10), highlightthickness=0, bd=0, anchor="w"
                   ).pack(fill="x", pady=(0, 12))

    status = tk.Label(body, text="", bg=PANEL, fg=COLOR_OK, font=FONT_SUB, anchor="w",
                      justify="left", wraplength=420)

    def current() -> BooruSettings:
        return BooruSettings(v_server.get().strip().rstrip("/"), v_user.get().strip(),
                             v_key.get().strip(), bool(v_auto.get()))

    def say(text: str, ok: bool = True) -> None:
        status.config(text=text, fg=COLOR_OK if ok else COLOR_FAIL)

    def save() -> None:
        s = current()
        if not s.server:
            say("Enter the server URL.", ok=False)
            return
        try:
            save_settings(s)
        except OSError as e:
            say(f"Could not save: {e}", ok=False)
            return
        say("Saved. Pending changes are pushed within a few seconds." if s.configured and s.auto_push
            else "Saved.")

    results: queue.Queue[tuple[bool, str]] = queue.Queue()

    def test() -> None:
        s = current()
        if not s.configured:
            say("Fill in the server, username and API key first.", ok=False)
            return
        say("Testing…", ok=True)

        def work() -> None:                       # network call off the UI thread
            try:
                me = BooruClient(s, timeout=8).whoami()
                results.put((True, f"Connected as {me.get('name', s.username)} "
                                   f"({me.get('level_string', 'unknown level')})."))
            except BooruError as e:
                results.put((False, str(e)))
            except Exception as e:                # noqa: BLE001 - show anything unexpected
                results.put((False, f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()
        poll()

    def poll() -> None:
        try:
            ok, text = results.get_nowait()
        except queue.Empty:
            if body.winfo_exists():
                body.after(150, poll)
            return
        if body.winfo_exists():
            say(text, ok)

    row = tk.Frame(body, bg=PANEL)
    row.pack(fill="x")
    _button(row, "Save", save, primary=True).pack(side="left")
    _button(row, "Test connection", test).pack(side="left", padx=(8, 0))
    status.pack(fill="x", pady=(12, 0))
    tk.Label(body, text=f"Stored in {settings_path().name} (not committed to git).",
             bg=PANEL, fg=FG_DIM, font=(_UI, 8), anchor="w").pack(fill="x", pady=(10, 0))
