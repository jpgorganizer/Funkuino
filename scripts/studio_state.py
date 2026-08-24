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
    # `sync-<mac>.json`, not every .json: the status folder holds the print
    # history as well, and picking that up would report "never synced".
    candidates = sorted(state_dir.glob("sync-*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, None
    newest = candidates[0]
    state = SyncState.load(state_dir, device_id=newest.stem.removeprefix("sync-"))
    meta = {"host": state.host, "deviceId": state.device_id,
            "fileMtime": newest.stat().st_mtime}
    return state, meta


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# Smart default RFID playMode per unit kind (firmware codes; see spec):
# 1 SINGLE_TRACK, 3 AUDIOBOOK, 5 ALL_TRACKS_OF_DIR_SORTED, 11 LOCAL_M3U.
DEFAULT_PLAYMODE = {"song": 1, "hoerspiel": 3, "album": 5, "folge": 5, "other": 3,
                    "stream": 11}

# Top-level library folder dedicated to standalone .m3u/.m3u8 playlists and
# webstreams (e.g. a single internet-radio URL in its own .m3u). Recognised
# recursively at any depth; see scan()'s docstring for the full picture of
# where else .m3u/.m3u8 is (and importantly isn't) recognised.
STREAM_ROOT_DIR = "Webstreams_Playlisten"

# .m3u is the original (often ASCII/Latin-1) playlist format; .m3u8 is the
# same format explicitly UTF-8-encoded. ESPuino's firmware plays both the same
# way (LOCAL_M3U), so Funkuino treats them identically throughout.
STREAM_EXTS = (".m3u", ".m3u8")


def assignment_key(file_or_url: str, remote_root: str = espuino.DEFAULT_REMOTE_ROOT) -> str:
    """An RFID assignment's ``fileOrUrl`` -> unit-id key: strip the leading
    slash, the configured remote_root prefix (join_remote's counterpart --
    without this, any ESPUINO_REMOTE_ROOT other than the default "/" makes
    every assignment key mismatch its unit id, e.g. "mp3/Lieder/x.mp3" never
    matching the unit id "Lieder/x.mp3"), and NFC-normalise."""
    s = _nfc(str(file_or_url or "").lstrip("/"))
    root = _nfc(str(remote_root or "").strip("/"))
    if root:
        if s == root:
            s = ""
        elif s.startswith(root + "/"):
            s = s[len(root) + 1:]
    return s


def build_card_map(assignments: list[dict],
                   remote_root: str = espuino.DEFAULT_REMOTE_ROOT) -> dict[str, list[str]]:
    """From ``GET /rfid`` entries, map unit-id key -> assigned card ids. Entries
    whose path matches no unit are simply never looked up by :func:`scan`."""
    out: dict[str, list[str]] = {}
    for a in assignments:
        key = assignment_key(a.get("fileOrUrl", ""), remote_root)
        if key:
            out.setdefault(key, []).append(str(a.get("id")))
    return out


def _files_with_ext(root: Path, exts: tuple[str, ...]) -> list[Path]:
    """All (non-ignored) files under ``root`` whose lowercased name ends with
    one of ``exts``, sorted by path. Shared recursive walk behind _mp3s() and
    the .m3u/.m3u8 lookup, so both stay consistent (ignore rules, sort order)."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not espuino.is_ignored(d))
        for fn in sorted(filenames):
            if fn.lower().endswith(exts) and not espuino.is_ignored(fn):
                out.append(Path(dirpath) / fn)
    return out


def _mp3s(root: Path) -> list[Path]:
    """All (non-ignored) .mp3 files under ``root``, sorted by path."""
    return _files_with_ext(root, (".mp3",))


def _streams(root: Path) -> list[Path]:
    """All (non-ignored) .m3u/.m3u8 files under ``root``, sorted by path."""
    return _files_with_ext(root, STREAM_EXTS)


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


def _is_album_dir(d: Path) -> bool:
    """A "<Artist> - <Album>" name, or any folder marked with a `.album` or
    `album.marker` file, is a single album card (its whole folder plays as
    one sorted list)."""
    return " - " in d.name or (d / ".album").is_file() or (d / "album.marker").is_file()


def scan(cfg: espuino.Config, sync_state: SyncState | None,
         print_state: PrintState,
         covers_dir: Path = espuino.DEFAULT_COVERS_DIR,
         card_map: dict[str, list[str]] | None = None) -> list[dict]:
    """Walk ``files/`` and return the flat list of card units (see module docstring).

    Derivation of a top-level entry:
    * ``Lieder`` dir  -> each mp3 inside is a ``song`` unit; each .m3u/.m3u8
      inside is a ``stream`` unit (grouped under "Lieder" either way).
    * ``Webstreams_Playlisten`` dir -> each .m3u/.m3u8 anywhere inside (any
      depth) is its own ``stream`` unit, grouped by its immediate parent
      folder's name (so a category subfolder becomes a group heading; a file
      sitting directly in the root groups under "Webstreams_Playlisten" itself).
    * dir with `` - `` in its name (or a `.album`/`album.marker` marker file)
      -> the whole dir is one ``album`` unit.
    * any other dir -> a *series*: each child subdir is either a nested
      ``album`` (same "<Artist> - <Album>"/marker rule) or a ``folge`` unit;
      each child mp3 is a ``hoerspiel`` unit; each child .m3u/.m3u8 is a
      ``stream`` unit -- but only at this level: a .m3u/.m3u8 *inside* a
      ``folge`` or nested ``album`` subfolder is not recognised, matching how
      those subfolders already bundle their mp3s into one unit rather than
      exposing individual files.
    * a loose top-level file -> an ``other`` unit (.mp3) or a ``stream`` unit
      (.m3u/.m3u8).
    Cover path mirrors the unit's relative path with a ``.jpg`` suffix (an
    album's/folge's single folder cover lives at ``<dirname>.jpg``).
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
                add("other", name, "", entry.stem, [entry],
                    str(Path(name).with_suffix(".jpg")))
            elif name.lower().endswith(STREAM_EXTS):
                add("stream", name, "", entry.stem, [entry],
                    str(Path(name).with_suffix(".jpg")))
            continue

        if name == "Lieder":
            for mp3 in _mp3s(entry):
                rel = mp3.relative_to(local_dir)
                add("song", rel.as_posix(), "Lieder", mp3.stem, [mp3],
                    rel.with_suffix(".jpg").as_posix())
            for m3u in _streams(entry):
                rel = m3u.relative_to(local_dir)
                add("stream", rel.as_posix(), "Lieder", m3u.stem, [m3u],
                    rel.with_suffix(".jpg").as_posix())

        elif name == STREAM_ROOT_DIR:
            for m3u in _streams(entry):
                rel = m3u.relative_to(local_dir)
                # Group by the immediate parent folder, however deep -- a file
                # sitting directly in the root has no such parent, so it falls
                # back to the root folder's own name as the group heading.
                group = m3u.parent.name if m3u.parent != entry else STREAM_ROOT_DIR
                add("stream", rel.as_posix(), group, m3u.stem, [m3u],
                    rel.with_suffix(".jpg").as_posix())

        elif _is_album_dir(entry):
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
                    if _is_album_dir(child):
                        add("album", f"{name}/{child.name}", name, child.name,
                            _mp3s(child), f"{name}/{child.name}.jpg")
                    else:
                        add("folge", f"{name}/{child.name}", name, child.name,
                            _mp3s(child), f"{name}/{child.name}.jpg")

                elif child.name.lower().endswith(".mp3"):
                    add("hoerspiel", f"{name}/{child.name}", name, child.stem,
                        [child], f"{name}/{child.stem}.jpg")

                elif child.name.lower().endswith(STREAM_EXTS):
                    add("stream", f"{name}/{child.name}", name, child.stem,
                        [child], f"{name}/{child.stem}.jpg")

    units.sort(key=lambda u: u["mtime"], reverse=True)
    return units
