#!/usr/bin/env python3
"""Client library for managing an ESPuino over its HTTP API.

Everything here talks to the same HTTP ``/explorer`` REST API that the device's
own web interface uses (documented in the firmware's REST-API.yaml / the Swagger
UI at http://<host>/swagger). We deliberately do NOT use the device's FTP
server -- see the "Why not FTP?" and "Device quirks" notes below.

Device quirks discovered on firmware 2026xxxx-DEV (ESP32-D0WD-V3), all worked
around in this module:

* mDNS is slow: every new TCP connection to ``espuino.local`` costs ~5 s for
  name resolution on macOS, and the server sends ``Connection: close`` so each
  request is a fresh connection. Resolving the hostname to an IP ONCE (see
  ``ESPuino._resolve``) makes every request ~0.03 s instead of ~5 s.
* Only ONE storage operation may be in flight at a time. Uploads must be
  strictly sequential, and you must never leave a download half-read: closing a
  streaming GET early wedges the SD writer and makes the next upload fail with
  HTTP 500. Therefore we never probe file sizes via a partial download.
* There is no cheap way to read a remote file's size: the ``/explorer`` listing
  omits sizes, a HEAD to ``/explorerdownload`` returns a stale/constant length,
  and reading Content-Length from a real GET means downloading the whole file.
  So change detection uses a local manifest instead (see sync_state.py).

  The FTP server does NOT interfere with HTTP uploads: uploads return HTTP 200
  whether FTP is running or stopped. The only thing that wedges the SD writer is
  a half-read streaming GET, which is why this module never does one.

Why not FTP? The firmware's FTP library (Joe91/ESP-FTP-Server-Lib) cannot list
directory contents (LIST/MLSD return only '.'/'..') and does not implement SIZE,
so it is useless for rsync-style change detection; it must also be re-enabled
after every reboot. The HTTP API does everything we need and is always
available, so we use it exclusively.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import posixpath
import shutil
import socket
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

import requests

# Where the *code* lives: wrappers, scripts, the Studio web assets. In a
# packaged app this is read-only (inside the bundle), so nothing may be written
# below it — user data goes to DATA_ROOT instead.
REPO_ROOT = Path(__file__).resolve().parent.parent

def _default_config_dir() -> Path:
    """The platform's own place for application configuration.

    Asked via ``sys.platform``, not ``os.uname()``: the latter does not exist on
    Windows, so importing this module raised AttributeError there before the
    first line of any command ran.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Funkuino"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Funkuino"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "funkuino"


# FUNKUINO_CONFIG_DIR points the whole installation at a different config
# directory — that is how the first-run flow gets tested repeatedly without
# touching the real one (`funkuino --config-dir /tmp/probe studio`), and how a
# second installation stays separate.
APP_CONFIG_DIR = Path(
    os.environ.get("FUNKUINO_CONFIG_DIR") or _default_config_dir()).expanduser()
APP_CONFIG_FILE = APP_CONFIG_DIR / "config.json"


def _resolve_data_root() -> Path:
    """Where the *data* lives: library, covers, print sheets, status.

    FUNKUINO_DATA_DIR > app config file > the checkout.

    The config file is the app's channel: it is written by the GUI's settings
    pane and read here, so `./sync` from a terminal and the app operate on the
    same library instead of silently diverging. It is absent in a plain
    checkout, which is why the default stays REPO_ROOT.
    """
    if env := os.environ.get("FUNKUINO_DATA_DIR"):
        return Path(env).expanduser().resolve()
    try:
        configured = json.loads(APP_CONFIG_FILE.read_text()).get("data_dir")
    except (OSError, ValueError):
        configured = None
    return Path(configured).expanduser().resolve() if configured else REPO_ROOT


DATA_ROOT = _resolve_data_root()


def credentials_file() -> Path:
    """Where the agent's token lives: with the installation, not the library.

    A media library gets copied, backed up and handed around; a credential
    should not ride along in it. Older locations are still honoured so an
    existing checkout keeps working.
    """
    primary = APP_CONFIG_DIR / "credentials.env"
    if primary.exists():
        return primary
    for legacy in (DATA_ROOT / ".env", REPO_ROOT / ".env"):
        if legacy.exists():
            return legacy
    return primary


def data_or_repo(name: str) -> Path:
    """Locate a per-installation side file (``.env``, ``.agent-allow.json``,
    ``CLAUDE.local.md``) in the data folder, falling back to the checkout.

    These belong to the data folder, but an existing checkout may already hold
    them (they predate DATA_ROOT, and the private overlay symlinks them into the
    repo), so a configured data folder must not silently lose them.
    """
    candidate = DATA_ROOT / name
    return candidate if candidate.exists() else REPO_ROOT / name

# --- Defaults ---------------------------------------------------------------
DEFAULT_HOST = "espuino.local"
DEFAULT_LOCAL_DIR = DATA_ROOT / "files"
DEFAULT_REMOTE_ROOT = "/"
# Full-resolution title images (for printing RFID cards) live here, mirroring the
# files/ layout. Not uploaded (the device has no display); see IGNORE_PATTERNS.
DEFAULT_COVERS_DIR = DATA_ROOT / "card-covers"
# Printable A4 sheets (cards.py) and the manifests. All under DATA_ROOT so a
# configured data folder carries the full state: library, covers, history.
PRINT_SHEETS_DIR = DATA_ROOT / "print-sheets"
# Deliberately not hidden: someone copying a library by hand picks the folders
# they can see, and losing this one silently means re-uploading everything and
# reprinting every card. One visible folder to take along — but still separate
# files inside, because the sync manifest is rewritten twice per uploaded file
# (the pending marker is what makes an interrupted upload safe) and must not
# share a file with anything else.
STATUS_DIR = DATA_ROOT / "status"
PRINT_STATE_FILE = STATUS_DIR / "print-history.json"
SYNC_STATE_DIR = STATUS_DIR

STATUS_README = """Dieser Ordner gehört zur Funkuino-Mediathek.

Hier merkt sich Funkuino, was bereits auf welchem ESPuino liegt und welche
Karten schon gedruckt sind. Beim Verschieben oder Sichern der Mediathek also
bitte mitnehmen.

Verloren ist nichts, wenn er fehlt: der nächste Abgleich lädt dann alles erneut
auf das Gerät, und im Kartendruck gelten wieder alle Cover als ungedruckt.
"""


def write_text_atomically(path: Path, text: str) -> None:
    """Replace a file's contents so a power cut cannot leave a half-file.

    Writing to a temp file and renaming is only half of it: the rename can
    reach the disk before the data does, and what survives a power cut is then
    an EMPTY file where the manifest used to be. fsync the temp file before the
    rename and the directory after it, so the order is guaranteed rather than
    hoped for. (On a network volume neither guarantee is worth much — that is
    the filesystem's business, not ours.)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def keep_backup(path: Path) -> None:
    """Copy the current state file aside as ``.bak`` — once per run.

    Losing a manifest is not data loss, but it does cost a full re-upload or a
    reprint of every card. One previous-good copy turns the worst case into
    "the last run is missing" instead.
    """
    if path.is_file():
        with contextlib.suppress(OSError):
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def read_json_state(path: Path) -> tuple[dict, str]:
    """Load a state file. Returns ``(data, status)``.

    status is ``ok``, ``missing``, ``recovered`` (the file was unreadable and
    the backup stood in) or ``lost`` (unreadable, no usable backup).

    A broken file is never simply ignored: the old behaviour started fresh and
    then overwrote the evidence with an empty manifest on the next save, so a
    corrupted state looked exactly like a new installation. It is moved aside
    instead, and the caller is told so it can say something.
    """
    if not path.is_file():
        return {}, "missing"
    try:
        return json.loads(path.read_text()), "ok"
    except (ValueError, OSError):
        pass
    with contextlib.suppress(OSError):
        path.replace(path.with_suffix(path.suffix + f".corrupt-{int(time.time())}"))
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.is_file():
        try:
            return json.loads(backup.read_text()), "recovered"
        except (ValueError, OSError):
            pass
    return {}, "lost"


def state_warning(kind: str, status: str) -> str | None:
    """A sentence for a state file that did not load cleanly, or None."""
    if status == "recovered":
        return (f"{kind} war beschädigt und wurde aus der Sicherung "
                "wiederhergestellt. Die letzte Sitzung fehlt darin evtl.")
    if status == "lost":
        return (f"{kind} war beschädigt und ließ sich nicht wiederherstellen "
                "— die beschädigte Datei liegt daneben (.corrupt-…).")
    return None


def ensure_status_dir() -> Path:
    """Create the status folder, with the note that explains what it is."""
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    readme = STATUS_DIR / "README.txt"
    if not readme.exists():
        with contextlib.suppress(OSError):
            readme.write_text(STATUS_README)
    return STATUS_DIR


HTTP_PORT = 80

# Web-interface control command codes (firmware src/values.h), sent over the
# /ws websocket as {"controls":{"action":<code>}}.
CMD_TELL_IP_ADDRESS = 151
CMD_PLAYPAUSE = 170
CMD_NEXTTRACK = 172
CMD_STOP = 182
CMD_RESTARTSYSTEM = 183

# Files/dirs that stay local and are never uploaded: macOS/editor cruft, and
# cover images (the device has no display — covers live next to the audio only
# for the computer and for printing RFID cards).
IGNORE_PATTERNS = (
    ".DS_Store",
    "._*",
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
    ".TemporaryItems",
    ".apdisk",
    "@eaDir",
    "Thumbs.db",
    "desktop.ini",
    "*.tmp",
    ".git",
    ".album",   # local folder-is-an-album marker; must never reach the device
    "*.jpg",
    "*.jpeg",
    "*.png",
    "*.gif",
    "*.bmp",
    "*.webp",
)

Logger = Callable[[str], None]


class _ProgressReader:
    """A read-only, sized body wrapper that reports upload progress.

    requests reads it in chunks and (because of ``__len__``) still sends a
    Content-Length rather than chunked encoding — which the device requires.
    ``callback(sent, total)`` is invoked as bytes are consumed."""

    def __init__(self, data: bytes, callback: Callable[[int, int], None]):
        self._data = data
        self._total = len(data)
        self._pos = 0
        self._cb = callback

    def __len__(self) -> int:
        return self._total

    def read(self, size: int = -1) -> bytes:
        chunk = self._data[self._pos:] if size < 0 else self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        if chunk:
            self._cb(self._pos, self._total)
        return chunk


# --- Configuration ----------------------------------------------------------
@dataclass
class Config:
    host: str = DEFAULT_HOST
    local_dir: Path = DEFAULT_LOCAL_DIR
    remote_root: str = DEFAULT_REMOTE_ROOT
    http_port: int = HTTP_PORT
    # Optional HTTP basic auth (only if the web interface is password protected).
    http_user: Optional[str] = None
    http_password: Optional[str] = None

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Defaults, overlaid with ESPUINO_* env vars, then explicit (non-None)
        keyword overrides (typically CLI arguments)."""
        cfg = cls(
            host=os.environ.get("ESPUINO_HOST", DEFAULT_HOST),
            local_dir=Path(os.environ.get("ESPUINO_LOCAL_DIR", str(DEFAULT_LOCAL_DIR))),
            remote_root=os.environ.get("ESPUINO_REMOTE_ROOT", DEFAULT_REMOTE_ROOT),
            http_user=os.environ.get("ESPUINO_HTTP_USER") or None,
            http_password=os.environ.get("ESPUINO_HTTP_PASSWORD") or None,
        )
        for key, value in overrides.items():
            if value is not None:
                if key == "local_dir":
                    value = Path(value)
                setattr(cfg, key, value)
        return cfg


# --- Helpers ----------------------------------------------------------------
def is_ignored(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in IGNORE_PATTERNS)


def cover_path_for(media_path: str | Path, lib_dir: str | Path,
                   covers_dir: str | Path = DEFAULT_COVERS_DIR) -> Path:
    """Map an audio file under ``lib_dir`` to its cover image under
    ``covers_dir`` (same relative path, .jpg)."""
    rel = Path(media_path).resolve().relative_to(Path(lib_dir).resolve())
    return Path(covers_dir) / rel.with_suffix(".jpg")


def join_remote(base: str, *parts: str) -> str:
    """Join remote path parts using POSIX separators, keeping a leading '/'.

    The result is Unicode-normalised to NFC because the device stores names in
    NFC while macOS hands us decomposed (NFD) names from the local filesystem;
    without this, e.g. 'Frühling' (device) would never match 'Frühling'
    (local) and every accented file would look missing and be re-uploaded.
    """
    path = base
    for part in parts:
        if part in ("", "."):
            continue
        path = posixpath.join(path, part)
    return unicodedata.normalize("NFC", posixpath.normpath(path))


# --- Client -----------------------------------------------------------------
class ESPuino:
    """Client for the ESPuino HTTP file-management API."""

    def __init__(self, cfg: Config, timeout: float = 15.0, upload_timeout: float = 600.0):
        self.cfg = cfg
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.session = requests.Session()
        if cfg.http_user:
            self.session.auth = (cfg.http_user, cfg.http_password or "")
        self._addr = self._resolve(cfg.host)
        self.base = f"http://{self._addr}:{cfg.http_port}"

    @staticmethod
    def _resolve(host: str) -> str:
        """Resolve host to an IP once, so we don't pay ~5 s of mDNS per request.
        Falls back to the original host if resolution fails."""
        try:
            return socket.gethostbyname(host)
        except OSError:
            return host

    # -- device info / state --
    def info(self) -> dict:
        r = self.session.get(f"{self.base}/info", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def restart(self) -> None:
        """Reboot the device (also the only way to stop the FTP server)."""
        try:
            self.session.post(f"{self.base}/restart", timeout=self.timeout)
        except requests.RequestException:
            pass  # the device drops the connection as it reboots

    # -- directory listing (fast + safe: reads fully, closes cleanly) --
    def list_dir(self, path: str) -> list[tuple[str, bool]]:
        """Return [(name, is_dir), ...] for a remote directory.

        A directory that does not exist yet is reported by the firmware as HTTP
        404 or 501; we treat both as "empty" so callers can index a remote root
        that has not been created yet."""
        r = self.session.get(
            f"{self.base}/explorer", params={"path": path}, timeout=self.timeout
        )
        if r.status_code in (404, 501):
            return []
        r.raise_for_status()
        entries: list[tuple[str, bool]] = []
        for obj in r.json():
            if "root" in obj:  # filesystem marker, e.g. {"name":"/","root":"sd"}
                continue
            name = obj.get("name", "")
            if name in ("", "/", ".", ".."):
                continue
            entries.append((name, bool(obj.get("dir", False))))
        return entries

    def walk(self, root: str) -> Iterator[tuple[str, bool]]:
        """Depth-first walk yielding (path, is_dir); dirs come after their
        contents so they can be removed safely."""
        for name, is_dir in self.list_dir(root):
            path = join_remote(root, name)
            if is_dir:
                yield from self.walk(path)
                yield (path, True)
            else:
                yield (path, False)

    def remote_index(self, root: str) -> tuple[set[str], set[str]]:
        """Return (files, dirs) sets of all remote paths under ``root``."""
        files: set[str] = set()
        dirs: set[str] = set()
        for path, is_dir in self.walk(root):
            (dirs if is_dir else files).add(path)
        return files, dirs

    # -- mutations --
    def upload(self, local_path: str | Path, remote_path: str,
               progress: Optional[Callable[[int, int], None]] = None) -> None:
        """Upload one file (raw octet-stream body, path incl. filename as a
        query param), exactly like the web UI. Parent dirs are auto-created.
        Callers MUST serialise uploads. ``progress(sent, total)`` is called as
        the body is sent.

        The device writes to SD slower than the network delivers, so a large
        POST body fills the TCP window and ``sendall`` blocks; a fixed short
        timeout then aborts a big upload part-way (the device keeps a truncated
        file). We therefore scale the socket timeout to the file size (assuming a
        conservative ~40 KiB/s floor). A single timeout value covers connect,
        send AND read, so the slow body send is not cut off."""
        data = Path(local_path).read_bytes()  # in-memory => explicit Content-Length
        timeout = max(self.timeout, len(data) // (40 * 1024))
        # The device occasionally resets a connection mid-request; retry a couple
        # of times before giving up (a re-upload just overwrites).
        for attempt in range(3):
            try:
                body = _ProgressReader(data, progress) if progress else data
                r = self.session.post(
                    f"{self.base}/explorer",
                    params={"path": remote_path},
                    data=body,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=timeout,
                )
                r.raise_for_status()
                return
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise
                time.sleep(1.5)

    def mkdir(self, path: str) -> None:
        r = self.session.put(
            f"{self.base}/explorer", params={"path": path}, timeout=self.timeout
        )
        r.raise_for_status()

    def delete(self, path: str) -> None:
        """Delete a remote file or directory. NOTE: the firmware stops playback
        (CMD_STOP) before deleting."""
        r = self.session.delete(
            f"{self.base}/explorer", params={"path": path}, timeout=self.timeout
        )
        r.raise_for_status()

    def rename(self, src: str, dst: str) -> None:
        r = self.session.patch(
            f"{self.base}/explorer",
            params={"srcpath": src, "dstpath": dst},
            timeout=self.timeout,
        )
        r.raise_for_status()

    # -- control commands (websocket) --
    def send_command(self, action: int) -> dict:
        """Trigger a CMD_* control command over the web-interface websocket."""
        import websocket  # from the `websocket-client` package

        ws = websocket.create_connection(
            f"ws://{self._addr}:{self.cfg.http_port}/ws", timeout=10
        )
        try:
            ws.send(json.dumps({"controls": {"action": action}}))
            ws.settimeout(5)
            try:
                return json.loads(ws.recv())
            except Exception:
                return {}
        finally:
            ws.close()
