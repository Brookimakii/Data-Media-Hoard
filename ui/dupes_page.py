"""
ui/dupes_page.py
----------------
Duplicate review in two screens.

  LIST    every group, one under the other (a recycling list: cheap even with thousands).
          Click a file to select it, click a group to open it. "Mark selected" sets the
          selected files to Keep / Throw away / It's an edit. "Process all…" (with a
          confirmation) applies every decision.
  DETAIL  one group, two files side by side with the metadata between them (zoom and
          pan mirrored). Under each image: Keep / Throw away / It's an edit.

    Keep           uploaded
    Throw away     not uploaded (flagged in hoard.db, the file stays on disk)
    It's an edit   different work, not a duplicate: uploaded and split off the group

Decisions are staged until "Process all". Until you touch a group, the best copy is
suggested as Keep and the others as Throw away. Nothing is ever deleted from disk.
"""

from __future__ import annotations

import io
import logging
import queue
import sqlite3
import threading
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

from core.dupe_flags import DupeFlags
from core.dupes import (
    FLOOR, DupeIndex, Entry, Group, Pair, ScanReport, build_groups, find_pairs,
)
from core.dupes_sort import SORT_OPTIONS, SORT_KEYS, direction_text, sort_groups
from core.dupes_review import (
    KEEP, Review, apply_plan, is_done, plan_process, undo_ops,
)
from core.catalogue import load_config
from core.file_db import get_db
from ui import back_stack
from ui.dupes_compare import STATE_COLOR, STATE_TEXT, Btn, ComparePanel, _Segments
from ui.canvas_list import CanvasList
from ui.theme import (
    ACCENT, ACCENT2, BORDER, COLOR_FAIL, COLOR_OK, COLOR_RUNNING, ENTRY_BG, FG, FG_DIM,
    FONT_BTN, PANEL,
)

log = logging.getLogger(__name__)

_UI = FONT_BTN[0]
_ROW_H, _CELLS, _CELL_W, _THUMB, _CELLS_X = 92, 8, 84, (72, 56), 172
_POLL_MS = 150

_ACTIVE: "DupesPage | None" = None
_KEYS_BOUND = False
_KEYS = (("<Left>", "left"), ("<Right>", "right"), ("<Shift-Left>", "shift-left"),
         ("<Shift-Right>", "shift-right"), ("<Prior>", "prev"), ("<Next>", "next"),
         ("<space>", "space"), ("<KeyRelease-space>", "space-up"), ("<Escape>", "escape"), ("<Key-f>", "fit"), ("<Key-1>", "actual"))


def _activate(page: "DupesPage") -> None:
    """Keyboard shortcuts go to the most recently built page (bound once, app-wide)."""
    global _ACTIVE, _KEYS_BOUND
    _ACTIVE = page
    if not _KEYS_BOUND:
        _KEYS_BOUND = True
        for seq, name in _KEYS:
            page.bind_all(seq, lambda _e, n=name: _ACTIVE and _ACTIVE.on_key(n), add="+")


class DupesPage(tk.Frame):
    def __init__(self, parent: tk.Misc, conn: sqlite3.Connection, db_path: str | None) -> None:
        super().__init__(parent, bg=PANEL)
        self._conn, self._db_path = conn, db_path
        self._flags = DupeFlags(conn)
        self._review = Review()
        self._entries: list[Entry] = []
        self._all_pairs: list[Pair] = []
        self._lookup: dict[tuple[int, int], float] = {}
        self._groups: list[Group] = []
        self._shown: list[Group] = []
        self._flagged: set[str] = set()
        self._gi = 0
        self._sel: set[str] = set()                  # selected file keys
        self._undo: list[list[tuple]] = []
        self._hide_done = True
        self._scan_root = ""
        self._base = self._download_base()
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._cancel = threading.Event()
        self._photos: OrderedDict[str, object] = OrderedDict()
        self._view_name = "list"
        self._v_folder = tk.StringVar(value=self._flags.get_pref("folder"))
        try:
            thr = int(self._flags.get_pref("threshold", "90"))
        except ValueError:
            thr = 90
        self._threshold = min(100, max(int(FLOOR), thr))
        self._sort_key = self._flags.get_pref("sort", "similarity")
        if self._sort_key not in SORT_KEYS:
            self._sort_key = "similarity"
        self._sort_flip = self._flags.get_pref("sort_flip", "0") == "1"
        self._build_list()
        self._build_detail()
        self._show("list")
        self._refresh_all()
        _activate(self)
        back_stack.register(self.handle_back)

    # ── LIST layout ───────────────────────────────────────────────────────────
    def _build_list(self) -> None:
        self._list = tk.Frame(self, bg=PANEL)
        bar = tk.Frame(self._list, bg=PANEL)
        bar.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(bar, text="Folder", bg=PANEL, fg=FG_DIM, font=(_UI, 9)).pack(side="left")
        tk.Entry(bar, textvariable=self._v_folder, bg=ENTRY_BG, fg=FG, insertbackground=FG,
                 relief="flat", highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT, font=(_UI, 10)).pack(side="left", fill="x", expand=True,
                                                             padx=8, ipady=4)
        Btn(bar, "Browse…", self._browse).pack(side="left")
        self._btn_scan = Btn(bar, "Scan", self._scan, "primary")
        self._btn_scan.pack(side="left", padx=(8, 0))

        ctl = tk.Frame(self._list, bg=PANEL)
        ctl.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(ctl, text="Similarity", bg=PANEL, fg=FG_DIM, font=(_UI, 9)).pack(side="left")
        self._scale = tk.Scale(ctl, from_=int(FLOOR), to=100, orient="horizontal", showvalue=0,
                               length=240, bg=PANEL, troughcolor=ENTRY_BG, highlightthickness=0,
                               bd=0, sliderrelief="flat", activebackground=ACCENT,
                               command=self._on_scale)
        self._scale.set(self._threshold)
        self._scale.pack(side="left", padx=8)
        self._scale.bind("<ButtonRelease-1>", lambda _e: self._regroup())
        self._lbl_thr = tk.Label(ctl, text=f"{self._threshold}%", bg=PANEL, fg=ACCENT2,
                                 font=(_UI, 10, "bold"), width=5, anchor="w")
        self._lbl_thr.pack(side="left")
        tk.Label(ctl, text="Sort by", bg=PANEL, fg=FG_DIM, font=(_UI, 9)).pack(side="left", padx=(18, 6))
        labels = {k: lbl for k, lbl, _d, _t in SORT_OPTIONS}
        self._sort_labels = {lbl: k for k, lbl in labels.items()}
        self._v_sort = tk.StringVar(value=labels[self._sort_key])
        menu = tk.OptionMenu(ctl, self._v_sort, *labels.values(), command=self._on_sort)
        menu.config(bg=ENTRY_BG, fg=FG, activebackground=ACCENT, activeforeground=FG,
                    highlightthickness=0, relief="flat", font=(_UI, 9), width=15, anchor="w")
        menu.pack(side="left")
        self._btn_flip = Btn(ctl, "", self._flip_sort)
        self._btn_flip.pack(side="left", padx=6)
        self._lbl_stats = tk.Label(ctl, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 9), anchor="e")
        self._lbl_stats.pack(side="right")

        tools = tk.Frame(self._list, bg=PANEL)
        tools.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(tools, text="Selected files:", bg=PANEL, fg=FG_DIM, font=(_UI, 9)
                 ).pack(side="left", padx=(0, 8))
        self._seg = _Segments(tools, self._mark_selected)
        self._seg.config(bg=PANEL)
        self._seg.pack(side="left")
        self._lbl_sel = tk.Label(tools, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 9))
        self._lbl_sel.pack(side="left", padx=10)
        self._btn_process = Btn(tools, "Process all…", self._process, "primary")
        self._btn_process.pack(side="right")
        self._btn_undo = Btn(tools, "↶ Undo", self._undo_last)
        self._btn_undo.pack(side="right", padx=6)
        self._btn_hide = Btn(tools, "", self._toggle_hide)
        self._btn_hide.pack(side="right", padx=6)

        self._empty = tk.Label(self._list, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 12))
        self._rlist = CanvasList(self._list, _ROW_H, self._draw_row, self._list_click,
                                 self._list_double, PANEL)
        self._rlist.pack(fill="both", expand=True, padx=12, pady=4)
        self._status = tk.Label(self._list, text="", bg=PANEL, fg=FG_DIM, font=(_UI, 9), anchor="w")
        self._status.pack(fill="x", padx=18, pady=(0, 10))

    def _build_detail(self) -> None:
        self._detail = ComparePanel(self, on_back=self._back, on_prev=lambda: self._goto(-1),
                                    on_next=lambda: self._goto(1), get_state=self._state_of,
                                    set_state=self._set_state)

    def _show(self, name: str) -> None:
        self._view_name = name
        self._list.pack_forget()
        self._detail.pack_forget()
        (self._list if name == "list" else self._detail).pack(fill="both", expand=True)

    def _say(self, text: str, error: bool = False) -> None:
        self._status.config(text=text, fg=COLOR_FAIL if error else FG_DIM)

    @staticmethod
    def _download_base() -> str:
        """download_dir from config.yaml: files live in  base / artist / site / ..."""
        root = Path(__file__).resolve().parent.parent
        try:
            cfg = load_config(str(root / "config.yaml"))
            return str((root / cfg.get("download_dir", "./download")).resolve())
        except Exception:
            return str((root / "download").resolve())

    # ── scan ──────────────────────────────────────────────────────────────────
    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self._v_folder.get() or str(Path.home()))
        if d:
            self._v_folder.set(d)

    def _scan(self) -> None:
        if self._busy:
            self._cancel.set()
            self._say("Stopping…")
            return
        folder = self._v_folder.get().strip()
        if not folder or not Path(folder).is_dir():
            self._say("Pick an existing folder first.", error=True)
            return
        self._flags.set_pref("folder", folder)
        self._scan_root = folder
        self._busy = True
        self._cancel.clear()
        self._btn_scan.config(text="Stop")
        self._say("Scanning…")
        threading.Thread(target=self._work, args=(folder, self._flags.ignored_pairs()),
                         daemon=True).start()
        self.after(_POLL_MS, self._poll)

    def _work(self, folder: str, ignored) -> None:
        """Background thread: its own SQLite connection, never touches Tk."""
        try:
            conn = sqlite3.connect(self._db_path) if self._db_path else self._conn
            rep = DupeIndex(conn).scan(
                folder, lambda n, t, p: self._queue.put(("scan", n, t)), self._cancel.is_set)
            if rep.cancelled:
                self._queue.put(("cancelled",))
                return
            self._queue.put(("match", 0, 1))
            pairs = find_pairs(rep.entries, progress=lambda n, t: self._queue.put(("match", n, t)))
            self._queue.put(("done", rep, pairs))
        except Exception as e:                           # noqa: BLE001
            log.exception("duplicate scan failed")
            self._queue.put(("error", f"{type(e).__name__}: {e}"))

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        last = None
        try:
            while True:
                last = self._queue.get_nowait()
                if last[0] in ("done", "cancelled", "error"):
                    break
                self._say(f"Scanning files… {last[1]} / {last[2]}" if last[0] == "scan"
                          else f"Comparing… {last[1]} / {last[2]}")
        except queue.Empty:
            pass
        if last and last[0] in ("done", "cancelled", "error"):
            self._busy = False
            self._btn_scan.config(text="Scan")
            if last[0] == "cancelled":
                self._say("Scan stopped.")
            elif last[0] == "error":
                self._say(f"Scan failed: {last[1]}", error=True)
            else:
                self._finish(last[1], last[2])
            return
        self.after(_POLL_MS, self._poll)

    def _finish(self, rep: ScanReport, pairs: list[Pair]) -> None:
        self._entries, self._all_pairs = rep.entries, pairs
        self._lookup = {(p.a, p.b): p.score for p in pairs}
        self._review = Review()
        self._sel.clear()
        self._undo.clear()
        self._regroup(reset=True)
        bad = f", {len(rep.errors)} unreadable" if rep.errors else ""
        self._say(f"Scanned {len(rep.entries)} files ({rep.computed} new){bad}.")

    # ── grouping ──────────────────────────────────────────────────────────────
    def _on_scale(self, value: str) -> None:
        self._threshold = int(float(value))
        self._lbl_thr.config(text=f"{self._threshold}%")

    def _regroup(self, reset: bool = False) -> None:
        self._flags.set_pref("threshold", str(self._threshold))
        self._flagged = self._flags.flagged_keys()
        ignored = self._flags.ignored_pairs()
        pairs = [p for p in self._all_pairs
                 if tuple(sorted((self._entries[p.a].key, self._entries[p.b].key))) not in ignored]
        self._groups = build_groups(self._entries, pairs, self._threshold)
        self._apply_filter(reset)

    def _apply_filter(self, reset: bool = False) -> None:
        self._flagged = self._flags.flagged_keys()
        self._shown = [g for g in self._groups
                       if not self._hide_done or not is_done(g, self._entries, self._flagged)]
        self._shown = sort_groups(self._shown, self._entries, self._sort_key, self._sort_flip,
                                  [self._base, self._scan_root], self._review, self._flagged)
        self._rlist.set_count(len(self._shown), reset=reset)
        self._refresh_all()

    def _on_sort(self, label: str) -> None:
        self._sort_key = self._sort_labels.get(label, "similarity")
        self._sort_flip = False
        self._save_sort()
        self._apply_filter(reset=True)

    def _flip_sort(self) -> None:
        self._sort_flip = not self._sort_flip
        self._save_sort()
        self._apply_filter(reset=True)

    def _save_sort(self) -> None:
        self._flags.set_pref("sort", self._sort_key)
        self._flags.set_pref("sort_flip", "1" if self._sort_flip else "0")

    def _toggle_hide(self) -> None:
        self._hide_done = not self._hide_done
        self._apply_filter(reset=True)

    def _refresh_all(self) -> None:
        done = sum(1 for g in self._groups if is_done(g, self._entries, self._flagged))
        self._lbl_stats.config(
            text=f"{len(self._entries)} files · {len(self._groups)} groups ({done} done) · "
                 f"{len(self._flagged)} flagged")
        self._btn_hide.config(text="Showing: to do only" if self._hide_done else "Showing: all groups")
        self._btn_flip.config(text="⇅ " + direction_text(self._sort_key, self._sort_flip))
        self._lbl_sel.config(text=f"{len(self._sel)} selected" if self._sel else "click files to select")
        if not self._shown:
            msg = ("Pick a folder and press Scan." if not self._entries else
                   "Nothing left to review at this similarity." if self._groups else
                   f"No duplicates at {self._threshold}% or more. Lower the similarity to look further.")
            self._empty.config(text=msg)
            self._empty.place(relx=0.5, rely=0.45, anchor="center")
        else:
            self._empty.place_forget()

    # ── rows ──────────────────────────────────────────────────────────────────
    def _photo(self, e: Entry):
        """Tk image of a file's preview (stored in the database at scan time), or None."""
        photo = self._photos.get(e.key, 0)
        if photo != 0:
            self._photos.move_to_end(e.key)
            return photo
        photo = None
        try:
            blob = DupeIndex.read_preview(self._conn, e.key)
            with Image.open(io.BytesIO(blob) if blob else e.path) as im:
                if not blob and im.format == "JPEG":
                    im.draft("RGB", (_THUMB[0] * 2, _THUMB[1] * 2))
                im.thumbnail(_THUMB)
                photo = ImageTk.PhotoImage(im.convert("RGB"))
        except Exception:                              # unreadable file / no preview
            photo = None
        self._photos[e.key] = photo
        while len(self._photos) > 2500:
            self._photos.popitem(last=False)
        return photo

    def _draw_row(self, cv: tk.Canvas, i: int, y: int, width: int, tag: str) -> None:
        """One group row, drawn straight on the canvas (cheap: no widgets)."""
        if i >= len(self._shown):
            return
        g = self._shown[i]
        st = self._review.states(g, self._entries, self._flagged)
        low = min(g.scores.values())
        if is_done(g, self._entries, self._flagged):
            chip, chip_color = "✓ done", COLOR_OK
        elif self._review.touched(g, self._entries):
            chip, chip_color = "● decided", ACCENT2
        else:
            chip, chip_color = "suggested", FG_DIM
        cv.create_rectangle(6, y + 2, max(width - 6, 7), y + _ROW_H - 2, fill=ENTRY_BG,
                            outline="", tags=(tag,))
        cv.create_text(20, y + 26, text=f"Group {i + 1}", anchor="w", fill=FG,
                       font=(_UI, 11, "bold"), tags=(tag,))
        cv.create_text(20, y + 47, anchor="w", fill=FG_DIM, font=(_UI, 9), tags=(tag,),
                       text=f"{len(g.members)} files · " + ("identical" if g.exact else f"≥ {low:.0f}%"))
        cv.create_text(20, y + 65, text=chip, anchor="w", fill=chip_color,
                       font=(_UI, 9, "bold"), tags=(tag,))
        for c, idx in enumerate(g.members[:_CELLS]):
            e = self._entries[idx]
            x0 = _CELLS_X + c * _CELL_W
            cx = x0 + _CELL_W // 2
            if e.key in self._sel:
                cv.create_rectangle(x0 + 1, y + 5, x0 + _CELL_W - 3, y + _ROW_H - 5,
                                    outline=ACCENT, width=2, tags=(tag,))
            photo = self._photo(e)
            if photo is not None:
                cv.create_image(cx, y + 8 + (_THUMB[1] - photo.height()) // 2, image=photo,
                                anchor="n", tags=(tag,))
            else:
                cv.create_text(cx, y + 8 + _THUMB[1] // 2, text="no preview", fill=FG_DIM,
                               font=(_UI, 8), tags=(tag,))
            s = st[idx]
            cv.create_text(cx, y + _THUMB[1] + 17, tags=(tag,), fill=STATE_COLOR[s],
                           font=(_UI, 9, "bold"),
                           text=("⛔ " if e.key in self._flagged else "") + STATE_TEXT[s])
            cv.create_text(cx, y + _THUMB[1] + 31, tags=(tag,), fill=FG_DIM, font=(_UI, 7),
                           text=f"{e.width}×{e.height}" if e.width else e.fmt.upper())

    def _member_at(self, i: int, x: int) -> int | None:
        """Entry index of the file under x in row i, or None (info block / empty space)."""
        g = self._shown[i]
        c = (x - _CELLS_X) // _CELL_W
        if x >= _CELLS_X and 0 <= c < min(len(g.members), _CELLS):
            return g.members[c]
        return None

    def _list_click(self, i: int, x: int) -> None:
        idx = self._member_at(i, x)
        if idx is None:
            self.open_group(i)                       # click on the group: open it
            return
        self._sel.symmetric_difference_update({self._entries[idx].key})   # click on a file
        self._refresh_all()
        self._rlist.redraw_row(i)

    def _list_double(self, i: int, x: int) -> None:
        idx = self._member_at(i, x)
        if idx is not None:
            self.open_group(i, idx)

    # ── list actions ──────────────────────────────────────────────────────────
    def _mark_selected(self, state: str) -> None:
        if not self._sel:
            self._say("Click one or more files first, then choose what to do with them.", error=True)
            return
        for k in self._sel:
            self._review.mark(k, state)
        n = len(self._sel)
        self._sel.clear()
        self._refresh_all()
        self._rlist.refresh()
        self._say(f"Marked {n} file(s): {STATE_TEXT[state].lower()}. Applied when you press Process all.")

    def _process(self) -> None:
        plan = plan_process(self._groups, self._entries, self._review, self._flagged)
        if not plan.groups:
            self._say("Nothing to process.", error=True)
            return
        skip = f"\n{plan.skipped} group(s) have no file kept and will be left alone." if plan.skipped else ""
        ok = messagebox.askokcancel(
            "Process all groups",
            f"{plan.groups} groups:\n"
            f"  • {plan.thrown} file(s) will be flagged: don't upload\n"
            f"  • {plan.kept} file(s) kept\n"
            f"  • {plan.edits} file(s) are edits (split off, uploaded){skip}\n\n"
            "Nothing is deleted from disk. You can undo this.")
        if not ok:
            return
        ops = apply_plan(plan, self._flags)
        done_keys = {self._entries[i].key for g in self._groups for i in g.members}
        self._review.clear(done_keys)
        self._undo.append(ops)
        self._sel.clear()
        self._regroup()
        self._say(f"Processed {plan.groups} groups: {plan.thrown} flagged, {plan.kept} kept, "
                  f"{plan.edits} edits.")

    def _undo_last(self) -> None:
        if not self._undo:
            self._say("Nothing to undo.")
            return
        undo_ops(self._undo.pop(), self._flags)
        self._regroup()
        self._say("Last processing undone.")

    # ── detail ────────────────────────────────────────────────────────────────
    def open_group(self, i: int, member: int | None = None) -> None:
        if not 0 <= i < len(self._shown):
            return
        self._gi = i
        g = self._shown[i]
        st = self._review.states(g, self._entries, self._flagged)
        order = sorted(g.members, key=lambda m: (st[m] != KEEP, g.members.index(m)))
        left = order[0]
        right = member if member is not None and member != left else next(
            (m for m in g.members if m != left), left)
        pos = {m: n for n, m in enumerate(g.members)}
        ents = [self._entries[m] for m in g.members]
        by_obj = {id(e): m for m, e in zip(g.members, ents)}
        self._detail.show(
            f"Group {i + 1} / {len(self._shown)} · {len(g.members)} files", ents, list(g.members),
            pos[left], pos[right],
            lambda a, b: self._lookup.get(tuple(sorted((by_obj[id(a)], by_obj[id(b)])))),
            [self._base, self._scan_root])
        self._show("detail")
        try:
            self.focus_set()          # a Folder/search entry keeping focus would swallow Space and arrows
        except tk.TclError:
            pass

    def _state_of(self, idx: int) -> str:
        g = self._shown[self._gi]
        return self._review.states(g, self._entries, self._flagged)[idx]

    def _set_state(self, idx: int, state: str) -> None:
        self._review.mark(self._entries[idx].key, state)

    def _back(self) -> None:
        self._show("list")
        self._refresh_all()
        self._rlist.refresh()
        self._rlist.see(self._gi)

    def _goto(self, d: int) -> None:
        if 0 <= self._gi + d < len(self._shown):
            self.open_group(self._gi + d)

    def handle_back(self) -> bool:
        """Mouse back button / Alt+Left: detail -> list, before leaving the page."""
        if self._view_name == "detail" and self.winfo_ismapped():
            self._back()
            return True
        return False

    def on_key(self, name: str) -> None:
        """Keyboard shortcuts of the detail view (ignored while typing)."""
        try:
            if self._view_name != "detail" or not self.winfo_ismapped():
                return
            if isinstance(self.focus_get(), tk.Entry):
                if name != "space-up":
                    return
        except tk.TclError:
            return
        d = self._detail
        {"left": lambda: d.step("right", -1), "right": lambda: d.step("right", 1),
         "shift-left": lambda: d.step("left", -1), "shift-right": lambda: d.step("left", 1),
         "prev": lambda: self._goto(-1), "next": lambda: self._goto(1), "space": lambda: d.hold(True),
         "space-up": lambda: d.hold(False), "escape": self._back, "fit": d.fit, "actual": d.actual}[name]()


def build_duplicates(parent: tk.Frame) -> None:
    db = get_db()
    if db is None:
        tk.Label(parent, text="Database not available.", bg=PANEL, fg=COLOR_RUNNING).pack(pady=40)
        return
    DupesPage(parent, db.connection, str(getattr(db, "_path", "")) or None).pack(fill="both", expand=True)
