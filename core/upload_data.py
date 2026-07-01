"""
core/upload_data.py
-------------------
Load and save the persistent data files used by the uploader:

  uploaded_dnu.json     — nested path tree; leaf values:
                            0 = do not upload
                            1 = already uploaded
  character_tags.json   — {"tag": [...aliases]} dict
  copyright_tags.json   — {"tag": [...aliases]} dict

Tag file layout:
  base / artist / site / [account /] image.ext
  base / artist / .tags / site / image.txt     (no account subfolder)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── File names ─────────────────────────────────────────────────────────────────
UPLOADED_DNU_FILE   = "uploaded_dnu.json"
CHARACTER_TAGS_FILE = "character_tags.json"
COPYRIGHT_TAGS_FILE = "copyright_tags.json"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


# ── JSON helpers ───────────────────────────────────────────────────────────────

def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Nested path tree ───────────────────────────────────────────────────────────

def _set_nested(tree: dict, parts: list[str], value: int) -> None:
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _get_nested(tree: dict, parts: list[str]) -> int | None:
    node = tree
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, int) else None


def _path_to_parts(base: Path, file: Path) -> list[str]:
    return list(file.relative_to(base).parts)


# ── UploadedDNU ────────────────────────────────────────────────────────────────

class UploadedDNU:
    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / UPLOADED_DNU_FILE
        self._tree: dict = _read_json(self._path) or {}
        log.debug("UploadedDNU loaded: %s", self._path)

    def is_dnu(self, base: Path, file: Path) -> bool:
        return _get_nested(self._tree, _path_to_parts(base, file)) == 0

    def is_uploaded(self, base: Path, file: Path) -> bool:
        return _get_nested(self._tree, _path_to_parts(base, file)) == 1

    def is_pending(self, base: Path, file: Path) -> bool:
        return _get_nested(self._tree, _path_to_parts(base, file)) is None

    def mark_dnu(self, base: Path, file: Path) -> None:
        log.debug("UploadedDNU: mark_dnu %s", file)
        _set_nested(self._tree, _path_to_parts(base, file), 0)

    def mark_uploaded(self, base: Path, file: Path) -> None:
        log.debug("UploadedDNU: mark_uploaded %s", file)
        _set_nested(self._tree, _path_to_parts(base, file), 1)

    def save(self) -> None:
        _write_json(self._path, self._tree)
        log.debug("UploadedDNU saved: %s", self._path)


# ── Tag catalogues ─────────────────────────────────────────────────────────────

class TagCatalogue:
    def __init__(self, path: Path) -> None:
        self._path = path
        raw = _read_json(path)
        self._tags: dict[str, list[str]] = raw if isinstance(raw, dict) else {}

    @property
    def tags(self) -> list[str]:
        return sorted(self._tags.keys())

    def add(self, tag: str, aliases: list[str] | None = None) -> None:
        if tag not in self._tags:
            self._tags[tag] = aliases or []

    def save(self) -> None:
        _write_json(self._path, self._tags)


def load_character_tags(data_dir: str | Path) -> TagCatalogue:
    return TagCatalogue(Path(data_dir) / CHARACTER_TAGS_FILE)


def load_copyright_tags(data_dir: str | Path) -> TagCatalogue:
    return TagCatalogue(Path(data_dir) / COPYRIGHT_TAGS_FILE)


# ── Image scanner ──────────────────────────────────────────────────────────────

def scan_upload_folder(folder: str | Path, dnu_registry: UploadedDNU) -> list[Path]:
    folder = Path(folder)
    pending: list[Path] = []
    skipped_uploaded = 0
    skipped_dnu = 0
    for f in sorted(folder.rglob("*")):
        if f.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if dnu_registry.is_uploaded(folder, f):
            skipped_uploaded += 1
            continue
        if dnu_registry.is_dnu(folder, f):
            skipped_dnu += 1
            continue
        pending.append(f)
    log.debug(
        "scan_upload_folder: %s -> %d pending, %d already uploaded, %d DNU",
        folder, len(pending), skipped_uploaded, skipped_dnu,
    )
    return pending


# ── Tag file lookup ────────────────────────────────────────────────────────────

def find_tag_file(image_path: Path, base: Path) -> Path | None:
    """
    Search for {image_stem}.txt by walking up the directory tree.

    Start from the image's parent directory and search recursively at each
    level. Move one folder higher on each failed attempt. Stop at base.
    """
    target  = image_path.stem + ".txt"
    current = image_path.parent

    while True:
        log.debug("find_tag_file: searching for %s under %s", target, current)
        matches = [p for p in current.rglob(target) if p.is_file()]
        if matches:
            log.debug("find_tag_file: found %s", matches[0])
            return matches[0]
        if current == base or current == current.parent:
            break
        current = current.parent

    log.debug("find_tag_file: not found anywhere up to %s", base)
    return None


def expected_tag_path(image_path: Path, base: Path) -> str:
    """Return a human-readable description of where we searched."""
    try:
        artist_folder = base / image_path.relative_to(base).parts[0]
    except ValueError:
        artist_folder = image_path.parent
    return f"{image_path.stem}.txt  (searched under {artist_folder})"


def read_tag_file(image_path: Path, base: Path) -> list[str]:
    """
    Read the tag file for an image.
    Prepends the artist name (first directory below base).
    Returns [artist_name] if no tag file is found.
    """
    log.debug("read_tag_file: image=%s base=%s", image_path, base)

    try:
        artist = image_path.relative_to(base).parts[0]
    except ValueError:
        artist = None

    tag_file = find_tag_file(image_path, base)

    if tag_file is None:
        return [artist] if artist else []

    tags = [t for t in re.split(r"[\s,]+", tag_file.read_text(encoding="utf-8")) if t.strip()]
    if artist and artist not in tags:
        tags.insert(0, artist)
    log.debug("read_tag_file: %s -> %s", image_path.name, tags)
    return tags


def write_tag_file(image_path: Path, base: Path, tags: list[str]) -> Path:
    """
    Write *tags* to this image's sidecar .txt file.

    If a sidecar already exists anywhere up the tree (per find_tag_file's
    search), overwrite that one so there's never more than one tag file
    per image. Otherwise create a new one at the canonical location:
        base / artist / .tags / site / image.txt
    derived from the image's own path (base / artist / site / [account /] image.ext).

    The artist name is NOT included in the written tags — read_tag_file()
    already prepends it on read, so storing it too would duplicate it.
    """
    existing = find_tag_file(image_path, base)
    if existing is not None:
        target = existing
    else:
        try:
            parts = list(image_path.relative_to(base).parts)
        except ValueError:
            parts = [image_path.parent.name, image_path.parent.name]
        artist = parts[0] if parts else image_path.parent.name
        site   = parts[1] if len(parts) > 1 else "unknown"
        target = base / artist / ".tags" / site / (image_path.stem + ".txt")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(" ".join(tags), encoding="utf-8")
    log.debug("write_tag_file: %s -> %s (tags=%s)", image_path.name, target, tags)
    return target


# ── Source derivation ──────────────────────────────────────────────────────────

def derive_source_from_path(base: Path, image_path: Path) -> str:
    parts = list(image_path.relative_to(base).parts)
    if len(parts) == 2:
        return parts[0]
    if len(parts) == 3:
        return f"{parts[0]} on {parts[1]}"
    if len(parts) >= 4:
        return f"{parts[0]} on {parts[1]} ({parts[2]})"
    return ""


# ── Source URL and description ─────────────────────────────────────────────────

def derive_source_url(image_path: Path, base: Path | None) -> str:
    """
    Derive the original source URL for this image.

    Strategy:
    1. Check gallery-dl sidecar .json for a URL field.
    2. TODO: fall back to per-site URL construction from the file path.
       The path structure is: base/artist/site/[account/]image.ext
       Each site has its own URL format — implement per-site logic here.
    """
    # 1. Try sidecar .json (gallery-dl writes these next to downloads)
    for suffix in (".json",):
        meta = image_path.with_suffix(suffix)
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                for key in ("webpage_url", "post_url", "url", "source"):
                    val = data.get(key)
                    if isinstance(val, str) and val.startswith("http"):
                        return val
            except (json.JSONDecodeError, OSError):
                pass

    # 2. TODO: per-site URL construction from path segments
    # Example structure: base / artist / site / [account /] image.ext
    # Implement one block per site, e.g.:
    #   if site == "pixiv":
    #       return f"https://www.pixiv.net/en/artworks/{account}"
    #   if site == "danbooru":
    #       ...

    return ""


def _description_from_sidecar(image_path: Path) -> str | None:
    """
    Check the gallery-dl sidecar .json next to the image and extract a
    description / caption directly from it if present.

    gallery-dl stores different fields depending on the site:
        Pixiv       → "description" (HTML string)
        Danbooru    → "description"
        Twitter/X   → "content" or "full_text"
        Bluesky     → "description"
        Fanbox      → "body.text" or "body.blocks[].text"
        Patreon     → "content"
        ArtStation  → "description"
        Generic     → "description", "caption", "text", "body"
    """
    meta = image_path.with_suffix(".json")
    if not meta.exists():
        return None

    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    # Flat keys tried in priority order
    for key in ("description", "caption", "content", "full_text", "text", "body"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            log.debug("find_sidecar_description: found in key '%s' (%s)", key, meta.name)
            return val.strip()
        # Fanbox stores body as a dict with nested blocks
        if isinstance(val, dict):
            # Try body.text directly
            text = val.get("text")
            if isinstance(text, str) and text.strip():
                log.debug("find_sidecar_description: found in key '%s.text' (%s)", key, meta.name)
                return text.strip()
            # Try body.blocks[].text (Fanbox article format)
            blocks = val.get("blocks", [])
            if isinstance(blocks, list):
                parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("text")]
                joined = "\n".join(parts).strip()
                if joined:
                    log.debug("find_sidecar_description: found in key '%s.blocks' (%s)", key, meta.name)
                    return joined

    log.debug("find_sidecar_description: no description field found in %s", meta.name)
    return None


def fetch_description(source_url: str, image_path: Path | None = None) -> str:
    """
    Get the post caption / description for an image.

    Strategy:
    1. Check gallery-dl sidecar .json next to the image — no HTTP needed.
    2. Fall back to a live HTTP request to the source URL, using per-site
       API endpoints where available, then a generic meta-tag scrape.

    Returns an empty string on any error or if no caption is found.
    """
    # 1. Sidecar first — fast, no network
    if image_path is not None:
        desc = _description_from_sidecar(image_path)
        if desc is not None:
            return desc

    if not source_url or not source_url.startswith("http"):
        log.debug("fetch_description: no usable source_url (%r), skipping", source_url)
        return ""

    try:
        import requests
        from urllib.parse import urlparse
    except ImportError:
        log.debug("fetch_description: requests not installed, skipping network fetch")
        return ""

    parsed = urlparse(source_url)
    host   = parsed.netloc.lower().lstrip("www.")

    headers = {"User-Agent": "Mozilla/5.0 (compatible; BooruManager/1.0)"}

    try:
        # ── Danbooru-family (e621, danbooru, etc.) ────────────────────────────
        # These expose a JSON API — much more reliable than scraping HTML.
        if any(h in host for h in ("danbooru", "e621", "e926", "e6ai")):
            # Convert post URL to API URL
            # e.g. https://danbooru.donmai.us/posts/123 → /posts/123.json
            api_url = source_url.rstrip("/") + ".json"
            log.debug("fetch_description: danbooru-family API GET %s", api_url)
            r = requests.get(api_url, headers=headers, timeout=15)
            if r.ok:
                data = r.json()
                return (data.get("description") or
                        data.get("post", {}).get("description") or "")

        # ── Pixiv ─────────────────────────────────────────────────────────────
        if "pixiv" in host:
            # Extract illust_id from URL
            import re
            m = re.search(r"/artworks/(\d+)", source_url)
            if m:
                illust_id = m.group(1)
                api_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
                log.debug("fetch_description: pixiv API GET %s", api_url)
                r = requests.get(api_url, headers={**headers, "Referer": "https://www.pixiv.net/"}, timeout=15)
                if r.ok:
                    data = r.json()
                    return data.get("body", {}).get("description") or ""

        # ── Twitter / X ───────────────────────────────────────────────────────
        # Twitter's API requires auth; scraping is unreliable.
        # TODO: implement with a Twitter API key or nitter instance.
        if any(h in host for h in ("twitter.com", "x.com")):
            log.debug("fetch_description: twitter/x not supported, skipping")
            return ""

        # ── Bluesky ───────────────────────────────────────────────────────────
        if "bsky" in host:
            # Convert bsky.app post URL to AT protocol API call
            import re
            m = re.search(r"/profile/([^/]+)/post/([^/?]+)", source_url)
            if m:
                handle, rkey = m.group(1), m.group(2)
                api_url = (
                    f"https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
                    f"?uri=at://{handle}/app.bsky.feed.post/{rkey}&depth=0"
                )
                log.debug("fetch_description: bluesky API GET %s", api_url)
                r = requests.get(api_url, headers=headers, timeout=15)
                if r.ok:
                    thread = r.json().get("thread", {})
                    post   = thread.get("post", {})
                    record = post.get("record", {})
                    return record.get("text") or ""

        # ── Fanbox ────────────────────────────────────────────────────────────
        if "fanbox" in host:
            # TODO: requires Fanbox session cookie for paid posts.
            log.debug("fetch_description: fanbox requires auth, skipping")
            return ""

        # ── Patreon ───────────────────────────────────────────────────────────
        if "patreon" in host:
            # TODO: requires Patreon auth token for paywalled posts.
            log.debug("fetch_description: patreon requires auth, skipping")
            return ""

        # ── Generic HTML fallback ─────────────────────────────────────────────
        # Try to extract a description meta tag or common caption selectors.
        log.debug("fetch_description: generic HTML fallback GET %s", source_url)
        r = requests.get(source_url, headers=headers, timeout=15)
        if not r.ok:
            log.debug("fetch_description: %s returned HTTP %s", source_url, r.status_code)
            return ""

        try:
            from html.parser import HTMLParser

            class _MetaParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.description = ""

                def handle_starttag(self, tag, attrs):
                    if tag == "meta":
                        d = dict(attrs)
                        if d.get("name", "").lower() in ("description", "og:description"):
                            self.description = d.get("content", "")

            p = _MetaParser()
            p.feed(r.text)
            return p.description
        except Exception:
            return ""

    except Exception as e:
        log.warning("fetch_description: error for %s: %s", source_url, e)
        return ""