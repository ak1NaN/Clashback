"""Content-addressed replay commands and native tick snapshots, checked before reuse."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import os
import fcntl
from contextlib import contextmanager

from .paths import ROOT, RUNTIME, CACHE

FILES = ("calibrated-match.json", "verified-opening.json", "replay.json", "report.json")
SNAPSHOT_FILES = ("snapshot.bin", "snapshot-index.json")
DIRECTORY = CACHE / "replays"
SESSIONS = CACHE / "sessions"


def file_hash(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def key(replay, cfg):
    h = hashlib.sha256(json.dumps({"schema": 3, "replay": replay, "engine": cfg["libg_sha256"]},
                                  sort_keys=True, separators=(",", ":")).encode())
    source = [ROOT / "neo_replay" / n for n in ("native_worker.py", "native_snapshots.py", "tick_cache.py", "importer.py", "match_config.py", "validation.py", "inference.py", "standard_match.json", "catalog.json")]
    source += sorted((ROOT / "vendor/crx").glob("*.py"))
    source += [ROOT / "vendor/crx/render" / name for name in ("live.py", "try_surface.py")]
    source += sorted((ROOT / "vendor/crx/bindings").glob("*.json"))
    source += [RUNTIME / "native/bionic_mac.dylib"]
    for path in source:
        actual = Path(os.environ.get("NEO_CATALOG", path)) if path.name == "catalog.json" else path
        h.update(str(path.relative_to(ROOT)).encode() + b"\0" + bytes.fromhex(file_hash(actual)))
    assets = Path(cfg["assets"])
    # Hash actual battle data, so manual balancing edits invalidate old simulations.
    logic = [assets / "version.number"]
    for folder in ("csv_logic", "locations", "tilemaps"):
        logic += sorted(p for p in (assets / folder).rglob("*") if p.is_file())
    for path in logic:
        h.update(str(path.relative_to(assets)).encode() + b"\0" + bytes.fromhex(file_hash(path)))
    return h.hexdigest()


def _verified(cache, files):
    try:
        checks = json.loads((cache / "checksums.json").read_text())
        if not isinstance(checks, dict):
            return False
        if any(checks.get(n) != file_hash(cache / n) for n in files):
            return False
    except (OSError, ValueError):
        return False
    return True


def _restore(identity, output, directory=DIRECTORY, snapshots=False):
    cache = Path(directory) / identity
    files = FILES + (SNAPSHOT_FILES if snapshots else ())
    if not _verified(cache, files):
        return False
    for name in files:
        shutil.copy2(cache / name, Path(output) / name)
    return True


def _save(identity, source, directory=DIRECTORY):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".replay-", dir=directory) as temp:
        temp = Path(temp) / "payload"
        temp.mkdir()
        checks = {}
        files = FILES + (SNAPSHOT_FILES if all((Path(source) / name).exists() for name in SNAPSHOT_FILES) else ())
        for name in files:
            shutil.copy2(Path(source) / name, temp / name)
            checks[name] = file_hash(temp / name)
        (temp / "checksums.json").write_text(json.dumps(checks, indent=2) + "\n")
        dest = directory / identity
        if dest.exists():
            shutil.rmtree(dest)
        # Keep TemporaryDirectory's own folder alive; move only its completed payload.
        temp.rename(dest)


@contextmanager
def locked(identity, directory):
    locks = Path(directory) / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / (identity + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def restore(identity, output, directory=DIRECTORY, snapshots=False):
    """Restore verified preparation files; tick files are opt-in and independent."""
    with locked(identity, directory):
        return _restore(identity, output, directory, snapshots=snapshots)


def restore_snapshots(identity, output, directory=DIRECTORY):
    """Missing or corrupt tick files never invalidate the retained command cache."""
    with locked(identity, directory):
        source = Path(directory) / identity
        if not _verified(source, FILES + SNAPSHOT_FILES):
            return False
        for name in SNAPSHOT_FILES:
            shutil.copy2(source / name, Path(output) / name)
        return True


def save(identity, source, directory=DIRECTORY):
    with locked(identity, directory):
        return _save(identity, source, directory)


@contextmanager
def session_lease(run):
    """Protect a session's snapshots until its native worker has stopped."""
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    with (run / ".tick-cache.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)


def migrate_legacy():
    """Move existing generated files, never the native runtime or source assets."""
    CACHE.mkdir(exist_ok=True)
    # Old running versions still use absolute runtime/ paths. Move them on the
    # next launch after they exit, rather than invalidate an open renderer.
    leases = []
    try:
        for folder in (RUNTIME / "sessions").glob("*"):
            if not folder.is_dir() or folder.is_symlink():
                continue
            lease = (folder / ".tick-cache.lock").open("a")
            leases.append(lease)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        def merge(source, target):
            if not source.exists():
                return
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
            elif source.is_dir() and target.is_dir() and not source.is_symlink():
                for child in list(source.iterdir()):
                    merge(child, target / child.name)
                source.rmdir()
            elif source.is_dir() and not source.is_symlink():
                shutil.rmtree(source)
            else:
                source.unlink()
        for source, target in ((RUNTIME / "cache", CACHE),
                               (RUNTIME / "sessions", SESSIONS),
                               (RUNTIME / "imports", CACHE / "imports"),
                               (RUNTIME / "preview", CACHE / "preview"),
                               (RUNTIME / "control-check", CACHE / "control-check")):
            merge(source, target)
    finally:
        for lease in leases:
            lease.close()


def _remove_tree(folder, summary, counter):
    for base, _dirs, files in os.walk(folder, followlinks=False):
        for name in files:
            summary[counter] += 1
            summary["bytes_removed"] += (Path(base) / name).lstat().st_size
    shutil.rmtree(folder)


def retain_current(identity, run, directory=DIRECTORY, sessions=SESSIONS,
                   snapshots=True, require_complete=True, parsed=None):
    """Remove all other match caches; retain HTML, configuration and live sessions."""
    directory, sessions, run = Path(directory), Path(sessions), Path(run).resolve()
    summary = {"cache_files_removed": 0, "session_files_removed": 0,
               "bytes_removed": 0, "active_sessions_skipped": 0}
    with locked("cache-retention", directory):
        if require_complete:
            with locked(identity, directory):
                required = FILES + (SNAPSHOT_FILES if snapshots else ())
                if not _verified(directory / identity, required):
                    raise ValueError("Current replay cache is not complete")
                if not all((run / name).is_file() for name in required):
                    raise ValueError("Current replay session is not complete")
        for folder in directory.iterdir():
            if folder.name == identity or folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir():
                continue
            with locked(folder.name, directory):
                _remove_tree(folder, summary, "cache_files_removed")
        if sessions.exists():
            for folder in sessions.iterdir():
                if folder.is_symlink() or not folder.is_dir() or folder.resolve() == run:
                    continue
                with (folder / ".tick-cache.lock").open("a") as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        summary["active_sessions_skipped"] += 1
                        continue
                    try:
                        _remove_tree(folder, summary, "session_files_removed")
                    finally:
                        fcntl.flock(lease, fcntl.LOCK_UN)
        if parsed and not summary["active_sessions_skipped"]:
            current = Path(parsed).resolve()
            for path in (CACHE / "imports").glob("*.json"):
                if path.resolve() != current:
                    summary["bytes_removed"] += path.stat().st_size
                    summary["cache_files_removed"] += 1
                    path.unlink()
    return summary
