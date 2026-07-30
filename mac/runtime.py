#!/usr/bin/env python3
"""Assemble the Python runtime that ships inside Funkuino.app.

A packaged app cannot rely on the user having Python, a venv or Homebrew, so the
bundle carries its own interpreter plus this project's dependencies. The build
is reproducible: a pinned python-build-standalone release (Astral's, the same
distribution uv uses), verified against the checksum published next to it, then
`pip install -r requirements.txt` into that interpreter.

    python3 mac/runtime.py --out mac/build/runtime

Cross-architecture builds are deliberately not supported: pip has to *run* the
target interpreter to install into it. Build the Intel disk image on an Intel
Mac (or under Rosetta).
"""
from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

# Pinned deliberately: a build should not change because upstream published a
# new release overnight. Bump both together.
RELEASE = "20260728"
PY_VERSION = "3.13.14"

BASE = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{RELEASE}")

# Dropped from the interpreter: none of it is reachable from this project, and
# tkinter alone drags in a Tk framework that would have to be signed as well.
PRUNE = ("test", "idlelib", "tkinter", "turtledemo", "lib2to3", "ensurepip")


def arch_tag(arch: str) -> str:
    return {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}[arch]


def fetch(url: str, dest: Path) -> None:
    print(f"  {url}")
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def download_interpreter(arch: str, work: Path) -> Path:
    name = (f"cpython-{PY_VERSION}+{RELEASE}-{arch_tag(arch)}-apple-darwin"
            "-install_only_stripped.tar.gz")
    archive = work / name
    if not archive.exists():
        print("Downloading the interpreter…")
        fetch(f"{BASE}/{name}", archive)

    # One SHA256SUMS covers the whole release (850+ assets), not one file each.
    sums = work / f"SHA256SUMS-{RELEASE}"
    if not sums.exists():
        fetch(f"{BASE}/SHA256SUMS", sums)
    expected = next((line.split()[0] for line in sums.read_text().splitlines()
                     if line.strip().endswith(name)), None)
    if expected is None:
        raise SystemExit(f"{name} is not listed in SHA256SUMS")

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected:
        archive.unlink()
        raise SystemExit(f"Checksum mismatch for {name}:\n"
                         f"  expected {expected}\n  got      {digest}")
    print(f"  checksum ok ({digest[:16]}…)")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Runtime directory to create")
    parser.add_argument("--arch", default=platform.machine(),
                        help="arm64 or x86_64 (default: this machine)")
    parser.add_argument("--requirements", default=None,
                        help="requirements.txt (default: the repo's)")
    parser.add_argument("--bundle-agent-cli", action="store_true",
                        help="Keep the Claude Code CLI that claude-agent-sdk "
                             "ships (245 MB). Off by default: the agent is "
                             "optional and uses the user's own installation.")
    args = parser.parse_args()

    if arch_tag(args.arch) != arch_tag(platform.machine()):
        raise SystemExit("Cross-architecture builds are not supported — pip has "
                         "to run the target interpreter. Build on that machine.")

    out = Path(args.out).resolve()
    requirements = Path(args.requirements) if args.requirements else \
        Path(__file__).resolve().parent.parent / "requirements.txt"
    work = out.parent / ".runtime-cache"
    work.mkdir(parents=True, exist_ok=True)

    archive = download_interpreter(args.arch, work)

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Extracting into {out} …")
    with tarfile.open(archive) as tar:
        tar.extractall(work / "extract", filter="data")
    shutil.move(str(work / "extract" / "python"), str(out))
    shutil.rmtree(work / "extract", ignore_errors=True)

    python = out / "bin" / "python3"
    print("Installing dependencies…")
    subprocess.run([str(python), "-m", "pip", "install", "--quiet",
                    "--disable-pip-version-check", "-r", str(requirements)],
                   check=True)

    stdlib = next((out / "lib").glob("python3.*"))
    for name in PRUNE:
        shutil.rmtree(stdlib / name, ignore_errors=True)

    # claude-agent-sdk ships a complete Claude Code binary (245 MB — five times
    # everything else combined). The SDK checks it first but falls back to the
    # user's own installation, which is what this project expects: the agent is
    # an optional feature, and its CLI is the user's to install and update.
    if not args.bundle_agent_cli:
        bundled = stdlib / "site-packages" / "claude_agent_sdk" / "_bundled"
        if bundled.exists():
            shutil.rmtree(bundled)
            print("  dropped the SDK's bundled Claude Code CLI")
    for cache in out.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Runtime ready: {out}  ({size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
