"""
core/artist_sync.py
-------------------
Automatic push of artist changes to the booru server, in a background thread.

    store mutation (create / edit / delete / undo / redo / import)
        └─ page calls sync.request_pending()
              └─ worker pushes every artist whose status is "pending"
                    ├─ ok      -> store.mark_pushed()  -> status "synced"
                    └─ failed  -> store.mark_failed()  -> status "failed" (+ message)
                                  the Retry button calls sync.retry(id) / retry_failed()

The worker never touches Tk. The UI polls `poll()` from its own timer and
repaints when something finished.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from core.artist_store import ArtistError, ArtistStore, plan
from core.booru_client import BooruClient, BooruError
from core.booru_settings import BooruSettings, load_settings

log = logging.getLogger(__name__)

_GAP_S = 0.3          # pause between two pushes: be gentle with the server


@dataclass
class SyncResult:
    artist_id: int
    ok: bool
    message: str = ""


class ArtistSync:
    def __init__(self, store: ArtistStore,
                 settings_loader: Callable[[], BooruSettings] = load_settings,
                 client_factory: Callable[[BooruSettings], BooruClient] = BooruClient,
                 gap: float = _GAP_S) -> None:
        self._store = store
        self._load_settings = settings_loader
        self._make_client = client_factory
        self._gap = gap
        self._jobs: queue.Queue[int] = queue.Queue()
        self._results: queue.Queue[SyncResult] = queue.Queue()
        self._lock = threading.Lock()
        self._queued: set[int] = set()
        self._active: int | None = None
        self._thread: threading.Thread | None = None

    # ── Public API (called from the UI thread) ────────────────────────────────

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="artist-sync", daemon=True)
            self._thread.start()

    def settings(self) -> BooruSettings:
        return self._load_settings()

    def request_pending(self) -> int:
        """Queue every pending artist if auto-push is on and a login is set.

        Returns how many were queued.
        """
        s = self._load_settings()
        if not (s.configured and s.auto_push):
            return 0
        return self._enqueue(self._store.pending_ids())

    def push_now(self) -> int:
        """Queue pending artists regardless of the auto-push switch."""
        if not self._load_settings().configured:
            return 0
        return self._enqueue(self._store.pending_ids())

    def retry(self, artist_id: int) -> None:
        self._store.clear_error(artist_id)
        self._enqueue([artist_id])

    def retry_failed(self) -> int:
        ids = self._store.failed_ids()
        for i in ids:
            self._store.clear_error(i)
        return self._enqueue(ids)

    def syncing_ids(self) -> set[int]:
        """Ids waiting in the queue or being pushed right now."""
        with self._lock:
            ids = set(self._queued)
            if self._active is not None:
                ids.add(self._active)
            return ids

    def poll(self) -> list[SyncResult]:
        """Results finished since the last call (non-blocking)."""
        out: list[SyncResult] = []
        while True:
            try:
                out.append(self._results.get_nowait())
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
            artist_id = self._jobs.get()
            with self._lock:
                self._queued.discard(artist_id)
                self._active = artist_id
            try:
                result = self.push_one(artist_id)
            except Exception as e:                       # never let the worker die
                log.exception("artist sync: unexpected error on #%s", artist_id)
                self._store.mark_failed(artist_id, f"Unexpected error: {e}")
                result = SyncResult(artist_id, False, f"Unexpected error: {e}")
            with self._lock:
                self._active = None
            self._results.put(result)
            time.sleep(self._gap)

    def push_one(self, artist_id: int) -> SyncResult:
        """Push one artist (blocking). Also usable directly, e.g. in tests."""
        try:
            local, remote_id, pushed = self._store.sync_view(artist_id)
        except ArtistError as e:
            return SyncResult(artist_id, False, str(e))

        action = plan(local, remote_id, pushed)
        if action == "noop":
            return SyncResult(artist_id, True, "Already in sync.")

        settings = self._load_settings()
        if not settings.configured:
            msg = "Server login not set. Open ⚙ settings."
            self._store.mark_failed(artist_id, msg)
            return SyncResult(artist_id, False, msg)

        client = self._make_client(settings)
        try:
            new_remote = self._apply(client, action, local, remote_id)
            if action != "delete" and new_remote is not None:
                self._verify(client, new_remote, local)
        except BooruError as e:
            log.warning("artist sync: #%s %s failed: %s", artist_id, action, e)
            self._store.mark_failed(artist_id, str(e))
            return SyncResult(artist_id, False, str(e))

        still_pending = self._store.mark_pushed(artist_id, new_remote, local)
        result = SyncResult(artist_id, True, f"{action} ok")
        if still_pending:                 # edited again while pushing: go round once more
            self._enqueue([artist_id])
        return result

    @staticmethod
    def _apply(client: BooruClient, action: str, local: dict, remote_id: int | None) -> int | None:
        """Perform `action`; returns the artist's remote id afterwards."""
        if action == "create":
            return client.create_artist(local)
        if action == "update":
            if not client.update_artist(remote_id, local):       # vanished on the server
                return client.create_artist(local)
            return remote_id
        if action == "restore":
            if remote_id is not None and client.update_artist(remote_id, local):
                return remote_id
            return client.create_artist(local)
        if action == "delete":
            client.delete_artist(remote_id)
            return remote_id
        raise BooruError(f"Unknown sync action '{action}'.")

    @staticmethod
    def _verify(client: BooruClient, remote_id: int, local: dict) -> None:
        """Read the artist back and make sure the server kept what we sent."""
        remote = client.get_artist(remote_id)
        if remote is None:
            raise BooruError("The server does not list the artist right after saving it.")
        bad = client.differences(local, remote)
        if bad:
            raise BooruError("The server stored different values for: " + ", ".join(bad) + ".")
