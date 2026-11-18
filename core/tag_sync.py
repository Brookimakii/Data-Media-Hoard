"""
core/tag_sync.py
----------------
Automatic push of tag changes to the booru server, plus "fetch" (pull tags from it),
both in a background thread. Same shape as core/artist_sync.py:

    store mutation (create / edit / delete / undo / redo / import)
        └─ page calls sync.request_pending()
              └─ worker pushes every tag whose status is "pending"
                    ├─ ok      -> store.mark_pushed()  -> "synced"
                    └─ failed  -> store.mark_failed()  -> "failed" (+ message, Retry button)

A push reads the server first and only sends the differences:
    * type          set on the server's existing tag (e621ng cannot create tags: a tag
                    exists once a post uses it)
    * synonyms      tag aliases  (synonym -> this tag); removed ones are deleted
    * implications  tag implications (this tag -> implied tag); removed ones are deleted
    * wiki          the wiki page titled with the tag name
Aliases and implications can wait for approval on the server; they count as pushed.
Deleting a tag removes the aliases / implications this app pushed; the tag and its wiki
page stay on the server (the API cannot delete them).

The worker never touches Tk; the UI polls `poll()` from its own timer.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from core.booru_client import CATEGORY_NAME, BooruClient, BooruError, norm_name
from core.booru_settings import BooruSettings, load_settings
from core.tag_store import TagError, TagStore, plan

log = logging.getLogger(__name__)

_GAP_S = 0.3


@dataclass
class SyncResult:
    tag_id: int
    ok: bool
    message: str = ""


@dataclass
class FetchResult:
    ok: bool
    message: str = ""
    summary: dict = field(default_factory=dict)


class TagSync:
    def __init__(self, store: TagStore,
                 settings_loader: Callable[[], BooruSettings] = load_settings,
                 client_factory: Callable[[BooruSettings], BooruClient] = BooruClient,
                 gap: float = _GAP_S) -> None:
        self._store = store
        self._load_settings = settings_loader
        self._make_client = client_factory
        self._gap = gap
        self._jobs: queue.Queue[int] = queue.Queue()
        self._results: queue.Queue[SyncResult] = queue.Queue()
        self._fetches: queue.Queue[FetchResult] = queue.Queue()
        self._lock = threading.Lock()
        self._queued: set[int] = set()
        self._active: int | None = None
        self._thread: threading.Thread | None = None
        self._fetching = False
        self._progress = (0, 0)

    # ── Public API (called from the UI thread) ────────────────────────────────

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="tag-sync", daemon=True)
            self._thread.start()

    def settings(self) -> BooruSettings:
        return self._load_settings()

    def request_pending(self) -> int:
        """Queue every pending tag if auto-push is on and a login is set."""
        s = self._load_settings()
        if not (s.configured and s.auto_push):
            return 0
        return self._enqueue(self._store.pending_ids())

    def push_now(self) -> int:
        if not self._load_settings().configured:
            return 0
        return self._enqueue(self._store.pending_ids())

    def retry(self, tag_id: int) -> None:
        self._store.clear_error(tag_id)
        self._enqueue([tag_id])

    def retry_failed(self) -> int:
        ids = self._store.failed_ids()
        for i in ids:
            self._store.clear_error(i)
        return self._enqueue(ids)

    def syncing_ids(self) -> set[int]:
        with self._lock:
            ids = set(self._queued)
            if self._active is not None:
                ids.add(self._active)
            return ids

    def poll(self) -> list[SyncResult]:
        out: list[SyncResult] = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                return out

    # fetch ---------------------------------------------------------------------

    def fetch(self, pattern: str, limit: int = 50) -> bool:
        """Start fetching tags matching `pattern` in the background.

        False when a fetch is already running or no login is set.
        """
        if not self._load_settings().configured:
            self._fetches.put(FetchResult(False, "Server login not set. Open ⚙ settings."))
            return False
        with self._lock:
            if self._fetching:
                return False
            self._fetching = True
            self._progress = (0, 0)
        threading.Thread(target=self._fetch_run, args=(pattern, limit),
                         name="tag-fetch", daemon=True).start()
        return True

    @property
    def fetching(self) -> bool:
        return self._fetching

    def fetch_progress(self) -> tuple[int, int]:
        return self._progress

    def poll_fetch(self) -> list[FetchResult]:
        out: list[FetchResult] = []
        while True:
            try:
                out.append(self._fetches.get_nowait())
            except queue.Empty:
                return out

    # ── Worker ────────────────────────────────────────────────────────────────

    def _enqueue(self, ids: list[int]) -> int:
        n = 0
        with self._lock:
            for i in ids:
                if i not in self._queued and i != self._active:
                    self._queued.add(i)
                    self._jobs.put(i)
                    n += 1
        if n:
            self.start()
        return n

    def _run(self) -> None:
        while True:
            tag_id = self._jobs.get()
            with self._lock:
                self._queued.discard(tag_id)
                self._active = tag_id
            try:
                result = self.push_one(tag_id)
            except Exception as e:                       # never let the worker die
                log.exception("tag sync: unexpected error on #%s", tag_id)
                self._store.mark_failed(tag_id, f"Unexpected error: {e}")
                result = SyncResult(tag_id, False, f"Unexpected error: {e}")
            with self._lock:
                self._active = None
            self._results.put(result)
            time.sleep(self._gap)

    def push_one(self, tag_id: int) -> SyncResult:
        """Push one tag (blocking). Also usable directly, e.g. in tests."""
        try:
            local, _rid, pushed = self._store.sync_view(tag_id)
        except TagError as e:
            return SyncResult(tag_id, False, str(e))

        action = plan(local, pushed)
        if action == "noop":
            return SyncResult(tag_id, True, "Already in sync.")

        settings = self._load_settings()
        if not settings.configured:
            msg = "Server login not set. Open ⚙ settings."
            self._store.mark_failed(tag_id, msg)
            return SyncResult(tag_id, False, msg)

        client = self._make_client(settings)
        try:
            if action == "delete":
                new_remote, note = self._delete(client, pushed)
            else:
                new_remote, note = self._push(client, local, pushed)
        except BooruError as e:
            log.warning("tag sync: #%s %s failed: %s", tag_id, action, e)
            self._store.mark_failed(tag_id, str(e))
            return SyncResult(tag_id, False, str(e))

        if self._store.mark_pushed(tag_id, new_remote, local):   # edited while pushing
            self._enqueue([tag_id])
        return SyncResult(tag_id, True, note or f"{action} ok")

    # ── The actual server work ────────────────────────────────────────────────

    @staticmethod
    def _push(client: BooruClient, local: dict, pushed: dict | None) -> tuple[int | None, str]:
        name = local["name"]
        was = pushed if pushed and not pushed.get("deleted") else {}
        remote = client.find_tag(name)
        rid = int(remote["id"]) if remote else None

        # type
        if remote is not None:
            if CATEGORY_NAME.get(int(remote.get("category", 0)), "general") != local["category"]:
                client.set_tag_category(rid, local["category"])
        elif local["category"] != "general":
            raise BooruError(
                f"The server has no tag '{name}' yet (a tag appears once a post uses it), "
                f"so its type '{local['category']}' cannot be set.")

        # synonyms and implications: send the differences only
        have_a = client.list_aliases(name)
        for syn in local["aliases"]:
            if syn not in have_a:
                client.create_alias(syn, name)
        for syn in set(was.get("aliases", [])) - set(local["aliases"]):
            if syn in have_a:
                client.delete_alias(have_a[syn])

        have_i = client.list_implications(name)
        for imp in local["implies"]:
            if imp not in have_i:
                client.create_implication(name, imp)
        for imp in set(was.get("implies", [])) - set(local["implies"]):
            if imp in have_i:
                client.delete_implication(have_i[imp])

        # wiki
        if local["wiki"].strip():
            page = client.find_wiki(name)
            if page is None:
                client.save_wiki(name, local["wiki"])
            elif str(page.get("body", "")).strip() != local["wiki"].strip():
                client.save_wiki(name, local["wiki"], int(page["id"]))

        # read back
        have_a, have_i = client.list_aliases(name), client.list_implications(name)
        missing = ([f"synonym {s}" for s in local["aliases"] if s not in have_a] +
                   [f"implication {i}" for i in local["implies"] if i not in have_i])
        if missing:
            raise BooruError("The server did not keep: " + ", ".join(missing) + ".")
        return rid, ""

    @staticmethod
    def _delete(client: BooruClient, pushed: dict | None) -> tuple[int | None, str]:
        """Remove what this app pushed. The tag itself and its wiki stay (no API for that)."""
        was = pushed or {}
        name = was.get("name", "")
        if name:
            have_a, have_i = client.list_aliases(name), client.list_implications(name)
            for syn in was.get("aliases", []):
                if syn in have_a:
                    client.delete_alias(have_a[syn])
            for imp in was.get("implies", []):
                if imp in have_i:
                    client.delete_implication(have_i[imp])
        return None, "synonyms and implications removed (the tag itself stays on the server)"

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _fetch_run(self, pattern: str, limit: int) -> None:
        try:
            self._fetches.put(self._fetch(pattern, limit))
        except Exception as e:
            log.exception("tag fetch failed")
            self._fetches.put(FetchResult(False, f"Unexpected error: {e}"))
        finally:
            self._fetching = False

    def _fetch(self, pattern: str, limit: int) -> FetchResult:
        client = self._make_client(self._load_settings())
        pattern = pattern.strip() or "*"
        try:
            found = client.search_tags(pattern, limit)
            self._progress = (0, len(found))
            items = []
            for n, t in enumerate(found, 1):
                name = norm_name(str(t.get("name", "")))
                if not name:
                    continue
                page = client.find_wiki(name)
                items.append({
                    "name": name,
                    "category": CATEGORY_NAME.get(int(t.get("category", 0)), "general"),
                    "wiki": str(page.get("body", "")) if page else "",
                    "aliases": sorted(client.list_aliases(name)),
                    "implies": sorted(client.list_implications(name)),
                    "remote_id": int(t["id"]) if t.get("id") is not None else None,
                })
                self._progress = (n, len(found))
                time.sleep(self._gap / 3)
        except BooruError as e:
            return FetchResult(False, str(e))
        if not found:
            return FetchResult(True, f"No tag on the server matches '{pattern}'.", {})
        summary = self._store.import_remote(items)
        return FetchResult(True, self.describe(summary, len(found)), summary)

    @staticmethod
    def describe(r: dict, total: int) -> str:
        parts = [f"{r['added']} new", f"{r['updated']} updated", f"{r['same']} unchanged"]
        if r["kept"]:
            parts.append(f"{r['kept']} kept (local edits not pushed yet)")
        if r["skipped"]:
            parts.append(f"{r['skipped']} skipped")
        return f"Fetched {total} tag(s): " + ", ".join(parts) + "."
