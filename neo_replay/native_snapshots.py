"""Current-build native battle snapshots, including the pending command queue.

All native methods must run on the battle's owning thread. The addresses below
are selected from the verified x86_64 build bindings.
"""
import ctypes
import hashlib
import json
from pathlib import Path
import time

from .tick_cache import TickCacheReader, TickCacheWriter

SUPPORTED_BUILDS = {"160402020", "160635001"}


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def board_digest(observation):
    value = {"tick": observation["tick"], "players": observation["players"],
             "objects": sorted(observation["objects"], key=lambda obj: obj["nativeObjectId"])}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class NativeSnapshotStream:
    """Reuse one native stream; never retain a native pointer in the disk cache."""
    def __init__(self, g, bindings):
        if bindings.build not in SUPPORTED_BUILDS:
            raise ValueError(f"Native snapshots have not been verified for build {bindings.build}")
        self.g, self.B = g, bindings
        self.stream = g.call(bindings.fn("native_alloc"), 0x80)
        ctypes.memset(self.stream, 0, 0x80)
        g.call(bindings.fn("byte_stream_ctor"), self.stream, 16384)

    def _reset(self):
        # Native reset clears byte/bit positions AND the cached VInt state.
        self.g.call(self.B.fn("replay_stream_reset"), self.stream)

    def _buffer(self):
        return ctypes.c_uint64.from_address(self.stream + 0x38).value

    def save(self, battle):
        self._reset()
        capacity = ctypes.c_int32.from_address(self.stream + 0x28).value
        ctypes.memset(self._buffer(), 0, capacity)
        # The stock rendered restore expects a has-header boolean before the
        # body. The match is already initialized, so no header is needed.
        self.g.call(self.B.fn("replay_stream_write_bool"), self.stream, 0)
        self.g.call(self.B.fn("battle_snapshot_encode"), battle, self.stream, 1, 0, 0, 0, 0)
        size = self.g.call(self.B.fn("replay_stream_size"), self.stream)
        if not 0 < size <= 1048576:
            raise ValueError(f"Unexpected native snapshot length {size}")
        return ctypes.string_at(self._buffer(), size)

    def load(self, battle, payload, sync=0):
        if not 0 < len(payload) <= 1048576:
            raise ValueError("Invalid native snapshot length")
        self._reset()
        self.g.call(self.B.fn("replay_stream_reserve"), self.stream, len(payload))
        ctypes.memmove(self._buffer(), payload, len(payload))
        ctypes.c_int32.from_address(self.stream + 0x14).value = len(payload)
        self.g.call(self.B.fn("replay_stream_flip"), self.stream)  # flip to read mode, including VInt caches
        if sync:
            if ctypes.c_uint64.from_address(sync + 8).value != battle:
                raise RuntimeError("Render snapshot belongs to a different active battle")
            # Stock restore dispatches state/listener notifications. The HUD is
            # refreshed separately without restarting the scene or the battle.
            if not self.g.call(self.B.fn("replay_stock_restore"), sync, self.stream):
                raise RuntimeError("Stock rendered snapshot restore failed")
        else:
            vtable = ctypes.c_uint64.from_address(self.stream).value
            read_bool = ctypes.c_uint64.from_address(vtable + 0xE0).value
            if self.g.call(read_bool - self.g.base, self.stream):
                raise ValueError("Unexpected full-header snapshot")
            self.g.call(self.B.fn("battle_snapshot_load"), battle, self.stream, 1, 0)

    def close(self):
        if self.stream:
            self.g.call(self.B.fn("replay_stream_dtor"), self.stream)
            self.g.call(self.B.fn("native_free"), self.stream)
            self.stream = 0


def build(output, cfg):
    """Fresh headless process: capture every tick from the accepted replay commands."""
    import os
    from .runner import status
    os.environ["CRX_ASSETS"] = cfg["assets"]
    from env import CrxEnv
    import gate3
    output = Path(output)
    replay_path = output / "replay.json"
    match = json.loads(replay_path.read_text())
    report = json.loads((output / "report.json").read_text())
    started = time.monotonic()
    env = CrxEnv(cfg["engine"], output / "snapshot-work", cfg["assets"])
    env.create_match(match)
    stream = NativeSnapshotStream(env.g, env.B)
    end_tick = report["end_tick"]
    status(output, "snapshots", f"正在缓存对局状态：0 / {end_tick} tick",
           fraction=0, current_tick=0, total_ticks=end_tick)
    try:
        with TickCacheWriter(output) as writer:
            while True:
                tick = env.tick()
                writer.add(tick, stream.save(env.battle))
                if tick and tick % 100 == 0:
                    status(output, "snapshots", f"正在缓存对局状态：{tick} / {end_tick} tick",
                           fraction=min(tick / max(1, end_tick), 1),
                           current_tick=tick, total_ticks=end_tick)
                if tick >= end_tick:
                    break
                gate3.step(env.g, env.state, 1)
                if env.tick() != tick + 1:
                    raise RuntimeError(f"Snapshot preparation stopped at tick {tick}")
                if env.tick() % 1000 == 0:
                    print(f"cache tick={env.tick()}/{report['end_tick']}", flush=True)
            final = env.observe()
            if board_digest(final) != report["simulation_state_sha256"]:
                raise RuntimeError("Snapshot replay differs from the prepared simulation; cache was not published")
            metadata = {"build": env.B.build, "libg_sha256": env.B.data["libg_sha256"],
                        "replay_sha256": sha256(replay_path), "tick_seconds": 0.05,
                        "cache_identity": cfg["cache_identity"],
                        "snapshot_format": "stock-header-flag-and-command-body-v1",
                        "simulation_state_sha256": board_digest(final)}
            writer.finish(metadata)
    finally:
        stream.close()
    report["tick_cache"] = {"first_tick": 0, "last_tick": env.tick(), "ticks": env.tick() + 1,
                            "bytes": (output / "snapshot.bin").stat().st_size,
                            "seconds": round(time.monotonic() - started, 3)}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    status(output, "snapshots", "逐 tick 缓存完成", fraction=1,
           current_tick=env.tick(), total_ticks=end_tick)
    print("Every-tick native cache ready: " + json.dumps(report["tick_cache"]), flush=True)


def install(live, g, B, manager, every_update):
    """Install disk-backed seeking; called on the renderer's update thread."""
    import os
    from live import ACC_FIELD
    directory = Path(os.environ["NEO_TICK_CACHE"])
    cache = TickCacheReader(directory, expected_engine=B.data["libg_sha256"],
                            expected_replay=hashlib.sha256(live.replay_text.encode()).hexdigest())
    if cache.index.get("cache_identity") != os.environ["NEO_CACHE_IDENTITY"]:
        cache.close()
        raise ValueError("Snapshot battle data or implementation identity mismatch")
    stream = NativeSnapshotStream(g, B)
    from .native_presentation import SnapshotPresentation
    presentation = SnapshotPresentation(g, B)
    pending_refresh = []
    q = lambda address: ctypes.c_uint64.from_address(address).value
    i32 = lambda address: ctypes.c_int32.from_address(address).value

    def rewind(tick):
        if i32(manager + B.field("manager.state_type")) != 4:
            raise RuntimeError("Cannot restore a snapshot outside a native battle")
        state = q(manager + B.field("manager.state"))
        battle = q(state + B.field("state.battle")) if state else 0
        if not battle:
            raise RuntimeError("Native battle disappeared before snapshot restoration")
        if os.environ.get("NEO_SNAPSHOT_DIAG"):
            sync = q(state + 0x80)
            listeners = q(sync + 0x10)
            callbacks = [hex(q(q(q(listeners + 8 * i))) - g.base)
                         for i in range(min(32, max(0, i32(sync + 0x1C))))]
            live.event("snapshot listeners", repr(callbacks))
            view = g.call(B.fn("replay_spectator_view"))
            if view:
                live.event("snapshot view", f"vtable {q(view)-g.base:#x}, seek {q(q(view)+0x9A8)-g.base:#x}")
        payload = cache.get(tick)
        stream.load(battle, payload, q(state + 0x80))
        if i32(battle + B.field("battle.tick")) != tick:
            raise RuntimeError("Native snapshot restored the wrong tick")
        ctypes.c_float.from_address(state + ACC_FIELD).value = 0.0
        presentation.restored()
        pending_refresh[:] = [(battle, tick + 1)]
        # encode/load use the same queue flag; future commands are already restored.
        live.event("snapshot loaded", f"tick {tick}, disk cache, {len(payload)} bytes")

    def guarded(action):
        try:
            action()
        except Exception as error:
            live.event("snapshot failed", repr(error))
            (directory / "snapshot-error.txt").write_text(str(error) + "\n")
            # The host watches this marker and closes playback; never simulate
            # onwards from a half-restored state or silently show a wrong frame.
            live.seek_phase = "snapshot-error"
            raise

    def finish_refresh():
        if not pending_refresh:
            return
        if i32(manager + B.field("manager.state_type")) != 4:
            raise RuntimeError("Native battle changed before HUD refresh")
        state = q(manager + B.field("manager.state"))
        battle = q(state + B.field("state.battle")) if state else 0
        expected_battle, target = pending_refresh[0]
        if battle != expected_battle:
            raise RuntimeError("Snapshot HUD refresh belongs to a different battle")
        if i32(battle + B.field("battle.tick")) >= target:
            pending_refresh.clear()
            presentation.refreshed()

    def update():
        if pending_refresh:
            guarded(finish_refresh)

    def close():
        pending_refresh.clear()
        if update in every_update:
            every_update.remove(update)
        cache.close()
        stream.close()
        live.keyframe_at = None
        live.precomputed_snapshots = False

    live.precomputed_snapshots = True
    live.keyframe_at = lambda target: target - 1 if cache.first_tick <= target - 1 <= cache.last_tick else None
    live.rewind = lambda tick: guarded(lambda: rewind(tick))
    live.clear_snapshots = close
    every_update.append(update)
    live.event("snapshot cache ready", f"{cache.first_tick}..{cache.last_tick}, every tick")
