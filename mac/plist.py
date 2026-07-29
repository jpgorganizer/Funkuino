#!/usr/bin/env python3
"""Write Funkuino.app's Info.plist.

Generated rather than checked in so the Makefile stays the single source of
name/version/identifier, and so the development code root can be baked in.
"""
from __future__ import annotations

import argparse
import plistlib
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--bundle-id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--min-macos", required=True)
    p.add_argument("--code-root", required=True,
                   help="Checkout whose bin/funkuino the shell starts (skeleton "
                        "stage; a release bundle carries its own runtime)")
    args = p.parse_args()

    info = {
        "CFBundleName": args.name,
        "CFBundleDisplayName": args.name,
        "CFBundleIdentifier": args.bundle_id,
        "CFBundleExecutable": args.name,
        "CFBundleIconFile": f"{args.name}.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": args.version,
        "CFBundleVersion": args.version,
        "LSMinimumSystemVersion": args.min_macos,
        "NSHighResolutionCapable": True,
        # The app talks to 127.0.0.1 (its own server) and to the ESPuino on the
        # LAN over plain HTTP — both need an ATS exception.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSLocalNetworkUsageDescription":
            "Funkuino spricht mit deinem ESPuino im lokalen Netzwerk.",
        # Shown when the chosen data folder sits in a TCC-protected location.
        # Without these the prompt looks like a crash.
        "NSDocumentsFolderUsageDescription":
            "Zugriff auf deinen Datenordner, wenn du ihn in „Dokumente“ legst.",
        "NSDesktopFolderUsageDescription":
            "Zugriff auf deinen Datenordner, wenn du ihn auf den Schreibtisch legst.",
        "NSDownloadsFolderUsageDescription":
            "Zugriff auf deinen Datenordner, wenn du ihn in „Downloads“ legst.",
        "NSRemovableVolumesUsageDescription":
            "Zugriff auf deine Mediathek, wenn sie auf einer externen Platte liegt.",
        "FunkuinoCodeRoot": args.code_root,
    }
    Path(args.out).write_bytes(plistlib.dumps(info))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
