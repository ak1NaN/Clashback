"""Infer missing native replay inputs by comparing legal replay branches.

Raw HTML times, event intervals, coordinates and official game data are kept.
Fitted RNG, skill choices and common native time origins remain in the report.
A terminal match does not establish per-frame identity.
"""
import json
import hashlib
from pathlib import Path
import shutil
import time


def unresolved_abilities(report):
    ignored = (len(report.get("ignored_post_end_abilities", []))
               if report.get("terminal_validation", {}).get("passed") is True
               and report.get("crowns_match") is True else 0)
    return max(0, report.get("unexecuted_abilities", 0) - ignored)


def score(report):
    """Prefer recorded execution, legal skill readiness, crowns, then each tower."""
    hp = 0
    rows = report.get("terminal_validation", {}).get("towers", [])
    for row in rows:
        hp += abs(row["simulated_king"] - row["recorded"]["king"])
        expected = sorted([row["recorded"]["princess0"], row["recorded"]["princess1"]])
        hp += sum(abs(x-y) for x, y in zip(row["simulated_princess"], expected))
    crowns = sum(abs(int(x)-int(y)) for x, y in zip(
        report["simulated_crowns"], report["recorded_crowns"]) if x is not None and y is not None)
    return (report["unexecuted_cards"] + unresolved_abilities(report), report["delayed_cards"],
            len(report.get("unverified_abilities", [])), crowns, hp if len(rows) == 2 else 10**9)


def matches(report):
    return (score(report) == (0, 0, 0, 0, 0)
            and report.get("terminal_validation", {}).get("passed") is True
            and report.get("crowns_match") is True)


def seed_candidates(limit):
    """Cover small seeds, bit patterns and the wider signed-32-bit domain."""
    values = list(range(8)) + [(1 << bits)-1 for bits in (5, 8, 16, 31)]
    index = 0
    while len(values) < limit:
        value = int.from_bytes(hashlib.sha256(f"neo-replay-seed-v1:{index}".encode()).digest()[:4], "big") & 0x7fffffff
        if value not in values:
            values.append(value)
        index += 1
    return values[:limit]


def resolve_alignment(parsed, cfg, run, env, command, phase, viewer, log,
                      *, offsets=(1, 2, 5, 7, 10, 15, 20)):
    """Fit one common native time origin, retaining every HTML event interval.

    This is a fitted alignment, not proof of server latency. Raw HTML times
    remain intact. Never move individual events to manufacture a match.
    """
    from .runner import status
    run = Path(run)
    baseline = json.loads((run / "report.json").read_text())
    if matches(baseline):
        return None
    replay = json.loads(Path(parsed).read_text())
    if len(replay.get("expected_tower_hp", [])) != 2:
        return None
    directory = run / "time-alignment"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    best, selected, report, chosen = score(baseline), run, baseline, 0
    attempts = []
    for offset in offsets:
        case = directory / f"offset-{offset:03d}"
        case.mkdir()
        for name in ("calibrated-match.json", "verified-opening.json"):
            shutil.copy2(run / name, case / name)
        candidate = json.loads(json.dumps(replay))
        candidate["duration"] += offset
        candidate["source_rules"]["native_time_offset_ticks"] = offset
        for event in candidate["events"]:
            event["tick"] += offset
        candidate_path = case / "parsed.json"
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False))
        status(run, "simulate", f"正在校准统一时间起点（+{offset * .05:.2f} 秒）…")
        code = phase(command("prepare", candidate_path, case, json.dumps(cfg)),
                     log, env, 300, viewer, stage="time-alignment")
        if code and (case / "ability-ambiguity.json").is_file():
            try:
                resolve(candidate_path, cfg, case, env, command, phase, viewer, log)
                code = 0
            except RuntimeError:
                if viewer and viewer.poll() is not None:
                    raise
                code = 1
        row = {"offset_ticks": offset, "returncode": code}
        attempts.append(row)
        if code or not (case / "report.json").is_file():
            continue
        result = json.loads((case / "report.json").read_text())
        value = score(result)
        row.update(score=list(value), recorded_observables_match=matches(result))
        if value < best:
            best, selected, report, chosen = value, case, result, offset
        if matches(result):
            break
    inference = {"method": "one-common-native-time-origin-fitted-to-HTML",
        "selected_offset_ticks": chosen, "selected_offset_seconds": chosen * .05,
        "baseline_score": list(score(baseline)), "best_score": list(best),
        "improved": best < score(baseline), "attempts": attempts,
        "recorded_observables_match": matches(report), "original_time_origin_proven": False,
        "relative_event_intervals_preserved": True, "raw_HTML_times_preserved": True,
        "scope": "One common offset applied to all events in native time only. This fitted alignment does not establish original server latency or per-frame identity."}
    if selected != run:
        shutil.copy2(selected / "replay.json", run / "replay.json")
        for name in ("seed_inference", "opening_inference"):
            if name in baseline:
                report[name] = baseline[name]
    report["time_alignment_inference"] = inference
    if unresolved_abilities(report):
        report["fidelity"] = "recorded-ability-missing"
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    (run / "time-alignment.json").write_text(json.dumps(inference, ensure_ascii=False, indent=2)+"\n")
    print(f"时间基准推断：统一 +{chosen * .05:.2f} 秒；"
          + ("记录数据吻合。" if matches(report) else "仍有未对齐记录。"), flush=True)
    return inference


def resolve_seed(parsed, cfg, run, env, command, phase, viewer, log,
                 *, max_seeds=32, time_limit=180):
    """Infer an absent RNG seed, preserving the best legal recorded execution.

    Search is bounded and stops at the first exact observable match. Equal
    outcomes retain the earlier candidate; no original RNG state is asserted.
    """
    from .runner import status
    run = Path(run)
    baseline = json.loads((run / "report.json").read_text())
    if matches(baseline):
        return None
    replay = json.loads(Path(parsed).read_text())
    # Without complete terminal evidence there is no sound HP-based selection.
    if len(replay.get("expected_tower_hp", [])) != 2:
        return None
    original_match = json.loads((run / "calibrated-match.json").read_text())
    initial_seed = original_match["rndSeed"]
    best, selected, best_report = score(baseline), run, baseline
    directory = run / "seed-inference"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    attempts = []
    started = time.monotonic()
    reason = "candidate-limit"
    candidate_cfg = dict(cfg, inference_seed_locked=True)
    for seed in seed_candidates(max_seeds):
        if seed == initial_seed:
            continue
        if time.monotonic() - started >= time_limit:
            reason = "time-limit"
            break
        case = directory / f"seed-{seed:03d}"
        case.mkdir()
        candidate = dict(replay, seed=seed)
        candidate_path = case / "parsed.json"
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False))
        row = {"requested_seed": seed}
        attempts.append(row)
        status(run, "simulate", f"正在按 HTML 比较随机种子（第 {len(attempts)} 个，最多 {max_seeds} 个）…")
        code = 0
        for _ in range(16):
            code = phase(command("calibrate-prepare", candidate_path, case, json.dumps(candidate_cfg)),
                         log, env, 300, viewer, stage="seed-inference")
            if code and (case / "ability-ambiguity.json").is_file():
                try:
                    resolve(candidate_path, candidate_cfg, case, env, command, phase, viewer, log)
                    code = 0
                except RuntimeError:
                    # Window cancellation and switching must reach the host.
                    if viewer and viewer.poll() is not None:
                        raise
                    row["error"] = "No completed legal ability branch for this seed"
            if code or (case / "verified-opening.json").is_file():
                break
        if code or not (case / "report.json").is_file():
            row.setdefault("error", "Native seed candidate did not complete")
            continue
        report = json.loads((case / "report.json").read_text())
        value = score(report)
        match = json.loads((case / "calibrated-match.json").read_text())
        row.update(seed=match["rndSeed"], score=list(value), recorded_observables_match=matches(report))
        if value < best:
            best, selected, best_report = value, case, report
        if matches(report):
            reason = "first-recorded-observable-match"
            break
    chosen = json.loads((selected / "calibrated-match.json").read_text())["rndSeed"]
    inference = {"method": "legal-native-seeds-compared-with-HTML",
        "baseline_seed": initial_seed, "selected_seed": chosen,
        "baseline_score": list(score(baseline)), "best_score": list(best),
        "improved": best < score(baseline), "attempted_seeds": len(attempts),
        "recorded_observables_match": matches(best_report),
        "stop_reason": reason, "candidate_limit": max_seeds, "time_limit_seconds": time_limit,
        "seconds": round(time.monotonic()-started, 3), "original_seed_proven": False,
        "tie_break": "first-in-stable-candidate-order", "attempts": attempts,
        "candidate_strategy": "small-seeds-bit-patterns-and-dispersed-31-bit-seeds",
        "scope": "Unknown RNG seeds inferred from recorded commands, crowns and terminal tower HP. Terminal agreement does not prove original RNG state or intermediate frames."}
    if selected != run:
        for name in ("calibrated-match.json", "verified-opening.json", "replay.json"):
            shutil.copy2(selected / name, run / name)
        opening = best_report.get("opening_inference", {})
        opening.update(default_seed=replay["seed"], seed_adjusted_for_opening=False,
                       seed_adjusted_for_terminal_comparison=chosen != initial_seed,
                       scope="Opening/card cycle verified; RNG seed selected by comparing recorded terminal observables.")
        best_report["opening_inference"] = opening
    best_report["seed_inference"] = inference
    (run / "report.json").write_text(json.dumps(best_report, ensure_ascii=False, indent=2)+"\n")
    (run / "seed-inference.json").write_text(json.dumps(inference, ensure_ascii=False, indent=2)+"\n")
    label = "记录数据吻合" if matches(best_report) else "仍有差异，保留最接近的候选"
    print(f"随机种子推断：比较 {len(attempts)} 个候选，选择 {chosen}；{label}。", flush=True)
    return inference


def resolve(parsed, cfg, run, env, command, phase, viewer, log, *, max_cases=32):
    """Fresh process per branch; publish the best report with explicit uncertainty."""
    from .runner import status
    run = Path(run)
    first = json.loads((run / "ability-ambiguity.json").read_text())
    queue = [{first["key"]: unit} for unit in first["candidates"]]
    visited = set()
    completed = []
    attempts = []
    ambiguities = {first["key"]: first}
    directory = run / "ability-inference"
    # A retry must not mistake an earlier branch's output for a fresh result.
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    while queue and len(attempts) < max_cases:
        plan = queue.pop(0)
        identity = json.dumps(plan, sort_keys=True)
        if identity in visited:
            continue
        visited.add(identity)
        case = directory / f"case-{len(attempts):03d}"
        case.mkdir()
        for name in ("calibrated-match.json", "verified-opening.json"):
            shutil.copy2(run / name, case / name)
        (case / "ability-plan.json").write_text(json.dumps(plan, indent=2))
        status(run, "simulate", f"正在比较技能候选（第 {len(attempts)+1} 个）…")
        code = phase(command("prepare", parsed, case, json.dumps(cfg)), log, env,
                     300, viewer, stage="ability-inference")
        row = {"plan": plan, "returncode": code}
        attempts.append(row)
        if not code and (case / "report.json").is_file():
            report = json.loads((case / "report.json").read_text())
            value = score(report)
            row.update(score=list(value), terminal_match=report["terminal_validation"]["passed"])
            completed.append((value, case, report, plan))
        elif (case / "ability-ambiguity.json").is_file():
            next_choice = json.loads((case / "ability-ambiguity.json").read_text())
            ambiguities[next_choice["key"]] = next_choice
            if next_choice["key"] not in plan:
                queue.extend(dict(plan, **{next_choice["key"]: unit})
                             for unit in next_choice["candidates"])
        else:
            row["error"] = "Native preparation failed without an ability-choice branch"
    if not completed:
        (run / "ability-inference.json").write_text(json.dumps(
            {"attempts": attempts, "search_complete": not queue and not any(
                "error" in row for row in attempts)}, ensure_ascii=False, indent=2))
        raise RuntimeError("技能候选均未能完成演算，请查看 ability-inference.json 和 prepare.log")
    completed.sort(key=lambda value: value[0])
    best_score, case, report, plan = completed[0]
    ties = sum(value[0] == best_score for value in completed)
    exact = (best_score == (0, 0, 0, 0, 0) and report["terminal_validation"]["passed"]
             and report["crowns_match"] is True)
    failed = sum("error" in row for row in attempts)
    inference = {"method": "legal-native-branches-compared-with-HTML",
        "selected_plan": plan, "best_score": list(best_score),
        "attempted_branches": len(attempts), "completed_branches": len(completed),
        "equally_best_branches": ties, "search_complete": not queue and not failed,
        "failed_branches": failed,
        "tie_break": "first-in-stable-native-candidate-order",
        "recorded_observables_match": exact, "ambiguities": list(ambiguities.values()),
        "original_target_proven": False,
        "scope": "Ability targets are inferred. Multiple branches can match terminal HTML; this does not prove the original skill target or intermediate battle states."}
    report["ability_inference"] = inference
    for name in ("replay.json", "ability-plan.json"):
        shutil.copy2(case / name, run / name)
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (run / "ability-inference.json").write_text(json.dumps(
        dict(inference, attempts=attempts), ensure_ascii=False, indent=2) + "\n")
    label = "记录数据吻合" if exact else "最接近的候选仍有差异"
    print(f"技能推断完成：{len(attempts)} 个候选分支，{label}；同分候选 {ties} 个。", flush=True)
    return inference
