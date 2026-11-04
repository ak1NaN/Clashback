#!/usr/bin/env python3
"""Read updated official game assets from MuMu; never change the device."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neo_replay.assets import digest
from updater.hotupdate import fingerprint, regular_path, runtime_database

DEFAULT_MUMU_PATH = Path("/Applications/MuMuPlayer.app")
BUNDLED_ADB = Path("Contents/MacOS/MuMuEmulator.app/Contents/MacOS/tools/adb")
DEFAULT_SERIAL = "127.0.0.1:16384"
REMOTE = "/data/user/0/com.supercell.clashroyale/update"
PACKAGE = "com.supercell.clashroyale"


def find_adb(adb=None, *, mumu_path=DEFAULT_MUMU_PATH):
    if adb is not None:
        candidate = Path(adb).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
        resolved = shutil.which(str(adb))
        if resolved:
            return Path(resolved)
        raise ValueError("找不到可执行的 ADB：" + str(adb))
    bundled = Path(mumu_path).expanduser() / BUNDLED_ADB
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled.resolve()
    raise ValueError(f"在 MuMu 安装路径下找不到可执行的 ADB：{bundled}。请修改 updater/config.json 的 mumu_path 或 adb_path。")


def export(adb=None, serial=DEFAULT_SERIAL, output=None, *, battle_only=False):
    """Atomically export present fingerprint assets; APK supplies unchanged files.

    The legacy positional (adb, serial, output) API remains supported. Default
    scope includes artwork, shaders and client tables as well as battle logic.
    """
    if output is None:
        raise ValueError("请指定导出目录")
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise ValueError("输出目录已存在，请指定一个新目录：" + str(output))
    adb = find_adb(adb)
    if not serial or not isinstance(serial, str):
        raise ValueError("请通过 --serial 指定 MuMu ADB 设备编号")

    def run(*args):
        try:
            result = subprocess.run([str(adb), "-s", serial, *args], capture_output=True, timeout=120)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"读取 MuMu 超时（{serial}）；请确认设备已启动，官方游戏已完成联网更新。") from error
        if result.returncode:
            detail = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
            raise ValueError(f"读取 MuMu 失败（{serial}）：{detail}\n请确认 MuMu 已启动、ADB 设备编号正确且官方游戏已完成更新。")
        return result

    try:
        state = subprocess.run([str(adb), "-s", serial, "get-state"], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"MuMu ADB 状态检查超时（{serial}），请先启动 MuMu。") from error
    if state.returncode or state.stdout.strip() != b"device":
        local = re.fullmatch(r"(?:127\.0\.0\.1|localhost):(\d+)", serial)
        if local and 1 <= int(local.group(1)) <= 65535:
            # A local ADB connection is not an emulator start or a root command.
            # Never initiate a connection to an arbitrary remote host.
            try:
                connected = subprocess.run([str(adb), "connect", serial], capture_output=True, timeout=15)
            except subprocess.TimeoutExpired as error:
                raise ValueError(f"无法连接 MuMu（{serial}），请先启动对应的 MuMu 设备。") from error
            if connected.returncode:
                detail = connected.stderr.decode(errors="replace").strip() or connected.stdout.decode(errors="replace").strip()
                raise ValueError(f"无法连接 MuMu（{serial}）：{detail}")
            state = run("get-state")
        if state.returncode or state.stdout.strip() != b"device":
            raise ValueError(f"MuMu 设备未连接（{serial}）；请先启动设备并确认 --serial，工具不会自动连接远程设备。")

    if b"uid=0(" not in run("shell", "id").stdout:
        raise ValueError("读取游戏私有资源目录需要 MuMu 已启用 root；工具不会修改设备权限，请在 MuMu 设置中启用后重试。")

    def client_build():
        report = run("shell", "dumpsys", "package", PACKAGE).stdout
        version = re.search(rb"(?m)^\s*versionCode=(\d+)\b", report)
        if not version:
            raise ValueError("无法读取 MuMu 皇室战争的 APK versionCode。")
        return version[1].decode()

    build = client_build()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mumu-export-", dir=output.parent) as temporary:
        stage = Path(temporary) / "assets"
        stage.mkdir()
        raw_fingerprint = run("exec-out", "cat", REMOTE + "/fingerprint.json").stdout
        (stage / "fingerprint.json").write_bytes(raw_fingerprint)
        data, entries = fingerprint(stage)
        # Only filenames named by the official manifest are pulled; no account,
        # preferences or other private game files are exported.
        listing = run("exec-out", "find", REMOTE, "-type", "f").stdout.decode("utf-8")
        present = {line[len(REMOTE) + 1:] for line in listing.splitlines() if line.startswith(REMOTE + "/")}
        selected = sorted(name for name in entries if name in present
                          and (not battle_only or name.startswith("csv_logic/")))
        provided, databases = [], {}
        for name in selected:
            target = regular_path(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            run("pull", REMOTE + "/" + name, str(target))
            with target.open("rb") as source:
                sqlite_header = source.read(16) == b"SQLite format 3\x00"
            if name == "assets.scdb" and sqlite_header:
                databases[name] = dict(runtime_database(target), fingerprint_sha1=entries[name])
            else:
                with target.open("rb") as source:
                    actual = hashlib.file_digest(source, "sha1").hexdigest()
                if actual != entries[name]:
                    raise ValueError("导出资源与 fingerprint 不符，未保存导出：" + name)
            provided.append(name)
        # The fingerprint can stay unchanged while the client updates its local
        # SQLite index. Re-read the database after all other asset transfers.
        for name, record in databases.items():
            repeated = Path(temporary) / "database-recheck" / name
            repeated.parent.mkdir(parents=True, exist_ok=True)
            run("pull", REMOTE + "/" + name, str(repeated))
            if digest(repeated) != record["sha256"]:
                raise ValueError("导出期间 MuMu 资源数据库发生变化，未保存导出；请等游戏更新结束后重试。")
            record["stable"] = True
            record["matching_reads"] = 2
        latest = run("exec-out", "cat", REMOTE + "/fingerprint.json").stdout
        if latest != raw_fingerprint or client_build() != build:
            raise ValueError("导出期间游戏版本发生变化，未保存导出；请等更新结束后重试。")
        info = {"schema": "neo-replay.battle-data-export.v1" if battle_only else "neo-replay.asset-update-export.v1",
                "package": PACKAGE, "client_build": build, "version": data["version"],
                "fingerprint_sha": data.get("sha"), "fingerprint_sha256": digest(stage / "fingerprint.json"),
                "fingerprint_stable": True, "provided_files": provided,
                "provided_logic_files": [name for name in provided if name.startswith("csv_logic/")],
                "runtime_databases": databases}
        if battle_only:
            info["scope"] = "csv_logic only; install verifies all effective logic files using APK fallback."
            marker = "battle-data-export.json"
        else:
            info["scope"] = ("All present fingerprint assets exported; install verifies every remaining file using APK fallback. "
                             "Runtime assets.scdb uses separately recorded SHA-256 and SQLite integrity, not download SHA-1.")
            marker = "asset-update-export.json"
        (stage / marker).write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
        if output.exists():
            raise ValueError("导出目标在处理中被创建，未覆盖：" + str(output))
        stage.rename(output)
    return info


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--adb", type=Path, help="指定 ADB，默认使用 MuMu 自带的版本")
    p.add_argument("--serial", default=DEFAULT_SERIAL)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--battle-only", action="store_true", help="兼容旧流程：只导出 csv_logic 战斗数据")
    a = p.parse_args()
    info = export(a.adb, a.serial, a.out, battle_only=a.battle_only)
    print(f"Exported official {info['version']}: {len(info['provided_files'])} assets "
          f"({len(info['runtime_databases'])} runtime database) -> {a.out}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        raise SystemExit(f"ERROR: {e}")
