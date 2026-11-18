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

Tags (standard e621ng; tags themselves cannot be created or deleted through the API):
    GET    /tags.json?search[name_matches]=...          find / search tags
    PUT    /tags/{id}.json          {"tag": {"category": 5}}
    GET    /tag_aliases.json?search[consequent_name]=x  synonyms of x
    POST   /tag_aliases.json        {"tag_alias": {"antecedent_name", "consequent_name"}}
    DELETE /tag_aliases/{id}.json
    GET    /tag_implications.json?search[antecedent_name]=x
    POST   /tag_implications.json   {"tag_implication": {"antecedent_name", "consequent_name"}}
    DELETE /tag_implications/{id}.json
    GET/POST/PUT /wiki_pages(.json)  {"wiki_page": {"title", "body"}}
Only the parameters the server permits are sent (it answers 403 otherwise).
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


# e621 tag category ids <-> our type names (6 = "invalid" is read as general)
CATEGORY_ID = {"general": 0, "artist": 1, "copyright": 3, "character": 4,
               "species": 5, "meta": 7, "lore": 8}
CATEGORY_NAME = {**{v: k for k, v in CATEGORY_ID.items()}, 6: "general"}
# Alias / implication statuses that mean "exists or is waiting for approval"
_LIVE = {"active", "pending", "queued", "processing", "approved"}


def _items(data: Any, key: str) -> list[dict[str, Any]]:
    """List endpoints answer with a bare list, or an object holding it under `key`."""
    if isinstance(data, dict):
        data = data.get(key, [])
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


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


    # ── Tags ──────────────────────────────────────────────────────────────────

    def _get_list(self, path: str, key: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        resp = self._check(self._request("GET", path, params=params), (200,))
        return _items(resp.json(), key)

    def find_tag(self, name: str) -> dict[str, Any] | None:
        """The server's tag called exactly `name` (None when it has none)."""
        rows = self._get_list("/tags.json", "tags", {
            "search[name_matches]": name, "search[hide_empty]": "false", "limit": 20})
        return next((r for r in rows if norm_name(str(r.get("name", ""))) == name), None)

    def search_tags(self, pattern: str, limit: int = 50) -> list[dict[str, Any]]:
        """Tags matching `pattern` ("*" wildcards), most used first."""
        return self._get_list("/tags.json", "tags", {
            "search[name_matches]": pattern, "search[hide_empty]": "false",
            "search[order]": "count", "limit": min(max(1, limit), 320)})

    def set_tag_category(self, remote_id: int, category: str) -> None:
        resp = self._request("PUT", f"/tags/{remote_id}.json",
                             json={"tag": {"category": CATEGORY_ID[category]}})
        self._check(resp, (200, 204))

    def list_aliases(self, name: str) -> dict[str, int]:
        """{synonym: alias id} of the live aliases pointing at `name`."""
        rows = self._get_list("/tag_aliases.json", "tag_aliases", {
            "search[consequent_name]": name, "limit": 320})
        return {norm_name(str(r["antecedent_name"])): int(r["id"]) for r in rows
                if str(r.get("status", "active")).split(":")[0] in _LIVE and "id" in r}

    def list_implications(self, name: str) -> dict[str, int]:
        """{implied tag: implication id} of the live implications starting at `name`."""
        rows = self._get_list("/tag_implications.json", "tag_implications", {
            "search[antecedent_name]": name, "limit": 320})
        return {norm_name(str(r["consequent_name"])): int(r["id"]) for r in rows
                if str(r.get("status", "active")).split(":")[0] in _LIVE and "id" in r}

    def create_alias(self, synonym: str, name: str) -> None:
        self._check(self._request("POST", "/tag_aliases.json", json={
            "tag_alias": {"antecedent_name": synonym, "consequent_name": name}}), (200, 201))

    def create_implication(self, name: str, implied: str) -> None:
        self._check(self._request("POST", "/tag_implications.json", json={
            "tag_implication": {"antecedent_name": name, "consequent_name": implied}}), (200, 201))

    def delete_alias(self, alias_id: int) -> None:
        resp = self._request("DELETE", f"/tag_aliases/{alias_id}.json")
        if resp.status_code != 404:
            self._check(resp, (200, 204))

    def delete_implication(self, implication_id: int) -> None:
        resp = self._request("DELETE", f"/tag_implications/{implication_id}.json")
        if resp.status_code != 404:
            self._check(resp, (200, 204))

    def find_wiki(self, name: str) -> dict[str, Any] | None:
        rows = self._get_list("/wiki_pages.json", "wiki_pages", {
            "search[title]": name, "limit": 5})
        return next((r for r in rows if norm_name(str(r.get("title", ""))) == name), None)

    def save_wiki(self, name: str, body: str, page_id: int | None = None) -> None:
        payload = {"wiki_page": {"title": name, "body": body}}
        if page_id is None:
            self._check(self._request("POST", "/wiki_pages.json", json=payload), (200, 201))
        else:
            self._check(self._request("PUT", f"/wiki_pages/{page_id}.json", json=payload),
                        (200, 204))
