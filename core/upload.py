"""
core/upload.py
--------------
Upload a single image to an e621ng instance via its API.

Completely decoupled from the UI — takes plain Python values,
returns a result dict. Callers decide how to surface errors.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Result type ────────────────────────────────────────────────────────────────

class UploadResult:
    """Returned by upload_image() regardless of success or failure."""

    def __init__(
        self,
        success: bool,
        post_id: int | None = None,
        message: str = "",
        raw: dict | None = None,
    ) -> None:
        self.success = success
        self.post_id = post_id
        self.message = message
        self.raw     = raw or {}

    def __repr__(self) -> str:
        if self.success:
            return f"<UploadResult OK post_id={self.post_id}>"
        return f"<UploadResult FAIL: {self.message}>"


# ── Uploader ───────────────────────────────────────────────────────────────────

def upload_image(
    *,
    server:      str,
    username:    str,
    api_key:     str,
    image_path:  Path,
    tags:        list[str],
    rating:      str,
    source:      str,
    description: str = "",
) -> UploadResult:
    """
    Upload a single image to an e621ng instance.

    Parameters
    ----------
    server      : base URL of the instance, e.g. "http://localhost:3000"
    username    : e621ng account username
    api_key     : e621ng API key
    image_path  : path to the image file
    tags        : list of tag strings (will be space-joined)
    rating      : "s" | "q" | "e"  (safe / questionable / explicit)
    source      : source URL or descriptive string
    description : optional post description

    Returns an UploadResult with success flag, post_id on success,
    and a human-readable message.
    """
    try:
        import requests
    except ImportError:
        log.error("upload_image: 'requests' is not installed")
        return UploadResult(
            success=False,
            message="'requests' is not installed. Run: pip install requests",
        )

    endpoint = server.rstrip("/") + "/uploads.json"

    payload: dict[str, Any] = {
        "upload[tag_string]":   " ".join(tags),
        "upload[rating]":       rating,
        "upload[source]":       source,
        "upload[description]":  description,
        "upload[parent_id]":    "",
        "upload[locked_tags]":  "",
        "upload[locked_rating]": "false",
        "upload[as_pending]":   "false",
    }

    mime, _ = mimetypes.guess_type(str(image_path))
    mime = mime or "application/octet-stream"

    log.debug(
        "upload_image: POST %s file=%s user=%s rating=%s tags=%d source=%s",
        endpoint, image_path.name, username, rating, len(tags), source or "-",
    )

    try:
        with image_path.open("rb") as img_file:
            files = {"upload[file]": (image_path.name, img_file, mime)}
            response = requests.post(
                endpoint,
                data=payload,
                files=files,
                auth=(username, api_key),
                timeout=60,
            )

        if response.status_code in (200, 201):
            data    = response.json()
            post_id = data.get("post_id") or data.get("id")
            log.debug("upload_image: success, post #%s (%s)", post_id, image_path.name)
            return UploadResult(
                success=True,
                post_id=post_id,
                message=f"Uploaded successfully (post #{post_id})",
                raw=data,
            )

        # e621ng returns errors as JSON {"reason": "..."} or {"errors": {...}}
        try:
            err_data = response.json()
            reason   = (
                err_data.get("reason")
                or str(err_data.get("errors", response.text))
            )
        except Exception:
            reason = response.text

        log.warning(
            "upload_image: HTTP %s for %s: %s",
            response.status_code, image_path.name, reason,
        )
        return UploadResult(
            success=False,
            message=f"HTTP {response.status_code}: {reason}",
            raw={"status_code": response.status_code, "body": reason},
        )

    except requests.exceptions.ConnectionError as e:
        log.warning("upload_image: connection error for %s: %s", image_path.name, e)
        return UploadResult(success=False, message=f"Connection error: {e}")
    except requests.exceptions.Timeout:
        log.warning("upload_image: timed out for %s", image_path.name)
        return UploadResult(success=False, message="Request timed out.")
    except OSError as e:
        log.warning("upload_image: file error for %s: %s", image_path.name, e)
        return UploadResult(success=False, message=f"File error: {e}")
    except Exception as e:
        log.error("upload_image: unexpected error for %s: %s", image_path.name, e)
        return UploadResult(success=False, message=f"Unexpected error: {e}")


# ── Rating helpers ─────────────────────────────────────────────────────────────

RATING_OPTIONS = [
    ("Safe",         "s"),
    ("Questionable", "q"),
    ("Explicit",     "e"),
]

def rating_label_to_code(label: str) -> str:
    """Convert display label to API code. Falls back to 's'."""
    for lbl, code in RATING_OPTIONS:
        if lbl.lower() == label.lower():
            return code
    return "s"