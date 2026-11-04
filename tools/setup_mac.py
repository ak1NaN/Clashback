#!/usr/bin/env python3
"""Build the x86_64 bridge and the small ARM64 frame window, locally."""
import argparse
import json
from pathlib import Path
import platform
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neo_replay.paths import RUNTIME, VIEWER


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--angle-from", type=Path, help="Folder containing x86_64 libEGL.dylib and libGLESv2.dylib")
    args = p.parse_args()
    if sys.platform != "darwin" or platform.machine() not in ("arm64", "x86_64"):
        p.error("This sample targets macOS / Apple Silicon with Rosetta.")
    out = RUNTIME / "native"
    out.mkdir(parents=True, exist_ok=True)
    bridge = out / "bionic_mac.dylib"
    sources = [ROOT / "vendor/crx/mac/bionic_mac.c", *list((ROOT / "native/compat").rglob("*.h"))]
    if not bridge.exists() or bridge.stat().st_mtime < max(p.stat().st_mtime for p in sources):
        subprocess.run(["clang", "-arch", "x86_64", "-mmacosx-version-min=14.4", "-O2", "-dynamiclib",
                    "-fvisibility=hidden", "-Wall", "-Wno-unused-parameter", "-I", str(ROOT / "native/compat"),
                    "-o", str(bridge),
                    str(ROOT / "vendor/crx/mac/bionic_mac.c")], check=True)
    angle = args.angle_from or Path.home() / "Library/Application Support/Steam/Steam.AppBundle/Steam/Contents/MacOS/Frameworks/Chromium Embedded Framework.framework/Versions/A/Libraries"
    (RUNTIME / "angle").mkdir(exist_ok=True)
    for name in ("libEGL.dylib", "libGLESv2.dylib"):
        src, dest = angle / name, RUNTIME / "angle" / name
        if not src.is_file():
            if dest.exists():
                continue
            p.error(f"Missing {src}; supply --angle-from with a local x86_64 ANGLE installation.")
        architectures = subprocess.check_output(["lipo", "-archs", str(src)], text=True).split()
        if "x86_64" not in architectures:
            p.error(f"{src} does not contain x86_64")
        if len(architectures) > 1:
            subprocess.run(["lipo", str(src), "-thin", "x86_64", "-output", str(dest)], check=True)
        elif src.resolve() != dest.resolve():
            import shutil
            shutil.copy2(src, dest)
    VIEWER.parent.mkdir(parents=True, exist_ok=True)
    with (VIEWER.parent.parent / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleExecutable": "ReplayWindow", "CFBundleIdentifier": "local.neo.ReplayWindow",
                       "CFBundleName": "Neo Replay", "CFBundlePackageType": "APPL",
                       "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "14.4"}, f)
    subprocess.run(["swiftc", "-O", "-o", str(VIEWER),
                    str(ROOT / "native/ReplayWindow.swift")], check=True)
    (RUNTIME / "setup.json").write_text(json.dumps({"python": sys.executable, "angle_source": str(angle),
                                                    "bridge_arch": "x86_64"}, indent=2) + "\n")
    print("Built native bridge, copied local ANGLE, built ReplayWindow. All outputs are in runtime/.")


if __name__ == "__main__":
    main()
