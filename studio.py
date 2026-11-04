#!/usr/bin/env python3
"""Minimal local RoyaleAPI HTML -> native Clash Royale replay. No emulator."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from neo_replay import assets, importer, cache, resourcefs
from neo_replay.paths import ROOT, RUNTIME, CACHE, USER_CONFIG, VIEWER, config, native_env


def x86_command(*args):
    python = os.environ.get("NEO_PYTHON_X86", sys.executable)
    return ["/usr/bin/arch", "-x86_64", python, str(ROOT / "neo_replay/native_worker.py"), *map(str, args)]


def doctor(check_gl=False):
    mac_version = tuple(map(int, platform.mac_ver()[0].split(".")[:2])) if sys.platform == "darwin" else ()
    checks = {"macOS >= 14.4": sys.platform == "darwin" and mac_version >= (14, 4), "Rosetta Python": False,
              "native bridge": (RUNTIME / "native/bionic_mac.dylib").is_file(),
              "ANGLE EGL": (RUNTIME / "angle/libEGL.dylib").is_file(),
              "ANGLE GLES": (RUNTIME / "angle/libGLESv2.dylib").is_file(),
              "display window": VIEWER.is_file()}
    if sys.platform == "darwin":
        py = os.environ.get("NEO_PYTHON_X86", sys.executable)
        result = subprocess.run(["/usr/bin/arch", "-x86_64", py, "-c", "import platform; print(platform.machine())"],
                                capture_output=True, text=True)
        checks["Rosetta Python"] = result.returncode == 0 and "x86_64" in result.stdout
    cfg = config()
    if cfg:
        missing = assets.missing_files(Path(cfg["engine"]), Path(cfg["assets"]))
        checks["game libraries + spectator assets"] = not missing
        checks["supported engine hash"] = Path(cfg["engine"], "libg.so").is_file() and \
            assets.digest(Path(cfg["engine"], "libg.so")) == cfg["libg_sha256"]
    else:
        missing = ["official x86_64 APKM and matching full assets (not imported yet)"]
        checks["game libraries + spectator assets"] = False
    for name, ok in checks.items():
        print(f"{'OK     ' if ok else 'MISSING'} {name}")
    if missing:
        print("\nRequired:\n  " + "\n  ".join(missing))
    if check_gl and all(checks[k] for k in ("Rosetta Python", "native bridge", "ANGLE EGL", "ANGLE GLES")):
        result = subprocess.run(x86_command("check-gl"), env=native_env(), cwd=ROOT, timeout=60)
        checks["ANGLE Metal readback"] = result.returncode == 0
    return all(checks.values())


def parse_html(html, output=None):
    replay = importer.convert(Path(html).expanduser().resolve())
    dest = Path(output) if output else CACHE / "imports" / f"{replay['tag']}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(replay, ensure_ascii=False, indent=2) + "\n")
    cards = sum(e["kind"] == "card_play" for e in replay["events"])
    print(f"Imported {replay['tag']}: {cards} card plays, {len(replay['events']) - cards} abilities")
    print(f"Players: {replay['players'][1]['name']} vs {replay['players'][0]['name']}")
    print("Parsed data:", dest)
    return replay, dest


def play(args):
    from neo_replay.runner import play as run
    from neo_replay.engine_lock import engine_access
    with engine_access():
        return run(args, x86_command, parse_html, doctor)


def main():
    if sys.argv[1:2] in (["update-game"], ["install-game"]):
        from updater.update import main as update_game
        return update_game(sys.argv[2:])
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    d = commands.add_parser("doctor", help="Check dependencies and game data")
    d.add_argument("--check-gl", action="store_true", help="Create a real Metal/ANGLE context and test pixel readback")
    s = commands.add_parser("setup", help="Build local macOS bridge and display; no game download")
    s.add_argument("--angle-from", type=Path)
    imp = commands.add_parser("import-html", help="Parse HTML without requiring engine files")
    imp.add_argument("html", type=Path)
    imp.add_argument("--out", type=Path)
    pl = commands.add_parser("play", aliases=["cache", "open"], help="打开 HTML；cache 只准备缓存")
    pl.add_argument("html", nargs="?", help="HTML 文件路径；留空先打开窗口")
    pl.add_argument("--prepare-only", action="store_true", help="只准备并保存缓存，不启动对战画面")
    pl.add_argument("--seconds", type=int, default=1800)
    pl.add_argument("--speed", type=float, default=1)
    pl.add_argument("--width", type=int, help="Game render width, 160..4096")
    pl.add_argument("--height", type=int, help="Game render height, 160..4096")
    pl.add_argument("--settings", "--config", type=Path, default=USER_CONFIG, help="播放设置，默认读取根目录 config.json")
    pl.add_argument("--exit-at-end", action="store_true", help="Close after reaching the simulated end tick")
    pl.add_argument("--no-window", action="store_true", help="Render into the session's frame.shm for diagnostics")
    pl.add_argument("--show-loading", action="store_true", help="Show the game's native loading screen instead of the simple local loading page")
    pl.add_argument("--rebuild", action="store_true", help="Force calibration and simulation instead of reusing a verified replay")
    pl.add_argument("--cpu-frames", action="store_true", help="Use slower RGBA readback instead of GPU shared surfaces")
    pl.add_argument("--recording-mode", action="store_true", help="干净录屏窗口，开局暂停，等待自动化播放指令")
    preview = commands.add_parser("preview-background", help="预览等待背景，不启动游戏引擎")
    preview.add_argument("--settings", "--config", type=Path, default=USER_CONFIG, help="播放设置，默认读取根目录 config.json")
    args = p.parse_args(sys.argv[1:] or ["play"])
    if args.command == "cache":
        args.prepare_only = True
        args.no_window = True
    if args.command == "preview-background":
        options = resourcefs.settings(args.settings)
        folder = CACHE / "preview"
        (folder / "live").mkdir(parents=True, exist_ok=True)
        (folder / "settings.json").write_text(json.dumps(options, default=str, ensure_ascii=False))
        return subprocess.run([str(VIEWER), str(folder / "live"), str(options["width"]), str(options["height"])], cwd=ROOT).returncode
    if args.command == "doctor":
        return 0 if doctor(args.check_gl) else 2
    if args.command == "setup":
        cmd = [sys.executable, str(ROOT / "tools/setup_mac.py")]
        if args.angle_from:
            cmd += ["--angle-from", str(args.angle_from)]
        return subprocess.run(cmd, cwd=ROOT).returncode
    if args.command == "import-html":
        parse_html(args.html, args.out)
        return 0
    return play(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
