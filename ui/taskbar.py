"""
ui/taskbar.py
-------------
Windows taskbar integration:
  - Set the window icon from icon.ico (works on all platforms)
  - Drive the Windows 7+ taskbar progress bar via ITaskbarList3 COM

Usage
-----
    from ui.taskbar import init_taskbar, set_taskbar_progress, clear_taskbar_progress

    init_taskbar(root)                          # call once after root is created
    set_taskbar_progress(root, done, total)     # 0 ≤ done ≤ total
    clear_taskbar_progress(root)                # remove the progress overlay
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

# ── Icon path ─────────────────────────────────────────────────────────────────

_ICON = Path(__file__).parent.parent / "icon.ico"

# ── ITaskbarList3 constants ───────────────────────────────────────────────────
# https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/ne-shobjidl_core-tbpflag
_TBPF_NOPROGRESS    = 0x0
_TBPF_INDETERMINATE = 0x1
_TBPF_NORMAL        = 0x2   # green bar
_TBPF_ERROR         = 0x4   # red bar
_TBPF_PAUSED        = 0x8   # yellow bar

_taskbar_com = None   # cached ITaskbarList3 instance


def _get_com() -> object | None:
    """
    Obtain (and cache) an ITaskbarList3 COM object.
    Returns None on non-Windows or if COM is unavailable.
    """
    global _taskbar_com
    if _taskbar_com is not None:
        return _taskbar_com
    if sys.platform != "win32":
        return None
    try:
        import comtypes.client as cc
        import comtypes.gen.TaskbarLib as tbl  # may not exist yet
    except ImportError:
        pass

    try:
        # comtypes approach
        import comtypes.client as cc
        cc.GetModule("taskbar")   # not always available
    except Exception:
        pass

    # Fallback: use ctypes directly (no extra dependencies beyond stdlib)
    try:
        import ctypes
        import ctypes.wintypes as wt

        CLSID_TaskbarList = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
        IID_ITaskbarList3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"

        class ITaskbarList3(ctypes.Structure):
            pass

        taskbar = ctypes.windll.ole32.CoCreateInstance
        # Use Shell32's TaskbarList COM object via ctypes
        from ctypes import HRESULT
        import ctypes

        shell32   = ctypes.windll.shell32
        ole32     = ctypes.windll.ole32

        # CLSID / IID as GUID structs
        def _guid(s: str):
            import uuid
            b = uuid.UUID(s).bytes_le
            return (ctypes.c_byte * 16)(*b)

        clsid = _guid(CLSID_TaskbarList)
        iid   = _guid(IID_ITaskbarList3)

        # CoCreateInstance
        ptr = ctypes.c_void_p()
        hr  = ole32.CoCreateInstance(
            clsid, None, 1,    # CLSCTX_INPROC_SERVER
            iid, ctypes.byref(ptr),
        )
        if hr != 0:
            return None

        # Build a minimal vtable wrapper
        # ITaskbarList3 vtable layout (offsets are slot indices):
        #  0 QueryInterface  1 AddRef  2 Release
        #  3 HrInit          4 AddTab  5 DeleteTab  6 ActivateTab  7 SetActiveAlt
        #  8 MarkFullscreenWindow
        #  9 SetProgressValue   10 SetProgressState
        # 11 RegisterTab  12 UnregisterTab
        # 13 SetTabOrder  14 SetTabActive
        # 15 ThumbBarAddButtons  16 ThumbBarUpdateButtons  17 ThumbBarSetImageList
        # 18 SetOverlayIcon  19 SetThumbnailTooltip  20 SetThumbnailClip

        vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))
        vt     = ctypes.cast(vtable[0], ctypes.POINTER(ctypes.c_void_p))

        HRESULT      = ctypes.c_long
        HWND         = ctypes.c_void_p
        ULONGLONG    = ctypes.c_ulonglong
        TBPF         = ctypes.c_int

        HrInit_t           = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
        SetProgressValue_t = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, HWND, ULONGLONG, ULONGLONG)
        SetProgressState_t = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, HWND, TBPF)

        hr_init     = HrInit_t(vt[3])
        set_value   = SetProgressValue_t(vt[9])
        set_state   = SetProgressState_t(vt[10])

        hr_init(ptr)

        _taskbar_com = (ptr, set_value, set_state)
        return _taskbar_com

    except Exception as e:
        print(f"[taskbar] COM init failed: {e}")
        return None


def _hwnd(root: tk.Tk) -> int:
    """Return the true top-level Win32 HWND for a tkinter root window."""
    try:
        import ctypes
        child = root.winfo_id()
        GA_ROOT = 2
        hwnd = ctypes.windll.user32.GetAncestor(child, GA_ROOT)
        return hwnd or child
    except Exception:
        return root.winfo_id()


# ── Public API ────────────────────────────────────────────────────────────────

def init_taskbar(root: tk.Tk) -> None:
    """
    Set the window icon (title bar + taskbar) and initialise the COM taskbar object.
    Call once after the root window is created and mapped.

    On Windows, iconbitmap() only updates the title bar icon.
    To update the taskbar button icon we must load the .ico as a Win32 HICON
    and send WM_SETICON to the HWND.
    """
    if _ICON.exists():
        try:
            root.iconbitmap(str(_ICON))
        except Exception as e:
            print(f"[taskbar] iconbitmap failed: {e}")

    if sys.platform == "win32" and _ICON.exists():
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32   = ctypes.windll.user32

            # Get the true top-level HWND (what Windows shows in the taskbar).
            # tkinter's winfo_id() returns a child HWND embedded inside a
            # container — GetAncestor(GA_ROOT=2) walks up to the real root.
            child_hwnd = root.winfo_id()
            GA_ROOT    = 2
            hwnd       = user32.GetAncestor(child_hwnd, GA_ROOT) or child_hwnd

            # Load both small (16x16) and large (32x32) icons from the .ico
            ICON_SMALL    = 0
            ICON_BIG      = 1
            IMAGE_ICON    = 1
            LR_LOADFROMFILE = 0x00000010
            WM_SETICON    = 0x0080

            for icon_type, size in ((ICON_SMALL, 16), (ICON_BIG, 32)):
                hicon = user32.LoadImageW(
                    None, str(_ICON), IMAGE_ICON,
                    size, size,
                    LR_LOADFROMFILE,
                )
                if hicon:
                    user32.SendMessageW(hwnd, WM_SETICON, icon_type, hicon)
                else:
                    print(f"[taskbar] LoadImageW returned NULL for size {size}")
        except Exception as e:
            print(f"[taskbar] taskbar icon set failed: {e}")

    # Eagerly init COM so the first progress update has no delay
    _get_com()


def set_taskbar_progress(root: tk.Tk, done: int, total: int) -> None:
    """
    Update the Windows taskbar progress bar.

    Parameters
    ----------
    done  : number of completed units
    total : total units (must be > 0)
    """
    com = _get_com()
    if com is None or total <= 0:
        return
    ptr, set_value, set_state = com
    hwnd = _hwnd(root)
    try:
        import ctypes
        set_state(ptr, hwnd, _TBPF_NORMAL)
        set_value(ptr, hwnd,
                  ctypes.c_ulonglong(done),
                  ctypes.c_ulonglong(total))
    except Exception as e:
        print(f"[taskbar] set_progress failed: {e}")


def set_taskbar_indeterminate(root: tk.Tk) -> None:
    """Show a pulsing indeterminate bar (e.g. while preparing jobs)."""
    com = _get_com()
    if com is None:
        return
    ptr, _, set_state = com
    try:
        set_state(ptr, _hwnd(root), _TBPF_INDETERMINATE)
    except Exception:
        pass


def set_taskbar_paused(root: tk.Tk) -> None:
    """Turn the taskbar bar yellow to signal a paused run."""
    com = _get_com()
    if com is None:
        return
    ptr, _, set_state = com
    try:
        set_state(ptr, _hwnd(root), _TBPF_PAUSED)
    except Exception:
        pass


def set_taskbar_error(root: tk.Tk) -> None:
    """Turn the taskbar bar red to signal a failed run."""
    com = _get_com()
    if com is None:
        return
    ptr, _, set_state = com
    try:
        set_state(ptr, _hwnd(root), _TBPF_ERROR)
    except Exception:
        pass



    """Turn the taskbar bar red to signal a failed run."""
    com = _get_com()
    if com is None:
        return
    ptr, _, set_state = com
    try:
        set_state(ptr, _hwnd(root), _TBPF_ERROR)
    except Exception:
        pass


def clear_taskbar_progress(root: tk.Tk) -> None:
    """Remove the progress overlay from the taskbar button."""
    com = _get_com()
    if com is None:
        return
    ptr, _, set_state = com
    try:
        set_state(ptr, _hwnd(root), _TBPF_NOPROGRESS)
    except Exception:
        pass