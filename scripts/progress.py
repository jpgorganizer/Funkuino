#!/usr/bin/env python3
"""Opt-in download-progress reporting for the intake pipeline.

``download.py`` (and wrappers built on it) is battle-tested; it changes behaviour ONLY
when the env var ``FUNKUINO_PROGRESS_FILE`` is set (Funkuino Studio sets it per
agent session). When it is set, the current download status is written to that
JSON file -- atomic tmp+replace, throttled to ~2/s -- so Studio can render a
live progress bar:

    {"phase": "probe"|"download"|"merge"|"done", "item": i, "total": n,
     "title": "<current track>", "bytes": b, "bytesTotal": t|null, "ts": ...}

When the env var is absent, everything here is inert (no file, no overhead).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ENV_VAR = "FUNKUINO_PROGRESS_FILE"
_MIN_INTERVAL = 0.5   # seconds between file writes (~2/s), except forced updates


class Progress:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.snap: dict = {"phase": "probe", "item": 0, "total": 0, "title": "",
                           "bytes": 0, "bytesTotal": None, "ts": 0.0}
        self._last = 0.0

    @property
    def active(self) -> bool:
        return self.path is not None

    def update(self, force: bool = False, **fields) -> None:
        if not self.path:
            return
        self.snap.update(fields)
        self.snap["ts"] = time.time()
        now = time.monotonic()
        if force or now - self._last >= _MIN_INTERVAL:
            self._last = now
            self._write()

    def _write(self) -> None:
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.snap))
            tmp.replace(self.path)          # atomic: reader never sees a partial
        except OSError:
            pass  # progress is best-effort; never break a download over it

    # -- convenience: phase/item flush immediately; bytes are throttled --
    def phase(self, phase: str) -> None:
        self.update(force=True, phase=phase)

    def item(self, i: int, total: int, title: str = "") -> None:
        self.update(force=True, item=i, total=total, title=title,
                    bytes=0, bytesTotal=None)

    def hook(self, d: dict) -> None:
        """A yt_dlp progress hook (``status`` downloading/finished). Reads the
        item index/total/title from the entry's info_dict so album/audiobook
        downloads self-report which track is in flight."""
        if not self.path:
            return
        info = d.get("info_dict") or {}
        fields: dict = {}
        idx = info.get("playlist_index") or info.get("autonumber")
        if idx:
            fields["item"] = idx
        total = info.get("n_entries") or info.get("playlist_count")
        if total:
            fields["total"] = total
        title = info.get("track") or info.get("title")
        if title:
            fields["title"] = str(title)
        status = d.get("status")
        if status == "downloading":
            fields["bytes"] = d.get("downloaded_bytes", 0)
            fields["bytesTotal"] = d.get("total_bytes") or d.get("total_bytes_estimate")
            self.update(**fields)
        elif status == "finished":
            if d.get("downloaded_bytes"):   # this file reached 100%
                fields["bytes"] = d["downloaded_bytes"]
                fields["bytesTotal"] = d["downloaded_bytes"]
            self.update(force=True, **fields)


def get() -> Progress:
    """A Progress bound to ``$FUNKUINO_PROGRESS_FILE``, or inert if unset."""
    return Progress(os.environ.get(ENV_VAR))
