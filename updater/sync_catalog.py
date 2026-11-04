#!/usr/bin/env python3
"""Regenerate card IDs/forms from the installed game data, preserving HTML aliases."""
import json
from pathlib import Path
import re
import sys
import argparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neo_replay import game_data
from neo_replay.paths import config


def sync(output=None, *, game=None):
    installed = json.loads((Path(game) / "manifest.json").read_text()) if game else config()
    artwork = Path(game) / "assets" if game else Path(installed["assets"])
    path = ROOT / "neo_replay/catalog.json"
    old = json.loads(path.read_text())
    previous = {r["id"]: r for r in old["cards"]}
    hero_names = set()
    for file in (artwork / "csv_logic/characters/hero_form").glob("*.toml"):
        source = game_data.text(file)
        hero_names.update(re.findall(r"\[SPELL_CHARACTER\.([^\]]+)\]", source))
        hero_names.update(re.findall(r"\[SPELL_BUILDING\.([^\]]+)\]", source))
        hero_names.update(re.findall(r"\[SPELL_OTHER\.([^\]]+)\]", source))
    norm = lambda s: re.sub("[^a-z0-9]", "", s.lower())
    cards = []
    for filename, base_id in (("spells_characters.csv", 26000000), ("spells_buildings.csv", 27000000), ("spells_other.csv", 28000000)):
        for index, row in enumerate(game_data.table(artwork / "csv_logic" / filename)):
            card_id = base_id + index
            original = previous.get(card_id, {})
            if original and original["name"] != row["Name"]:
                raise ValueError(f"Native card ID mapping changed: {card_id}: {original['name']} -> {row['Name']}")
            aliases = set(original.get("aliases", []))
            aliases.update(norm(v) for v in (row["Name"], row.get("IconFile", ""), row.get("TID", ""), Path(row.get("HighresImageFilename", "")).stem) if v)
            cards.append({"id": card_id, "name": row["Name"], "aliases": sorted(aliases),
                "rarity": row.get("Rarity") or original.get("rarity", "Common"),
                "hero": row["Name"] in hero_names or original.get("hero", False),
                "cost": row.get("ManaCost") or original.get("cost", "0")})
    old.update(source="Installed build " + str(installed["build"]) + "; native CSV row IDs and hero TOML declarations", cards=cards)
    Path(output or path).write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n")
    print(f"Synced {len(cards)} native card definitions and {len(hero_names)} hero declarations")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path)
    p.add_argument("--game", type=Path, help="Validate a staged bundle before activation")
    args = p.parse_args()
    sync(args.out, game=args.game)
