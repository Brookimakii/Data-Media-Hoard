"""
core/booru_client.py
--------------------
Thin HTTP client for the artist endpoints of an e621ng instance.

Everything that depends on the server's API shape lives here (and in
`artist_params`), so adjusting it after testing against the real instance
means editing this file only.

Requests are JSON with HTTP basic auth (username + API key):
    POST   /artists.json        {"artist": {...}}   -> artist JSON (with "id")
    PUT    /artists/{id}.json   {"artist": {...}}   -> 204 / artist JSON
    DELETE /artists/{id}.json                       -> 204
    GET    /artists/{id}.json                       -> artist JSON (read-back check)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from core.booru_settings import BooruSettings

log = logging.getLogger(__name__)

_USER_AGENT = "BooruManager/1.0"


class BooruError(Exception):
    """A push failed. `str(e)` is shown to the user next to the Retry button."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def norm_name(name: str) -> str:
    """e621-style name: lowercase, spaces become underscores."""
    return re.sub(r"\s+", "_", name.strip()).lower()


def artist_params(snap: dict[str, Any]) -> dict[str, Any]:
    """Local snapshot -> the `artist` object sent to the server.

    Only parameters the server permits (it answers 403 "unpermitted parameters"
    otherwise): is_active is local-only, other names go as other_names_string.
    """
    other = [norm_name(n) for n in snap.get("other_names", []) if n.strip()]
    return {
        "name": norm_name(snap["name"]),
        "other_names_string": " ".join(other),
        "group_name": norm_name(snap.get("group_name", "")),
        "url_string": "\n".join(snap.get("urls", [])),
        "notes": snap.get("notes", ""),
    }


def _error_text(resp: requests.Response) -> str:
    """Best short explanation from an error response."""
    try:
        data = resp.json()
    except ValueError:
        data = None
    msg = ""
    if isinstance(data, dict):
        for key in ("reason", "message", "error", "errors", "msg"):
            if data.get(key):
                msg = data[key] if isinstance(data[key], str) else str(data[key])
                break
    if not msg:
        msg = (resp.text or "").strip().replace("\n", " ")
    return f"HTTP {resp.status_code}: {msg[:200]}" if msg else f"HTTP {resp.status_code}"


class BooruClient:
    def __init__(self, settings: BooruSettings, session: requests.Session | None = None,
                 timeout: float = 15, sleep=time.sleep) -> None:
        self._base = settings.server.strip().rstrip("/")
        self._auth = (settings.username, settings.api_key)
        self._session = session or requests.Session()
        self._timeout = timeout
        self._sleep = sleep

    # ── Low level ─────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kw: Any) -> requests.Response:
        url = self._base + path
        for attempt in (1, 2):
            try:
                resp = self._session.request(
                    method, url, auth=self._auth, timeout=self._timeout,
                    headers={"User-Agent": _USER_AGENT}, **kw)
            except requests.ConnectionError as e:
                raise BooruError(f"Cannot reach {self._base} ({type(e).__name__}).") from e
            except requests.Timeout as e:
                raise BooruError(f"{self._base} did not answer in {self._timeout:g}s.") from e
            except requests.RequestException as e:
                raise BooruError(f"Network error: {e}") from e

            if resp.status_code == 429 and attempt == 1:          # rate limited: wait once
                try:
                    wait = min(float(resp.headers.get("Retry-After", "2")), 30.0)
                except ValueError:
                    wait = 2.0
                log.info("Rate limited, waiting %.1fs", wait)
                self._sleep(wait)
                continue
            return resp
        return resp                                                # pragma: no cover

    def _check(self, resp: requests.Response, ok: tuple[int, ...]) -> requests.Response:
        if resp.status_code in ok:
            return resp
        if resp.status_code in (401, 403):
            raise BooruError(f"Login refused ({_error_text(resp)}). Check username and API key "
                             "in the ⚙ settings.", resp.status_code)
        raise BooruError(_error_text(resp), resp.status_code)

    # ── Account ───────────────────────────────────────────────────────────────

    def whoami(self) -> dict[str, Any]:
        """The logged-in account (raises BooruError when the login does not work)."""
        resp = self._check(self._request(
            "GET", "/users.json",
            params={"search[name_matches]": self._auth[0], "limit": 1}), (200,))
        data = resp.json()
        users = data if isinstance(data, list) else data.get("users", [])
        if not users:
            raise BooruError(f"No account named '{self._auth[0]}' on this server.")
        return users[0]

    # ── Artists ───────────────────────────────────────────────────────────────

    def create_artist(self, snap: dict[str, Any]) -> int:
        resp = self._check(self._request(
            "POST", "/artists.json", json={"artist": artist_params(snap)}), (200, 201))
        try:
            return int(resp.json()["id"])
        except (ValueError, KeyError, TypeError) as e:
            raise BooruError("The server created the artist but returned no id.") from e

    def update_artist(self, remote_id: int, snap: dict[str, Any]) -> bool:
        """False when the artist no longer exists on the server (404)."""
        resp = self._request("PUT", f"/artists/{remote_id}.json",
                             json={"artist": artist_params(snap)})
        if resp.status_code == 404:
            return False
        self._check(resp, (200, 204))
        return True

    def delete_artist(self, remote_id: int) -> None:
        resp = self._request("DELETE", f"/artists/{remote_id}.json")
        if resp.status_code != 404:          # already gone counts as deleted
            self._check(resp, (200, 204))

    def get_artist(self, remote_id: int) -> dict[str, Any] | None:
        resp = self._request("GET", f"/artists/{remote_id}.json")
        if resp.status_code == 404:
            return None
        self._check(resp, (200,))
        return resp.json()

    # ── Read-back check ───────────────────────────────────────────────────────

    @staticmethod
    def differences(snap: dict[str, Any], remote: dict[str, Any]) -> list[str]:
        """Fields the server stored differently from what we sent.

        Only fields present in the server's answer are compared, and names are
        compared in their normalised form.
        """
        want = artist_params(snap)
        bad: list[str] = []
        if "name" in remote and norm_name(str(remote["name"])) != want["name"]:
            bad.append("name")
        if "group_name" in remote and norm_name(str(remote["group_name"] or "")) != want["group_name"]:
            bad.append("group_name")
        if isinstance(remote.get("other_names"), list):
            got = {norm_name(str(n)) for n in remote["other_names"]}
            if got != set(want["other_names_string"].split()):
                bad.append("other_names")
        return bad