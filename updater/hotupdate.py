"""Merge fingerprint-verified hot assets into a fresh APK extraction before pruning."""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile

from neo_replay.assets import digest
from neo_replay import resourcefs

from .packages import safe_name


def regular_path(root, name):
    """Reject symlink components before reading or writing an exported asset."""
    root = Path(root).resolve()
    relative = safe_name(str(name))
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Hot-update paths cannot contain symlinks: {name}")
    return current


@contextmanager
def payload(path):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        root = path / "assets" if (path / "assets/fingerprint.json").is_file() else path
        yield root
        return
    from neo_replay.paths import CACHE
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hotupdate-", dir=CACHE) as temp:
        root = Path(temp)
        with zipfile.ZipFile(path) as z:
            for item in z.infolist():
                name = safe_name(item.filename)
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError(f"Hot update contains a symlink: {name}")
                target = root / name
                if item.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(item) as src, target.open("wb") as dst: shutil.copyfileobj(src, dst)
        yield root / "assets" if (root / "assets/fingerprint.json").is_file() else root


def fingerprint(root):
    path = regular_path(root, "fingerprint.json")
    if not path.is_file():
        raise ValueError("热更新缺少 fingerprint.json：请提供游戏完成联网更新后导出的资源目录或 ZIP。")
    data = json.loads(path.read_text())
    version = data.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid hot-update fingerprint version")
    entries = {}
    for row in data.get("files", []):
        name = str(safe_name(row["file"]))
        sha = row.get("sha", "")
        if name in entries or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError(f"Invalid/duplicate fingerprint entry: {name}")
        entries[name] = sha
    if not entries: raise ValueError("Hot-update fingerprint has no files")
    return data, entries


def runtime_database(path):
    """Inspect a device's expanded scene database without treating it as CDN bytes."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("缺少 MuMu 运行时资源数据库：" + str(path))
    try:
        # immutable avoids creating journal/WAL files beside a read-only export.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("MuMu 资源数据库未通过 SQLite 完整性校验")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"asset", "meta"}.issubset(tables):
                raise ValueError("MuMu 资源数据库缺少 asset/meta 表")
            for table in ("asset", "meta"):
                columns = {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
                if not {"path", "data"}.issubset(columns):
                    raise ValueError("MuMu 资源数据库表结构不兼容：" + table)
    except sqlite3.DatabaseError as error:
        raise ValueError("MuMu assets.scdb 不是有效的 SQLite 资源数据库：" + str(error)) from error
    return {"sha256": digest(path), "bytes": path.stat().st_size,
            "sqlite_integrity": "ok", "tables": ["asset", "meta"],
            "source": "MuMu runtime SQLite; separate from the fingerprint download SHA-1"}


def exported_runtime_databases(root, data, entries):
    """Allow only the explicitly recorded runtime SCDB exception to SHA-1 checks."""
    marker = regular_path(root, "asset-update-export.json")
    if not marker.is_file():
        return {}
    info = json.loads(marker.read_text())
    if (info.get("schema") != "neo-replay.asset-update-export.v1"
            or info.get("version") != data["version"]
            or info.get("fingerprint_sha") != data.get("sha")
            or info.get("fingerprint_sha256") != digest(root / "fingerprint.json")
            or info.get("fingerprint_stable") is not True):
        raise ValueError("MuMu 完整导出标记与 fingerprint 不一致或未完成稳定性校验")
    records = info.get("runtime_databases", {})
    if not isinstance(records, dict) or set(records) - {"assets.scdb"}:
        raise ValueError("不支持的 MuMu 运行时数据库记录")
    verified = {}
    for name, record in records.items():
        if (not isinstance(record, dict) or name not in entries
                or record.get("fingerprint_sha1") != entries[name]
                or record.get("stable") is not True or record.get("matching_reads") != 2):
            raise ValueError("MuMu 运行时数据库记录无效：" + name)
        actual = runtime_database(regular_path(root, name))
        if record.get("sha256") != actual["sha256"]:
            raise ValueError("MuMu 运行时数据库在导出后被修改：" + name)
        verified[name] = dict(actual, fingerprint_sha1=entries[name], stable=True, matching_reads=2)
    return verified


def apply(artwork, update):
    """Validate the whole effective fingerprint first; callers use disposable staging trees."""
    artwork = Path(artwork)
    base, _ = fingerprint(artwork)
    with payload(update) as root:
        data, entries = fingerprint(root)
        marker = regular_path(root, "battle-data-export.json")
        battle_only = marker.is_file() and json.loads(marker.read_text()).get("schema") == "neo-replay.battle-data-export.v1"
        runtime = exported_runtime_databases(root, data, entries)
        if battle_only and (root / "asset-update-export.json").is_file():
            raise ValueError("同一热更新目录不能同时标记为战斗表导出与完整资源导出")
        if battle_only:
            entries = {n: sha for n, sha in entries.items() if n.startswith("csv_logic/")}
            if not entries: raise ValueError("热更新清单没有战斗逻辑文件")
        if base["version"].split(".")[:2] != data["version"].split(".")[:2]:
            raise ValueError(f"热更新客户端分支不匹配：{base['version']} / {data['version']}")
        bad = []
        for name, sha in entries.items():
            if name in runtime:
                continue  # Already checked against export SHA-256 and SQLite integrity.
            src = regular_path(root, name)
            dst = regular_path(artwork, name)
            p = src if src.is_file() else dst
            if not p.is_file(): bad.append(name); continue
            with p.open("rb") as f: actual = hashlib.file_digest(f, "sha1").hexdigest()
            if actual != sha: bad.append(name)
        if bad:
            raise ValueError("热更新不完整或校验失败，尚未合并：" + ", ".join(bad[:8]))
        metadata = regular_path(root, "hotupdate-meta.json")
        effective = json.loads(metadata.read_text()).get("effective_at") if metadata.is_file() else None
        copied = []
        for name in entries:
            src = root / name
            if not src.is_file(): continue
            dst = artwork / name; dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst); copied.append(name)
        shutil.copy2(root / "fingerprint.json", artwork / "fingerprint.json")
        kind = ("fingerprint-verified-assets-with-runtime-db" if runtime else
                "fingerprint-verified-battle-data" if battle_only else "fingerprint-verified-hotupdate")
        provenance = {"kind": kind, "version": data["version"],
            "fingerprint_sha": data.get("sha"), "fingerprint_sha256": digest(root / "fingerprint.json"),
            "base_version": base["version"], "base_fingerprint_sha": base.get("sha"),
            "source": str(Path(update).expanduser().resolve()), "verified_files": len(entries) - len(runtime),
            "merged_files": copied, "effective_at": effective}
        provenance["scope"] = "All csv_logic entries verified against the official fingerprint; non-logic assets retain the supplied APK data." if battle_only else "All fingerprint entries verified."
        if runtime:
            provenance["runtime_databases"] = runtime
            provenance["scope"] = ("All effective fingerprint files except assets.scdb verified by SHA-1. "
                                   "MuMu runtime assets.scdb separately verified by export SHA-256 and SQLite integrity; "
                                   "its bytes are not claimed to match the fingerprint download SHA-1.")
        (artwork / ".hotupdate-provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2)+"\n")
        return provenance


def apply_to_player(artwork, update, profile, installed):
    """Verify the effective spectator files against MuMu on a disposable copy.

    The installed bundle has already discarded menu/audio assets. Requiring
    those files to exist would prevent incremental updates without an APKM.
    """
    from . import packing

    artwork = Path(artwork)
    base, base_entries = fingerprint(artwork)
    with payload(update) as root:
        data, entries = fingerprint(root)
        if (root / "battle-data-export.json").exists():
            raise ValueError("请使用 MuMu 完整导出，旧版仅战斗表导出不能同步画面资源。")
        if base["version"].split(".")[:2] != data["version"].split(".")[:2]:
            raise ValueError(f"MuMu 客户端分支已变化：{base['version']} → {data['version']}；请同时提供对应的 x86_64 APKM。")
        if tuple(map(int, data["version"].split("."))) < tuple(map(int, base["version"].split("."))):
            raise ValueError(f"MuMu 数据 {data['version']} 比已安装的 {base['version']} 旧。")
        runtime = exported_runtime_databases(root, data, entries)
        copied = set()
        for name, expected in entries.items():
            if packing.excluded(name):
                continue
            src = regular_path(root, name)
            if not src.is_file():
                continue
            if name not in runtime:
                with src.open("rb") as f:
                    if hashlib.file_digest(f, "sha1").hexdigest() != expected:
                        raise ValueError("MuMu 导出文件校验失败：" + name)
            dst = regular_path(artwork, name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.add(name)
        for name in base_entries.keys() - entries.keys():
            for path in {name, resourcefs.STARTUP_ALIASES.get(name, name)}:
                dst = regular_path(artwork, path)
                if dst.is_file():
                    dst.unlink()
        files = packing.selected(artwork, profile, "battle", inventory=entries)
        verified = 0
        reused = []
        bad = []
        for name, path in files.items():
            expected = entries.get(name)
            if expected is None or name in runtime:
                continue
            with path.open("rb") as f:
                actual = hashlib.file_digest(f, "sha1").hexdigest()
            if actual == expected:
                verified += 1
            elif (name in ("assets.scdb", "supercell_id_config.json") and name not in copied
                  and base_entries.get(name) == expected
                  and installed.get("data_provenance", {}).get("kind", "").startswith("fingerprint-verified")):
                # Player installation already checked its SHA-256. These two
                # files were transformed locally after official validation.
                reused.append(name)
            else:
                bad.append(name)
        if bad:
            raise ValueError("MuMu 缺少更新后的观战文件，请检查游戏是否更新完毕或提供对应 APKM："
                             + ", ".join(bad[:8]))
        shutil.copy2(root / "fingerprint.json", artwork / "fingerprint.json")
        provenance = {
            "kind": "fingerprint-verified-spectator-hotupdate", "version": data["version"],
            "fingerprint_sha": data.get("sha"), "fingerprint_sha256": digest(artwork / "fingerprint.json"),
            "base_version": base["version"], "base_fingerprint_sha": base.get("sha"),
            "source": str(Path(update).expanduser().resolve()),
            "verified_files": verified, "merged_files": sorted(copied.intersection(files)),
            "reused_unchanged_local_files": reused, "runtime_databases": runtime,
            "scope": "Verified retained fingerprint assets only; APK assets outside the fingerprint are unchanged.",
        }
        (artwork / ".hotupdate-provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")
        return provenance
