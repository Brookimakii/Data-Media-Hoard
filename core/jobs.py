"""
core/jobs.py
------------
Builds the flat list of download jobs from a set of selected artists and
the allowed site whitelist from config.

A "job" is a plain dict:
    {
        "artist": str,   # artist name
        "site":   str,   # site key  (e.g. "pixiv")
        "url":    str,   # single URL to pass to gallery-dl
        "output": str,   # destination directory path
    }

Path layout
-----------
    Single URL for a site  →  {base}/{artist}/{site}/
    Multiple URLs          →  {base}/{artist}/{site}/{account_name}/

The account_name is extracted from the URL by get_account_name().
Edit that function to fine-tune per-site logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    pass  # avoid circular import; build_command imported lazily below

import os
from typing import Any
from urllib.parse import urlparse


# ── Account name extractor ────────────────────────────────────────────────────

# URL path segments to ignore when looking for an account/user identifier.
_SKIP_SEGMENTS = {"users", "en", "user", "profile", "artist", "artists"}


def get_account_name(site: str, url: str) -> str:
    """
    Extract a human-readable account identifier from a URL.

    Default behaviour: walk the URL path from right to left and return
    the first segment that is not in _SKIP_SEGMENTS.

    TODO: add per-site branches below as needed.
    Examples of what you might want to customise:
        pixiv      /en/users/12345      → "12345"
        twitter    /ArtistHandle        → "ArtistHandle"
        fanbox     artist.fanbox.cc     → subdomain "artist"
        artstation /artwork vs /store   → handle edge cases
    """
    parsed = urlparse(url)

    # ── per-site overrides (add your own here) ────────────────────────────────
    # if site == "fanbox":
    #     # fanbox uses subdomain: https://artistname.fanbox.cc
    #     subdomain = parsed.hostname.split(".")[0] if parsed.hostname else None
    #     if subdomain and subdomain not in _SKIP_SEGMENTS:
    #         return subdomain

    # ── default: rightmost meaningful path segment ────────────────────────────
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return "unknown"

    for segment in reversed(parts):
        if segment.lower() not in _SKIP_SEGMENTS:
            return segment

    return parts[-1]


# ── Output path builder ───────────────────────────────────────────────────────

def build_output_path(
    base_dir: str,
    artist_name: str,
    site: str,
    urls: list[str],
) -> list[tuple[str, str]]:
    """
    Return a list of (url, output_directory) pairs.

    Single URL  →  base / artist / site
    Multi  URLs →  base / artist / site / account_name   (one folder per account)
    """
    safe_artist = artist_name.replace(" ", "_")

    if len(urls) == 1:
        out = os.path.join(base_dir, safe_artist, site)
        return [(urls[0], out)]

    return [
        (url, os.path.join(base_dir, safe_artist, site, get_account_name(site, url)))
        for url in urls
    ]


# ── Job builder ───────────────────────────────────────────────────────────────

def build_jobs(
    artists: list[dict[str, Any]],
    downloadable_sites: set[str],
    base_dir: str,
    gdl_config: str = "./config.json",
) -> list[dict[str, str]]:
    """
    Build a flat list of download jobs from the selected artists.

    Only sites present in *downloadable_sites* are included.
    Each element of the returned list is a job dict
    (see module docstring for the schema).

    Parameters
    ----------
    artists            : list of artist dicts (from core.catalogue.load_artists)
    downloadable_sites : set of site keys allowed by the config
    base_dir           : root directory for all downloads
    gdl_config         : path to the gallery-dl config.json file
    """
    jobs: list[dict[str, str]] = []

    for artist in artists:
        artist_name = artist.get("name", "unknown")

        for _category, entries in (artist.get("media") or {}).items():
            if not entries or not isinstance(entries, dict):
                continue

            for site, raw_url in entries.items():
                if site not in downloadable_sites:
                    continue

                urls = raw_url if isinstance(raw_url, list) else [raw_url]
                # Filter out any None / empty values
                urls = [u for u in urls if u]

                for url, out_path in build_output_path(base_dir, artist_name, site, urls):
                    jobs.append({
                        "artist":     artist_name,
                        "site":       site,
                        "url":        url,
                        "output":     out_path,
                        "gdl_config": gdl_config,
                    })

    return jobs


# ── Job helpers ───────────────────────────────────────────────────────────────

def jobs_by_artist(jobs: list[dict]) -> dict[str, list[dict]]:
    """Group a flat job list by artist name. Preserves insertion order."""
    grouped: dict[str, list[dict]] = {}
    for job in jobs:
        grouped.setdefault(job["artist"], []).append(job)
    return grouped


def summarise_jobs(jobs: list[dict]) -> str:
    """Return a human-readable summary string, useful for logging."""
    by_artist = jobs_by_artist(jobs)
    lines = [f"{len(jobs)} job(s) across {len(by_artist)} artist(s):"]
    for artist, artist_jobs in by_artist.items():
        sites = ", ".join(j["site"] for j in artist_jobs)
        lines.append(f"  {artist}: {sites}")
    return "\n".join(lines)


def build_run_summary(
    jobs: list[dict],
    config: dict,
    parallel: bool,
    max_parallel: int = 1,
) -> str:
    """
    Build a full pre-run summary for display in the log widget.
    Shows the active configuration and every gallery-dl command that will run.
    """
    SEP  = "─" * 60
    lines: list[str] = []

    # ── Configuration block ───────────────────────────────────────────────────
    lines.append(SEP)
    lines.append("  RUN CONFIGURATION")
    lines.append(SEP)
    lines.append(f"  gallery-dl config : {config.get('gdl_config', './config.json')}")
    lines.append(f"  download dir      : {config.get('download_dir', './download')}")
    lines.append(f"  archives dir      : {config.get('archives_dir', './archives')}")
    lines.append(f"  log dir           : {config.get('log_dir', './logs')}")
    if parallel:
        lines.append(f"  execution mode    : parallel (max {max(1, int(max_parallel))} concurrent)")
    else:
        lines.append("  execution mode    : sequential")
    sites = sorted(config.get("downloadable_sites", []))
    lines.append(f"  downloadable sites: {', '.join(sites) if sites else '(none)'}")
    lines.append(f"  total jobs        : {len(jobs)}")
    lines.append("")

    # ── Commands block ────────────────────────────────────────────────────────
    lines.append(SEP)
    lines.append("  COMMANDS")
    lines.append(SEP)

    by_artist = jobs_by_artist(jobs)
    for artist, artist_jobs in by_artist.items():
        lines.append(f"  [{artist}]")
        for job in artist_jobs:
            from core.download import build_command
            cmd_str = "    " + " ".join(build_command(job))
            lines.append(cmd_str)
        lines.append("")

    lines.append(SEP)
    lines.append("")

    return "\n".join(lines)