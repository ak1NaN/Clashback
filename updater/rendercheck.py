"""Check that a staged bundle actually draws a battle before installation."""
import json
import os
from pathlib import Path
import struct
import subprocess
import time

from neo_replay.paths import ROOT, native_env


def render_options(replay, live, width, height, end_tick):
    return dict(
        NEO_RENDER_WIDTH=str(width), NEO_RENDER_HEIGHT=str(height),
        CRX_RENDER_ORDER="device_first", CRX_KEEP_RESOURCE_VARIANT="1",
        CRX_UNPATCHED="renderer_bootstrap_call,renderer_objects_block,renderer_device_block,renderer_resolution_call,renderer_late_configure_call",
        CRX_RENDER_REPLAY=str(replay), CRX_RENDER_REPLAY_AT="0", CRX_RENDER_PUMP="0",
        CRX_RENDER_LOAD_WARP="20", CRX_LIVE_SKIP_INTRO="1", CRX_RENDER_FPS="0",
        CRX_RENDER_LIVE=str(live), CRX_LIVE_SURFACE="0", CRX_LIVE_BOARD="0",
        CRX_LIVE_END_TICK=str(end_tick), CRX_LIVE_SNAPSHOTS="0",
    )


def check(game, folder, command):
    folder = Path(folder).resolve()
    live = folder / "live"
    live.mkdir(parents=True)
    replay = folder / "match.json"
    match = json.loads((ROOT / "neo_replay/standard_match.json").read_text())
    match["endTick"] = 100
    replay.write_text(json.dumps(match))
    (live / "control.txt").write_text("paused=0 speed=1 step=0 mark=0 seek_id=0\n")
    env = native_env()
    env.pop("NEO_TICK_CACHE", None)
    env.update(render_options(replay, live, 360, 640, match["endTick"]))
    env.update(CRX_ASSETS=str(game / "assets"), NEO_VIEW_OWNER="1")
    log = folder / "render.log"
    with log.open("w") as output:
        worker = subprocess.Popen(command("render", game / "engine", folder / "work", game / "assets", folder, 45),
                                  cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            end = time.monotonic() + 60
            while worker.poll() is None and time.monotonic() < end:
                frame = live / "frame.shm"
                if frame.is_file():
                    with frame.open("rb") as f:
                        header = f.read(64)
                        if len(header) == 64 and header[:4] == b"CRXF":
                            _, _, w, h, tick = struct.unpack_from("<4sIIIi", header)
                            if tick >= 60 and (w, h) == (360, 640):
                                pixels = f.read(w * h * 4)
                                if len(pixels) == w * h * 4 and len({pixels[i:i+3] for i in range(0, len(pixels), 128)}) > 32:
                                    return {"tick": tick, "rendered_size": [w, h], "native_battle_drawn": True}
                time.sleep(.1)
        finally:
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()
    tail = "\n".join(log.read_text(errors="replace").splitlines()[-12:])
    raise RuntimeError("候选包未能渲染对战，未替换旧数据：\n" + tail)
