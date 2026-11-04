"""Local HTML library: stable replay IDs, retained input, and preparation-cache links."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import fcntl

from .paths import ROOT

DIRECTORY = ROOT / "replays"


def index():
    path = DIRECTORY / "index.json"
    return json.loads(path.read_text()) if path.exists() else {}


def resolve(value):
    raw = str(value).strip()
    candidate = Path(raw).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    try:
        parts = shlex.split(raw)
        if len(parts) == 1:
            candidate = Path(parts[0]).expanduser()
            if candidate.is_file():
                return candidate.resolve()
    except ValueError:
        pass
    records = index()
    if raw in records:
        candidate = DIRECTORY / records[raw]["html"]
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(f"找不到 HTML 文件或缓存对局编号：{raw}")


def remember(html, replay, identity, report):
    DIRECTORY.mkdir(exist_ok=True)
    name = f"{replay['tag']}-{replay['source_sha256'][:12]}.html"
    target = DIRECTORY / name
    if Path(html).resolve() != target.resolve():
        shutil.copy2(html, target)
    with (DIRECTORY / "index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = index()
        records[replay["tag"]] = {"html": name, "cache_key": identity,
            "players": [p["name"] for p in replay["players"]],
            "build": report["build"], "end_tick": report["end_tick"],
            "fidelity": report.get("fidelity"), "unexecuted_cards": report.get("unexecuted_cards", 0),
            "terminal_validation": report.get("terminal_validation"),
            "updated": datetime.now(timezone.utc).isoformat()}
        temp = DIRECTORY / "index.tmp.json"
        temp.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
        temp.replace(DIRECTORY / "index.json")
    return target
