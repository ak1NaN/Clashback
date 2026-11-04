"""Render gate 1, first probe: boot the engine as crx does, then hand it a window (the Android surface callbacks) and record
every EGL call and frame it makes. Options (environment):

    CRX_UNPATCHED=renderer_bootstrap_call,...   leave these renderer bypasses out of GameMain::init (gate2d.TEMPORARY)
    CRX_RENDER_NOSKIP=1                           do not set the skip-core-and-presentation flag after GameMain::init
    CRX_EGL_TRACE=1                               print each EGL call as it happens

    python3 /ce/crx/render/try_surface.py /libs /work /assets /out [seconds]
"""
import ctypes
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import egl_bridge as EB  # noqa: E402  (shims registered before the engine is loaded)


def main():
    libdir, work, assets, out = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
    seconds = float(sys.argv[5]) if len(sys.argv) > 5 else 20
    W, H = int(os.environ.get('NEO_RENDER_WIDTH', '720')), int(os.environ.get('NEO_RENDER_HEIGHT', '1600'))
    live_dir = os.environ.get('CRX_RENDER_LIVE')                # the live viewer (live.py): frames out, controls in
    EB.install(width=W, height=H, out=str(out / 'frames'), every=0 if live_dir else int(os.environ.get('CRX_RENDER_EVERY', '20')))
    if live_dir:
        import live as LV
        EB.STATE['live'] = LV.Live(live_dir, EB)
    EB.log('sound', f'{EB.no_sound(libdir / "libg.so")} FMOD functions report errors (no sound)')
    import gate2b as B
    import gate2c as C
    import gate2d as D
    import jni as J
    import loader as L
    for name in ('data', 'cache', 'external'):
        (work / name).mkdir(parents=True, exist_ok=True)
    L.install_crash_report()
    ld, g, vm = B.start(libdir, work)
    (out / 'libg_base.txt').write_text(hex(g.base))                # for watch_threads.py's stack labels
    if os.environ.get('CRX_RENDER_HOOKS'):                          # 150535029 only: which files the resource loader tries
        import hooks as HK
        HK.print_engine_errors(g)
        rva0 = lambda a: hex(a - g.base) if a and g.base <= a < g.base + 0x2000000 else hex(a or 0)   # noqa: E731
        HK.watch(g, 0x1425DE0, '554157415641545348' '83ec304889fb',
                 lambda rdi, *a: EB.log('shader thread', f'started, loader {hex(rdi)}'))
        HK.watch(g, 0x147FFB0, '5541574156415541545348' '81ecb8000000', lambda *a: EB.log('shader scan', 'started'))
        HK.watch(g, 0x1353C10, '554157415641554154534883ec18',
                 lambda rdi, rsi, rdx, ret: EB.log('File', f'{ctypes.string_at(rsi).decode(errors="replace") if rsi else None}'
                                                         f' from {rva0(ret)}'))
        for site in (0x12B0D56, 0x12B0DBB):                         # File(path) in the resource file loader (rsi = path)
            HK.watch_call(g, site, lambda rdi, rsi, rdx, ret: EB.log('file', ctypes.string_at(rsi).decode(errors='replace')
                                                                     if rsi else '(null)'))
    store = {}

    def text(handle):
        kind, value = vm.objects.get(handle or 0, ('null', None))
        return value if kind == 'string' else None

    def store_kv(args):
        key, value = J.va_args(args[3], 2)
        store[text(key)] = text(value)
        return 0
    java_assets(vm, J, L, Path(assets))
    base = 'com/supercell/titan/ApplicationUtilBase.'
    import bindings as BN
    BUILD['used'] = str(BN.of(g).data['build'])
    EB.log('build', BUILD['used'])
    vm.answers[base + 'getAppVersion()Ljava/lang/String;'] = place('version')
    vm.answers[base + 'storeKeyValue(Ljava/lang/String;Ljava/lang/String;)V'] = store_kv
    vm.answers[base + 'getKeyValue(Ljava/lang/String;)Ljava/lang/String;'] = lambda args: store.get(text(J.va_args(args[3], 1)[0]))
    step = lambda title, *a, **k: (EB.log('step', title), B.call_native(vm, *a, **k))[1]  # noqa: E731
    step('nOnNativeLibrariesLoaded', 'com/supercell/titan/TitanApplication.nOnNativeLibrariesLoaded')
    step('nOnApplicationCreate', 'com/supercell/titan/GameApp.nOnApplicationCreate')
    step('createGameMain', 'com/supercell/titan/GameApp.createGameMain', vm.new('android/content/res/AssetManager', 'assets'),
         vm.string(str(work / 'data')), vm.string(str(work / 'cache')), vm.string(str(work / 'external')),
         ctypes.c_long(8 << 30), ctypes.c_int(W), ctypes.c_int(H), ctypes.c_int(320), ctypes.c_float(320.0),
         ctypes.c_float(320.0), ctypes.c_int(2), vm.string(str(work / 'external')), vm.new('com/supercell/titan/GameApp', 'activity'))
    early = BUILD['used'] == '150535029'          # 150535029: asset roots before nOnCreate (it starts the shader-loading thread);
    if os.environ.get('CRX_RENDER_ORDER') == 'device_first' and early:   # later builds set up the asset store in start-up
        C.init_resources(g, assets)              # themselves (a second init crashed): crx's usual order, after nOnCreate
    step('nOnCreate', 'com/supercell/titan/GameApp.nOnCreate')
    if os.environ.get('CRX_RENDER_ORDER') == 'device_first' and not early:
        import bindings as BN                      # start-up makes the game singleton on its own thread (boot.py waits too)
        slot = g.addr(BN.of(g).glob('game_singleton_global'))
        t0 = time.time()
        while not ctypes.c_uint64.from_address(slot).value and time.time() - t0 < 15:
            time.sleep(0.1)
        EB.log('game singleton', f'after {time.time() - t0:.1f} s')
        if os.environ.get('CRX_RENDER_INIT_RESOURCES', '1') == '1':
            C.init_resources(g, assets)
    if os.environ.get('CRX_RENDER_ORDER') == 'device_first':
        return device_first(g, vm, work, assets, out, seconds, step, C, D, J, L)
    natural = bool(os.environ.get('CRX_RENDER_NATURAL'))         # the phone's order: window now, the game starts itself
    if natural:
        info = {'helper_after': 0}
    else:
        time.sleep(5)
        C.init_resources(g, assets)
    if not natural:
        if os.environ.get('CRX_RENDER_BOOTCTX'):
            EB.make_boot_context()
        EB.log('step', 'GameMain::init')
        info = D.game_main_init(g)
        if os.environ.get('CRX_RENDER_NOSKIP'):
            flag = D.bindings.of(g).location('skip_core_and_presentation_flag')
            if flag:
                ctypes.c_uint8.from_address(flag).value = 0
        EB.log('step', f'GameMain::init done, helper {hex(info["helper_after"] or 0)}')
    surface = vm.new('android/view/Surface', 'surface')
    seen = len(J.LOG)
    for title, args in (('nOnStart', ()), ('nOnSurfaceCreated', (surface,)),
                        ('nOnSurfaceChanged', (surface, ctypes.c_int(W), ctypes.c_int(H))),
                        ('nOnResume', ()), ('nOnWindowFocusChanged', (ctypes.c_int(1),))):
        try:
            step(title, 'com/supercell/titan/GameApp.' + title, *args)
        except Exception as e:                                         # noqa: BLE001
            EB.log('step', f'{title} failed: {e}')
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(1)
    jni_calls = [f'{n} {d}' for n, d in J.LOG[seen:] if n not in ('DeleteLocalRef', 'ExceptionCheck', 'NewGlobalRef', 'DeleteGlobalRef')]
    print('--- EGL / steps')
    for row in EB.LOG:
        print('  ', row)
    print(f'--- frames swapped: {EB.STATE["frames"]}, saved: {len(EB.STATE["saved"])}')
    print(f'--- GL functions fetched: {len(EB.STATE["procs"])}')
    print(f'--- JNI after the surface: {len(jni_calls)}', jni_calls[:40])
    print('--- missing / stub functions called:', dict(L.STUBS_CALLED))
    sys.stdout.flush()
    os._exit(0)


def java_assets(vm, J, L, root):
    """Android's Java AssetManager for the engine's directory listings (the shader loader lists sc3d/shaders this way):
    Context.getAssets() and AssetManager.list(path) -> the file names under the assets folder, as a String[]."""
    manager = vm.new('android/content/res/AssetManager', 'assets')
    vm.answers['android/content/Context.getAssets()Landroid/content/res/AssetManager;'] = manager

    def listing(args):
        handle = J.va_args(args[3], 1)[0]
        kind, path = vm.objects.get(handle or 0, ('null', None))
        folder = root / (path or '')
        names = sorted(p.name for p in folder.iterdir()) if kind == 'string' and folder.is_dir() else []
        from neo_replay import resourcefs
        resourcefs.record(path or '.', folder.is_dir(), 'list')
        EB.log('AssetManager.list', f'{path!r}: {len(names)} entries')
        return vm.new('array', [vm.string(n) for n in names])
    vm.answers['android/content/res/AssetManager.list(Ljava/lang/String;)[Ljava/lang/String;'] = listing

    def element(_env, arr, index, *_):
        kind, value = vm.objects.get(arr or 0, ('null', None))
        return value[index] if kind == 'array' and 0 <= index < len(value) else 0
    cb = L.callback(ctypes.c_long, *[ctypes.c_void_p] * 6)(
        lambda env, arr, index, *rest: element(env, arr, (index or 0) & 0xffffffff, *rest) or 0)
    vm.env_table[J.NAMES.index('GetObjectArrayElement')] = cb
    J.KEEP.append(cb) if hasattr(J, 'KEEP') else None
    java_floats(vm, J, L)


def java_floats(vm, J, L):
    """Float and double JNI calls answer in the float register: the stand-in returned every value as an integer, so the game
    read whatever was left in xmm0 (Display.getRefreshRate drives its frame pacing and time step). These now return real
    floats from vm.answers (0.0 when there is none); the refresh rate is 60 Hz."""
    vm.answers['android/view/Display.getRefreshRate()F'] = float(os.environ.get('CRX_REFRESH', '60'))
    keep = []
    for name in J.NAMES:
        kind = 'Float' if 'Float' in name else 'Double' if 'Double' in name else None
        if not kind or 'Array' in name or not name.startswith(('Call', 'GetStatic', 'Get')) or 'ID' in name:
            continue
        ctype = ctypes.c_float if kind == 'Float' else ctypes.c_double

        def fn(*args, _n=name):
            target = vm.ids.get(args[2] or 0) if len(args) > 2 else None
            key = f'{target[0]}.{target[1]}{target[2]}' if target else f'id {args[2] if len(args) > 2 else None}'
            answer = vm.answers.get(key)
            if callable(answer):
                answer = answer(args)
            vm._note(_n, key + ('' if answer is not None else '   (no answer: 0.0)'))
            return float(answer or 0.0)
        cb = ctypes.CFUNCTYPE(ctype, *[ctypes.c_void_p] * 6)(fn)
        keep.append(cb)
        vm.env_table[J.NAMES.index(name)] = ctypes.cast(cb, ctypes.c_void_p).value
    L.KEEP.extend(keep)


def start_replay_on_frame(g, D, text, at_frame):
    """FirstLight's offline native-render path (cr_replay_probe.cpp process_native_render_request_on_frame_thread), on the
    game's own frame thread: a native string with the replay JSON, the manager's state slot cleared if it holds the login /
    loading screen object, then set_replay_data(manager, string) (it takes the string). The battle's tick is logged on every
    frame from then on and goes into the saved frames' names."""
    B = D.bindings.of(g)
    q = lambda a: ctypes.c_uint64.from_address(a).value if a else 0          # noqa: E731
    i32 = lambda a: ctypes.c_int32.from_address(a).value if a else 0         # noqa: E731
    manager = q(g.addr(B.glob('manager_global')))
    done = {'sent': False, 'last': None}

    def battle_tick():
        state = q(manager + B.field('manager.state'))
        if i32(manager + B.field('manager.state_type')) != 4 or not state:
            return None
        battle = q(state + B.field('state.battle'))
        return i32(battle + B.field('battle.tick')) if battle else None

    def tables_ready():                                 # crx tables.py's check, inlined (its import scans memory)
        try:
            loading = q(manager + B.field('manager.state'))
            task = q(loading + B.field('loading.task')) if loading else 0
            tables = q(task + B.field('task.tables')) if task else 0
            return bool(tables) and ctypes.c_uint8.from_address(tables + B.field('tables.complete')).value == 1
        except Exception:                                                     # noqa: BLE001
            return False

    game_slot = g.addr(B.glob('game_singleton_global'))
    done['suppressed'] = 0

    def suppress_connection_failure():
        """Offline there is no server: the app queues 'connection failed' (event 2 or 3 at game +0x1bc, flags +0x1b8), which
        freezes the replay's clock. Cleared without dispatching it, as FirstLight's offline cold start does."""
        game = q(game_slot)
        if game and i32(game + place('event')) in (2, 3):
            ctypes.c_uint64.from_address(game + place('event')).value = 0
            ctypes.c_uint16.from_address(game + place('event_flags')).value = 0
            done['suppressed'] += 1
            if done['suppressed'] <= 3:
                EB.log('connection failure suppressed', f'#{done["suppressed"]}')

    skip_flag = B.location('skip_core_and_presentation_flag')    # set: the replay controller never steps the battle
    app_pause = g.addr(place('app_pause_check'))                 # 'paused' = an app event is pending (game +0x1bc != 0):
    if ctypes.string_at(app_pause, 11) == bytes.fromhex('83bf' + place('event').to_bytes(4, 'little').hex() + '00' '0f95c0c3'):
        ctypes.memmove(app_pause, bytes.fromhex('31c0c3'), 3)                       # re-queued 'connection failed'
        EB.log('app pause check', 'patched to never pause')
    wait_check = g.addr(place('logic_wait_check'))
    if ctypes.string_at(wait_check, 6) == bytes.fromhex('555348' '83ec08')[:6] or True:
        head = ctypes.string_at(wait_check, 8)
        if head[:5] == bytes.fromhex('5553504889')[:5] or head[:4] == bytes.fromhex('55535048'):
            ctypes.memmove(wait_check, bytes.fromhex('31c0c3'), 3)      # xor eax,eax; ret: the logic never 'waits'
            EB.log('logic wait check', 'patched to never wait (its limit comes from a server config that is absent)')
        else:
            EB.log('logic wait check', f'unexpected bytes {head.hex()}: not patched')
    dt_slot = record_dt(g, B)                                   # manager_update's dt and call count, no Python
    diag = os.environ.get('CRX_RENDER_DIAG') == '1'             # call counters on the battle's clock functions: diagnosis only
    if diag:
        clock_slot = record_calls(g, 0xCE26D0, '5541574156415453' '4883ec60' 'f30f1144241c')    # controller clock (slot 13)
        step_slot = record_calls(g, 0xCE2CC0, '554157415641554154' '53' '4881ec98000000')    # its battle step
        tick_slot = record_calls(g, 0x10B8AE0, '5541574156415541545350' '4889fb')            # one logic tick
        inner_slot = record_calls(g, 0xD8BFA0, '554157415641554154534881eca8000000')         # its inner update
        past_slot = record_calls(g, 0xCE2D31, 'f30f10442410' 'f30f584344' 'f30f114344')      # step: past its early exits

    if os.environ.get('CRX_THREAD_DIAG'):            # which thread runs the battle's logic ticks, vs the frame hook
        import threading
        import hooks as HK
        seen_ticks = set()
        HK.watch(g, 0x10B8AE0, '5541574156415541545350' '4889fb',
                 lambda *a: (seen_ticks.add(threading.get_native_id()) if len(seen_ticks) < 4 else None))
        done['tick_threads'] = seen_ticks
    # work for the game's update thread (the stub at manager_update's entry runs it, between updates): handing the manager a
    # replay and reading the board. From the frame hook (another thread) it raced the update: crashes, 2026-10-13.
    import loader as L
    update_work = []

    every_update = []                              # called at the start of every update (live.py's trace), kept armed

    def run_update_work():
        while update_work:
            fn = update_work.pop(0)
            try:
                fn()
            except Exception as e:                                     # noqa: BLE001
                EB.log('update-thread work failed', repr(e))
        for fn in every_update:
            try:
                fn()
            except Exception as e:                                     # noqa: BLE001
                EB.log('every-update work failed', repr(e))
        if every_update:
            ctypes.c_uint8.from_address(dt_slot + 24).value = 1
    ctypes.c_uint64.from_address(dt_slot + 32).value = L.callback(None)(run_update_work)

    def on_update_thread(fn):
        update_work.append(fn)
        ctypes.c_uint8.from_address(dt_slot + 24).value = 1
    live = EB.STATE.get('live')
    if live:
        live.dt_slot = dt_slot
        live.on_update_thread = on_update_thread
        live.update_busy = lambda: bool(update_work)
        if os.environ.get('CRX_LIVE_BOARD', '1') == '1':      # the window's troop labels and engine loop view
            every_update.append(live.feed_board)
            ctypes.c_uint8.from_address(dt_slot + 24).value = 1
        if os.environ.get('CRX_LIVE_TRACE'):
            every_update.append(live.trace_board)
            ctypes.c_uint8.from_address(dt_slot + 24).value = 1
        live.replay_text = text

    if os.environ.get('CRX_RENDER_HOOK'):            # experiments on the update thread (e.g. live_battle.py)
        import importlib
        importlib.import_module(os.environ['CRX_RENDER_HOOK']).setup(
            {'g': g, 'B': B, 'EB': EB, 'manager': manager, 'live': live, 'every_update': every_update,
             'on_update_thread': on_update_thread})
        ctypes.c_uint8.from_address(dt_slot + 24).value = 1

    def on_frame(n):
        game_frame(n)
        if live:
            tick = battle_tick() if done['sent'] else None
            live.on_frame(n, tick, q(manager + B.field('manager.state')) if tick is not None else 0)

    current = {'text': text}                      # the replay a restart hands the manager (live.load_game swaps it)

    def set_replay_text(new):
        current['text'] = new
        if live:
            live.replay_text = new

    if live:
        live.set_replay_text = set_replay_text

    def send_replay(n, restart=False, replay_text=None):
        """Hand the manager the replay (again: a restart from tick 0, as crx env's in-process restart; the viewer's rewind).
        The slot's object goes back after the call (the loading screen at the start, the running battle on a restart): the
        manager replaces it at its next update. Leaving the slot empty on a restart crashed at once (2026-10-13)."""
        replay_text = replay_text or current['text']
        if os.environ.get('NEO_VIEW_OWNER') is not None:
            from neo_replay.native_presentation import select_replay_view
            select_replay_view(g, B, replay_text, int(os.environ['NEO_VIEW_OWNER']))
        native = g.call(B.fn('native_alloc'), 16)
        g.call(B.fn('string_from_utf8'), native, ctypes.c_char_p(replay_text.encode()))
        prior = q(manager + B.field('manager.state'))
        EB.log('replay', f'frame {n}: manager {hex(manager)} state type {i32(manager + B.field("manager.state_type"))} '
               f'slot {hex(prior)}')
        slot = manager + B.field('manager.state')
        ctypes.c_uint64.from_address(slot).value = 0      # the setter reads a non-null slot as a replay controller
        g.call(B.fn('set_replay_data'), manager, native)
        if prior and os.environ.get('CRX_RENDER_REPLAY_RESTORE', '1') == '1':
            ctypes.c_uint64.from_address(slot).value = prior   # crx's in-process restart: the screen object goes back
        EB.log('replay', 'set_replay_data done')

    if live:
        import json
        import observe
        reader = observe.Reader(g)

        def board():
            """The board as crx reads it: both players (elixir, hand, cycle) and every object (units, towers, spells)."""
            if i32(manager + B.field('manager.state_type')) != 4:      # not in a battle (loading, a replay starting)
                return None
            state = q(manager + B.field('manager.state'))
            battle = q(state + B.field('state.battle')) if state else 0
            if not battle:
                return None
            logic = reader.logic(battle)               # at the battle's first frames its logic and players are not there yet
            if not logic or not all(reader.player(logic, o) for o in (0, 1)) or \
                    not all(q(logic + reader.f['logic.deck'] + 8 * o) for o in (0, 1)):
                return None
            return {'tick': battle_tick(), 'players': reader.players_state(battle), 'objects': reader.objects(battle)}
        live.restart = lambda: send_replay(EB.STATE['frames'], restart=True)
        if BUILD['used'] in SNAPSHOT and os.environ.get('CRX_LIVE_SNAPSHOTS', '1') == '1':
            keyframes(live, g, B, manager, every_update)
        live.board = board
        try:
            live.end_tick = int(os.environ.get('CRX_LIVE_END_TICK') or json.loads(text).get('endTick', 0))   # the bar's end
        except ValueError:
            pass

    def game_frame(n):
        if done['sent']:
            suppress_connection_failure()
            want = int(os.environ.get('CRX_RENDER_SKIPFLAG', '0'))
            if skip_flag and ctypes.c_uint8.from_address(skip_flag).value != want:
                ctypes.c_uint8.from_address(skip_flag).value = want
                done['skip_cleared'] = done.get('skip_cleared', 0) + 1
                if done['skip_cleared'] <= 2:
                    EB.log('skip flag', f'frame {n}: was set, cleared')
        load_warp = float(os.environ.get('CRX_RENDER_LOAD_WARP', '0') or 0)
        if live and load_warp and not done['sent'] and not done.get('load_warped'):
            done['load_warped'] = True
            live.warp(load_warp)                       # the loading screen on fast clock time (bionic_mac's warp)
            EB.log('loading', f'clock x{load_warp} until the battle starts')
        if not done['sent'] and n % 200 == 0:
            EB.log('loading', f'frame {n}: state type {i32(manager + B.field("manager.state_type"))}, tables ready {tables_ready()}')
        if not done['sent'] and n >= at_frame and (tables_ready() or os.environ.get('CRX_RENDER_REPLAY_NOWAIT')):
            done['sent'] = True
            if done.get('load_warped'):
                live.warp(1.0)
            on_update_thread(lambda: send_replay(n))
            return
        if done['sent'] and n % 300 == 0 and os.environ.get('CRX_COUNT_IMPORTS'):    # C library calls per frame
            import loader as L2
            now = {k: ctypes.c_uint64.from_address(c).value for k, (c, _s) in L2.IMPORT_COUNTS.items()}
            prev = done.get('imp_prev', {})
            per = sorted(((v - prev.get(k, 0)) / 300, k) for k, v in now.items())[::-1]
            EB.log('C library calls per frame', ', '.join(f'{k} {v:.1f}' for v, k in per if v >= 0.05 and k != '__ring__'))
            if '__ring__' in L2.IMPORT_COUNTS:
                from collections import Counter
                ring = L2.IMPORT_COUNTS['__ring__'][0]
                callers = Counter(L2.where(ctypes.c_uint64.from_address(ring + 8 * i).value) for i in range(256))
                EB.log('callers', str(callers.most_common(6)))
            done['imp_prev'] = now
        if done['sent'] and EB.GL_COUNTS and n % 300 == 0:      # CRX_GL_COUNT: GL calls per frame over the last 300 frames
            now = EB.gl_counts()
            prev = done.get('gl_prev', {})
            per = sorted(((v - prev.get(k, 0)) / 300, k) for k, v in now.items())[::-1]
            EB.log('gl calls per frame', ', '.join(f'{k} {v:.0f}' for v, k in per[:25] if v >= 0.5))
            done['gl_prev'] = now
        if done['sent'] and n % 300 == 0 and os.environ.get('CRX_CLOCK_DIAG'):
            lib = ctypes.CDLL(None)
            lib.crx_clock_calls.restype = ctypes.c_long
            EB.log('clock calls by Linux clock id', str({c: lib.crx_clock_calls(c) for c in range(12) if lib.crx_clock_calls(c)}))
        if done['sent'] and n % 300 == 0 and 'tick_threads' in done:
            import threading
            EB.log('threads', f'frame hook on {threading.get_native_id()}; logic ticks on {sorted(done["tick_threads"])}')
        if done['sent'] and n % 300 == 0 and diag:       # why the clock may stand still (FirstLight probe's fields; diagnosis
            ctl = q(manager + B.field('manager.state'))       # only: it calls engine code from the frame thread)
            if not ctl:
                return
            game = q(g.addr(B.glob('game_singleton_global')))
            f32 = lambda a: ctypes.c_float.from_address(a).value if a else 0.0      # noqa: E731
            gpause = g.call(0xD13290, restype=ctypes.c_uint8)
            app = g.call(0x7259E0) or 0
            apause = g.call(0x72D220, app, restype=ctypes.c_uint8) if app else -1
            other = g.call(0xBFC710) or 0
            EB.log('pause', f'frame {n}: global {gpause} app {hex(app)} app-pause {apause} bfc710 {hex(other)}')
            battle = q(ctl + 0x90) if ctl else 0
            logic = q(battle + 0xA8) if battle else 0
            sub = q(logic + 0x1B0) if logic else 0
            EB.log('logic', f'frame {n}: battle {hex(battle)} logic {hex(logic)} state {i32(logic + 0x18) if logic else None} '
                   f'sub {i32(sub + 8) if sub else None} ended? {g.call(0x10B94D0, battle, restype=ctypes.c_uint8) if battle else None} '
                   f'running? {g.call(0x10B9A00, battle, restype=ctypes.c_uint8) if battle else None} tick {i32(battle + 0x60) if battle else None} '
                   f'logic+0x198 {i32(logic + 0x198) if logic else None} battle+0x1f0 {i32(battle + 0x1f0) if battle else None} '
                   f'+0x1fc {i32(battle + 0x1fc) if battle else None} +0x24 {i32(battle + 0x24) if battle else None}')
            EB.log('manager_update', f'frame {n}: calls {ctypes.c_uint64.from_address(dt_slot + 8).value} last dt '
                   f'{ctypes.c_float.from_address(dt_slot).value:.4f}')
            for nm, sl in ((('clock', clock_slot), ('step', step_slot), ('logic tick', tick_slot), ('inner', inner_slot),
                            ('past exits', past_slot)) if diag else ()):
                EB.log(nm, f'frame {n}: calls {ctypes.c_uint64.from_address(sl + 8).value} last xmm0 '
                       f'{ctypes.c_float.from_address(sl).value:.4f}')
            EB.log('acc', f'frame {n}: +0x44 {ctypes.c_float.from_address(ctl + 0x44).value:.3f} +0x40 '
                   f'{ctypes.c_float.from_address(ctl + 0x40).value:.3f} +0x1cc {ctypes.c_uint8.from_address(ctl + 0x1cc).value} '
                   f'+0x329 {ctypes.c_uint8.from_address(ctl + 0x329).value}')
            EB.log('controller', f'frame {n}: vtable {hex(q(ctl) - g.base) if ctl else 0} state {i32(ctl + 0x30)} acc {f32(ctl + 0x44):.3f} speed {f32(ctl + 0x74):.3f} '
                   f'suspended {ctypes.c_uint8.from_address(ctl + 0x18c).value if ctl else -1}; game {hex(game)} '
                   f'event {i32(game + 0x1bc) if game else None} flags {ctypes.c_uint16.from_address(game + 0x1b8).value if game else None}')
        if done['sent'] and os.environ.get('CRX_RENDER_PUMP', '1') == '1':
            g.call(B.fn('manager_update'), manager, ctypes.c_float(0.05))   # the frame loop hands it no time: one tick
        if done['sent']:                                                       # per frame from here (headless pump)
            tick = battle_tick()
            if tick != done['last'] and (tick is None or done['last'] is None or tick // 100 != done['last'] // 100):
                EB.log('battle', f'frame {n}: state type {i32(manager + B.field("manager.state_type"))} tick {tick}')
            done['last'] = tick
    EB.STATE['on_frame'] = on_frame
    if os.environ.get('CRX_SEAL_CODE', '1' if sys.platform == 'darwin' else '0') == '1':   # every patch is in: code read-only
        import loader as L
        sealed = sum(L.seal_code(m) for ld in L.LOADERS for m in ld.modules)
        EB.log('code', f'{sealed} executable segments made read-only (Rosetta stops re-checking them)')
    EB.STATE['frame_name'] = lambda n: (f'frame_{n:05d}_tick_{done["last"]}.png' if done['sent'] and done['last'] is not None
                                        else None)


def data_page(L):
    """A page for a stub's counters, apart from the stub's code: under Rosetta (macOS) a write to a page that holds translated
    code throws the translation away, so a stub writing next to itself was translated again on every call (40% of the
    render thread's time, found 2026-10-13)."""
    buf = ctypes.create_string_buffer(4096 + 4096)
    L.KEEP.append(buf)
    return (ctypes.addressof(buf) + 4095) & ~4095


def record_calls(g, rva, head_hex):
    """The same no-Python entry stub on any function whose first bytes (head_hex, >= 14, position-independent) are given:
    +0 float xmm0 at the last call, +8 u64 calls."""
    import struct
    import loader as L
    site, head = g.addr(rva), bytes.fromhex(head_hex)
    if ctypes.string_at(site, len(head)) != head:
        raise RuntimeError(f'bytes at {rva:#x} differ')
    slot, stub = data_page(L), L.alloc_exec(4096)       # data and code on separate pages (see data_page)
    code = b'\x48\xb8' + struct.pack('<Q', slot) + bytes.fromhex('f30f1100' '48ff4008')
    code += head + b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', site + len(head))
    ctypes.memmove(stub, code, len(code))
    jump = b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', stub) + b'\x90' * (len(head) - 14)
    ctypes.memmove(site, jump, len(jump))
    return slot


def record_dt(g, B):
    """A machine-code entry stub on manager_update(manager, float dt): the dt the frame loop hands it is scaled and topped
    up first (the live viewer's pause / step / speed), then stored, and calls counted, in a small memory block (no Python
    on the path). Returns the block's address: +0 float dt as passed on, +8 u64 calls, +16 float scale (1.0: unchanged),
    +20 float extra seconds added once (then cleared), +24 u8: when set, the stub clears it and calls the function at +32
    first (live.py's update-thread work: a restart, a board read), on the thread that runs the battle, between updates;
    the dt at +0 is read again after it, so that work can change this update's time (play mode's exact hold).
    manager_update runs on the game's update thread, not the thread that swaps frames: a restart made from the frame hook
    raced a running update and crashed a busy battle (2026-10-13)."""
    import struct
    import loader as L
    site = g.addr(B.fn('manager_update'))
    head = b''.join(x if isinstance(x, bytes) else bytes.fromhex(x) for x in B.insns('manager_update')[:6])         # 17 bytes, position-independent
    if ctypes.string_at(site, len(head)) != head:
        raise RuntimeError('manager_update bytes differ')
    slot, stub = data_page(L), L.alloc_exec(4096)       # data and code on separate pages (see data_page)
    ctypes.c_float.from_address(slot + 16).value = 1.0
    code = (b'\x48\xb8' + struct.pack('<Q', slot)            # mov rax, slot
            + bytes.fromhex('f30f594010' 'f30f584014'           # mulss xmm0, [rax+16]; addss xmm0, [rax+20]
                            'c7401400000000'                    # mov dword [rax+20], 0
                            'f30f1100' '48ff4008'               # movss [rax], xmm0; inc qword [rax+8]
                            '80781800' '7437'                   # cmp byte [rax+24], 0; je done
                            'c6401800'                          # mov byte [rax+24], 0
                            '57' '56' '52' '51' '4150' '4151'   # push rdi, rsi, rdx, rcx, r8, r9
                            '4883ec18' 'f30f7f0424'             # sub rsp, 24 (16-byte aligned call); movdqu [rsp], xmm0
                            'ff5020'                            # call [rax+32]: the update thread's Python hook
                            'f30f6f0424' '4883c418'             # movdqu xmm0, [rsp]; add rsp, 24
                            '4159' '4158' '59' '5a' '5e' '5f')   # pop r9, r8, rcx, rdx, rsi, rdi
            + b'\x48\xb8' + struct.pack('<Q', slot)            # mov rax, slot
            + bytes.fromhex('f30f1000'))                        # movss xmm0, [rax]: the hook may set this update's dt; done:
    code += head + b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', site + len(head))
    ctypes.memmove(stub, code, len(code))
    jump = b'\xff\x25\x00\x00\x00\x00' + struct.pack('<Q', stub) + b'\x90' * (len(head) - 14)
    ctypes.memmove(site, jump, len(jump))
    return slot


def native_threads(g):
    """Each thread of this process: its name, the system call it waits in, and the return addresses on its stack that fall
    in the engine or the graphics driver (a heuristic scan from the stack pointer; gdb cannot read registers under Rosetta)."""
    maps = []
    for line in open('/proc/self/maps'):
        parts = line.split()
        lo, hi = (int(x, 16) for x in parts[0].split('-'))
        if 'x' in parts[1] and len(parts) > 5:
            maps.append((lo, hi, parts[5].rsplit('/', 1)[-1], int(parts[2], 16)))

    def where(a):
        if g.base <= a < g.base + 0x1B00000:
            return f'libg+{a - g.base:x}'
        for lo, hi, name, off in maps:
            if lo <= a < hi and name.startswith(('libgallium', 'libEGL', 'libGLES', 'libc.so', 'libLLVM', 'libglapi')):
                return f'{name}+{a - lo + off:x}'
        return None
    import glob
    for task in sorted(glob.glob('/proc/self/task/*')):
        try:
            comm = open(task + '/comm').read().strip()
            sysc = open(task + '/syscall').read().split()
        except OSError:
            continue
        frames = []
        if len(sysc) >= 3 and sysc[0] not in ('running',):
            sp = int(sysc[-2], 16)
            for k in range(0, 0x4000, 8):
                try:
                    v = ctypes.c_uint64.from_address(sp + k).value
                except Exception:                                                  # noqa: BLE001
                    break
                w = where(v)
                if w and not (frames and frames[-1] == w):
                    frames.append(w)
                if len(frames) >= 12:
                    break
        sys.stderr.write(f'thread {task.rsplit("/", 1)[-1]} {comm}: syscall {sysc[0] if sysc else "?"} | {" <- ".join(frames)}\n')
    sys.stderr.flush()


# The renderer's own engine places, per build (found in 160402020 with port_render.py from 150535029; checked by hand: the
# instructions printed there, all six references to the device agreeing, the event clear writing +0x1c4 / +0x1c0).
PLACES = {
    '150535029': {'app_pause_check': 0x72D220, 'logic_wait_check': 0xD50CD0, 'device_global': 0x1ABDF20,
                  'pipeline_fn': 0x1463490, 'event': 0x1BC, 'event_flags': 0x1B8, 'version': '15.535.29'},
    '160402020': {'app_pause_check': 0x746880, 'logic_wait_check': 0xD857A0, 'device_global': 0x1B516F0,
                  'pipeline_fn': 0x14E90C0, 'event': 0x1C4, 'event_flags': 0x1C0, 'version': '16.402.20'},
}
PLACES["160635001"] = {'app_pause_check': 7949264, 'logic_wait_check': 15116528, 'pipeline_fn': 23063488, 'device_global': 29872536, 'event': 452, 'event_flags': 448, 'version': '16.635.1'}
BUILD = {'used': '150535029'}                      # set from the loaded engine's bindings (main)


# The game's own battle snapshot (save: the encoder its battle checksum uses; load: as its replay player calls it) and the
# byte stream they use. Loading empties the battle's command queue: the commands landing after the snapshot go back in.
SNAPSHOT = {'150535029': {'encode': 0x10B96F0, 'load': 0x10BA1B0, 'stream': 0x1274770},
            '160402020': {'encode': 0x1112610, 'load': 0x11130F0, 'stream': 0x12DCE10}, "160635001": {'encode': 19010416, 'load': 19013840, 'stream': 20900032}}   # crx/bindings (ported 2026-10-17)
KEY_EVERY = 20                                     # ticks between snapshots (1 s of game time; ~1.6 KB each)


def keyframes(live, g, B, manager, every_update):
    """Snapshots of the battle every KEY_EVERY ticks as it plays (on the update thread), and live.rewind(k): the battle
    back to snapshot k with every later command queued again. Checked 2026-10-14: after a rewind the battle goes on exactly
    as recorded (158 traced ticks), and the picture is the same as the first time."""
    import json
    import env as ENV
    import observe
    A = SNAPSHOT[BUILD['used']]
    q = lambda a: ctypes.c_uint64.from_address(a).value                 # noqa: E731
    i32 = lambda a: ctypes.c_int32.from_address(a).value                # noqa: E731
    saved = {}

    def battle_now():
        if i32(manager + B.field('manager.state_type')) != 4:
            return 0, 0
        state = q(manager + B.field('manager.state'))
        return (q(state + B.field('state.battle')) if state else 0), state

    def save():
        battle, _state = battle_now()
        if not battle or live.seek_phase in ('restart', 'snapshot'):
            return
        tick = i32(battle + B.field('battle.tick'))
        if tick < KEY_EVERY or tick % KEY_EVERY or tick in saved:     # not at tick 0: the battle is not all there yet
            return
        logic = q(battle + B.field('battle.logic'))
        if not logic or not all(q(logic + B.field('logic.player') + 8 * o) for o in (0, 1)):
            return
        stream = g.call(B.fn('native_alloc'), 0x80)
        ctypes.memset(stream, 0, 0x80)
        g.call(A['stream'], stream, 1 << 14)
        ctypes.memset(q(stream + 0x30), 0, 1 << 14)       # its buffer comes uncleared; the encoder ORs bits into it
        g.call(A['encode'], battle, stream, 0, 0, 0, 0, 0)
        saved[tick] = stream

    def rewind(key):
        battle, state = battle_now()
        stream = saved.get(key)
        if not battle or not stream:
            return
        ctypes.c_int32.from_address(stream + 0x14).value = 0
        ctypes.c_int32.from_address(stream + 0x1c).value = 0
        g.call(A['load'], battle, stream, 1, q(state + 0x80))
        e = object.__new__(ENV.CrxEnv)
        e.g, e.B, e.reader, e.battle, e.inject_log = g, B, observe.Reader(g), battle, []
        commands = list(json.loads(live.replay_text).get('cmd') or [])
        if getattr(live, 'play', None) is not None:                         # live play: the plays made since the start
            commands += live.play.commands
        again = [c for c in commands if (c.get('c', {}).get('t2') or 0) + 1 > key]
        ok = sum(1 for c in again if e._inject(c))
        live.event('snapshot loaded', f'tick {key}, {ok} of {len(again)} later commands queued again')

    # strictly before the target: a unit's target position is refreshed by a game step, so the board read exactly at a
    # loaded snapshot's tick still showed the old one (seek proof, 2026-10-14); one step or more after the load, it matches
    live.keyframe_at = lambda target: max((k for k in saved if k < target), default=None)
    live.rewind = rewind
    live.clear_snapshots = saved.clear              # another game loaded into this session: its snapshots are not this one's
    every_update.append(save)


def place(name):
    return PLACES[BUILD['used']][name]


APP_PAUSE_CHECK = 0x72D220        # 150535029: GameApp paused? = a pending app event; the replay clock stops while true
LOGIC_WAIT_CHECK = 0xD50CD0       # 150535029: logic 'wait' check (tick >= a limit from server config; 0 offline)
DEVICE_GLOBAL = 0x1ABDF20          # 150535029: the graphics device (shared_ptr), created by the render thread (+0x14293a1)
SHADER_LOADER_GLOBAL = 0x1ACF1C8   # 150535029: the shader loader nOnStart makes (+0x145a130); +0x48 its thread
SHADER_LOADER_START = 0x1425CE0    # (loader): scan the shader folders on a new thread (sc3d/shaders/*.shader)
PIPELINE_FN, PIPELINE_HEAD = 0x1463490, '41574156534883ec104989d64989f7'   # device -> pipeline from a shader description


def device_first(g, vm, work, assets, out, seconds, step, C, D, J, L):
    """The phone's order: the window first (the render thread creates the graphics device), the render thread held at its
    first pipeline, then GameMain::init with its renderer steps on (they need the device; GL calls on a context shared with
    the render thread's), then the render thread let go."""
    import threading
    import hooks as HK
    if os.environ.get('CRX_RENDER_WATCHDOG'):
        import faulthandler
        faulthandler.dump_traceback_later(float(os.environ['CRX_RENDER_WATCHDOG']), repeat=True)   # outside the GIL
    EB.make_boot_context()                           # before the render thread's context: that one shares with it
    go = threading.Event()
    held = []

    def hold(rdi, rsi, rdx, ret):
        if not go.is_set():
            if not held:
                EB.log('render thread held at its first pipeline', hex(ret))
            held.append(1)
            go.wait()
    HK.watch(g, place('pipeline_fn'), PIPELINE_HEAD, hold)
    hooked = [(place('pipeline_fn'), PIPELINE_HEAD)]
    rva = lambda a: hex(a - g.base) if a and g.base <= a < g.base + 0x2000000 else hex(a or 0)   # noqa: E731
    q = lambda a: ctypes.c_uint64.from_address(a).value if a else 0                              # noqa: E731
    seen_mat = set()

    def material(rdi, rsi, rdx, ret):                # 0x132f6d0(out, material, key): the material's shader slots
        if rsi not in seen_mat and len(seen_mat) < 12:
            seen_mat.add(rsi)
            EB.log('material', f'{hex(rsi)} vtable {rva(q(rsi))} +0x20 {hex(q(rsi + 0x20))} +0x50 {hex(q(rsi + 0x50))} '
                   f'from {rva(ret)}')
    if os.environ.get('CRX_RENDER_HOOKS'):
        HK.watch(g, 0x132F6D0, '5541574156415541545348' '83ec28', material)
        hooked.append((0x132F6D0, '5541574156415541545348' '83ec28'))
    files = []

    def file_open(rdi, rsi, rdx, ret):             # 0x1353c10 File(this, path, ...): every file the engine names
        if len(files) < 400:
            name = ctypes.string_at(rsi).decode(errors='replace') if rsi else '(null)'
            files.append(name)
            EB.log('File', f'{name} from {rva(ret)}')
    pass
    surface = vm.new('android/view/Surface', 'surface')
    for title, args in (('nOnStart', ()), ('nOnSurfaceCreated', (surface,)),
                        ('nOnSurfaceChanged', (surface, ctypes.c_int(EB.STATE['width']), ctypes.c_int(EB.STATE['height']))),
                        ('nOnResume', ()), ('nOnWindowFocusChanged', (ctypes.c_int(1),))):
        step(title, 'com/supercell/titan/GameApp.' + title, *args)
    device = g.addr(place('device_global'))
    t0 = time.time()
    while not ctypes.c_uint64.from_address(device).value and time.time() - t0 < 60:
        time.sleep(0.2)
    EB.log('device', f'{hex(ctypes.c_uint64.from_address(device).value)} after {time.time() - t0:.1f} s; held: {bool(held)}')
    EB.log('step', 'GameMain::init (renderer steps per CRX_UNPATCHED)')
    info = D.game_main_init(g)
    flag = D.bindings.of(g).location('skip_core_and_presentation_flag')
    if flag:
        ctypes.c_uint8.from_address(flag).value = 0
    EB.log('step', f'GameMain::init done, helper {hex(info["helper_after"] or 0)}; render thread let go')
    EB.release_boot_context()
    go.set()
    time.sleep(0.5)
    for rva_, head in hooked:                        # hooks off once done: a hooked function called in a tight loop
        ctypes.memmove(g.addr(rva_), bytes.fromhex(head), len(bytes.fromhex(head)))   # starves the render thread
    EB.log('hooks', f'{len(hooked)} removed')
    for title, args in (('nOnResume', ()), ('nOnWindowFocusChanged', (ctypes.c_int(1),))):   # foreground again, now that
        step(title + ' (again)', 'com/supercell/titan/GameApp.' + title, *args)           # the render thread runs
    if os.environ.get('CRX_PC_SAMPLE'):                # diagnosis (macOS): where each thread spends its time, to out/pc_sample.txt
        sys.path.insert(0, str(HERE.parent / 'mac'))
        import pc_sample
        threading.Timer(25, lambda: pc_sample.start(float(os.environ['CRX_PC_SAMPLE']), L.where, str(out / 'pc_sample.txt'))).start()
    replay_file = os.environ.get('CRX_RENDER_REPLAY')
    if replay_file:
        start_replay_on_frame(g, D, Path(replay_file).read_text(), int(os.environ.get('CRX_RENDER_REPLAY_AT', '3000')))
    t0 = time.time()
    last, still = EB.STATE['frames'], 0
    while time.time() - t0 < seconds:
        time.sleep(1)
        still = still + 1 if EB.STATE['frames'] == last else 0
        last = EB.STATE['frames']
        if still == 8:                                # no frame for 8 s: where is every thread?
            EB.log('stall', f'no frame since {last}')
            native_threads(g)
    for row in EB.LOG:
        print('  ', row)
    print(f'--- frames swapped: {EB.STATE["frames"]}, saved: {len(EB.STATE["saved"])}')
    from collections import Counter
    print('--- JNI methods:', sorted({d.split(' on ')[0] for n, d in J.LOG if 'Method' in n}))
    print('--- JNI calls:', len(J.LOG), Counter(f'{n} {d.split(" on ")[0]}' for n, d in J.LOG[-20000:]).most_common(8))
    print('--- asset opens:', len(L.ASSET_LOG), Counter(L.ASSET_LOG[-5000:]).most_common(4))
    print('--- missing / stub functions called:', {k: v for k, v in L.STUBS_CALLED.items() if 'FMOD' not in k})
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
