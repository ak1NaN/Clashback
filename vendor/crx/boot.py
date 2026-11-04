"""The whole start-up so far (gates 2a-2d) in one call: load, JNI_OnLoad, the Android start-up entry points, the headless hold,
the asset system, GameMain::init. Returns (libg module, stand-in Java, details)."""
import ctypes
import time
from pathlib import Path

import gate2b as B
import gate2c as C
import gate2d as D
import bindings
import jni as J
import loader as L


def boot(libdir: Path, work: Path, assets: str, hold: float = 5.0, on_loaded=None):
    """Wait at most hold seconds for GameMain/context, then load resources.

    on_loaded(g): called right after the library is loaded, before any engine code runs (for hooks).
    """
    for name in ('data', 'cache', 'external'):
        (work / name).mkdir(parents=True, exist_ok=True)
    L.install_crash_report()
    ld, g, vm = B.start(libdir, work)
    if on_loaded:
        on_loaded(g)
    store = {}

    def text(handle):
        kind, value = vm.objects.get(handle or 0, ('null', None))
        return value if kind == 'string' else None

    def store_kv(args):
        key, value = J.va_args(args[3], 2)
        store[text(key)] = text(value)
        return 0
    base = 'com/supercell/titan/ApplicationUtilBase.'
    vm.answers[base + 'getAppVersion()Ljava/lang/String;'] = '15.535.29'
    vm.answers[base + 'storeKeyValue(Ljava/lang/String;Ljava/lang/String;)V'] = store_kv
    vm.answers[base + 'getKeyValue(Ljava/lang/String;)Ljava/lang/String;'] = lambda args: store.get(text(J.va_args(args[3], 1)[0]))
    B.call_native(vm, 'com/supercell/titan/TitanApplication.nOnNativeLibrariesLoaded')
    B.call_native(vm, 'com/supercell/titan/GameApp.nOnApplicationCreate')
    B.call_native(vm, 'com/supercell/titan/GameApp.createGameMain', vm.new('android/content/res/AssetManager', 'assets'),
                  vm.string(str(work / 'data')), vm.string(str(work / 'cache')), vm.string(str(work / 'external')),
                  ctypes.c_long(8 << 30), ctypes.c_int(1080), ctypes.c_int(2400), ctypes.c_int(420), ctypes.c_float(420.0),
                  ctypes.c_float(420.0), ctypes.c_int(2), vm.string(str(work / 'external')), vm.new('com/supercell/titan/GameApp', 'activity'))
    B.call_native(vm, 'com/supercell/titan/GameApp.nOnCreate')
    # nOnCreate may initialize the context asynchronously. Keep the original
    # headless wait budget, but proceed as soon as the required state exists.
    # Refresh the singleton each time: it may not have been published yet.
    engine = bindings.of(g)
    waiting = time.monotonic()
    deadline = waiting + max(0.0, hold)
    while True:
        game = C.g64(g, engine.glob('game_singleton_global'))
        context = ctypes.c_uint64.from_address(game + engine.field('game.context')).value if game else 0
        if game and context:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f'GameMain/context did not become ready within {hold:.2f} seconds')
        time.sleep(min(0.01, remaining))
    wait_seconds = time.monotonic() - waiting
    print(f'Headless context ready after {wait_seconds:.3f}s', flush=True)
    C.init_resources(g, assets)
    info = D.game_main_init(g)
    info['store'] = store
    info['context_wait_seconds'] = wait_seconds
    return g, vm, info
