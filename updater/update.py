"""Sync official MuMu data and install a verified spectator bundle."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from neo_replay import assets, cache
from neo_replay.engine_lock import engine_access
from neo_replay.paths import ROOT, CACHE, GAME, read_user_settings

from . import hotupdate, packing
from .export_mumu_update import DEFAULT_MUMU_PATH, DEFAULT_SERIAL, export, find_adb
from .packages import extract_package

UPDATER = Path(__file__).resolve().parent
UPDATE_CONFIG = UPDATER / "config.json"


def configure(args):
    config_path, settings = read_user_settings(args.config, "mumu")
    fields = {"mumu_path", "adb_path", "serial", "apkm_path", "assets_package"}
    if settings.keys() - fields:
        raise ValueError(f"更新配置仅支持这些字段：{', '.join(sorted(fields))}")
    for key, value in settings.items():
        if not isinstance(value, str):
            raise ValueError(f"更新配置 {key} 必须是字符串。")

    def config_path_for(key):
        value = settings.get(key, "").strip()
        return (config_path.parent / Path(value).expanduser()).resolve() if value else None

    args.mumu_path = config_path_for("mumu_path") or DEFAULT_MUMU_PATH
    args.adb = args.adb or config_path_for("adb_path")
    args.apkm = args.apkm or config_path_for("apkm_path")
    args.assets_package = args.assets_package or config_path_for("assets_package")
    args.serial = args.serial if args.serial is not None else settings.get("serial", DEFAULT_SERIAL).strip()
    if not args.serial:
        raise ValueError("更新配置 serial 不能为空。")
    return args


def owned_bundle(path):
    """Only generated top-level game bundles may be retired automatically."""
    path = Path(path)
    if path.is_symlink() or path.parent != ROOT or not path.name.startswith("game"):
        return False
    try:
        manifest = json.loads((path / "manifest.json").read_text())
        return manifest.get("schema") == 1 and bool(manifest.get("libg_sha256"))
    except (OSError, ValueError):
        return False


def activate(staged_game, staged_catalog, workspace):
    """Install only after all validation, restoring the old pair on exceptions."""
    destination = GAME
    catalog = ROOT / "neo_replay/catalog.json"
    if destination.exists() and not owned_bundle(destination):
        raise ValueError("game/ 不是本工具生成的资源目录，未覆盖。请先移走该目录。")
    # Keep rollback material outside TemporaryDirectory: an exceptional rollback
    # must never let temporary-workspace cleanup delete the only old bundle.
    recovery = ROOT / ".update-rollback"
    if recovery.exists():
        raise RuntimeError(f"存在上次更新的回滚目录，未覆盖：{recovery}")
    recovery.mkdir()
    try:
        shutil.copy2(catalog, recovery / "catalog.json")
    except BaseException:
        # Nothing live has moved yet; an incomplete recovery header must not
        # block the next update after a full disk or interrupted write.
        shutil.rmtree(recovery)
        raise
    backup = recovery / "game"
    moved_old = installed = False
    try:
        if destination.exists():
            destination.rename(backup)
            moved_old = True
        Path(staged_game).rename(destination)
        installed = True
        Path(staged_catalog).replace(catalog)
    except BaseException as failure:
        errors = []
        try:
            if installed:
                destination.rename(Path(workspace) / "rejected-game")
            if moved_old:
                backup.rename(destination)
        except OSError as error:
            errors.append(str(error))
        try:
            restored_catalog = Path(workspace) / "restored-catalog.json"
            shutil.copy2(recovery / "catalog.json", restored_catalog)
            restored_catalog.replace(catalog)
        except OSError as error:
            errors.append(str(error))
        if errors:
            raise RuntimeError(f"更新失败且回滚未全部完成，原文件保留在 {recovery}：" + "; ".join(errors)) from failure
        shutil.rmtree(recovery)
        raise
    def retire(path):
        try:
            shutil.rmtree(path)
        except OSError as error:
            print(f"资源已更新，但旧文件未清理：{path}（{error}）", flush=True)
    retire(recovery)
    for name in ("replays", "sessions", "imports", "preview"):
        shutil.rmtree(CACHE / name, ignore_errors=True)
    return destination


def verify_export(update):
    with hotupdate.payload(update) as root:
        marker = root / "asset-update-export.json"
        if not marker.is_file():
            raise ValueError("请选择更新器从 MuMu 导出的完整资源目录或 ZIP。")
        metadata = json.loads(marker.read_text())
        if metadata.get("package") != "com.supercell.clashroyale":
            raise ValueError("导出来源不是 MuMu 中的官方皇室战争客户端。")
        data, entries = hotupdate.fingerprint(root)
        hotupdate.exported_runtime_databases(root, data, entries)
        build = metadata.get("client_build")
        if not isinstance(build, str) or not build.isdecimal():
            raise ValueError("导出没有记录 MuMu 的 APK 版本，请用当前更新器重新导出。")
        return build


def run(args):
    packages = [Path(p).expanduser().resolve() for p in (args.apkm, args.assets_package) if p]
    if args.assets_package and not args.apkm:
        raise ValueError("--assets-package 需与 --apkm 一起使用。")
    for package in packages:
        if not package.is_file():
            raise ValueError(f"找不到安装包：{package}")
    adb = find_adb(args.adb, mumu_path=args.mumu_path)
    with engine_access(updating=True):
        cache.migrate_legacy()
        update_cache = CACHE / "updates"
        update_cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".work-", dir=update_cache) as temp:
            workspace = Path(temp)
            live_game = GAME
            old = json.loads((live_game / "manifest.json").read_text()) if owned_bundle(live_game) else None
            if not packages and not old:
                raise ValueError("首次安装请同时提供包含 x86_64 分包的 --apkm。")
            print("1/5 读取 MuMu 中已完成的官方更新…", flush=True)
            update = workspace / "mumu-update"
            export(adb, args.serial, update)
            mumu_build = verify_export(update)
            extracted = workspace / "source"
            engine, artwork = extracted / "engine", extracted / "assets"
            if packages:
                print("2/5 提取新 APKM 的 x86_64 引擎和资源…", flush=True)
                engine.mkdir(parents=True)
                artwork.mkdir()
                for package in packages:
                    extract_package(package, engine, artwork, workspace)
            else:
                print("2/5 校验现有观战包，复用当前 x86_64 引擎…", flush=True)
                if not packing.audit(live_game):
                    raise ValueError("现有包已被修改或缺文件，请用匹配的 APKM 和 MuMu 重新同步。")
                shutil.copytree(live_game / "engine", engine)
                shutil.copytree(live_game / "assets", artwork)
            missing = assets.missing_files(engine, artwork)
            if missing:
                raise ValueError("缺少 x86_64 库或观战资源：" + ", ".join(missing[:6]))
            sha = assets.digest(engine / "libg.so")
            build = str(assets.supported().get(sha, ""))
            from neo_replay.native_snapshots import SUPPORTED_BUILDS
            if build not in SUPPORTED_BUILDS:
                raise ValueError(f"此 APKM 的引擎尚未适配：SHA256={sha}。旧版未更改。")
            if build != mumu_build:
                raise ValueError(f"MuMu APK 为 {mumu_build}，本次 x86_64 引擎为 {build}。请提供与 MuMu 对应的 x86_64 APKM；旧版未更改。")
            profile_path = args.profile or UPDATER / "profiles" / f"observed-{build}.json"
            profile = json.loads(profile_path.read_text())
            if build != str(profile["build"]):
                raise ValueError("引擎版本与资源清单不一致；旧版未更改。")
            print("3/5 按 MuMu fingerprint 同步，再裁剪观战资源…", flush=True)
            provenance = (hotupdate.apply(artwork, update) if packages else
                          hotupdate.apply_to_player(artwork, update, profile, old))
            previous_version = old.get("data_provenance", {}).get("version") if old else None
            if previous_version and tuple(map(int, previous_version.split("."))) > tuple(map(int, provenance["version"].split("."))):
                raise ValueError(f"MuMu 数据 {provenance['version']} 比已安装的 {previous_version} 旧。")
            staged = workspace / "new-game"
            manifest = packing.build(artwork, engine, staged, profile_path, "battle", packages)
            manifest["data_provenance"]["mumu_client_build"] = mumu_build
            if not packages:
                manifest["packages"] = old.get("packages", [])
            print("4/5 同步卡牌目录，校验文件并试渲染一局…", flush=True)
            from .sync_catalog import sync
            from .rendercheck import check
            from studio import x86_command
            catalog = workspace / "catalog.json"
            sync(catalog, game=staged)
            if not packing.audit(staged):
                raise ValueError("候选资源包校验失败；旧版未更改。")
            manifest["render_check"] = check(staged, workspace / "render-check", x86_command)
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            manifest["update_pipeline"] = "mumu-sync-v2"
            (staged / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
            print("5/5 替换旧资源并清理旧回放缓存…", flush=True)
            destination = activate(staged, catalog, workspace)
        print(f"已同步 MuMu {provenance['version']}（引擎 {build}）→ {destination}", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="从 MuMu 同步皇室战争数据并更新本地回放器")
    parser.add_argument("--config", type=Path, default=UPDATE_CONFIG,
                        help="更新配置文件，默认读取 updater/config.json")
    parser.add_argument("--apkm", type=Path, help="只有换客户端或首次安装才需提供匹配的 x86_64 APKM")
    parser.add_argument("--assets-package", type=Path, help="APKM 以外的可选资源分包")
    parser.add_argument("--mumu", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--adb", type=Path, help="本次运行覆盖配置中的 ADB 路径")
    parser.add_argument("--serial", help="本次运行覆盖配置中的 MuMu ADB 设备地址")
    parser.add_argument("--profile", type=Path, help="覆盖此引擎的资源清单")
    try:
        return run(configure(parser.parse_args(argv)))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
