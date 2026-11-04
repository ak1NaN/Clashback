"""Live viewer, the container side: every frame the game swaps goes to LIVE_DIR/frame.bin for the Mac window
(viewer/Viewer.swift), and the window's controls in LIVE_DIR/control.txt steer the battle's clock. Only with
CRX_RENDER_LIVE=<dir>; nothing here runs otherwise.

frame.bin: a 64-byte header, then width*height RGBA rows bottom-up (as OpenGL reads them):
    '<4sIIIiiff' magic b'CRXF', frame, width, height, tick (-1 before the battle), paused (0/1), speed, game seconds
On a Mac (the game in a Mac process, live_mac.sh) the same layout lives in frame.shm, memory both processes map: the frame
is read from the GPU straight into it, nothing is written to disk. Header +32: a sequence number, odd while a frame is
being written (the window skips a frame it read while the number was odd or changed).
control.txt: 'paused=0 speed=1 step=0 mark=0' (written whole by the window). step and mark are counters: each increase of
step asks for one more engine tick while paused; each increase of mark saves the current frame as a PNG (proof frames).
Optional pause_at=T: stop at tick T (pauses two ticks before it, then steps one tick at a time: exact even if a slow frame
moves two ticks; used to compare two renderers, and a straight run with jumps, at the same tick).
Jumps (the window's time bar and back buttons): seek=T seek_id=N; each new seek_id moves the battle to tick T and pauses
there. With a precomputed tick cache, both directions restore T-1 and advance once without restarting the battle.
Older engines without this cache seek forward in batches and restart only when no backward keyframe is available.
dump=N (a counter): the board crx reads (both players, every object) goes to board_<N>_tick_<T>.json.
Header +36..+51 (int32): the battle's last tick (the replay's endTick), seeking (0/1), the seek's target, restarts so far.
Header +52..+63 (CRX_LIVE_SURFACE=1, surface_out.py): two IOSurface ids and the one holding the frame; the pixels after the
header are then not written (except when a proof frame is saved).

The clock. Speed is the replay controller's own speed (controller +0x74, 1.0): its clock (150535029 +0xce26d0) takes
min(dt, 0.1 s) and multiplies it by that speed, so x4 really runs 4 ticks of game time per 50 ms. Scaling the frame's dt
instead stops at x2 (the 0.1 s cap = 2 ticks per frame). Pause and step use try_surface.record_dt's stub on the manager's
dt: scale 0 = paused; a step is one tick's worth of time (TICK / speed) added once while paused, until the tick moves.
"""
SPEED_FIELD = 0x74                # 150535029: replay controller speed (float)
ACC_FIELD = 0x44                  # 150535029: its time not yet turned into ticks (float seconds)
import ctypes
import json
import mmap
import os
import struct
import sys
import time
from pathlib import Path

TICK = 0.05                       # seconds of game time per engine tick (20 ticks a second)
SEEK_SPEED = 256.0                # the controller's speed while jumping: its clock takes min(dt, 0.1) x speed, so a frame can
SEEK_MAX = 512                    # carry up to 0.1 x 256 / 0.05 = 512 ticks
INTRO_WARP = 30.0
SURFACE = os.environ.get('CRX_LIVE_SURFACE') == '1' and sys.platform == 'darwin'   # draw into the window (surface_out.py)
TRACE = os.environ.get('CRX_LIVE_TRACE')                # a .jsonl: the board every TRACE_EVERY ticks (training-game check)
TRACE_EVERY = int(os.environ.get('CRX_LIVE_TRACE_EVERY', '5'))
# while tracing, the battle is fed exactly one tick per update (a slow frame at x2 once moved two ticks and the trace missed
# one); and CRX_LIVE_END_TICK: pause there (a training game's end: crx ends it, the replay itself would run on)
END_TICK = int(os.environ.get('CRX_LIVE_END_TICK', '0'))
INTRO_FRAMES = int(os.environ.get('CRX_INTRO_FRAMES', '240'))
# CRX_LIVE_SKIP_INTRO=1 (live_mac.sh's default): the intro is never shown, on the first start as after a jump
SKIP_INTRO = os.environ.get('CRX_LIVE_SKIP_INTRO') == '1' and sys.platform == 'darwin'
# CRX_LIVE_START_TICK=T: once the battle is up, jump to tick T and play on from there (a Hog micro starts just before the Hog)
START_TICK = int(os.environ.get('CRX_LIVE_START_TICK', '0'))


def _warp_library():
    try:
        lib = ctypes.CDLL(None)
        lib.crx_time_warp.argtypes = [ctypes.c_double]
        lib.crx_steady_seconds.restype = ctypes.c_double
        return lib
    except (OSError, AttributeError):
        return None


_WARP: list = []


def warp_library():
    """bionic_mac's clock warp (looked up on first use: the library is loaded after this module is imported)."""
    if not _WARP:
        _WARP.append(_warp_library() if sys.platform == 'darwin' else None)
    return _WARP[0]
SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)


class Live:
    def __init__(self, folder, EB):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.EB = EB
        self.control = self.dir / 'control.txt'
        self.events = open(self.dir / 'events.txt', 'a', buffering=1)
        self.seen_mtime = None
        self.paused, self.speed, self.step_seen, self.mark_seen = False, 1.0, None, None
        self.owed, self.step_from, self.step_wait = 0, None, 0
        self.marks = []
        self.pause_at = None
        self.seek_seen, self.seek_target, self.seek_phase, self.seek_wait, self.seek_t0 = None, None, None, 0, 0.0
        self.dump_seen, self.dumps = None, []
        self.restarts, self.end_tick = 0, 0
        self.intro_until = None
        self.first_intro = None                   # the first start's intro skip (SKIP_INTRO): None not yet, >0 until frame, 0 done
        self.frames = 0
        self.traced = set()
        self.end_paused = False
        self.end_pause = END_TICK                 # pause on this tick (CRX_LIVE_END_TICK; each loaded game sets its own)
        self.load_file, self.load_seen, self.load_mtime = self.dir / 'load.json', None, None
        self.hold_at = None                       # play mode (play_server.py): the battle never runs past this tick
        self.play_clock = None                    # (wall time, tick, speed) the play-mode battle keeps to
        self.last_frame_time = time.time()
        self.replay_text = None
        self.play = None                          # the PlayServer, for the window's clicks
        self.click_seen = None
        self.restart = self.board = None          # set by try_surface: hand the replay again; read the board
        self.rewind = self.keyframe_at = None     # set by try_surface: the game's own battle snapshots (an instant rewind)
        # set by try_surface: run a function on the game's update thread at its next battle-manager update (the battle is
        # between ticks there and nothing else changes it); whether such work is still waiting
        self.on_update_thread = lambda fn: fn()
        self.update_busy = lambda: False
        self.dt_slot = None
        self.tick = -1
        self.t0 = time.time()
        w, h = EB.STATE['width'], EB.STATE['height']
        self.shared = sys.platform == 'darwin'
        if self.shared:
            with open(self.dir / 'frame.shm', 'wb') as f:
                f.truncate(64 + w * h * 4)
            self.fd = os.open(self.dir / 'frame.shm', os.O_RDWR)
            self.buf = mmap.mmap(self.fd, 64 + w * h * 4)
            self.base = ctypes.addressof(ctypes.c_char.from_buffer(self.buf))
        else:
            self.buf = ctypes.create_string_buffer(64 + w * h * 4)
            self.base = ctypes.addressof(self.buf)
        self.pixels = self.base + 64
        self.seq = 0
        self.surface = None                       # surface_out.SurfaceOut once the game's context exists (CRX_LIVE_SURFACE)
        self.event('start', f'{w}x{h}')

    def event(self, what, details=''):
        self.events.write(f'{time.time() - self.t0:9.3f} tick {self.tick:5d} {what} {details}\n')

    def check_load(self):
        """A binge session: LIVE_DIR/load.json {"id", "replay", "start", "end"} (written by the Lab) loads another game of the
        same build into this running game: the replay is swapped, the battle restarts in place (no boot), jumps to `start`
        and plays on, and pauses at `end`."""
        try:
            st = os.stat(self.load_file)
        except OSError:
            return
        if st.st_mtime_ns == self.load_mtime or self.first_intro or self.seek_target is not None:
            return
        self.load_mtime = st.st_mtime_ns
        try:
            req = json.loads(self.load_file.read_text())
            text = Path(req['replay']).read_text()
        except (OSError, ValueError, KeyError) as e:
            self.event('load failed', str(e))
            return
        if req.get('id') == self.load_seen or not getattr(self, 'set_replay_text', None):
            return
        self.load_seen = req.get('id')
        self.set_replay_text(text)
        if getattr(self, 'clear_snapshots', None):
            self.on_update_thread(self.clear_snapshots)
        self.command_list = None
        self.end_pause = int(req.get('end') or 0)
        self.end_paused = False
        try:
            self.end_tick = self.end_pause or int(json.loads(text).get('endTick', 0))
        except ValueError:
            self.end_tick = self.end_pause
        self.event('load', f"{req['replay']} start {req.get('start', 0)} end {self.end_pause}")
        self.resume_after_seek = True
        self.start_seek(max(0, int(req.get('start') or 0)), force_restart=True)

    def read_control(self):
        self.check_load()
        try:
            st = os.stat(self.control)
        except OSError:
            return
        if st.st_mtime_ns == self.seen_mtime:
            return
        self.seen_mtime = st.st_mtime_ns
        try:
            kv = dict(part.split('=', 1) for part in self.control.read_text().split() if '=' in part)
        except OSError:
            return
        paused = kv.get('paused', '0') == '1'
        speed = float(kv.get('speed', '1'))
        step, mark = int(kv.get('step', '0')), int(kv.get('mark', '0'))
        self.pause_at = int(kv['pause_at']) if 'pause_at' in kv else None
        seek_id, dump = int(kv.get('seek_id', '0')), int(kv.get('dump', '0'))
        if 'play' in kv and self.play is not None and kv['play'] != self.click_seen:     # a click: SEQ:CARD:X:Y
            if self.click_seen is not None:
                _seq, card, x, y = kv['play'].split(':')
                self.play.click(int(card), int(x), int(y))
                self.event('click', kv['play'])
            self.click_seen = kv['play']
        if 'ability' in kv and self.play is not None and kv['ability'] != getattr(self, 'ability_seen', None):  # SEQ:CARD
            if getattr(self, 'ability_seen', None) is not None:
                self.play.ability_click(int(kv['ability'].split(':')[1]))
                self.event('ability click', kv['ability'])
            self.ability_seen = kv['ability']
        if self.step_seen is None:                 # the first read sets the counters' baseline
            self.step_seen, self.mark_seen, self.seek_seen, self.dump_seen = step, mark, seek_id, dump
        if seek_id > self.seek_seen and 'seek' in kv:
            self.seek_seen = seek_id
            self.start_seek(max(0, int(kv['seek'])))
        if dump > self.dump_seen:
            self.dump_seen = dump
            self.dumps.append(dump)
        if paused != self.paused:
            self.paused = paused
            self.event('pause' if paused else 'resume')
        if speed != self.speed and 0 < speed <= 256:            # the window offers SPEEDS; tests may ask more
            self.speed = speed
            self.event('speed', f'x{speed:g}')
        if step > self.step_seen:
            self.owed += step - self.step_seen
            self.event('step asked', f'{step - self.step_seen}')
        if mark > self.mark_seen:
            self.mark_seen = mark
            self.marks.append(mark)                 # saved after this frame is read back (write)
        self.step_seen = max(self.step_seen, step)

    def start_seek(self, target, force_restart=False):
        if getattr(self, 'precomputed_snapshots', False):
            target = max(2, target)
        if self.end_tick:
            target = min(target, self.end_tick)
        if self.seek_target is not None and getattr(self, 'precomputed_snapshots', False) and not force_restart:
            self.pending_seek_target = target
            self.event('seek queued', f'latest target {target}')
            return
        restart_needed = force_restart or self.tick < 0 or target < self.tick
        key = self.keyframe_at(target) if (restart_needed or getattr(self, 'precomputed_snapshots', False)) and not force_restart and self.keyframe_at and self.tick >= 0 and \
            self.seek_phase not in ('restart', 'snapshot') else None
        self.event('seek asked', f'{self.tick} -> {target}' + (f' (snapshot {key})' if key is not None else
                   ' (restart)' if restart_needed and self.seek_phase != 'restart' else ''))
        if self.seek_target is None:
            self.seek_t0 = time.time()
        self.seek_target, self.paused, self.owed, self.step_from, self.pause_at = target, True, 0, None, None
        if key is not None:
            # back to the latest snapshot at or before the target (the game's own battle save, taken every KEY_EVERY ticks
            # as the game played), then forward to the exact tick: no restart from tick 0, no intro, one frame
            self.on_update_thread(lambda: self.rewind(key))
            self.seek_phase = 'snapshot'
        elif restart_needed and self.seek_phase != 'restart' and self.restart:
            self.on_update_thread(self.restart)              # the battle goes back to tick 0 with the same plays
            self.restarts += 1
            self.seek_phase, self.seek_wait = 'restart', 0
            self.intro_until = self.warp_start()             # the start-of-battle intro, run on fast time
        elif self.seek_phase != 'restart':
            self.seek_phase = 'forward'

    # The game's start-of-battle intro (overlay, VS banner, camera; ~3.5 s) runs on its steady clock, not on battle time, so a
    # restart would show it over the jumped-to tick. bionic_mac (the Mac path) can run that clock faster (INTRO_WARP x); the
    # intro takes at most a slice of time per frame, so it needs frames too: the warp holds for INTRO_FRAMES frames after the
    # restart (the frame limiter, seeing time fly, stops sleeping). Without it (the container) the intro plays as usual.
    def warp_start(self):
        if not warp_library():
            return None
        self.warp(INTRO_WARP)
        return self.frames + INTRO_FRAMES

    def warp(self, k):
        if warp_library():
            warp_library().crx_time_warp(ctypes.c_double(k))

    def steady(self):
        return warp_library().crx_steady_seconds() if warp_library() else 0.0

    def seek_frame(self, controller, scale, extra):
        """One frame of a jump: feed the battle exactly the time for min(left, 512) ticks, at the controller's seek speed."""
        scale.value = 0.0                                    # the frame's own time counts for nothing while jumping
        if self.seek_phase == 'snapshot-error':
            extra.value = 0.0
            return
        if self.seek_phase == 'snapshot':                    # the snapshot is loaded at the next update
            if self.update_busy():
                return
            self.seek_phase = 'forward'
            return
        if self.seek_phase == 'restart':                     # the new battle takes over at the next manager update
            if self.update_busy():                           # the restart itself has not run yet
                return
            if self.intro_until and self.frames < self.intro_until:
                # the intro: its first part runs on clock time (warped), the VS banner on the manager's time; the battle's own
                # clock is held (speed 1e-6: under a microsecond of battle time a frame), so no tick moves meanwhile
                scale.value = INTRO_WARP
                if controller:
                    ctypes.c_float.from_address(controller + SPEED_FIELD).value = 1e-6
                return
            if self.intro_until:
                self.warp(1.0)
                self.intro_until = None
                self.event('intro frames done', f'frame {self.frames}, {time.time() - self.seek_t0:.2f} s after the jump was asked')
            self.seek_wait += 1
            if self.seek_wait < 3 or self.tick < 0:
                return
            if self.tick > self.seek_target:
                return
            self.seek_phase = 'forward'
        left = self.seek_target - self.tick
        if left < 0:                                         # (only if a frame overshot) start again from tick 0
            target = self.seek_target
            self.seek_target = self.seek_phase = None
            extra.value = 0.0
            self.start_seek(target)
            return
        if left == 0:
            self.event('seek done', f'tick {self.tick} in {time.time() - self.seek_t0:.2f} s')
            self.seek_target = self.seek_phase = None
            pending = getattr(self, 'pending_seek_target', None)
            self.pending_seek_target = None
            if pending is not None:
                self.start_seek(pending)
                return
            if getattr(self, 'resume_after_seek', False):       # the start jump (START_TICK): play on, not paused
                self.resume_after_seek, self.paused = False, False
            if controller:
                ctypes.c_float.from_address(controller + SPEED_FIELD).value = self.speed
            return
        k = 0
        if controller:
            ctypes.c_float.from_address(controller + SPEED_FIELD).value = SEEK_SPEED
            # the controller works given time off over several updates, so its unspent time (+0x44) is counted: the top-up
            # brings it to (ticks left + half a tick), never more. The half tick keeps float rounding (0.04999 for 0.05)
            # from adding or losing a tick; it stays in the account and only moves the next tick's moment.
            unspent = ctypes.c_float.from_address(controller + ACC_FIELD).value
            give = (min(left, SEEK_MAX) + 0.5) * TICK - unspent
            k = min(left, SEEK_MAX)
            if give > 0:
                extra.value = give / SEEK_SPEED
        if os.environ.get('CRX_SEEK_LOG'):
            self.event('seek frame', f'tick {self.tick} left {left} k {k} controller {controller:#x}')

    def on_frame(self, n, tick, controller=0):
        """After the game's own on_frame work, before the swap: apply the controls to the clock for the next update."""
        self.tick = -1 if tick is None else tick
        self.frames = n
        self.read_control()
        if self.dumps and self.board and self.seek_target is None and not self.update_busy():
            self.on_update_thread(self.write_board)     # read where the battle lives, between updates
        if self.pause_at is not None and not self.paused and self.tick >= self.pause_at - 2 and self.tick >= 0:
            # stop two ticks early, then exact one-tick steps to it: a slow frame can move a running battle two ticks at once
            self.paused = True
            self.owed = max(0, self.pause_at - self.tick)
            self.event('pause', f'near pause_at {self.pause_at} at {self.tick}; {self.owed} steps to it')
            self.pause_at = None
        if self.dt_slot is None:
            return
        scale = ctypes.c_float.from_address(self.dt_slot + 16)
        extra = ctypes.c_float.from_address(self.dt_slot + 20)
        # A requested opening pause must not freeze the loading/scene startup.
        # Hold the battle once tick 0 exists, after the stock intro is skipped.
        if self.tick < 0:
            scale.value = 1.0
            return
        if SKIP_INTRO and self.first_intro is None and self.tick >= 0 and controller and self.seek_target is None:
            self.first_intro = self.warp_start() or 0        # the first start's intro too, on fast time and not shown
            self.intro_t0 = time.time()
        if self.first_intro:
            if self.frames < self.first_intro:
                scale.value = INTRO_WARP                     # as a jump's restart: the intro flies, the battle stays at tick 0
                ctypes.c_float.from_address(controller + SPEED_FIELD).value = 1e-6
                return
            self.warp(1.0)
            self.first_intro = 0
            self.event('intro skipped', f'frame {n}, {time.time() - self.intro_t0:.2f} s')
            if START_TICK > 0:
                self.resume_after_seek = True
                self.start_seek(START_TICK)
        if self.seek_target is not None:
            self.seek_frame(controller, scale, extra)
            return
        if controller:
            speed = ctypes.c_float.from_address(controller + SPEED_FIELD)
            if speed.value != self.speed:
                speed.value = self.speed
        if self.end_pause and not self.paused and self.tick >= self.end_pause - 2 and self.pause_at is None and \
                not self.end_paused and self.tick < self.end_pause:
            self.end_paused = True                          # stop exactly on the last tick (the pause_at steps)
            self.pause_at = self.end_pause
        frame_dt = min(0.1, max(0.0, time.time() - self.last_frame_time))
        self.last_frame_time = time.time()
        if self.hold_at is not None and controller and not self.paused:
            # play mode: this frame's time at the chosen speed, but never a whole tick past the granted tick (the
            # unspent time counted, aimed half a tick short of the next: float rounding cannot carry it over)
            # Kept to the wall clock: after the loop's short waits (its decisions) the battle catches up, so on average it
            # runs at exactly the chosen speed.
            scale.value = 0.0
            unspent = ctypes.c_float.from_address(controller + ACC_FIELD).value
            now_s = time.time()
            if self.play_clock is None or self.play_clock[2] != self.speed or self.tick < 0:
                self.play_clock = (now_s, max(self.tick, 0), self.speed)
            t0, tick0, _sp = self.play_clock
            behind = (tick0 + (now_s - t0) * self.speed / TICK) - (max(self.tick, 0) + unspent / TICK)
            if behind > 40:                                  # far behind (a long wait): start the clock again from here
                self.play_clock, behind = (now_s, max(self.tick, 0), self.speed), 0.0
            cap = (self.hold_at - max(self.tick, 0) + 0.5) * TICK - unspent
            give = min(frame_dt * self.speed + max(0.0, behind) * TICK * 0.5, cap, 0.1 * self.speed)
            if give > 0:
                extra.value = give / self.speed
            return
        if not self.paused and TRACE and controller and self.tick >= 0:
            # the board traced at every tick: never two ticks in one update, and the time it gets is this frame's own
            # (the wall clock at the chosen speed). Giving each update a whole tick made the game run at the frame rate:
            # 55 ticks/s instead of 20 once frames were drawn straight into the window (2026-10-14).
            scale.value, self.owed, self.step_from = 0.0, 0, None
            unspent = ctypes.c_float.from_address(controller + ACC_FIELD).value
            cap = (1.5 * TICK - unspent) / self.speed                       # wall seconds: at most one more tick
            give = min(frame_dt, cap)
            if give > 0:
                extra.value = give
            return
        if not self.paused:
            scale.value, self.owed, self.step_from = 1.0, 0, None
            return
        scale.value = 0.0
        if self.step_from is not None and self.tick != self.step_from:        # the asked tick happened
            self.event('stepped', f'{self.step_from} -> {self.tick}')
            self.owed, self.step_from = self.owed - 1, None
        if self.owed > 0 and tick is not None:
            if self.step_from is None:
                self.step_from, self.step_wait = self.tick, 0
            if self.step_wait == 0:
                extra.value = TICK / self.speed     # one tick's worth after the speed; the remainder is kept
            self.step_wait = (self.step_wait + 1) % 3         # give it a frame or two to land before topping up again

    def trace_board(self):
        """CRX_LIVE_TRACE: the board every TRACE_EVERY ticks, read at the start of every update (try_surface's every_update)."""
        if not self.board or self.seek_target is not None:
            return
        board = self.board()
        if not board or board['tick'] is None or board['tick'] % TRACE_EVERY or board['tick'] in self.traced:
            return
        self.traced.add(board['tick'])
        import json
        with open(TRACE, 'a') as f:
            f.write(json.dumps(board, sort_keys=True) + '\n')

    def feed_board(self):
        """CRX_LIVE_BOARD (live_mac.sh's default): the board at every tick, for the window's troop labels and its engine loop
        view: every object as the engine has it (troops, towers, their state), both players, and the engine's command queue
        (each command's tap and landing tick, both sides): LIVE_DIR/board.json, replaced atomically."""
        if not self.board or self.seek_target is not None:
            return
        board = self.board()
        if not board or board['tick'] is None or board['tick'] == getattr(self, 'fed_tick', None):
            return
        self.fed_tick = tick = board['tick']
        import json
        owner = {(p['accountIdHigh'], p['accountIdLow']): p['owner'] for p in board['players']}
        if getattr(self, 'command_list', None) is None or getattr(self, 'command_count', -1) != self.command_total():
            cmds = list(json.loads(self.replay_text).get('cmd') or []) if getattr(self, 'replay_text', None) else []
            if getattr(self, 'play', None) is not None:
                cmds += self.play.commands
            self.command_list, self.command_count = cmds, self.command_total()
        queue = []
        for c in self.command_list:
            body = c.get('c', {})
            lands = (body.get('t2') or 0) + 1
            if body.get('t', 0) <= tick + 60 and tick - 60 <= lands:
                queue.append({'owner': owner.get((body.get('idHi'), body.get('idLo')), -1), 'card': (body.get('sel') or {}).get('os'),
                              'tap': body.get('t'), 'lands': lands, 'x': body.get('px'), 'y': body.get('py'),
                              'ability': 'cgid' in body})
        board['queue'] = queue
        tmp = self.dir / 'board.tmp'
        tmp.write_text(json.dumps(board, separators=(',', ':')))
        tmp.replace(self.dir / 'board.json')

    def command_total(self):
        return len(self.play.commands) if getattr(self, 'play', None) is not None else 0

    def write_board(self):
        import json
        if not self.dumps:
            return
        board = self.board()
        while self.dumps:
            k = self.dumps.pop(0)
            path = self.dir / f'board_{k:03d}_tick_{self.tick}.json'
            path.write_text(json.dumps(board, sort_keys=True))
            self.event('board', path.name)

    def write(self, n):
        if SKIP_INTRO and (self.first_intro or (self.intro_until and self.seek_phase == 'restart')):
            return                                   # the intro is not shown: the window keeps the picture it has
        EB, w, h = self.EB, self.EB.STATE['width'], self.EB.STATE['height']
        game_s = self.tick * TICK if self.tick >= 0 else 0.0
        seeking = self.seek_target is not None
        extra_header = (self.end_tick, int(seeking), self.seek_target if seeking else -1, self.restarts)
        if self.shared:
            seq = ctypes.c_uint32.from_address(self.base + 32)
            if SURFACE and self.surface is None:
                try:
                    import surface_out
                    self.surface = surface_out.SurfaceOut(EB, w, h)
                    struct.pack_into('<II', self.buf, 52, *self.surface.ids)
                    self.event('surface', f'IOSurfaces {self.surface.ids}: the game draws into the window')
                except Exception as error:                                     # noqa: BLE001
                    self.surface = False
                    self.event('surface', f'failed, reading frames back instead: {error!r}')
            self.seq += 1
            seq.value = 2 * self.seq - 1                                        # odd: being written
            if self.surface:
                struct.pack_into('<i', self.buf, 60, self.surface.blit())
                if self.marks:                                                  # a proof frame wants the pixels too
                    EB.gles.glReadPixels(0, 0, w, h, 0x1908, 0x1401, self.pixels)
            else:
                EB.gles.glReadPixels(0, 0, w, h, 0x1908, 0x1401, self.pixels)
            struct.pack_into('<4sIIIiiff', self.buf, 0, b'CRXF', n, w, h, self.tick, int(self.paused), self.speed, game_s)
            struct.pack_into('<iiii', self.buf, 36, *extra_header)
            seq.value = 2 * self.seq
        else:
            EB.gles.glReadPixels(0, 0, w, h, 0x1908, 0x1401, self.pixels)
            struct.pack_into('<4sIIIiiff', self.buf, 0, b'CRXF', n, w, h, self.tick, int(self.paused), self.speed, game_s)
            struct.pack_into('<iiii', self.buf, 36, *extra_header)
            tmp = self.dir / 'frame.tmp'
            with open(tmp, 'wb') as f:
                f.write(self.buf.raw)
            os.replace(tmp, self.dir / 'frame.bin')
        while self.marks:
            self.save_png(f'mark_{self.marks.pop(0):03d}_frame_{n}')

    def save_png(self, label):
        w, h = self.EB.STATE['width'], self.EB.STATE['height']
        path = self.dir / f'{label}_tick_{self.tick}.png'
        self.EB.png(path, w, h, ctypes.string_at(self.pixels, w * h * 4))
        self.event('saved', path.name)
