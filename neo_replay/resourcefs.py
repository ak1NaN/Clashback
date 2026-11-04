"""Small asset boundary: silent playback, explicit mounts, reproducible read logs."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil

from .paths import read_user_settings

AUDIO = ("soundbanks/", "sfx/", "audio/")
AUDIO_SUFFIXES = (".bank", ".ogg", ".wav", ".mp3", ".wem", ".fsb")
_seen = set()
_log_fd = None
DEFAULT_LOCATION = 15000199
STARTUP_ALIASES = {"sc/level_bootcamp_arena_dl.sc": "shared/startup/arena.sc",
                   "sc/level_bootcamp_arena_dl_0.sctx": "shared/startup/arena_0.sctx"}


def arena_asset(name):
    return name.startswith(("sc/level_", "sc/arena_")) and not name.startswith("sc/level_decos")


def resolve_asset(root, name):
    if name in STARTUP_ALIASES and (root / STARTUP_ALIASES[name]).is_file():
        return root / STARTUP_ALIASES[name]
    return root / name


def relative(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or "\\" in name:
        return None
    return p.as_posix()


def audio(name):
    return name.startswith(AUDIO) or name.lower().endswith(AUDIO_SUFFIXES)


def record(name, exists, kind="open"):
    """One O_APPEND write per unique path/outcome; survives native os._exit."""
    global _log_fd
    dest = os.environ.get("NEO_ASSET_TRACE")
    name = relative(name)
    if not dest or name is None:
        return
    key = (name, bool(exists), kind)
    if key in _seen:
        return
    _seen.add(key)
    if _log_fd is None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        _log_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    row = {"path": name, "exists": bool(exists), "kind": kind, "pid": os.getpid()}
    os.write(_log_fd, (json.dumps(row, ensure_ascii=False) + "\n").encode())


def settings(path):
    path, value = read_user_settings(path, "player")
    w, h = value.get("width", 720), value.get("height", 1600)
    if type(w) is not int or type(h) is not int or not 160 <= w <= 4096 or not 160 <= h <= 4096:
        raise ValueError("width/height must be integers in 160..4096")
    value.update(width=w, height=h)
    value["location"] = DEFAULT_LOCATION
    mounts = {}
    for logical, target in value.get("mounts", {}).items():
        if relative(logical) is None:
            raise ValueError(f"Invalid game asset path: {logical}")
        if arena_asset(logical) and not logical.startswith("sc/level_ronintemple"):
            raise ValueError("本运行器固定使用默认竞技场，不接受其他竞技场挂载")
        target = Path(target).expanduser()
        target = (path.parent / target).resolve() if not target.is_absolute() else target.resolve()
        if not target.exists():
            raise ValueError(f"Mounted asset does not exist: {target}")
        mounts[logical] = target
    value["mounts"] = mounts
    background = value.get("loading_background")
    if background:
        background = Path(background).expanduser()
        background = (path.parent / background).resolve() if not background.is_absolute() else background.resolve()
        if not background.is_file():
            raise ValueError(f"Loading background does not exist: {background}")
        value["loading_background"] = str(background)
    if not isinstance(value.get("loading_tip", ""), str):
        raise ValueError("loading_tip must be text")
    if value.get("location") is not None and (type(value["location"]) is not int or value["location"] <= 0):
        raise ValueError("location must be a positive native location ID")
    return value


def mounted_view(source, output, mounts):
    """Resolve explicit file/folder mounts for both AAsset and ordinary native file IO.

    Rebuild only inside the fresh session; no link is shipped into another project.
    Copying the directory tree of links is cheap (the artwork is not duplicated).
    """
    source, output = Path(source), Path(output)
    if not mounts:
        return source
    output.mkdir()
    for entry in source.rglob("*"):
        rel = entry.relative_to(source)
        if any(str(rel) == n or str(rel).startswith(n.rstrip("/") + "/") for n in mounts):
            continue
        dest = output / rel
        if entry.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(entry.resolve())
    for name, target in sorted(mounts.items(), key=lambda kv: len(PurePosixPath(kv[0]).parts)):
        dest = output / name
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
        # Ancestor mounts would create symlink parents; rejecting them avoids
        # accidentally writing through a mount into the user's source directory.
        if any(p.is_symlink() for p in dest.parents if p != output and output in p.parents):
            raise ValueError("Overlapping folder/file mounts are not supported")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(target, target_is_directory=target.is_dir())
    return output
