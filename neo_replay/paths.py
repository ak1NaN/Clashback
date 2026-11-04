"""All project-owned files live below this directory."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
CACHE = ROOT / "cache"
CRX = ROOT / "vendor" / "crx"
GAME = ROOT / "game"
ENGINE_LOCK = CACHE / ".engine.lock"
LEGACY_GAME_CONFIG = ROOT / "config" / "game.json"
USER_CONFIG = ROOT / "config.json"
VIEWER = ROOT / "Clashback.app/Contents/MacOS/Clashback"


def read_user_settings(path, section):
    """Return the selected settings and the path used to resolve relative values."""
    path = Path(path).expanduser().resolve()
    if section == "player" and path == USER_CONFIG and not path.exists():
        legacy = ROOT / "config/player.json"
        if legacy.is_file():
            path = legacy
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 JSON 对象：{path}")
    if "player" in value or "mumu" in value:
        value = value.get(section, {})
        if not isinstance(value, dict):
            raise ValueError(f"配置 {section} 必须是 JSON 对象：{path}")
    return path, value


def config():
    manifest_path = GAME / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        cfg = {key: manifest[key] for key in ("build", "libg_sha256", "mode")}
        cfg.update(engine=str(GAME / "engine"), assets=str(GAME / "assets"),
                   audio=manifest.get("audio", False),
                   validated_html_sha256=manifest.get("validated_html_sha256", []))
    elif LEGACY_GAME_CONFIG.is_file():
        cfg = json.loads(LEGACY_GAME_CONFIG.read_text())
    else:
        return {}
    for name in ("engine", "assets"):
        value = Path(cfg[name]).expanduser()
        cfg[name] = str((ROOT / value).resolve() if not value.is_absolute() else value.resolve())
    if os.environ.get("NEO_ASSET_ROOT"):
        cfg["assets"] = str(Path(os.environ["NEO_ASSET_ROOT"]).expanduser().resolve())
    if os.environ.get("NEO_ENGINE_ROOT"):
        cfg["engine"] = str(Path(os.environ["NEO_ENGINE_ROOT"]).expanduser().resolve())
        from .assets import digest, supported
        sha = digest(Path(cfg["engine"]) / "libg.so")
        if sha not in supported():
            raise ValueError(f"Unknown engine {sha}; port the bindings before executing a new build")
        cfg.update(libg_sha256=sha, build=str(supported()[sha]))
        if os.environ.get("NEO_ASSET_ROOT"):
            # The builder profiles new HTML against an explicit complete source,
            # even when invoked from a previously measured small bundle.
            cfg["mode"] = "profiling"
    return cfg


def native_env():
    env = dict(os.environ)
    env.update(CRX_BIONIC_MAC=str(RUNTIME / "native" / "bionic_mac.dylib"),
               CRX_ANGLE=str(RUNTIME / "angle"), PYTHONUNBUFFERED="1",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def data_provenance(assets):
    manifest = Path(assets).parent / "manifest.json"
    if manifest.is_file():
        return json.loads(manifest.read_text()).get("data_provenance", {}) or {"kind": "apk-data"}
    staged = Path(assets) / ".hotupdate-provenance.json"
    return json.loads(staged.read_text()) if staged.is_file() else {"kind": "apk-data"}
