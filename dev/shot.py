#!/usr/bin/env python3
"""Screenshot Funkuino Studio headlessly — for checking UI work without a browser.

    .venv/bin/python dev/shot.py                     # Bibliothek, seeded library
    .venv/bin/python dev/shot.py karten sync agent   # several tabs, one browser
    .venv/bin/python dev/shot.py --no-ffmpeg         # force the missing-tool banner
    .venv/bin/python dev/shot.py --empty --keep      # empty library, leave both running

It starts its own Studio against a throwaway data folder (so the real library,
the manifests and the agent token are never touched), drives one persistent
headless Chrome over the DevTools protocol, and shuts both down afterwards.

Why a persistent browser and not `chrome --headless --screenshot=out.png`:
that one-shot form writes the file and then frequently does not exit, so a loop
over several views wedges on the second one. It also cannot reach anything but
the landing view — Studio's tabs are JS with no URL routing, so Kartendruck and
Agent need an actual click. Driving CDP costs ~80 lines and needs no new
dependency: websocket-client is already in requirements.txt.

Not shipped: the wheel installs only scripts/, and the sdist lists its files
explicitly, so dev/ stays a checkout-only tool.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websocket  # websocket-client, already a project dependency

REPO_ROOT = Path(__file__).resolve().parent.parent
CHROME = os.environ.get(
    "FUNKUINO_CHROME",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

# Studio's tabs are buttons with a data-tab attribute (studio_web/index.html).
TABS = ("bibliothek", "agent", "sync", "karten")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    raise SystemExit(f"ERROR: {url} did not come up within {timeout:.0f}s")


# One unit per kind, spelled exactly as the naming conventions in CLAUDE.md
# describe them — a Hörspiel episode is <Series>/<NNN> - <Title>.mp3, and getting
# that wrong makes the row show up as the wrong kind, which is precisely the
# thing these screenshots are meant to reveal.
SEED_UNITS = (
    # (audio path under files/, cover path under card-covers/, colour)
    ("Lieder/Rolf Zuckowski - Wie schön dass du geboren bist.mp3",
     "Lieder/Rolf Zuckowski - Wie schön dass du geboren bist.jpg", (198, 84, 60)),
    ("Die Sonnenblume/001 - Der grosse Regen.mp3",
     "Die Sonnenblume/001 - Der grosse Regen.jpg", (66, 116, 168)),
    ("Rolf Zuckowski - Meine Lieder/01 Der Herbst ist da.mp3",
     "Rolf Zuckowski - Meine Lieder.jpg", (120, 140, 76)),
    ("Rolf Zuckowski - Meine Lieder/02 Winterkinder.mp3", None, None),
)


def seed_library(data_dir: Path) -> None:
    """One song, one Hörspiel and one album, so the library view has substance.

    Silent audio: the point is the layout of the rows, and generating real audio
    would drag ffmpeg into a tool whose whole job is to run *without* it.
    """
    from PIL import Image  # project dependency (cards.py)

    for audio, cover, colour in SEED_UNITS:
        path = data_dir / "files" / audio
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 40000)
        if not cover:
            continue
        dest = data_dir / "card-covers" / cover
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            # 16:9 with a square centre, like the video thumbnails cards.py trims.
            im = Image.new("RGB", (1280, 720), (18, 18, 20))
            im.paste(Image.new("RGB", (720, 720), colour), (280, 0))
            im.save(dest, quality=88)


class Devtools:
    """The few CDP calls this needs, over one websocket to one page target."""

    def __init__(self, port: int):
        targets = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list").read())
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise SystemExit("ERROR: headless Chrome exposed no page target")
        # No Origin header: Chrome rejects DevTools sockets from an origin it was
        # not told to allow, and the alternative — starting it with
        # --remote-allow-origins=* — widens the browser's policy instead of
        # making our one client behave.
        self.ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"],
                                              timeout=30, suppress_origin=True)
        self._id = 0

    def call(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            # Events (Page.*, Runtime.*) interleave with replies; ours has the id.
            if message.get("id") == self._id:
                if "error" in message:
                    raise SystemExit(f"ERROR: {method} failed: {message['error']}")
                return message.get("result", {})

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ws.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Headless screenshots of Funkuino Studio.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("views", nargs="*", default=["bibliothek"],
                        help=f"tabs to capture, any of: {', '.join(TABS)}")
    parser.add_argument("--out", default=None,
                        help="output directory (default: dev/shots/)")
    parser.add_argument("--data-dir", default=None,
                        help="library to point Studio at (default: a throwaway one)")
    parser.add_argument("--empty", action="store_true",
                        help="do not seed demo content into the throwaway library")
    parser.add_argument("--no-ffmpeg", action="store_true",
                        help="hide ffmpeg from Studio, to capture the warning banner")
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--scale", type=float, default=2.0,
                        help="device pixel ratio (2 = retina-sharp text)")
    parser.add_argument("--settle", type=float, default=1.2,
                        help="seconds to wait after load/click before capturing")
    parser.add_argument("--keep", action="store_true",
                        help="leave Studio and Chrome running, and print the URL")
    args = parser.parse_args(argv)

    unknown = [v for v in args.views if v not in TABS]
    if unknown:
        parser.error(f"unknown view(s): {', '.join(unknown)} (have: {', '.join(TABS)})")
    if not Path(CHROME).exists():
        raise SystemExit(f"ERROR: no Chrome at {CHROME}\n"
                         f"       Set FUNKUINO_CHROME to its path.")

    out_dir = Path(args.out) if args.out else REPO_ROOT / "dev" / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "funkuino-shot"
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else scratch / "library"
    data_dir.mkdir(parents=True, exist_ok=True)
    if not args.data_dir and not args.empty:
        seed_library(data_dir)

    env = dict(os.environ)
    env["FUNKUINO_DATA_DIR"] = str(data_dir)
    # A scratch config dir keeps the real credentials.env out of the session:
    # the agent tab then simply reports itself as not set up.
    env["FUNKUINO_CONFIG_DIR"] = str(scratch / "config")
    if args.no_ffmpeg:
        ffmpeg = shutil.which("ffmpeg", path=env.get("PATH"))
        if ffmpeg:
            drop = str(Path(ffmpeg).parent)
            env["PATH"] = os.pathsep.join(
                p for p in env["PATH"].split(os.pathsep) if p != drop)

    port, cdp_port = free_port(), free_port()
    studio = subprocess.Popen(
        [str(REPO_ROOT / "bin" / "funkuino"), "studio", "--no-browser",
         "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        # Own process group, so cleanup takes the whole tree and never this shell.
        start_new_session=True)
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--hide-scrollbars",
         f"--remote-debugging-port={cdp_port}",
         f"--user-data-dir={scratch / 'chrome'}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)

    written: list[Path] = []
    try:
        wait_for(f"http://127.0.0.1:{port}/")
        wait_for(f"http://127.0.0.1:{cdp_port}/json/version")
        dt = Devtools(cdp_port)
        dt.call("Page.enable")
        dt.call("Emulation.setDeviceMetricsOverride", width=args.width,
                height=args.height, deviceScaleFactor=args.scale, mobile=False)
        dt.call("Page.navigate", url=f"http://127.0.0.1:{port}/")
        time.sleep(args.settle)

        for view in args.views:
            if view != "bibliothek":
                dt.call("Runtime.evaluate", expression=(
                    f"document.querySelector('.tab[data-tab=\"{view}\"]').click()"))
                time.sleep(args.settle)
            png = dt.call("Page.captureScreenshot", format="png")["data"]
            dest = out_dir / f"{view}.png"
            dest.write_bytes(base64.b64decode(png))
            written.append(dest)
            print(dest)
        dt.close()
    finally:
        if args.keep:
            print(f"\nStill running: http://127.0.0.1:{port}/  (library: {data_dir})")
            print(f"Stop with: kill -- -{studio.pid} -{chrome.pid}")
        else:
            for proc in (chrome, studio):
                with contextlib.suppress(Exception):
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=10)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
