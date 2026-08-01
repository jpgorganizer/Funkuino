#!/usr/bin/env python3
"""Extract cover images from files/ into covers/ (for printing RFID cards).

For every audio file under files/ that has an embedded cover, the full-resolution
image is written to covers/<same relative path>.jpg. The device has no display,
so covers are not synced (see IGNORE_PATTERNS) — this is purely a local set of
printable title images. New downloads save their cover automatically; run this to
(re)generate covers for the existing library.

Idempotent: an up-to-date cover is skipped unless --force.

    ./covers                 # whole library
    ./covers "Bibi und Tina" # just one folder
    ./covers --force
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import espuino
import prepare


def extract_all(root: Path, lib: Path, covers_dir: Path, force: bool = False, log=print) -> int:
    written = skipped = no_art = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not espuino.is_ignored(d)]
        for fn in sorted(filenames):
            if not fn.lower().endswith(".mp3"):
                continue
            mp3 = Path(dirpath) / fn
            dest = espuino.cover_path_for(mp3, lib, covers_dir)
            if not force and dest.exists() and dest.stat().st_mtime >= mp3.stat().st_mtime:
                skipped += 1
                continue
            if prepare.make_jpg(mp3, dest):
                log(f"  cover  {dest.relative_to(covers_dir)}")
                written += 1
            else:
                no_art += 1
    log(f"\nCovers: {written} written, {skipped} up-to-date, {no_art} without embedded art.")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract cover images from files/ into covers/ for printing cards.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("path", nargs="?", help="Limit to a subpath under files/ (default: all)")
    parser.add_argument("--force", action="store_true", help="Rewrite even up-to-date covers")
    args = parser.parse_args(argv)
    espuino.require_ffmpeg("extract cover images")

    cfg = espuino.Config.from_env()
    lib = Path(cfg.local_dir)
    root = lib
    if args.path:
        p = Path(args.path)
        root = p if p.is_absolute() else lib / args.path
    if not root.exists():
        print(f"ERROR: not found: {root}", file=sys.stderr)
        return 1

    extract_all(root, lib, espuino.DEFAULT_COVERS_DIR, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
