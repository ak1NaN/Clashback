"""Deterministic, silent spectator bundles from local APK/APKM/XAPK or asset folders.

The native engine is version-pinned. New artwork can be extracted with the same
workflow; unknown engine bytes must be ported/validated before they can execute.
"""
import fnmatch
import json
from pathlib import Path
import shutil
import tempfile
import sqlite3
import subprocess
import sys

from neo_replay import assets, resourcefs
from neo_replay.paths import ROOT

COMBAT = ("sc/chr_*", "sc/building_*", "sc/spell_*", "sc/effects*", "sc/vfx_*", "sc/ability_*",
          "sc/ui_card_*", "sc3d/characters/*", "sc3d/shared/*", "sc3d/effects/*",
          "sc3d/props/elixir_pump/*", "sc3d/props/fisherman/*")

# Menu texture families. The native startup loader still opens scene
# descriptors, and eagerly loads these few menu atlases before the replay.
MENU_ATLASES = (
    "ui_arena_icon", "ui_autochess_badges", "ui_autochess_ingame", "ui_autochess_meta",
    "ui_battle_pass", "ui_chain_offer", "ui_chest",
    "ui_clan_rush", "ui_clan_wars", "ui_collection_event", "ui_cosmetic_collection",
    "ui_custom_offer", "ui_deck_builder", "ui_deck_skin_customization", "ui_deck_wizard",
    "ui_flash_offer", "ui_giveaway_tutorial", "ui_hero_form_reveal", "ui_main_screen", "ui_milestone_event",
    "ui_mini_chain_offer", "ui_mini_pass", "ui_multireward_popup", "ui_mystery_box",
    "ui_season_popup", "ui_shop_background", "ui_snippet_collection",
    "ui_snippet_season", "ui_streak_event", "ui_support_cards_autochess", "ui_survey_popup",
    "ui_terms_of_service", "ui_tower_skins", "ui_trophy_road",
)
BOOTSTRAP_MENU_TEXTURES = frozenset({
    "sc/ui_battle_pass_0.sctx",
    "sc/ui_chest_0.sctx", "sc/ui_chest_1.sctx", "sc/ui_chest_2.sctx",
    "sc/ui_clan_wars_0.sctx", "sc/ui_clan_wars_1.sctx",
    "sc/ui_mystery_box_0.sctx", "sc/ui_mystery_box_1.sctx",
})
PREVIEW_MODELS = frozenset({
    "sc3d/characters/hero/hero_goblins/model/hero_goblins_lookdev_lod0.glb",
    "sc3d/characters/hero/hero_tombstone/model/hero_tombstone_lookdev_test.glb",
    "sc3d/characters/hero/hero_wizard/model/hero_wizard_lod0_lookdev.glb",
})


def menu_atlas(name):
    if not name.startswith("sc/"):
        return False
    stem = Path(name).stem
    return any(stem == prefix or stem.startswith(prefix + "_") for prefix in MENU_ATLASES)


def excluded(name):
    if resourcefs.arena_asset(name) and not name.startswith(("sc/level_bootcamp_arena_dl", "sc/level_ronintemple")):
        return True
    if name.startswith(("sc3d/maps/", "locations/", "tilemaps/")) and name not in ("locations/ronintemple_arena.csv", "tilemaps/tilemap.csv"):
        return True
    if (name in PREVIEW_MODELS or resourcefs.audio(name) or "/test/" in name
            or name.startswith(("flutter_assets/", "sc3d/test/"))):
        return True
    if name.endswith(".sctx") and name not in BOOTSTRAP_MENU_TEXTURES:
        return menu_atlas(name) or name.startswith(("sc/emotes_", "sc/towerskin_", "sc/Chibi"))
    return False


def prune_scene_index(artwork):
    """Remove the scene registrations for deliberately excluded assets."""
    database = Path(artwork) / "assets.scdb"
    with sqlite3.connect(database) as connection:
        names = [r[0] for r in connection.execute("SELECT path FROM meta")
                 if r[0].startswith(("sc/level_", "sc/arena_", "sc3d/maps/"))
                 and not r[0].startswith(("sc/level_bootcamp_arena_dl", "sc/level_ronintemple", "sc/level_decos"))]
        for table in ("asset", "meta"):
            connection.executemany("DELETE FROM " + table + " WHERE path=?", [(n,) for n in names])
        connection.commit()
        connection.execute("VACUUM")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Pruned scene database failed integrity check")
    return names


def selected(source, profile, mode, *, inventory=()):
    available = {p.relative_to(source).as_posix(): p for p in source.rglob("*") if p.is_file()}
    for original, mapped in resourcefs.STARTUP_ALIASES.items():
        if original not in available and mapped in available: available[original] = available[mapped]
    known = available.keys() | set(inventory)
    wanted = (set(profile["files"]) - set(resourcefs.STARTUP_ALIASES.values())) | {"version.number", "locations/ronintemple_arena.csv", "tilemaps/tilemap.csv",
        "sc/level_bootcamp_arena_dl.sc", "sc/level_bootcamp_arena_dl_0.sctx", "sc/level_ronintemple_arena.sc", "sc/level_ronintemple_arena_0.sctx", "sc/level_ronintemple_arena_1.sctx"}
    # The client preloads registry scenes even for direct replay. Resolve SC
    # references from current CSVs so new banners/emotes/tower skins survive
    # pruning. Arena/audio exclusions below still apply.
    from neo_replay.game_data import table
    scene_files = set()
    custom_banners = set()
    shared_scenes = set()
    for registry in (source / "csv_logic").glob("*.csv"):
        for row in table(registry):
            references = {v for v in row.values() if isinstance(v, str) and v.startswith("sc/") and v.endswith(".sc")}
            scene_files.update(references)
            if (registry.name == "banners.csv" and (row.get("OwnedByDefault") or "").lower() not in ("true", "1")
                    and not row["Name"].startswith("Default")):
                custom_banners.update(references)
            else:
                shared_scenes.update(references)
    scene_files.intersection_update(known)
    wanted.update(scene_files)
    prefixes = tuple(n.removesuffix(".sc") + "_" for n in scene_files)
    if prefixes:
        wanted.update(n for n in known if n.startswith(prefixes))
    # Preserve complete logic schemas. They are small, mutually linked, and
    # cutting rows would change simulation/card IDs, not just presentation.
    wanted.update(n for n in known if n.startswith(("csv_logic/", "csv_client/", "sc3d/shaders/")))
    if mode == "battle":
        wanted.update(n for n in known if any(fnmatch.fnmatchcase(n, g) for g in COMBAT))
    banner_textures = {n for n in wanted if n.endswith(".sctx") and
                       n.rsplit("_", 1)[0] + ".sc" in custom_banners - shared_scenes}
    wanted = {n for n in wanted if not excluded(n)} - banner_textures
    missing = sorted(wanted - available.keys())
    if missing:
        raise ValueError("Profile does not match these assets; first missing files: " + ", ".join(missing[:8]))
    return {n: available[n] for n in sorted(wanted)}


def build(source, engine, destination, profile_path, mode="battle", package_sources=()):
    source, engine, destination = map(lambda p: Path(p).resolve(), (source, engine, destination))
    if destination == ROOT or destination == source or destination in source.parents or destination.exists():
        raise ValueError("Output must be a NEW directory, outside the source assets")
    profile = json.loads(Path(profile_path).read_text())
    sha = assets.digest(engine / "libg.so")
    supported = assets.supported()
    if sha not in supported:
        raise ValueError(f"Unknown libg.so SHA-256 {sha}. Extraction is possible, but engine bindings must be ported before packaging/running.")
    if str(supported[sha]) != str(profile["build"]):
        raise ValueError("Engine build and resource profile do not match")
    files = selected(source, profile, mode)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".spectator-", dir=destination.parent) as temp:
        stage = Path(temp) / "game"
        output = stage / "assets"
        output.mkdir(parents=True)
        records = []
        for n, original in files.items():
            packed_name = resourcefs.STARTUP_ALIASES.get(n, n)
            dest = output / packed_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, dest)
            if n == "supercell_id_config.json":
                data = json.loads(dest.read_text()); data["Enabled"] = False
                dest.write_text(json.dumps(data, indent=2) + "\n")
            records.append({"path": packed_name, "source_path": n, "bytes": dest.stat().st_size, "sha256": assets.digest(dest)})
        removed_registrations = prune_scene_index(output)
        for item in records:
            if item["path"] == "assets.scdb":
                item.update(bytes=(output / item["path"]).stat().st_size, sha256=assets.digest(output / item["path"]))
        storage = {}
        if sys.platform == "darwin":
            # Compress after rewriting the SQLite index. macOS decompresses
            # transparently, so native reads and manifest hashes still see the
            # original asset bytes.
            compressed = Path(temp) / "compressed-assets"
            subprocess.run(["/usr/bin/ditto", "--hfsCompression", str(output), str(compressed)], check=True)
            allocated = sum((output / item["path"]).stat().st_blocks * 512 for item in records)
            compressed_allocated = sum((compressed / item["path"]).stat().st_blocks * 512 for item in records)
            if compressed_allocated < allocated:
                shutil.rmtree(output)
                compressed.rename(output)
            storage = {"allocated_bytes": min(allocated, compressed_allocated),
                       "saved_bytes": max(0, allocated - compressed_allocated)}
        lib = stage / "engine"
        lib.mkdir()
        for n in ("libg.so", "libc++_shared.so"):
            shutil.copy2(engine / n, lib / n)
        included = {p.resolve() for p in files.values()}
        removed = [{"path": p.relative_to(source).as_posix(), "bytes": p.stat().st_size}
                   for p in sorted(source.rglob("*")) if p.is_file() and p.resolve() not in included]
        manifest = {"schema": 1, "build": str(supported[sha]), "libg_sha256": sha, "mode": mode,
                    "profile": Path(profile_path).name, "audio": False,
                    "scope": "battle assets plus measured bootstrap dependencies" if mode == "battle" else "measured example replay; other decks need a new profile",
                    "assets_bytes": sum(r["bytes"] for r in records), "removed_bytes": sum(r["bytes"] for r in removed),
                    "files": records, "removed": removed,
                    "packages": [{"name": Path(p).name, "sha256": assets.digest(p)} for p in package_sources]}
        manifest["engine_files"] = [{"path": p.name, "bytes": p.stat().st_size, "sha256": assets.digest(p)} for p in sorted(lib.iterdir())]
        if storage:
            manifest["assets_storage"] = storage
        manifest.update(default_location=resourcefs.DEFAULT_LOCATION, removed_scene_registrations=removed_registrations)
        provenance = source / ".hotupdate-provenance.json"
        if provenance.is_file():
            manifest["data_provenance"] = json.loads(provenance.read_text())
        elif (source / "fingerprint.json").is_file():
            fp = json.loads((source / "fingerprint.json").read_text())
            manifest["data_provenance"] = {"kind": "apk-only", "version": fp.get("version"),
                "fingerprint_sha": fp.get("sha"), "fingerprint_sha256": assets.digest(source / "fingerprint.json")}
        manifest["validated_html_sha256"] = profile.get("validated_html_sha256", [r["html_sha256"] for r in profile.get("evidence", [])])
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        stage.rename(destination)
    print(f"Packed {mode}: {len(records)} assets, {manifest['assets_bytes']/2**20:.2f} MiB; removed {manifest['removed_bytes']/2**20:.2f} MiB")
    return manifest


def audit(game):
    game = Path(game)
    manifest = json.loads((game / "manifest.json").read_text())
    failures = []
    for item in manifest["files"]:
        p = game / "assets" / item["path"]
        if not p.is_file() or assets.digest(p) != item["sha256"]:
            failures.append(item["path"])
    expected = {r["path"] for r in manifest["files"]}
    extra = [p.relative_to(game / "assets").as_posix() for p in (game / "assets").rglob("*")
             if p.is_file() and p.relative_to(game / "assets").as_posix() not in expected]
    links = [str(p) for p in game.rglob("*") if p.is_symlink()]
    audio = [p.relative_to(game).as_posix() for p in game.rglob("*") if p.is_file() and resourcefs.audio(p.relative_to(game).as_posix().removeprefix("assets/"))]
    libraries = manifest.get("engine_files", [{"path": "libg.so", "sha256": manifest["libg_sha256"]}])
    engine_ok = all((game / "engine" / r["path"]).is_file() and assets.digest(game / "engine" / r["path"]) == r["sha256"] for r in libraries)
    result = {"ok": not (failures or extra or links or audio) and engine_ok,
              "checked_assets": len(expected), "bad_hashes": failures, "extra_files": extra,
              "symlinks": links, "audio_files": audio, "engine_hash_ok": engine_ok}
    print(json.dumps(result, indent=2))
    return result["ok"]
