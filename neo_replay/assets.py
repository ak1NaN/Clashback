"""Game asset checks shared by playback and installation."""
import hashlib
import json
from pathlib import Path

from .paths import CRX


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def supported():
    return {json.loads(p.read_text())["libg_sha256"]: json.loads(p.read_text())["build"]
            for p in (CRX / "bindings").glob("[0-9]*.json")}


def missing_files(engine, assets):
    missing = [str(engine / n) for n in ("libg.so", "libc++_shared.so") if not (engine / n).is_file()]
    missing += [str(assets / n) for n in ("version.number", "csv_logic", "sc", "sc3d") if not (assets / n).exists()]
    if not (assets / "shader").exists() and not (assets / "sc3d/shaders").exists():
        missing.append(str(assets / "shader") + " (or sc3d/shaders)")
    return missing

