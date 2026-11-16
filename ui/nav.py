"""
ui/nav.py
---------
Navigation shell: header with burger button + breadcrumb, a slide-in drawer
listing every feature, a home screen, one landing screen per macro feature,
and one page per sub feature.

No page scrolls: landing screens are laid out with grid weights so they fill
the window at any size.

Routes (tuples):
    ("home",)
    ("macro", macro_key)
    ("sub", macro_key, sub_key)
"""

from __future__ import annotations

import logging

import tkinter as tk
from typing import Callable, Iterator

from ui.features import FEATURES, find_macro, find_sub
from ui import back_stack
from ui.mouse_nav import install_mouse_nav
from ui.nav_history import NavHistory
from ui.theme import (
    ACCENT, ACCENT2, BG, BORDER, BTN_BG, BTN_FG, BTN_HOV, ENTRY_BG, FG, FG_DIM,
    PANEL, SEL_BG,
    FONT_BTN, FONT_EMOJI, FONT_HEAD, FONT_SUB, FONT_TAGS,
)

_UI = FONT_BTN[0]
_EMOJI = FONT_EMOJI[0]
DRAWER_W = 300
HOME = ("home",)


def _walk(w: tk.Misc) -> Iterator[tk.Misc]:
    yield w
    for c in w.winfo_children():
        yield from _walk(c)


def _within(w: tk.Misc | None, ancestor: tk.Misc) -> bool:
    while w is not None:
        if w is ancestor:
            return True
        w = getattr(w, "master", None)
    return False


# ── Card ───────────────────────────────────────────────────────────────────────
class Card(tk.Frame):
    """Big clickable tile: icon, title, description, optional badge."""

    def __init__(self, parent: tk.Misc, icon: str, title: str, desc: str,
                 command: Callable[[], None] | None = None, badge: str = "",
                 compact: bool = False) -> None:
        """compact=True lays icon and text side by side, so the card stays short
        enough for screens with several rows (nothing gets clipped)."""
        super().__init__(parent, bg=ENTRY_BG, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BORDER)
        enabled = command is not None
        inner = tk.Frame(self, bg=ENTRY_BG)
        inner.pack(expand=True, padx=18, pady=10 if compact else 24)

        icon_font = (_EMOJI, 22 if compact else 30)
        icon_lbl = tk.Label(inner, text=icon, bg=ENTRY_BG,
                            fg=ACCENT if enabled else FG_DIM, font=icon_font)
        if compact:
            icon_lbl.pack(side="left", padx=(0, 14))
            text = tk.Frame(inner, bg=ENTRY_BG)
            text.pack(side="left")
            align, justify, title_pad = "w", "left", (0, 2)
        else:
            icon_lbl.pack()
            text = inner
            align, justify, title_pad = "center", "center", (10, 2)

        tk.Label(text, text=title, bg=ENTRY_BG, fg=FG if enabled else FG_DIM,
                 font=(_UI, 14, "bold")).pack(anchor=align, pady=title_pad)
        desc_lbl = tk.Label(text, text=desc, bg=ENTRY_BG, fg=FG_DIM, font=FONT_SUB,
                            wraplength=240, justify=justify)
        desc_lbl.pack(anchor=align)
        if badge:
            tk.Label(text, text=badge, bg=ENTRY_BG, fg=ACCENT2,
                     font=FONT_TAGS).pack(anchor=align, pady=(6, 0))

        # Wrap the description to the card's real width instead of a fixed 240px
        reserved = 130 if compact else 50
        self.bind("<Configure>",
                  lambda e: desc_lbl.config(wraplength=max(120, e.width - reserved)),
                  add="+")

        if enabled:
            self.configure(cursor="hand2")
            for w in _walk(self):
                w.bind("<Enter>", lambda _e: self._hover(True), add="+")
                w.bind("<Leave>", self._on_leave, add="+")
                w.bind("<Button-1>", lambda _e: command(), add="+")

    def _on_leave(self, _e: tk.Event) -> None:
        # Moving between child labels fires Leave/Enter; only un-hover if the
        # pointer really left the card.
        x, y = self.winfo_pointerxy()
        if not _within(self.winfo_containing(x, y), self):
            self._hover(False)

    def _hover(self, on: bool) -> None:
        bg = SEL_BG if on else ENTRY_BG
        for w in _walk(self):
            w.configure(bg=bg)
        self.configure(highlightbackground=ACCENT if on else BORDER)


# ── Shell ──────────────────────────────────────────────────────────────────────
class Shell:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.route: tuple = HOME
        self._pages: dict[tuple, tk.Frame] = {}
        self._current: tk.Frame | None = None
        self._drawer: tk.Frame | None = None
        self._drawer_open = False
        self._anim: str | None = None
        self._history = NavHistory()

        self._build_header()
        self.content = tk.Frame(root, bg=PANEL)
        self.content.pack(fill="both", expand=True)

        root.bind("<Escape>", lambda _e: self.close_drawer())
        root.bind_all("<Button-1>", self._outside_click, add="+")
        install_mouse_nav(root, self.go_back, self.go_forward)
        self.navigate(HOME)

    # ── header ────────────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x")

        self._burger = tk.Label(bar, text="☰", bg=BG, fg=FG, cursor="hand2",
                                font=(_UI, 20), padx=18, pady=8)
        self._burger.pack(side="left")
        self._burger.bind("<Button-1>", lambda _e: self.toggle_drawer())
        self._burger.bind("<Enter>", lambda _e: self._burger.config(fg=ACCENT))
        self._burger.bind("<Leave>", lambda _e: self._burger.config(fg=FG))

        self._crumbs = tk.Frame(bar, bg=BG)
        self._crumbs.pack(side="left", padx=(4, 0))

        self._brand = tk.Label(bar, text="✦ BOORU MANAGER", bg=BG, fg=ACCENT,
                               font=(_UI, 11, "bold"))
        self._brand.pack(side="right", padx=(0, 20))

        # ⚙ settings button: far right, shown only when the current module has
        # settings (see _update_settings_button). Packed before the brand label
        # so it sits at the right edge.
        self._gear = tk.Label(bar, text="⚙", bg=BG, fg=FG, cursor="hand2",
                              font=(_UI, 18), padx=14, pady=8)
        self._gear.bind("<Button-1>", lambda _e: self.open_settings())
        self._gear.bind("<Enter>", lambda _e: self._gear.config(fg=ACCENT))
        self._gear.bind("<Leave>", lambda _e: self._gear.config(fg=FG))
        self._settings_win: tk.Toplevel | None = None

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

    # ── settings ──────────────────────────────────────────────────────────────
    def _current_settings(self):
        """Settings builder of the module on screen, or None."""
        if self.route[0] != "sub":
            return None
        return find_sub(self.route[1], self.route[2]).settings

    def _update_settings_button(self) -> None:
        if self._current_settings() is not None:
            if not self._gear.winfo_manager():
                self._gear.pack(side="right", before=self._brand)
        else:
            self._gear.pack_forget()

    def open_settings(self) -> None:
        build = self._current_settings()
        if build is None:
            return
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            self._settings_win.focus_force()
            return

        sub = find_sub(self.route[1], self.route[2])
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title(f"{sub.title} — Settings")
        win.configure(bg=PANEL)
        win.transient(self.root)
        win.minsize(420, 200)

        tk.Label(win, text=f"{sub.icon}  {sub.title} settings", bg=PANEL, fg=FG,
                 font=(_UI, 14, "bold"), anchor="w").pack(fill="x", padx=24, pady=(20, 10))
        tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

        body = tk.Frame(win, bg=PANEL)
        body.pack(fill="both", expand=True, padx=24, pady=18)
        try:
            build(body)
        except Exception as exc:        # a broken settings page must not crash the app
            for w in body.winfo_children():
                w.destroy()
            tk.Label(body, text=f"Could not load settings: {exc}", bg=PANEL,
                     fg=FG_DIM, font=FONT_SUB, wraplength=380,
                     justify="left").pack(anchor="w")

        tk.Frame(win, bg=BORDER, height=1).pack(fill="x")
        foot = tk.Frame(win, bg=PANEL)
        foot.pack(fill="x", padx=24, pady=12)
        close = tk.Label(foot, text="Close", bg=BTN_BG, fg=BTN_FG, font=FONT_BTN,
                         padx=18, pady=6, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: win.destroy())
        close.bind("<Enter>", lambda _e: close.config(bg=BTN_HOV))
        close.bind("<Leave>", lambda _e: close.config(bg=BTN_BG))
        win.bind("<Escape>", lambda _e: win.destroy())

        # Centre over the main window, then take focus
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.focus_force()

    def _crumb_trail(self) -> list[tuple[str, tuple]]:
        trail = [("Home", HOME)]
        if self.route[0] in ("macro", "sub"):
            m = find_macro(self.route[1])
            trail.append((m.title, ("macro", m.key)))
        if self.route[0] == "sub":
            s = find_sub(self.route[1], self.route[2])
            trail.append((s.title, self.route))
        return trail

    def _render_crumbs(self) -> None:
        for w in self._crumbs.winfo_children():
            w.destroy()
        trail = self._crumb_trail()
        for i, (text, route) in enumerate(trail):
            last = i == len(trail) - 1
            lbl = tk.Label(self._crumbs, text=text, bg=BG,
                           fg=FG if last else FG_DIM,
                           font=(_UI, 11, "bold" if last else "normal"))
            lbl.pack(side="left")
            if not last:
                lbl.config(cursor="hand2")
                lbl.bind("<Button-1>", lambda _e, r=route: self.navigate(r))
                lbl.bind("<Enter>", lambda _e, l=lbl: l.config(fg=ACCENT))
                lbl.bind("<Leave>", lambda _e, l=lbl: l.config(fg=FG_DIM))
                tk.Label(self._crumbs, text="›", bg=BG, fg=FG_DIM,
                         font=(_UI, 11), padx=8).pack(side="left")

    # ── navigation ────────────────────────────────────────────────────────────
    def go_back(self) -> None:
        if back_stack.try_back():          # a page closed one of its own inner screens
            return
        route = self._history.back(self.route)
        logging.getLogger("ui.mouse_nav").debug("go_back from %s -> %s", self.route, route)
        if route is not None:
            self.navigate(route, _record=False)

    def go_forward(self) -> None:
        route = self._history.forward(self.route)
        logging.getLogger("ui.mouse_nav").debug("go_forward from %s -> %s", self.route, route)
        if route is not None:
            self.navigate(route, _record=False)

    def navigate(self, route: tuple, _record: bool = True) -> None:
        self.close_drawer()
        if route == self.route and self._current is not None:
            return
        if route not in self._pages:
            self._pages[route] = self._build_page(route)
        if self._current is not None:
            if _record:
                self._history.visit(self.route)   # new page: forward list is cleared
            self._current.pack_forget()
        self._current = self._pages[route]
        self._current.pack(fill="both", expand=True)
        self.route = route
        self._render_crumbs()
        self._update_settings_button()
        # Settings belong to the module they were opened from
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.destroy()

    def _build_page(self, route: tuple) -> tk.Frame:
        page = tk.Frame(self.content, bg=PANEL)
        if route == HOME:
            self._build_home(page)
        elif route[0] == "macro":
            self._build_macro(page, route[1])
        else:
            self._build_sub(page, route[1], route[2])
        return page

    @staticmethod
    def _heading(parent: tk.Frame, title: str, subtitle: str) -> None:
        tk.Label(parent, text=title, bg=PANEL, fg=FG, font=FONT_HEAD,
                 anchor="w").pack(fill="x", padx=40, pady=(24, 2))
        tk.Label(parent, text=subtitle, bg=PANEL, fg=FG_DIM, font=FONT_SUB,
                 anchor="w").pack(fill="x", padx=40, pady=(0, 12))

    @staticmethod
    def _grid(parent: tk.Frame, cols: int) -> tk.Frame:
        g = tk.Frame(parent, bg=PANEL)
        g.pack(fill="both", expand=True, padx=34, pady=(0, 24))
        for c in range(cols):
            g.columnconfigure(c, weight=1, uniform="col")
        return g

    def _build_home(self, page: tk.Frame) -> None:
        self._heading(page, "What do you want to do?", "Pick an area to get started.")
        g = self._grid(page, len(FEATURES))
        g.rowconfigure(0, weight=1)
        for i, m in enumerate(FEATURES):
            n = sum(s.available for s in m.subs)
            Card(g, m.icon, m.title, m.desc,
                 command=lambda k=m.key: self.navigate(("macro", k)),
                 badge=f"{n} tool{'s' if n != 1 else ''}",
                 ).grid(row=0, column=i, sticky="nsew", padx=8, pady=8)

    def _build_macro(self, page: tk.Frame, macro_key: str) -> None:
        m = find_macro(macro_key)
        self._heading(page, f"{m.icon}  {m.title}", m.desc)
        cols = 2
        g = self._grid(page, cols)
        rows = (len(m.subs) + cols - 1) // cols
        for r in range(rows):
            g.rowconfigure(r, weight=1, uniform="row")
        for i, s in enumerate(m.subs):
            Card(g, s.icon, s.title, s.desc,
                 command=(lambda k=s.key: self.navigate(("sub", macro_key, k)))
                 if s.available else None,
                 badge="" if s.available else "TBD", compact=True,
                 ).grid(row=i // cols, column=i % cols, sticky="nsew", padx=8, pady=8)

    def _build_sub(self, page: tk.Frame, macro_key: str, sub_key: str) -> None:
        s = find_sub(macro_key, sub_key)
        if s.builder is not None:
            try:
                s.builder(page)
            except Exception as exc:     # never leave a blank page: show what failed
                import logging
                import traceback
                logging.getLogger(__name__).exception("Could not build %s", s.title)
                for w in page.winfo_children():
                    w.destroy()
                box = tk.Frame(page, bg=PANEL)
                box.pack(expand=True, padx=40)
                tk.Label(box, text=f"{s.title} could not load", bg=PANEL, fg=FG,
                         font=(_UI, 16, "bold")).pack()
                tk.Label(box, text=f"{type(exc).__name__}: {exc}", bg=PANEL, fg=FG_DIM,
                         font=FONT_SUB, wraplength=560, justify="center").pack(pady=(6, 0))
                tk.Label(box, text=traceback.format_exc().strip().splitlines()[-3], bg=PANEL,
                         fg=FG_DIM, font=FONT_SUB, wraplength=560).pack(pady=(10, 0))
            return
        box = tk.Frame(page, bg=PANEL)
        box.pack(expand=True)
        tk.Label(box, text=s.icon, bg=PANEL, fg=ACCENT, font=(_EMOJI, 44)).pack()
        tk.Label(box, text=s.title, bg=PANEL, fg=FG,
                 font=(_UI, 18, "bold")).pack(pady=(10, 2))
        tk.Label(box, text="Not plugged in yet.", bg=PANEL, fg=FG_DIM,
                 font=FONT_SUB).pack()

    # ── drawer ────────────────────────────────────────────────────────────────
    def toggle_drawer(self) -> None:
        self.close_drawer() if self._drawer_open else self.open_drawer()

    def open_drawer(self) -> None:
        if self._drawer_open:
            return
        if self._drawer is None:
            self._drawer = tk.Frame(self.root, bg=PANEL, highlightthickness=1,
                                    highlightbackground=BORDER)
        self._fill_drawer()
        self._drawer.place(x=-DRAWER_W, y=0, width=DRAWER_W, relheight=1)
        self._drawer.lift()
        self._drawer_open = True
        self._slide(-DRAWER_W, 0)

    def close_drawer(self) -> None:
        if not self._drawer_open:
            return
        self._drawer_open = False
        x = self._drawer.winfo_x()
        self._slide(x, -DRAWER_W, then=self._drawer.place_forget)

    def _slide(self, x: float, target: int, then: Callable[[], None] | None = None) -> None:
        if self._anim:
            self.root.after_cancel(self._anim)
            self._anim = None
        x += (target - x) * 0.45
        if abs(target - x) < 2:
            self._drawer.place_configure(x=target)
            if then:
                then()
            return
        self._drawer.place_configure(x=round(x))
        self._anim = self.root.after(12, lambda: self._slide(x, target, then))

    def _outside_click(self, e: tk.Event) -> None:
        if (self._drawer_open and not _within(e.widget, self._drawer)
                and e.widget is not self._burger):
            self.close_drawer()

    def _fill_drawer(self) -> None:
        d = self._drawer
        for w in d.winfo_children():
            w.destroy()

        top = tk.Frame(d, bg=PANEL)
        top.pack(fill="x", padx=20, pady=(18, 10))
        tk.Label(top, text="✦ BOORU MANAGER", bg=PANEL, fg=ACCENT,
                 font=(_UI, 12, "bold")).pack(side="left")
        close = tk.Label(top, text="✕", bg=PANEL, fg=FG_DIM, cursor="hand2",
                         font=(_UI, 12))
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self.close_drawer())
        tk.Frame(d, bg=BORDER, height=1).pack(fill="x")

        self._drawer_row("🏠", "Home", HOME, indent=0, bold=True)
        for m in FEATURES:
            tk.Frame(d, bg=PANEL, height=10).pack()
            self._drawer_row(m.icon, m.title, ("macro", m.key), indent=0, bold=True)
            for s in m.subs:
                self._drawer_row(s.icon, s.title, ("sub", m.key, s.key),
                                 indent=22, enabled=s.available)

    def _drawer_row(self, icon: str, text: str, route: tuple, indent: int,
                    bold: bool = False, enabled: bool = True) -> None:
        selected = route == self.route
        base = SEL_BG if selected else PANEL
        fg = ACCENT if selected else (FG if enabled else FG_DIM)

        row = tk.Frame(self._drawer, bg=base)
        row.pack(fill="x")
        ic = tk.Label(row, text=icon, bg=base, fg=fg, font=(_EMOJI, 12),
                      width=2, anchor="w")
        ic.pack(side="left", padx=(20 + indent, 6), pady=7)
        lb = tk.Label(row, text=text if enabled else f"{text}  (TBD)", bg=base, fg=fg,
                      font=(_UI, 11, "bold" if bold else "normal"), anchor="w")
        lb.pack(side="left", fill="x", expand=True)

        if not enabled:
            return
        for w in (row, ic, lb):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda _e, r=route: self.navigate(r))
            w.bind("<Enter>", lambda _e: [x.configure(bg=ENTRY_BG) for x in (row, ic, lb)])
            w.bind("<Leave>", lambda _e: [x.configure(bg=base) for x in (row, ic, lb)])
