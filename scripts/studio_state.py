#!/usr/bin/env python3
"""Library scan for Funkuino Studio: derive the per-card "unit" model + status.

A **unit** is one RFID card. This walks the top level of ``files/`` and turns it
into a flat list of units, each annotated with the four pipeline states the
dashboard shows: whether a cover exists (Download/Cover), whether it is on the
device (Sync, against the chosen upload manifest), and whether it has been laid
out for printing (Druck, against the print manifest).

The module is deliberately pure/synchronous and import-only: the studio backend
runs :func:`scan` in an executor thread. It reuses the same helpers the rest of
the repo uses (``espuino.join_remote``/``is_ignored``, ``sync_state``,
``print_state``) so a unit's derived status matches what ``./sync`` and
``./cards`` would actually do.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import espuino
from print_state import PrintState, _key
from sync_state import SyncState


def pick_manifest(state_dir: str | Path) -> tuple[SyncState | None, dict | None]:
    """The newest upload manifest in ``state_dir`` (there is one per device).

    Returns ``(state, meta)`` where meta is ``{host, deviceId, fileMtime}``, or
    ``(None, None)`` if no manifest exists yet (fresh checkout / never synced)."""
    state_dir = Path(state_dir)
    candidates = sorted(state_dir.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, None
    newest = candidates[0]
    state = SyncState.load(state_dir, device_id=newest.stem)
    meta = {"host": state.host, "deviceId": state.device_id,
            "fileMtime": newest.stat().st_mtime}
    return state, meta


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# Smart default RFID playMode per unit kind (firmware codes; see spec):
# 1 SINGLE_TRACK, 3 AUDIOBOOK, 5 ALL_TRACKS_OF_DIR_SORTED.
DEFAULT_PLAYMODE = {"song": 1, "hoerspiel": 3, "album": 5, "folge": 5, "other": 3}


def assignment_key(file_or_url: str) -> str:
    """An RFID assignment's ``fileOrUrl`` -> unit-id key: strip the leading
    slash and NFC-normalise, so it can be matched against a unit's ``id``."""
    return _nfc(str(file_or_url or "").lstrip("/"))


def build_card_map(assignments: list[dict]) -> dict[str, list[str]]:
    """From ``GET /rfid`` entries, map unit-id key -> assigned card ids. Entries
    whose path matches no unit are simply never looked up by :func:`scan`."""
    out: dict[str, list[str]] = {}
    for a in assignments:
        key = assignment_key(a.get("fileOrUrl", ""))
        if key:
            out.setdefault(key, []).append(str(a.get("id")))
    return out


def _mp3s(root: Path) -> list[Path]:
    """All (non-ignored) .mp3 files under ``root``, sorted by path."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not espuino.is_ignored(d))
        for fn in sorted(filenames):
            if fn.lower().endswith(".mp3") and not espuino.is_ignored(fn):
                out.append(Path(dirpath) / fn)
    return out


def _sync_status(files: list[Path], local_dir: Path, remote_root: str,
                 state: SyncState | None) -> str:
    """Aggregate a unit's per-file upload state into one label."""
    if state is None:
        return "unknown"
    total = pending = unchanged = 0
    for f in files:
        rel = f.relative_to(local_dir)
        remote = espuino.join_remote(remote_root, *rel.parts)
        size = f.stat().st_size
        mtime = int(f.stat().st_mtime)
        total += 1
        if state.is_pending(remote):
            pending += 1
        elif state.is_unchanged(remote, size, mtime):
            unchanged += 1
    if total == 0:
        return "unsynced"
    if unchanged == total:
        return "synced"
    if pending:
        return "pending"
    if unchanged:
        return "partial"
    return "unsynced"


def _print_status(cover: Path, cover_rel: str, state: PrintState) -> str:
    """Print state for a unit's single cover image."""
    if not cover.is_file():
        return "no_cover"
    st = cover.stat()
    if state.is_printed(cover_rel, st.st_size, st.st_mtime):
        return "printed"
    if _key(cover_rel) in state.printed:
        return "changed"
    return "new"


def _make_unit(kind: str, unit_id: str, series: str, title: str,
               files: list[Path], cover_rel: str, local_dir: Path,
               covers_dir: Path, remote_root: str,
               sync_state: SyncState | None, print_state: PrintState) -> dict:
    stats = [f.stat() for f in files]
    cover = covers_dir / cover_rel
    cover_ok = cover.is_file()
    return {
        "id": _nfc(unit_id),
        "kind": kind,
        "series": _nfc(series),
        "title": _nfc(title),
        "files": len(files),
        "bytes": sum(s.st_size for s in stats),
        "mtime": max((s.st_mtime for s in stats), default=0.0),
        "cover": {"rel": _nfc(cover_rel),
                  "status": "present" if cover_ok else "missing"},
        "sync": _sync_status(files, local_dir, remote_root, sync_state),
        "print": _print_status(cover, cover_rel, print_state),
    }


def scan(cfg: espuino.Config, sync_state: SyncState | None,
         print_state: PrintState,
         covers_dir: Path = espuino.DEFAULT_COVERS_DIR,
         card_map: dict[str, list[str]] | None = None) -> list[dict]:
    """Walk ``files/`` and return the flat list of card units (see module docstring).

    Derivation of a top-level entry:
    * ``Lieder`` dir  → each mp3 inside is a ``song`` unit.
    * dir with `` - `` in its name → the whole dir is one ``album`` unit.
    * any other dir → a *series*: each child subdir is a ``folge`` unit, each
      child mp3 a ``hoerspiel`` unit.
    * a loose top-level mp3 → an ``other`` unit.
    Cover path mirrors the unit's relative path with a ``.jpg`` suffix (an album's
    single folder cover lives at ``<dirname>.jpg``).
    """
    local_dir = Path(cfg.local_dir)
    covers_dir = Path(covers_dir)
    remote_root = cfg.remote_root
    units: list[dict] = []
    if not local_dir.is_dir():
        return units

    def add(kind, unit_id, series, title, files, cover_rel):
        if files:
            unit = _make_unit(kind, unit_id, series, title, files,
                              cover_rel, local_dir, covers_dir,
                              remote_root, sync_state, print_state)
            # Attach assigned card ids only when the RFID state is known at all
            # (card_map None -> omit 'cards' so the UI can render "–").
            if card_map is not None:
                unit["cards"] = card_map.get(unit["id"], [])
            units.append(unit)

    for entry in sorted(local_dir.iterdir(), key=lambda p: p.name):
        name = entry.name
        if espuino.is_ignored(name):
            continue

        if entry.is_file():
            if name.lower().endswith(".mp3"):
                stem = entry.stem
                add("other", name, "", stem, [entry],
                    str(Path(name).with_suffix(".jpg")))
            continue

        if name == "Lieder":
            for mp3 in _mp3s(entry):
                rel = mp3.relative_to(local_dir)
                add("song", str(rel), "Lieder", mp3.stem, [mp3],
                    str(rel.with_suffix(".jpg")))
        elif " - " in name or (entry / ".album").is_file():
            # A "<Artist> - <Album>" name, or any folder marked with a `.album`
            # file, is a single album card (its whole folder plays as one list).
            if " - " in name:
                artist, _, album = name.partition(" - ")
            else:
                artist, album = name, name
            add("album", name, artist, album, _mp3s(entry), f"{name}.jpg")
        else:  # a series folder
            for child in sorted(entry.iterdir(), key=lambda p: p.name):
                if espuino.is_ignored(child.name):
                    continue
                if child.is_dir():
                    add("folge", f"{name}/{child.name}", name, child.name,
                        _mp3s(child), f"{name}/{child.name}.jpg")
                elif child.name.lower().endswith(".mp3"):
                    add("hoerspiel", f"{name}/{child.name}", name, child.stem,
                        [child], f"{name}/{child.stem}.jpg")

    units.sort(key=lambda u: u["mtime"], reverse=True)
    return units
