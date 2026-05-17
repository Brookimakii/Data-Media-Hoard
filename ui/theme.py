"""
ui/theme.py
-----------
All visual constants: colours, fonts, spacing, emoji indicators.
Nothing else lives here — import from this module wherever a colour
or font is needed to keep the whole UI visually consistent.
"""

from __future__ import annotations

from pathlib import Path

from core.catalogue import load_config

# ── Colour palette ─────────────────────────────────────────────────────────────
BG       = "#0f0f13"
PANEL    = "#16161d"
ACCENT   = "#7c6af7"
ACCENT2  = "#c084fc"
FG       = "#e8e8f0"
FG_DIM   = "#6b6b80"
BORDER   = "#2a2a38"
ENTRY_BG = "#1e1e2a"
BTN_BG   = "#7c6af7"
BTN_FG   = "#ffffff"
BTN_HOV  = "#9580ff"
SEL_BG   = "#2a2040"

# Semantic colours used in status indicators
COLOR_PENDING = FG_DIM
COLOR_RUNNING = "#f5a623"
COLOR_OK      = "#7ec87e"
COLOR_FAIL    = "#e06c6c"

# Subtle row alternation inside scrollable lists
ROW_ALT = "#181826"

# ── Fonts ──────────────────────────────────────────────────────────────────────
def _read_font_family_config() -> dict[str, str]:
    defaults = {
        "ui_family": "Segoe UI",
        "mono_family": "Consolas",
        "emoji_family": "Segoe UI Emoji",
        "serif_family": "Georgia",
    }
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    try:
        cfg = load_config(cfg_path)
        ui_cfg = cfg.get("ui", {}) if isinstance(cfg, dict) else {}
        font_cfg = ui_cfg.get("fonts", {}) if isinstance(ui_cfg, dict) else {}
        if not isinstance(font_cfg, dict):
            return defaults
        return {
            "ui_family": str(font_cfg.get("ui_family") or defaults["ui_family"]),
            "mono_family": str(font_cfg.get("mono_family") or defaults["mono_family"]),
            "emoji_family": str(font_cfg.get("emoji_family") or defaults["emoji_family"]),
            "serif_family": str(font_cfg.get("serif_family") or defaults["serif_family"]),
        }
    except Exception:
        return defaults


_FONTS = _read_font_family_config()

FONT_HEAD  = (_FONTS["serif_family"], 22, "bold")
FONT_SUB   = (_FONTS["serif_family"], 11, "italic")
FONT_BODY  = (_FONTS["mono_family"], 10)
FONT_MONO  = (_FONTS["mono_family"],  9)
FONT_BTN   = (_FONTS["ui_family"],   10, "bold")
FONT_TAGS  = (_FONTS["mono_family"],  8)
FONT_BOLD  = (_FONTS["mono_family"], 10, "bold")
FONT_STRI  = (_FONTS["mono_family"], 10, "overstrike")
FONT_EMOJI = (_FONTS["emoji_family"], 10)

# ── Status circle emoji ────────────────────────────────────────────────────────
CIRCLE_GRAY   = "\U00002B55"   # ⭕  pending / not started
CIRCLE_ORANGE = "\U0001F7E0"   # 🟠  in progress
CIRCLE_GREEN  = "\U0001F7E2"   # 🟢  success
CIRCLE_RED    = "\U0001F534"   # 🔴  failure

STATUS_CIRCLES = {
    "pending": (CIRCLE_GRAY,   COLOR_PENDING),
    "running": (CIRCLE_ORANGE, COLOR_RUNNING),
    "ok":      (CIRCLE_GREEN,  COLOR_OK),
    "fail":    (CIRCLE_RED,    COLOR_FAIL),
}

# ── Misc layout constants ──────────────────────────────────────────────────────
PAD_OUTER = 30   # horizontal padding for top-level sections
COLS      = 3    # columns in the artist checklist grid