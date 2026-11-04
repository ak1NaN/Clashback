"""Compare actual execution and terminal tower HP against the recorded HTML."""
def terminal(replay, observation, cards_executed):
    expected = replay.get("expected_tower_hp", [])
    towers = {o["nativeObjectId"]: o["hp"] or 0 for o in observation["objects"]
              if 5000000 <= o["nativeObjectId"] < 5000006}
    rows = []
    for owner, record in enumerate(expected):
        base = 5000000 + owner*3
        king = towers.get(base, 0)
        princess = sorted([towers.get(base+1, 0), towers.get(base+2, 0)])
        normalized = record["king"] == 0 and record["total"] == 0 and king == 0
        raw_princess = princess
        if normalized: princess = [0, 0]
        recorded_princess = sorted([record["princess0"], record["princess1"]])
        total = king + sum(princess)
        rows.append({"owner": owner, "player": replay["players"][owner]["name"],
            "recorded": record, "simulated_king": king, "simulated_princess": princess,
            "raw_simulated_princess": raw_princess, "defeated_king_normalization": normalized,
            "simulated_total": total, "total_difference": total-record["total"],
            "matches": king == record["king"] and princess == recorded_princess and total == record["total"]})
    cards_recorded = sum(e["kind"] == "card_play" for e in replay["events"])
    hp_match = all(r["matches"] for r in rows) if len(rows) == 2 else None
    return {"cards_match": cards_executed == cards_recorded,
        "tower_hp_match": hp_match, "passed": cards_executed == cards_recorded and hp_match is True,
        "towers": rows, "scope": "Recorded card execution count, King HP, both Princess HP values per owner (unordered), and totals. Not a per-frame reference comparison."}
