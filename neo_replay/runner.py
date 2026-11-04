"""Local playback orchestration, with a visible loading window and reusable input cache."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import struct
import time
import uuid

from . import assets, cache, library, resourcefs
from .paths import ROOT, CACHE, VIEWER, config, native_env


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()


# These are UI work weights, not promises about remaining wall-clock time.
PROGRESS_RANGES = {
    "idle": (0, 0), "prepare": (0, .05), "calibrate": (.05, .15),
    "simulate": (.15, .65), "snapshots": (.65, .90), "cache": (.65, .90),
    "partial": (.90, .90), "validation": (.90, .90), "render": (.90, .99),
    "playing": (1, 1),
}


def status(run, phase, text, notice=None, *, fraction=None, current_tick=None, total_ticks=None):
    """Map stage work to a monotonic 0–100% preparation indicator."""
    dest = run / "progress.json"
    previous = json.loads(dest.read_text()) if dest.exists() else {}
    if notice is None:
        notice = previous.get("notice", "")
    start, end = PROGRESS_RANGES.get(phase, (0, 0))
    stage_fraction = min(1, max(0, float(fraction))) if fraction is not None else 0
    overall = start + (end-start)*stage_fraction
    if phase not in ("idle", "prepare"):
        overall = max(overall, previous.get("fraction") or 0)
    labels = {"idle": "等待打开 HTML", "prepare": "准备对局", "cache": "读取缓存", "calibrate": "手牌校准",
              "simulate": "本地演算", "snapshots": "逐 tick 缓存", "partial": "准备画面",
              "validation": "核对对局", "render": "启动原生画面", "playing": "回放就绪"}
    progress = {"phase": phase, "phase_label": labels.get(phase, phase),
                "message": text, "notice": notice or "", "fraction": overall,
                "stage_fraction": fraction, "progress_kind": "weighted-total",
                "current_tick": current_tick, "total_ticks": total_ticks}
    temp = dest.with_suffix(".tmp")
    temp.write_text(json.dumps(progress, ensure_ascii=False))
    temp.replace(dest)


class SwitchReplay(Exception):
    def __init__(self, path): self.path = path


def phase(command, log, env, timeout, viewer=None, stage=None):
    started = time.perf_counter()
    process = subprocess.Popen(command, env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if viewer:
                request = Path(log.name).parent / "live/open-html.txt"
                if request.exists(): raise SwitchReplay(request.read_text().strip())
            if viewer and viewer.poll() is not None:
                raise RuntimeError("已关闭回放窗口，准备任务已取消")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"原生准备超时（{timeout} 秒），请查看 prepare.log")
            time.sleep(.1)
        return process.returncode
    finally:
        stop(process)
        timing = Path(log.name).parent / "phase-timings.json"
        rows = json.loads(timing.read_text()) if timing.exists() else []
        row = {"phase": stage, "seconds": round(time.perf_counter()-started, 3),
               "returncode": process.returncode}
        detail = timing.parent / "preparation-attempt.json"
        if stage == "calibrate-prepare" and detail.exists():
            row["stages"] = json.loads(detail.read_text())
        rows.append(row)
        timing.write_text(json.dumps(rows, indent=2) + "\n")


def prepare(replay, parsed, cfg, run, env, command, rebuild, html, viewer=None):
    from .native_snapshots import SUPPORTED_BUILDS
    snapshots = cfg["build"] in SUPPORTED_BUILDS
    identity = cache.key(replay, cfg)
    cfg["cache_identity"] = identity
    retained = cache.retain_current(identity, run, snapshots=snapshots,
                                    require_complete=False, parsed=parsed)
    (run / "cache-retention.json").write_text(json.dumps(retained, indent=2) + "\n")
    if retained["cache_files_removed"] or retained["session_files_removed"]:
        print(f"已删除旧对局全部缓存 {retained['bytes_removed'] / (1024 * 1024):.1f} MiB；保留 HTML 和配置。", flush=True)
    cached = not rebuild and cache.restore(identity, run)
    tick_cached = bool(cached and snapshots and cache.restore_snapshots(identity, run))
    log = run / "prepare.log"
    if cached:
        log.write_text("Verified command cache hit: " + identity + "\n")
        if tick_cached:
            with log.open("a") as output:
                output.write("Verified every-tick cache hit\n")
            status(run, "cache", "已读取当前对局的命令与逐 tick 缓存…", fraction=1)
            print("命中当前对局完整缓存，跳过校准、模拟和逐 tick 缓存生成。", flush=True)
        else:
            status(run, "cache", "已读取对局命令，跳过起手校准和HTML重建…", fraction=0)
            print("命中对局命令缓存，跳过起手校准和HTML语义模拟。", flush=True)
    with log.open("a" if cached else "w") as output:
        if not cached:
            for attempt in range(16):
                status(run, "calibrate", f"正在校准起手牌（第 {attempt+1} 次）…", fraction=attempt/16)
                (run / "preparation-attempt.json").unlink(missing_ok=True)
                code = phase(command("calibrate-prepare", parsed, run, json.dumps(cfg)), output, env, 300, viewer,
                             stage="calibrate-prepare")
                if code and (run / "ability-ambiguity.json").is_file():
                    from .inference import resolve
                    resolve(parsed, cfg, run, env, command, phase, viewer, output)
                    code = 0
                if code or (run / "verified-opening.json").exists(): break
            if code:
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-12:])
                raise RuntimeError(f"起手校准或对局模拟失败：\n{tail}\n完整日志：{log}")
            if not (run / "verified-opening.json").exists():
                raise RuntimeError(f"起手校准失败，请查看 {log}")
            if not all((run / name).is_file() for name in ("replay.json", "report.json")):
                raise RuntimeError(f"对局模拟没有生成完整结果，请查看 {log}")
            from .inference import resolve_seed, resolve_alignment
            resolve_seed(parsed, cfg, run, env, command, phase, viewer, output)
            resolve_alignment(parsed, cfg, run, env, command, phase, viewer, output)
        if snapshots and not tick_cached:
            status(run, "snapshots", "正在从已记录命令补建逐 tick 缓存…" if cached else "正在缓存每个 tick 的对局状态…")
            print("正在从已记录的原生命令生成逐 tick 缓存。", flush=True)
            code = phase(command("snapshots", run, json.dumps(cfg)), output, env, 300, viewer, stage="snapshots")
            if code:
                raise RuntimeError(f"逐 tick 缓存生成失败，请查看 {log}")
    if not cached or (snapshots and not tick_cached):
        cache.save(identity, run)
    report = json.loads((run / "report.json").read_text())
    report["cache_reuse"] = {"commands": bool(cached), "snapshots": tick_cached}
    library.remember(html, replay, identity, report)
    return report, cached, identity


def play(args, command, parse_html, doctor):
    started = time.perf_counter()
    cache.migrate_legacy()
    if not args.html and (args.no_window or args.prepare_only):
        if os.isatty(0):
            args.html = input("请输入或拖入 RoyaleAPI HTML 文件路径（也可填写已缓存的对局编号）：\n> ")
        if not args.html:
            raise ValueError("无窗口或只准备缓存时，请提供 HTML 文件路径或对局编号")
    options = resourcefs.settings(args.settings)
    width, height = args.width or options["width"], args.height or options["height"]
    if not 160 <= width <= 4096 or not 160 <= height <= 4096:
        raise ValueError("width/height must be in 160..4096")
    if not 0.5 <= args.speed <= 8 or args.seconds <= 0:
        raise ValueError("播放速度应在 0.5～8 倍之间，运行时间必须大于 0")
    if options.get("loading_background"):
        check = subprocess.run([str(VIEWER), "--check-image", options["loading_background"]], capture_output=True, text=True)
        if check.returncode:
            raise ValueError("背景图片无法解码：" + check.stderr.strip())
    # The waiting window and the chosen replay share a session, so selecting an
    # HTML starts preparation in place without closing or reopening the window.
    run = CACHE / "sessions" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    live = run / "live"; live.mkdir(parents=True)
    session_settings = {**options, "width": width, "height": height, "show_native_loading": args.show_loading,
                        "recording_mode": args.recording_mode,
                        "mounts": {k: str(v) for k, v in options["mounts"].items()}}
    (run / "settings.json").write_text(json.dumps(session_settings, ensure_ascii=False, indent=2) + "\n")
    (live / "control.txt").write_text(f"paused={int(args.recording_mode)} speed={args.speed} step=0 mark=0 seek_id=0\n")
    viewer = worker = None
    display = os.environ.get("NEO_RECORDING_VIEWER", str(VIEWER)) if args.recording_mode else str(VIEWER)
    lease = cache.session_lease(run)
    next_html = None
    lease.__enter__()
    try:
        if not args.html:
            status(run, "idle", "点击“打开 HTML”选择对局")
            viewer = subprocess.Popen([display, str(live), str(width), str(height)], cwd=ROOT)
            request = live / "open-html.txt"
            while not args.html:
                if viewer.poll() is not None:
                    return 0
                if request.exists():
                    args.html = request.read_text().strip()
                    request.unlink()
                if not args.html:
                    time.sleep(.1)
            started = time.perf_counter()
        status(run, "prepare", "正在准备本地对局…")
        html = library.resolve(args.html)
        replay, parsed = parse_html(html)
        replay["location"] = resourcefs.DEFAULT_LOCATION
        parsed.write_text(json.dumps(replay, ensure_ascii=False, indent=2) + "\n")
        if not doctor(): raise ValueError("运行环境不完整，请执行 doctor 检查")
        cfg = config()
        if cfg.get("mode") == "measured" and assets.digest(html) not in cfg.get("validated_html_sha256", []):
            raise ValueError("这个专用资产包尚未采样这份 HTML，请重新构建资源或使用通用观战包。")
        from .paths import data_provenance
        balance_assets = Path(cfg["assets"])
        provenance = data_provenance(balance_assets)
        cfg["data_provenance"] = provenance
        cfg["assets"] = str(resourcefs.mounted_view(balance_assets, run / "asset-view", options["mounts"]))
        (run / "data-provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2)+"\n")
        env = native_env(); env.update(CRX_ASSETS=cfg["assets"], NEO_ASSET_TRACE=str(run / "asset-reads.jsonl"))
        print("Session:", run, flush=True)
        stages = {"parse_and_checks_seconds": time.perf_counter()-started}
        if viewer is None and not args.no_window and not args.prepare_only:
            viewer = subprocess.Popen([display, str(live), str(width), str(height)], cwd=ROOT)
        ready = time.perf_counter()
        report, cached, identity = prepare(replay, parsed, cfg, run, env, command, args.rebuild, html, viewer)
        stages.update(prepare_seconds=time.perf_counter()-ready, prepare_cache_hit=cached,
                      tick_cache_hit=report["cache_reuse"]["snapshots"], cache_key=identity)
        if (run / "phase-timings.json").exists():
            stages["preparation_phases"] = json.loads((run / "phase-timings.json").read_text())
        print("缓存编号：", replay["tag"], flush=True)
        print("本地模拟皇冠：", report["simulated_crowns"], "网页记录：", report["recorded_crowns"], flush=True)
        if report.get("native_ended_early"):
            print(f"注意：原生模拟提前结束，有 {report['unexecuted_cards']} 张后续出牌未执行；此缓存是部分重建，不是完整历史还原。", flush=True)
            session_settings["reconstruction_notice"] = f"模拟提前结束，{report['unexecuted_cards']} 张后续出牌未执行"
            (run / "settings.json").write_text(json.dumps(session_settings, ensure_ascii=False, indent=2)+"\n")
            status(run, "partial", "部分重建，正在启动画面…", "部分重建："+session_settings["reconstruction_notice"])
        if not report["crowns_match"] or report["delayed_cards"]:
            print("提示：HTML 为重建回放，结果或时机存在差异，详情见 report.json。", flush=True)
        verification = report.get("terminal_validation", {})
        if verification:
            from .inference import unresolved_abilities
            count = f"{report['cards_executed']}/{report['cards_recorded']}"
            if verification["passed"]:
                message = f"终局校验通过：出牌 {count}，双方塔血量与 HTML 一致"
                if unresolved_abilities(report):
                    message = f"血量和出牌核对通过：{count}；技能记录尚未完整执行"
            else:
                differences = "；".join(f"{r['player']} 塔总血量差 {r['total_difference']:+d}" for r in verification["towers"])
                message = f"校验未通过：出牌 {count}；{differences or 'HTML 未提供完整终局血量'}"
            if report.get("ability_inference"):
                inference = report["ability_inference"]
                message += "；技能对象由终局记录推断"
                if inference["equally_best_branches"] > 1:
                    message += f"（{inference['equally_best_branches']} 个同分候选）"
                if not inference["search_complete"]:
                    message += "，候选搜索尚未穷尽"
            if report.get("seed_inference"):
                inference = report["seed_inference"]
                message += ("；随机种子按 HTML 推断" if inference["improved"] else
                            f"；已比较 {inference['attempted_seeds']} 个候选种子")
            if report.get("time_alignment_inference", {}).get("improved"):
                offset = report["time_alignment_inference"]["selected_offset_seconds"]
                message += f"；统一时间基准推断 +{offset:.2f} 秒"
            if unresolved_abilities(report):
                message += f"；{unresolved_abilities(report)} 条技能记录未执行"
            elif report.get("ignored_post_end_abilities"):
                message += f"；已忽略 {len(report['ignored_post_end_abilities'])} 条赛后技能记录"
            print(message, flush=True)
            status(run, "validation", message, message)
        if args.prepare_only:
            (run / "timings.json").write_text(json.dumps(stages, indent=2) + "\n")
            print(f"以后可执行：./run.sh play {replay['tag']}", flush=True)
            return 0
        status(run, "render", "缓存已就绪，正在启动原生渲染…")
        env.update(NEO_RENDER_WIDTH=str(width), NEO_RENDER_HEIGHT=str(height), CRX_RENDER_ORDER="device_first", CRX_KEEP_RESOURCE_VARIANT="1",
            CRX_UNPATCHED="renderer_bootstrap_call,renderer_objects_block,renderer_device_block,renderer_resolution_call,renderer_late_configure_call",
            CRX_RENDER_REPLAY=str(run / "replay.json"), CRX_RENDER_REPLAY_AT="300" if args.show_loading else "0", CRX_RENDER_PUMP="0",
            CRX_RENDER_LOAD_WARP="1" if args.show_loading else "20", CRX_LIVE_SKIP_INTRO="1", CRX_RENDER_FPS="30" if args.recording_mode else "0",
            CRX_RENDER_LIVE=str(live), CRX_LIVE_SURFACE="0" if args.no_window or args.cpu_frames else "1", CRX_LIVE_BOARD="0",
            CRX_LIVE_END_TICK=str(report["end_tick"]), CRX_LIVE_SNAPSHOTS="1" if report.get("tick_cache") else "0")
        if cfg["build"] in ("160402020", "160635001"):
            env["NEO_VIEW_OWNER"] = str(replay["source_rules"]["team_owner"])
        else:
            env.pop("NEO_VIEW_OWNER", None)
        if report.get("tick_cache"):
            env["NEO_TICK_CACHE"] = str(run)
            env["NEO_CACHE_IDENTITY"] = identity
        else:
            env.pop("NEO_TICK_CACHE", None)
        with (run / "render.log").open("w") as output:
            worker = subprocess.Popen(command("render", cfg["engine"], run / "render-work", cfg["assets"], run, args.seconds),
                cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            print(f"原生画面 {width}×{height}；左右键前后 1 秒，上下键调整 0.5～8 倍速，空格暂停。", flush=True)
            deadline = time.monotonic()+args.seconds
            first_frame = first_battle = False
            while worker.poll() is None and time.monotonic() < deadline:
                if args.recording_mode and (live / "close.txt").exists():
                    break
                if (run / "snapshot-error.txt").exists():
                    raise RuntimeError("快照恢复失败，已停止播放：" + (run / "snapshot-error.txt").read_text().strip())
                if viewer and viewer.poll() is not None: break
                request = live / "open-html.txt"
                if request.exists():
                    next_html = request.read_text().strip(); break
                frame = live / "frame.shm"
                if frame.exists():
                    with frame.open("rb") as f: header = f.read(64)
                    if len(header) == 64 and header[:4] == b"CRXF":
                        tick = struct.unpack_from("<i", header, 16)[0]
                        if not first_frame:
                            first_frame=True; stages["first_frame_seconds"]=time.perf_counter()-started
                            status(run, "render", "原生画面已启动，正在进入对局…", fraction=.75)
                        if tick > 10 and not first_battle:
                            first_battle=True; stages["battle_ready_seconds"]=time.perf_counter()-started
                            status(run, "playing", "原生对战回放", fraction=1)
                        if args.exit_at_end and tick >= report["end_tick"]:
                            (run / "completed.json").write_text(json.dumps({"tick": tick, "end_tick": report["end_tick"], "frames": struct.unpack_from("<I",header,4)[0]})+"\n")
                            break
                time.sleep(.1)
            (run / "timings.json").write_text(json.dumps(stages, indent=2)+"\n")
            if worker.poll() not in (None, 0):
                raise RuntimeError(f"渲染器异常退出（{worker.returncode}），请查看 {run/'render.log'}")
            if args.exit_at_end and not next_html and not (run / "completed.json").exists():
                raise RuntimeError("运行时间内没有播到结束，请查看 render.log")
    except SwitchReplay as request:
        next_html = request.path
    finally:
        try:
            stop(worker)
        finally:
            try:
                stop(viewer)
            finally:
                lease.__exit__(None, None, None)
    if next_html:
        args.html = next_html
        return play(args, command, parse_html, doctor)
    return 0
