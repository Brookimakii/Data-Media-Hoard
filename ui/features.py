"""
ui/features.py
--------------
Single source of truth for the app's navigation tree:

    Macro feature (landing screen)
        └── Sub feature (page)

To plug an existing tab back in, set its `builder` — a callable that
receives an empty tk.Frame and builds the page into it, e.g.

    from ui.downloader_tab import build_downloader
    SubFeature("downloader", "Downloader", "⬇", "…", builder=build_downloader)

A sub feature with `available=False` is shown as a dimmed "TBD" card and
cannot be opened.

To give a module a settings dialog, set `settings` — a callable that receives
an empty tk.Frame (the dialog body) and builds the settings into it. The ⚙
button at the top right of the header only appears on modules that set it.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable

from ui.artists_page import build_artists
from ui.booru_settings_ui import build_booru_settings


@dataclass(frozen=True)
class SubFeature:
    key: str
    title: str
    icon: str
    desc: str
    builder: Callable[[tk.Frame], None] | None = None   # None → "not plugged in" page
    available: bool = True
    settings: Callable[[tk.Frame], None] | None = None  # None → no ⚙ button


@dataclass(frozen=True)
class MacroFeature:
    key: str
    title: str
    icon: str
    desc: str
    subs: tuple[SubFeature, ...]


_TBD = "To be defined"

FEATURES: tuple[MacroFeature, ...] = (
    MacroFeature(
        "media", "Media Management", "🖼",
        "Collect, tag, clean up and publish your media.",
        (
            SubFeature("downloader", "Downloader", "⬇",
                       "Download galleries from your artist list."),
            SubFeature("tag_media", "Tag Media", "🏷",
                       "Batch-tag downloaded media."),
            SubFeature("duplicates", "Duplicates", "🔍",
                       "Find and resolve duplicate and near-duplicate files."),
            SubFeature("uploader", "Uploader", "⬆",
                       "Upload your tagged media to a booru."),
        ),
    ),
    MacroFeature(
        "fic", "Fic Management", "📖",
        "Follow, update and archive your fanfiction.",
        (
            SubFeature("fic_tracker", "Fic Tracker", "📖",
                       "Track reading status and updates of your fics."),
            SubFeature("fic_tbd", "TBD", "✦", _TBD, available=False),
        ),
    ),
    MacroFeature(
        "tags", "Tags Management", "🏷",
        "Keep your tag vocabulary clean and consistent.",
        (
            SubFeature("artists", "Artists", "🎨",
                       "Manage artist entries: names, groups, URLs and notes.",
                       builder=build_artists,
                       settings=build_booru_settings),
            SubFeature("tags", "Tags", "🏷",
                       "Add, edit, delete and roll back tags and their wiki."),
            SubFeature("pools", "Pools", "📚",
                       "Build ordered pools of posts, such as comics and series."),
            SubFeature("sets", "Sets", "🗂",
                       "Group posts into sets."),
            SubFeature("post_relations", "Posts Relationship", "🔗",
                       "Manage parent and child links between posts."),
        ),
    ),
)


def find_macro(key: str) -> MacroFeature:
    return next(m for m in FEATURES if m.key == key)


def find_sub(macro_key: str, sub_key: str) -> SubFeature:
    return next(s for s in find_macro(macro_key).subs if s.key == sub_key)
