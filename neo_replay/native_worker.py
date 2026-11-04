"""Runs only under x86_64 Python. No model, Android, ADB, or network."""
import copy
import ctypes
import json
import os
from pathlib import Path
import platform
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor/crx")]
from neo_replay import importer


def ordered_hand(players):
    return [[c["cardId"] for c in sorted(p["hand"], key=lambda h: h["handIndex"])] +
            [c["cardId"] for c in sorted(p["cycle"], key=lambda h: h["cycleIndex"])]
            for p in sorted(players, key=lambda p: p["owner"])]


def calibrate(parsed_path, output, cfg):
    """Measure one fresh deal. A separate process validates the remapped match.

    This avoids the upstream in-process restart, which crashes on this Mac.
    Return the fresh, verified environment so preparation can continue without
    loading another engine or creating another battle.
    """
    os.environ["CRX_ASSETS"] = cfg["assets"]
    from env import CrxEnv
    replay = json.loads(Path(parsed_path).read_text())
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "verified-opening.json").unlink(missing_ok=True)
    env = CrxEnv(cfg["engine"], out / "deal-work", cfg["assets"])
    saved = out / "calibrated-match.json"
    match = json.loads(saved.read_text()) if saved.exists() else importer.match_for(replay, replay["prepared_decks"])
    seed = match["rndSeed"] if saved.exists() else replay["seed"]
    match.update(rndSeed=seed, time=-1, cmd=[], evt=[], endTick=7200)
    obs = env.create_match(match)
    print("Measured opening:", ordered_hand(obs["players"]), flush=True)
    actual = ordered_hand(obs["players"])
    ready = all(importer.compatible(v[:4], v[4:], [e["card"] for e in replay["events"]
                if e["kind"] == "card_play" and e["owner"] == o]) for o, v in enumerate(actual))
    if ready:
        saved.write_text(json.dumps(match))
        (out / "verified-opening.json").write_text(json.dumps(actual))
        print("Measured deal supports every recorded card play", flush=True)
        return env, replay, match, obs
    def try_next_seed(reason):
        # HTML has no RNG seed. Search by opening-cycle compatibility only;
        # terminal HP never participates in selecting a deal seed.
        if cfg.get("inference_seed_locked"):
            raise RuntimeError("候选种子无法满足HTML出牌轮转：" + reason)
        state_path = out / "calibration-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"rejected": []}
        state["rejected"].append({"seed": seed, "reason": reason})
        rejected = {row["seed"] for row in state["rejected"]}
        next_seed = next((n for n in range(64) if n not in rejected), None)
        if next_seed is None:
            raise RuntimeError("未找到与HTML轮转相容的原生发牌种子，请查看 calibration-state.json")
        proposed = importer.match_for(replay, replay["prepared_decks"])
        proposed.update(rndSeed=next_seed, time=-1, cmd=[], evt=[], endTick=7200)
        saved.write_text(json.dumps(proposed))
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        print(f"原生发牌轮转不相容，继续推断起手种子 {next_seed}：{reason}", flush=True)
    decks = []
    for p in sorted(obs["players"], key=lambda p: p["owner"]):
        slots = [c["deckSlot"] for c in sorted(p["hand"], key=lambda h: h["handIndex"])] + \
                [c["deckSlot"] for c in sorted(p["cycle"], key=lambda h: h["cycleIndex"])]
        if sorted(slots) != list(range(8)):
            raise RuntimeError("Engine opening hand is not a complete eight-card deal")
        desired = replay["opening"][p["owner"]]
        actual_cards = actual[p["owner"]]
        if 27000007 in actual_cards:
            index = actual_cards.index(27000007)
            if index < 4:
                raise RuntimeError("原生起手出现圣水收集器，未发布回放")
            plays = [e["card"] for e in replay["events"]
                     if e["kind"] == "card_play" and e["owner"] == p["owner"]]
            ids = [c["id"] for c in replay["players"][p["owner"]]["cards"]]
            try:
                _, hand, cycle = importer.choose(ids, plays, importer.LAYOUT[p["owner"]],
                                                native_cycle={index - 4: 27000007})
                desired = hand + cycle
            except ValueError:
                try_next_seed("当前种子的收集器轮转无法执行HTML出牌顺序")
                return
        mapped = [0] * 8
        for slot, card in zip(slots, desired):
            mapped[slot] = card
        decks.append(mapped)
    match = importer.match_for(replay, decks)
    match.update(rndSeed=seed, time=-1, cmd=[], evt=[], endTick=7200)
    if saved.exists() and json.loads(saved.read_text()) == match:
        try_next_seed("重复校准未改变不相容的起手")
        return
    (out / "calibrated-match.json").write_text(json.dumps(match))
    print("Opening deal measured; proposed match saved for independent validation", flush=True)


def board(obs):
    return {"tick": obs["tick"], "players": obs["players"],
            "objects": sorted(obs["objects"], key=lambda o: o["nativeObjectId"])}


def prepare(parsed_path, output, cfg):
    os.environ["CRX_ASSETS"] = cfg["assets"]
    from env import CrxEnv
    replay = json.loads(Path(parsed_path).read_text())
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    work = out / "headless-work"
    print("Booting native engine", flush=True)
    env = CrxEnv(cfg["engine"], work, cfg["assets"])
    print("Native data ready", flush=True)
    match = json.loads((out / "calibrated-match.json").read_text())
    obs = env.create_match(match)
    simulate(env, replay, match, obs, out, cfg)


def calibrate_prepare(parsed_path, output, cfg):
    """Validate one fresh deal, then simulate on that same battle if it is ready.

    An incompatible deal only writes the proposed deck order and returns. The
    host launches another independent process to validate that proposal.
    """
    from neo_replay.runner import status
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    timing = out / "preparation-attempt.json"
    stages = []
    stage = "calibrate"
    started = time.perf_counter()
    try:
        prepared = calibrate(parsed_path, out, cfg)
        stages.append({"phase": stage, "seconds": round(time.perf_counter()-started, 3)})
        stage = None
        if prepared is None:
            return
        status(out, "calibrate", "起手牌校准完成", fraction=1)
        status(out, "simulate", "正在本地模拟整场对局并生成缓存…")
        stage = "simulate"
        started = time.perf_counter()
        env, replay, match, obs = prepared
        simulate(env, replay, match, obs, out, cfg)
        stages.append({"phase": stage, "seconds": round(time.perf_counter()-started, 3)})
        stage = None
    finally:
        if stage is not None:
            stages.append({"phase": stage, "seconds": round(time.perf_counter()-started, 3), "failed": True})
        timing.write_text(json.dumps(stages, indent=2) + "\n")


def simulate(env, replay, match, obs, out, cfg):
    """Run the established replay preparation algorithm on a tick-zero battle."""
    from neo_replay.runner import status
    ambiguity = out / "ability-ambiguity.json"
    ambiguity.unlink(missing_ok=True)
    plan_path = out / "ability-plan.json"
    ability_plan = json.loads(plan_path.read_text()) if plan_path.exists() else {}
    verified = json.loads((out / "verified-opening.json").read_text())
    if ordered_hand(obs["players"]) != verified:
        print("Actual opening:", ordered_hand(obs["players"]), "requested:", replay["opening"], flush=True)
        print("Actual players:", obs["players"], flush=True)
        raise RuntimeError("Calibrated opening hand did not validate; no replay was emitted")
    print("Opening hand and cycle verified", flush=True)
    captured = []
    original = env._inject

    def capture(command):
        accepted = original(command)
        if accepted:
            captured.append(copy.deepcopy(command))
        return accepted
    env._inject = capture
    events = replay["events"]
    for e in events:
        if e["kind"] == "card_play":
            env.queue_card_at(e["owner"], e["card"], min(e["x"], 17999), min(e["y"], 31999), execute_tick=e["tick"])
    abilities = {}
    for e in events:
        if e["kind"] == "ability":
            abilities.setdefault(e["tick"], []).append(e)
    limit = min(7200, max(6200, replay["duration"]))
    traced = 0
    last_ability_source = {}
    unverified_abilities = []
    # HTML command timing supplies an estimate, not an exact native end tick.
    # Keep this stage below completion until the native result is available.
    estimated_ticks = min(limit, max(1, replay["duration"]))
    status(out, "simulate", "正在本地演算整场对局…", fraction=0,
           current_tick=env.tick(), total_ticks=estimated_ticks)
    with (out / "headless.trace.jsonl").open("w") if os.environ.get("NEO_VERIFY_TRACE") else open(os.devnull, "w") as trace:
        while env.tick() < limit:
            now = env.tick()
            obs = env.observe()
            if obs["ended"]:
                break
            for player in obs["players"]:
                ptr = env.reader.player(env.reader.logic(env.battle), player["owner"])
                for controller in env.reader.abilities(ptr):
                    if controller["championObjectIds"]:
                        last_ability_source[player["owner"]] = max(controller["championObjectIds"])
            for e in abilities.get(now + 1, []):
                candidates = []
                player = env.reader.player(env.reader.logic(env.battle), e["owner"])
                controllers = env.reader.abilities(player)
                live_ids = {o["nativeObjectId"] for o in obs["objects"] if o["owner"] == e["owner"]}
                for a in controllers:
                    if a["buttonState"] in (2, 4) and a["remainingCooldownMs"] == 0 and a["remainingChargesRaw"] != 0:
                        candidates += [i for i in a["championObjectIds"] if i in live_ids]
                candidates = sorted(set(candidates))
                if len(candidates) > 1:
                    key = f"{e['owner']}:{now + 1}"
                    chosen = ability_plan.get(key)
                    if chosen in candidates:
                        candidates = [chosen]
                    else:
                        names = {s['nativeObjectId']: s.get('name', '')
                                 for s in env.reader.unit_states(env.battle)}
                        ambiguity.write_text(json.dumps({"key": key, "tick": now + 1,
                            "source_tick": e["source_time_value"], "owner": e["owner"],
                            "candidates": candidates,
                            "names": {str(i): names.get(i, '') for i in candidates}}, ensure_ascii=False))
                        raise RuntimeError(f"技能对象需推断：tick {now + 1}，候选 {candidates}")
                if len(candidates) != 1:
                    if not candidates and e["owner"] in last_ability_source:
                        source_id = last_ability_source[e["owner"]]
                        env.scheduled.append((now+1, e["owner"], 'ability', source_id, 0))
                        unverified_abilities.append({"tick": now+1, "owner": e["owner"], "source": source_id,
                            "notice": "Recorded command queued; readiness/effect not asserted."})
                    else:
                        raise RuntimeError(f"Ability at tick {now + 1}: expected one ready source, got {candidates}")
                else:
                    env.queue_ability_action_at(SimpleNamespace(owner=e["owner"], secondary_index=candidates[0]))
            env.step()
            if env.tick() == now:
                break
            if os.environ.get("NEO_VERIFY_TRACE") and (env.tick() % 5 == 0 or env.tick() in (1, 2)):
                trace.write(json.dumps(board(env.observe()), separators=(",", ":")) + "\n")
                traced += 1
            if env.tick() % 100 == 0:
                if env.tick() % 500 == 0:
                    print(f"simulate tick={env.tick()}", flush=True)
                status(out, "simulate", f"已演算 {env.tick() * 0.05:.1f} 秒（{env.tick()} tick）…",
                       fraction=min(.99, env.tick()/estimated_ticks),
                       current_tick=env.tick(), total_ticks=estimated_ticks)
    final = env.observe()
    if os.environ.get("NEO_VERIFY_TRACE") and final["tick"] % 5:
        with (out / "headless.trace.jsonl").open("a") as trace:
            trace.write(json.dumps(board(final), separators=(",", ":")) + "\n")
            traced += 1
    dropped = [h for h in env.hold_log if h[4] == "dropped"]
    completed = sum(h[4] == "played" for h in env.hold_log)
    expected_cards = sum(e["kind"] == "card_play" for e in events)
    expected_abilities = sum(e["kind"] == "ability" for e in events)
    queued_abilities = sum(c["ct"] == env.B.data["command_types"]["ability"] for c in captured)
    incomplete = completed != expected_cards or bool(dropped)
    if incomplete and not final["ended"]:
        raise RuntimeError(f"Replay incomplete without a native end: {completed}/{expected_cards} cards, {len(dropped)} dropped")
    delays = [h[3] for h in env.hold_log if h[4] == "played" and h[3]]
    match.update(cmd=captured, endTick=final["tick"], time=-1, evt=[])
    expected = replay.get("expected", [])
    comparable = len(expected) == 2 and all(x is not None for x in expected)
    crowns_match = final["crownsRaw"] == [int(x) for x in expected] if comparable else None
    from neo_replay.validation import terminal
    from neo_replay.native_snapshots import board_digest
    verification = terminal(replay, final, completed)
    ignored_post_end = ([{"owner": e["owner"], "source_tick": e["source_time_value"],
                         "native_execute_tick": e["tick"], "native_end_tick": final["tick"]}
                        for e in events if e["kind"] == "ability" and e["tick"] > final["tick"]]
                       if verification["passed"] and crowns_match is True else [])
    report = {"source": replay["url"], "build": cfg["build"], "end_tick": final["tick"],
              "simulation_state_sha256": board_digest(final),
              "cards_executed": completed, "commands": len(captured), "trace_frames": traced,
              "cards_recorded": expected_cards, "unexecuted_cards": expected_cards-completed,
              "abilities_recorded": expected_abilities, "abilities_queued": queued_abilities,
              "unexecuted_abilities": expected_abilities-queued_abilities,
              "ignored_post_end_abilities": ignored_post_end,
              "native_ended_early": incomplete, "dropped_cards": len(dropped),
              "simulated_crowns": final["crownsRaw"], "recorded_crowns": expected,
              "crowns_match": crowns_match, "delayed_cards": len(delays), "max_delay_ticks": max(delays, default=0),
              "fidelity": "terminal-hp-and-plays-match" if verification["passed"] else "partial-reconstruction" if incomplete else "terminal-validation-mismatch",
              "terminal_validation": verification, "source_rules": replay.get("source_rules"),
              "data_provenance": cfg.get("data_provenance"),
              "opening_inference": {"original_seed_known": False, "default_seed": replay["seed"],
                  "selected_seed": match["rndSeed"],
                  "seed_adjusted_for_opening": match["rndSeed"] != replay["seed"],
                  "scope": "Seed chosen using opening/card-cycle compatibility; original RNG seed is absent from HTML."},
              "unverified_abilities": unverified_abilities,
              "notice": "HTML is the reference for recorded commands and terminal HP. The report compares these observables; it does not establish per-frame identity."}
    (out / "replay.json").write_text(json.dumps(match, separators=(",", ":")))
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    status(out, "simulate", "本地演算完成", fraction=1,
           current_tick=final["tick"], total_ticks=final["tick"])
    print(json.dumps(report, ensure_ascii=False), flush=True)


def check_gl():
    sys.path.insert(0, str(ROOT / "vendor/crx/render"))
    import egl_bridge as EB
    EB.STATE.update(width=64, height=64)
    ctx = EB.make_boot_context()
    if not ctx:
        raise RuntimeError(f"ANGLE context failed, EGL error={EB.egl.eglGetError():#x}")
    EB.gles.glGetString.argtypes = [ctypes.c_uint]
    EB.gles.glGetString.restype = ctypes.c_char_p
    EB.gles.glClearColor.argtypes = [ctypes.c_float] * 4
    EB.gles.glClear.argtypes = [ctypes.c_uint]
    EB.gles.glClearColor(0.25, 0.5, 0.75, 1.0)
    EB.gles.glClear(0x4000)
    pixels = ctypes.create_string_buffer(64 * 64 * 4)
    EB.gles.glReadPixels(0, 0, 64, 64, 0x1908, 0x1401, pixels)
    pixel = list(pixels.raw[:4])
    if not all(abs(a - b) <= 2 for a, b in zip(pixel, [64, 128, 191, 255])):
        raise RuntimeError(f"ANGLE readback failed: {pixel}")
    print(json.dumps({"renderer": EB.gles.glGetString(0x1F01).decode(), "pixel": pixel, "ok": True}), flush=True)


def main():
    if platform.machine() != "x86_64":
        raise RuntimeError("Native worker requires arch -x86_64 Python")
    if sys.argv[1] == "check-gl":
        check_gl()
    elif sys.argv[1] == "calibrate":
        calibrate(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]))
    elif sys.argv[1] == "prepare":
        prepare(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]))
    elif sys.argv[1] == "calibrate-prepare":
        calibrate_prepare(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]))
    elif sys.argv[1] == "snapshots":
        from neo_replay.native_snapshots import build
        build(sys.argv[2], json.loads(sys.argv[3]))
    elif sys.argv[1] == "render":
        sys.path.insert(0, str(ROOT / "vendor/crx/render"))
        import live
        class ReplayLive(live.Live):
            def start_seek(self, target, force_restart=False):
                super().start_seek(target, force_restart=force_restart)
                self.end_paused = False
        live.Live = ReplayLive
        import try_surface
        if os.environ.get("NEO_TICK_CACHE"):
            from neo_replay.native_snapshots import install
            try_surface.keyframes = install
        sys.argv = [str(try_surface.__file__)] + sys.argv[2:]
        try_surface.main()
    else:
        raise ValueError("Unknown native worker command")


if __name__ == "__main__":
    try:
        main()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)  # libg exit-time destructors need Android; never run them in this host.
    except Exception:
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
